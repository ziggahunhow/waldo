"""LINE Messaging API bot for FaceFind.

Flow
────
  Any text message → extract Google Drive folder link(s) → download images
  to cache → match against hardcoded reference photos → reply with matched
  images, followed by a concluding "done" message (or an error message if
  something fails along the way).

  Non-text messages (photos, stickers, etc.) get no special handling.

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
    PushMessageRequest,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

from cache import get_cache_dir, list_cached_images
from drive import download_images, extract_folder_id
from recognizer import encode_references, is_match

_DRIVE_LINK_RE = re.compile(
    r"https://drive\.google\.com/drive/(?:u/\d+/)?folders/[a-zA-Z0-9_-]+"
)

# TODO: hardcode needs to be removed later — replace with a real way for
# users to supply their own reference photos.
_REF_DIR = Path.home() / "Documents" / "photos" / "target_person"
_DEFAULT_TOLERANCE = 0.25
_DEFAULT_DETECTOR = "insightface"

_MAX_IMAGES_TO_SEND = 10


def _reference_photo_paths() -> list[str]:
    if not _REF_DIR.exists():
        return []
    return sorted(
        str(p) for p in _REF_DIR.iterdir()
        if p.is_file() and not p.name.startswith(".")
    )


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


# ── Text handler ───────────────────────────────────────────────────────────────

@handler.add(MessageEvent, message=TextMessageContent)
def on_text(event: MessageEvent) -> None:
    user_id = event.source.user_id
    text = event.message.text.strip()
    logger.info("on_text user=%s text=%r", user_id, text)

    urls = list(dict.fromkeys(_DRIVE_LINK_RE.findall(text)))
    if not urls:
        _reply(event.reply_token, [_txt(
            "No Google Drive links found in your message.\n"
            "Send a link like: https://drive.google.com/drive/folders/…"
        )])
        return

    _reply(event.reply_token, [_txt(
        f"🔍 Starting search across {len(urls)} folder(s)…\nI'll message you when done!"
    )])

    threading.Thread(
        target=_run_search,
        args=(user_id, urls),
        daemon=True,
    ).start()


# ── Background search ──────────────────────────────────────────────────────────

def _run_search(user_id: str, urls: list[str]) -> None:
    public_url = os.environ.get("PUBLIC_URL", "").rstrip("/")
    logger.info("search start user=%s urls=%d", user_id, len(urls))

    try:
        ref_paths = _reference_photo_paths()
        if not ref_paths:
            _push(user_id, [_txt(f"❌ No reference photos found in {_REF_DIR}")])
            return

        known = encode_references(ref_paths, detector=_DEFAULT_DETECTOR)
        if not known:
            _push(user_id, [_txt("⚠️ No faces detected in the reference photos.")])
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
            _push(user_id, [_txt("❌ No images found in the specified folder(s).")])
            return

        _push(user_id, [_txt(f"🔎 Scanning {len(all_images)} image(s)…")])

        matches: list[tuple[Path, str]] = []
        for img_path, folder_id in all_images:
            try:
                if is_match(str(img_path), known, tolerance=_DEFAULT_TOLERANCE, detector=_DEFAULT_DETECTOR):
                    matches.append((img_path, folder_id))
            except Exception:
                pass

        if matches and public_url:
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
        elif matches:
            names = "\n".join(f"• {p.name}" for p, _ in matches[:20])
            extra = f"\n…and {len(matches) - 20} more" if len(matches) > 20 else ""
            _push(user_id, [_txt(
                f"Matched files:\n{names}{extra}\n\n"
                "Tip: set PUBLIC_URL in .env to receive images directly in chat."
            )])

        logger.info(
            "search done user=%s matches=%d scanned=%d",
            user_id, len(matches), len(all_images),
        )
        _push(user_id, [_txt(
            f"✅ Done — found {len(matches)} match(es) out of {len(all_images)} photo(s)."
        )])

    except Exception as e:
        logger.exception("search failed user=%s", user_id)
        _push(user_id, [_txt(f"❌ Search error: {e}")])
