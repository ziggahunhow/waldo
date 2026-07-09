"""LINE Messaging API bot for FaceFind.

Conversation flow
─────────────────
  idle            → user sends a photo → collecting_refs
  collecting_refs → user taps Search   → awaiting_url
  awaiting_url    → user sends URL/text with Drive link → searching (thread)
  searching       → push matches back  → collecting_refs (ready for next run)

Settings sub-flow (available any time refs exist):
  settings → set tolerance | set model → back to collecting_refs

Rich Menu (configured externally in the LINE Developers console)
──────────────────────────────────────────────────────────────
  Buttons must use "postback" actions (not "message") so editing a button's
  display label never breaks matching. Required data / displayText:
    data=action=change_ref  displayText=更改參考照片  (clear refs, ask for new photos)
    data=action=paste_text  displayText=貼上文字串    (ask for Drive-link text, run search)
  See docs/superpowers/specs/2026-07-10-line-bot-chinese-triggers-design.md

Setup
─────
  LINE_CHANNEL_SECRET       – channel secret from LINE Developer Console
  LINE_CHANNEL_ACCESS_TOKEN – long-lived token from LINE Developer Console.
                              If unset, LINE_CHANNEL_ID + LINE_CHANNEL_SECRET
                              are exchanged for a short-lived token instead.
  LINE_CHANNEL_ID           – channel ID; used with LINE_CHANNEL_SECRET to
                              exchange a short-lived access token when
                              LINE_CHANNEL_ACCESS_TOKEN isn't set.
  PUBLIC_URL                – publicly reachable base URL of this server
                              (e.g. https://abc123.ngrok.io)
                              Required to send matched images; without it the
                              bot lists filenames only.
"""

import logging
import os
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

from linebot.v3 import WebhookHandler
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    ImageMessage,
    MessagingApi,
    MessagingApiBlob,
    MessageAction,
    PushMessageRequest,
    QuickReply,
    QuickReplyItem,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import (
    ImageMessageContent,
    MessageEvent,
    PostbackEvent,
    TextMessageContent,
)

from cache import get_cache_dir, list_cached_images
from drive import download_images, extract_folder_id
from recognizer import DETECTORS, encode_references, is_match

_DRIVE_LINK_RE = re.compile(
    r"https://drive\.google\.com/drive/(?:u/\d+/)?folders/[a-zA-Z0-9_-]+"
)

# ── Session store ──────────────────────────────────────────────────────────────

_sessions: dict[str, dict] = {}


def _session(user_id: str) -> dict:
    if user_id not in _sessions:
        _sessions[user_id] = {
            "state": "idle",
            "ref_paths": [],
            "tolerance": 0.25,
            "detector": "mediapipe",
            "tmpdir": tempfile.mkdtemp(prefix="facefind_line_"),
        }
    return _sessions[user_id]


def _reset_session(user_id: str) -> None:
    sess = _sessions.pop(user_id, None)
    if sess and (tmp := sess.get("tmpdir")):
        shutil.rmtree(tmp, ignore_errors=True)


# ── LINE API helpers ───────────────────────────────────────────────────────────

handler = WebhookHandler(os.environ.get("LINE_CHANNEL_SECRET", ""))

# Cached short-lived channel access token (exchanged from channel ID + secret).
_token_cache: dict = {"token": None, "expires_at": 0.0}
_token_lock = threading.Lock()
_LINE_TOKEN_URL = "https://api.line.me/v2/oauth/accessToken"


def _access_token() -> str:
    """Return a Messaging API access token.

    Prefers an explicitly-set long-lived LINE_CHANNEL_ACCESS_TOKEN. Otherwise
    exchanges LINE_CHANNEL_ID + LINE_CHANNEL_SECRET for a short-lived token via
    LINE's client-credentials grant, caching it until shortly before expiry.
    """
    explicit = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    if explicit:
        return explicit

    with _token_lock:
        now = time.time()
        if _token_cache["token"] and now < _token_cache["expires_at"]:
            return _token_cache["token"]

        channel_id = os.environ.get("LINE_CHANNEL_ID", "").strip()
        channel_secret = os.environ.get("LINE_CHANNEL_SECRET", "").strip()
        if not (channel_id and channel_secret):
            raise RuntimeError(
                "LINE credentials missing: set LINE_CHANNEL_ACCESS_TOKEN, or both "
                "LINE_CHANNEL_ID and LINE_CHANNEL_SECRET."
            )

        resp = requests.post(
            _LINE_TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": channel_id,
                "client_secret": channel_secret,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        _token_cache["token"] = data["access_token"]
        # Refresh a minute early to avoid using a token mid-expiry.
        _token_cache["expires_at"] = now + max(0, int(data.get("expires_in", 2592000)) - 60)
        return _token_cache["token"]


def _cfg() -> Configuration:
    return Configuration(access_token=_access_token())


def _reply(reply_token: str, messages: list) -> None:
    with ApiClient(_cfg()) as client:
        MessagingApi(client).reply_message(
            ReplyMessageRequest(reply_token=reply_token, messages=messages)
        )


def _push(user_id: str, messages: list) -> None:
    with ApiClient(_cfg()) as client:
        MessagingApi(client).push_message(
            PushMessageRequest(to=user_id, messages=messages)
        )


def _txt(text: str) -> TextMessage:
    return TextMessage(text=text)


def _qr(*items: tuple[str, str]) -> QuickReply:
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(label=label, text=cmd))
        for label, cmd in items
    ])


def _search_qr() -> QuickReply:
    return _qr(
        ("🔍 Search", "search"),
        ("⚙️ Settings", "settings"),
        ("↩ Reset", "reset"),
    )


# ── Image handler ──────────────────────────────────────────────────────────────

@handler.add(MessageEvent, message=ImageMessageContent)
def on_image(event: MessageEvent) -> None:
    user_id = event.source.user_id
    sess = _session(user_id)
    logger.info("on_image user=%s state=%s", user_id, sess["state"])

    if sess["state"] == "searching":
        _reply(event.reply_token, [_txt(_SEARCHING_MSG)])
        return

    with ApiClient(_cfg()) as client:
        content: bytes = MessagingApiBlob(client).get_message_content(
            message_id=event.message.id
        )

    idx = len(sess["ref_paths"])
    path = Path(sess["tmpdir"]) / f"ref_{idx}.jpg"
    path.write_bytes(content)
    sess["ref_paths"].append(str(path))
    sess["state"] = "collecting_refs"

    count = len(sess["ref_paths"])
    _reply(event.reply_token, [
        TextMessage(
            text=f"Got reference photo {count}! Send more, or tap Search when ready.",
            quick_reply=_search_qr(),
        )
    ])


# ── Text handler ───────────────────────────────────────────────────────────────

@handler.add(MessageEvent, message=TextMessageContent)
def on_text(event: MessageEvent) -> None:  # noqa: C901  (state machine, complexity is expected)
    user_id = event.source.user_id
    text = event.message.text.strip()
    sess = _session(user_id)
    low = text.lower()
    state = sess["state"]
    logger.info("on_text user=%s state=%s text=%r", user_id, state, text)

    # ── Global commands ──────────────────────────────────────────────────────
    if low in ("reset", "/reset", "start", "/start"):
        _reset_session(user_id)
        _reply(event.reply_token, [
            _txt("↩ Reset! Send me reference photos of the person you're looking for.")
        ])
        return

    if low in ("help", "/help"):
        _reply(event.reply_token, [_txt(
            "FaceFind — how to use:\n"
            "1️⃣  Send reference photo(s) of the person\n"
            "2️⃣  Tap Search and send a Google Drive folder link\n"
            "3️⃣  I'll find matching photos!\n\n"
            "You can also paste any text (email, chat log) containing Drive links.\n\n"
            "Commands: reset · settings · search · help"
        )])
        return

    # ── Settings: open menu ───────────────────────────────────────────────────
    if low == "settings":
        sess["state"] = "settings_menu"
        _reply(event.reply_token, [
            TextMessage(
                text=(
                    "⚙️ Settings\n"
                    f"Tolerance: {sess['tolerance']}  ·  Model: {sess['detector']}"
                ),
                quick_reply=_qr(
                    ("📊 Tolerance", "set tolerance"),
                    ("🤖 Model", "set model"),
                    ("← Back", "back"),
                ),
            )
        ])
        return

    # ── Settings: tolerance ───────────────────────────────────────────────────
    if low == "set tolerance":
        sess["state"] = "settings_tolerance"
        _reply(event.reply_token, [_txt(
            f"Send a number from 0.1 (strict) to 1.0 (loose).\n"
            f"Current: {sess['tolerance']}  ·  Recommended: 0.2 – 0.35"
        )])
        return

    if state == "settings_tolerance":
        try:
            val = float(text)
            if not (0.1 <= val <= 1.0):
                raise ValueError
            sess["tolerance"] = round(val, 2)
            sess["state"] = "collecting_refs" if sess["ref_paths"] else "idle"
            _reply(event.reply_token, [
                TextMessage(
                    text=f"Tolerance set to {sess['tolerance']} ✓",
                    quick_reply=_search_qr() if sess["ref_paths"] else None,
                )
            ])
        except ValueError:
            _reply(event.reply_token, [_txt("Please send a number between 0.1 and 1.0.")])
        return

    # ── Settings: model ───────────────────────────────────────────────────────
    if low == "set model":
        _reply(event.reply_token, [
            TextMessage(
                text="Choose detection model:",
                quick_reply=_qr(
                    ("MediaPipe", "model mediapipe"),
                    ("HOG", "model hog"),
                    ("CNN (slow)", "model cnn"),
                ),
            )
        ])
        return

    if low.startswith("model "):
        chosen = low.split(" ", 1)[1]
        if chosen in DETECTORS:
            sess["detector"] = chosen
            sess["state"] = "collecting_refs" if sess["ref_paths"] else "idle"
            _reply(event.reply_token, [
                TextMessage(
                    text=f"Model set to {chosen} ✓",
                    quick_reply=_search_qr() if sess["ref_paths"] else None,
                )
            ])
        else:
            _reply(event.reply_token, [_txt(
                f"Unknown model. Choose from: {', '.join(DETECTORS)}"
            )])
        return

    if low in ("back", "← back"):
        sess["state"] = "collecting_refs" if sess["ref_paths"] else "idle"
        _reply(event.reply_token, [
            TextMessage(
                text=f"{len(sess['ref_paths'])} reference photo(s) loaded.",
                quick_reply=_search_qr() if sess["ref_paths"] else None,
            )
        ])
        return

    # ── Initiate search ───────────────────────────────────────────────────────
    if low == "search":
        if not sess["ref_paths"]:
            _reply(event.reply_token, [
                _txt("Send me reference photos of the person you're looking for first!")
            ])
            return
        sess["state"] = "awaiting_url"
        _reply(event.reply_token, [
            _txt("Send a Google Drive folder link, or paste any text containing one.")
        ])
        return

    # ── Drive URL / text containing Drive links ───────────────────────────────
    if state == "awaiting_url" or _DRIVE_LINK_RE.search(text):
        if not sess["ref_paths"]:
            _reply(event.reply_token, [
                _txt("I need reference photos first. Send me a photo of the person.")
            ])
            return

        urls = list(dict.fromkeys(_DRIVE_LINK_RE.findall(text)))

        if not urls:
            msg = (
                "No Google Drive folder links found.\n"
                "Please send a link like:\nhttps://drive.google.com/drive/folders/…"
                if state == "awaiting_url"
                else "Not sure what to do with that. Send reference photos or a Drive link."
            )
            _reply(event.reply_token, [_txt(msg)])
            return

        if sess["state"] == "searching":
            _reply(event.reply_token, [_txt("A search is already running — please wait!")])
            return

        sess["state"] = "searching"
        _reply(event.reply_token, [_txt(
            f"🔍 Starting search across {len(urls)} folder(s)…\n"
            f"Model: {sess['detector']}  ·  Tolerance: {sess['tolerance']}\n"
            "I'll message you as matches come in!"
        )])

        threading.Thread(
            target=_run_search,
            args=(user_id, urls, list(sess["ref_paths"]), sess["tolerance"], sess["detector"]),
            daemon=True,
        ).start()
        return

    # ── Fallback ──────────────────────────────────────────────────────────────
    if state == "idle":
        _reply(event.reply_token, [
            _txt("👋 Send me reference photos of the person you're looking for!")
        ])
    else:
        _reply(event.reply_token, [
            TextMessage(
                text="Send a Drive folder link, or tap Search.",
                quick_reply=_search_qr() if sess["ref_paths"] else None,
            )
        ])


# ── Postback handler (Rich Menu buttons) ────────────────────────────────────────

_SEARCHING_MSG = "Search in progress — please wait."
_REF_PHOTO_PROMPT = "請上傳3-5張人像照片"
_PASTE_TEXT_PROMPT = "請貼上包含 Google Drive 資料夾連結的文字"

_POSTBACK_CHANGE_REF = "action=change_ref"
_POSTBACK_PASTE_TEXT = "action=paste_text"


def _handle_change_ref(reply_token: str, sess: dict) -> None:
    if sess["state"] == "searching":
        _reply(reply_token, [_txt(_SEARCHING_MSG)])
        return
    sess["ref_paths"] = []
    sess["state"] = "idle"
    _reply(reply_token, [_txt(_REF_PHOTO_PROMPT)])


def _handle_paste_text(reply_token: str, sess: dict) -> None:
    if sess["state"] == "searching":
        _reply(reply_token, [_txt(_SEARCHING_MSG)])
        return
    if not sess["ref_paths"]:
        _reply(reply_token, [_txt(_REF_PHOTO_PROMPT)])
        return
    sess["state"] = "awaiting_url"
    _reply(reply_token, [_txt(_PASTE_TEXT_PROMPT)])


@handler.add(PostbackEvent)
def on_postback(event: PostbackEvent) -> None:
    user_id = event.source.user_id
    data = event.postback.data
    sess = _session(user_id)
    logger.info("on_postback user=%s state=%s data=%s", user_id, sess["state"], data)

    if data == _POSTBACK_CHANGE_REF:
        _handle_change_ref(event.reply_token, sess)
    elif data == _POSTBACK_PASTE_TEXT:
        _handle_paste_text(event.reply_token, sess)
    else:
        logger.warning("on_postback user=%s unrecognized data=%s", user_id, data)


# ── Background search ──────────────────────────────────────────────────────────

_MAX_IMAGES_TO_SEND = 10


def _run_search(
    user_id: str,
    urls: list[str],
    ref_paths: list[str],
    tolerance: float,
    detector: str,
) -> None:
    public_url = os.environ.get("PUBLIC_URL", "").rstrip("/")
    logger.info(
        "search start user=%s urls=%d tolerance=%s detector=%s",
        user_id, len(urls), tolerance, detector,
    )

    try:
        known = encode_references(ref_paths, detector=detector)
        if not known:
            _push(user_id, [_txt(
                "⚠️ No faces detected in your reference photos.\n"
                "Try a clearer, well-lit photo."
            )])
            return

        all_images: list[tuple[Path, str]] = []
        for url in urls:
            try:
                folder_id = extract_folder_id(url)
            except ValueError as e:
                logger.warning("search user=%s invalid url=%s: %s", user_id, url, e)
                _push(user_id, [_txt(f"⚠️ Skipping invalid URL: {e}")])
                continue

            cache_dir = get_cache_dir(folder_id)
            cached = list_cached_images(cache_dir)

            if not cached:
                _push(user_id, [_txt("⬇️ Downloading folder… this may take a moment.")])
                try:
                    download_images(url, cache_dir)
                except Exception as e:
                    logger.exception("search user=%s download failed url=%s", user_id, url)
                    _push(user_id, [_txt(f"⚠️ Download failed: {e}")])
                    continue
                cached = list_cached_images(cache_dir)

            all_images.extend((img, folder_id) for img in cached)

        if not all_images:
            _push(user_id, [_txt("No images found in the specified folder(s).")])
            return

        _push(user_id, [_txt(f"🔎 Scanning {len(all_images)} image(s)…")])

        matches: list[tuple[Path, str]] = []
        for img_path, folder_id in all_images:
            try:
                if is_match(str(img_path), known, tolerance=tolerance, detector=detector):
                    matches.append((img_path, folder_id))
            except Exception:
                pass

        if not matches:
            _push(user_id, [_txt(
                f"😔 No matches found in {len(all_images)} photos.\n"
                "Try raising the tolerance or using a clearer reference photo."
            )])
            return

        logger.info(
            "search done user=%s matches=%d scanned=%d",
            user_id, len(matches), len(all_images),
        )
        _push(user_id, [_txt(
            f"✅ Found {len(matches)} match(es) out of {len(all_images)} photos!"
        )])

        if public_url:
            for img_path, folder_id in matches[:_MAX_IMAGES_TO_SEND]:
                img_url = f"{public_url}/api/image/{folder_id}/{img_path.name}"
                _push(user_id, [ImageMessage(
                    original_content_url=img_url,
                    preview_image_url=img_url,
                )])
            if len(matches) > _MAX_IMAGES_TO_SEND:
                _push(user_id, [_txt(
                    f"Showing first {_MAX_IMAGES_TO_SEND} of {len(matches)} matches.\n"
                    "Open the web UI to see all results."
                )])
        else:
            # No public URL — list filenames as fallback
            names = "\n".join(f"• {p.name}" for p, _ in matches[:20])
            extra = f"\n…and {len(matches) - 20} more" if len(matches) > 20 else ""
            _push(user_id, [_txt(
                f"Matched files:\n{names}{extra}\n\n"
                "Tip: set PUBLIC_URL in .env to receive images directly in chat."
            )])

    except Exception as e:
        logger.exception("search failed user=%s", user_id)
        _push(user_id, [_txt(f"❌ Search error: {e}")])

    finally:
        if user_id in _sessions:
            _sessions[user_id]["state"] = "collecting_refs"
