"""LINE Messaging API bot for FaceFind.

Flow
────
  Commands: /help (show usage), /stop (cancel the caller's active search).

  Any other text message: extract Google Drive folder link(s) → download
  images to cache → match against hardcoded reference photos → reply with
  matched images, followed by a concluding "done" message (or an error
  message if something fails along the way). A repeat search with the exact
  same set of Drive URLs (an album already saved from a prior >10-match run)
  is served straight from that cache instead of re-downloading/re-matching.
  Only one search runs at a time per user; a second link while one is
  already running gets an "already running" reply instead of starting a
  second one.

  Any message that isn't a command and contains no Drive link is ignored
  silently. Non-text messages (photos, stickers, etc.) also get no special
  handling.

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

import hashlib
import logging
import os
import re
import shutil
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

ALBUMS_DIR = Path(__file__).parent / "albums"

# One active search per user at a time — /stop signals the matching Event.
_active_searches: dict[str, threading.Event] = {}

_HELP_TEXT = (
    "📖 可用指令：\n"
    "/help — 顯示這個說明\n"
    "/stop — 停止目前正在執行的搜尋\n\n"
    "使用方式：直接傳送包含 Google Drive 資料夾連結的訊息，我就會開始搜尋符合的照片。"
)


def _reference_photo_paths() -> list[str]:
    if not _REF_DIR.exists():
        return []
    return sorted(
        str(p) for p in _REF_DIR.iterdir()
        if p.is_file() and not p.name.startswith(".")
    )


def _album_id(urls: list[str]) -> str:
    """Deterministic album id for a set of Drive URLs — same URLs (any order)
    always hash to the same id, so re-running a search refreshes the same
    album instead of creating a new one."""
    key = "|".join(sorted(urls))
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def _save_album(album_id: str, matches: list[tuple[Path, str]]) -> None:
    album_dir = ALBUMS_DIR / album_id
    album_dir.mkdir(parents=True, exist_ok=True)
    seen = set()
    for img_path, folder_id in matches:
        name = img_path.name
        if name in seen:
            name = f"{img_path.stem}_{folder_id[:6]}{img_path.suffix}"
        seen.add(name)
        shutil.copy2(img_path, album_dir / name)


def _album_result_text(album_id: str, filenames: list[str], public_url: str) -> str:
    if public_url:
        return (
            f"📁 找到 {len(filenames)} 張符合的照片，請點此查看：\n"
            f"{public_url}/album/{album_id}"
        )
    names = "\n".join(f"• {n}" for n in filenames[:20])
    extra = f"\n…還有 {len(filenames) - 20} 張" if len(filenames) > 20 else ""
    return (
        f"符合的檔案：\n{names}{extra}\n\n"
        "提示：在 .env 中設定 PUBLIC_URL 即可以相簿形式查看。"
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


def _push(target_id: str, messages: list) -> None:
    with ApiClient(_cfg()) as client:
        MessagingApi(client).push_message(
            PushMessageRequest(to=target_id, messages=messages)
        )


def _txt(text: str) -> TextMessage:
    return TextMessage(text=text)


def _push_target(source) -> str:
    """Where push_message should send follow-up messages for this event's
    source. A user's own id only reaches their 1:1 chat with the bot — in a
    group or multi-person room, replies must target the group/room id
    instead, or push_message silently DMs the sender."""
    if source.type == "group":
        return source.group_id
    if source.type == "room":
        return source.room_id
    return source.user_id


# ── Text handler ───────────────────────────────────────────────────────────────

@handler.add(MessageEvent, message=TextMessageContent)
def on_text(event: MessageEvent) -> None:
    target_id = _push_target(event.source)
    text = event.message.text.strip()
    logger.info("on_text target=%s text=%r", target_id, text)

    low = text.lower()

    if low == "/help":
        _reply(event.reply_token, [_txt(_HELP_TEXT)])
        return

    if low == "/stop":
        stop_event = _active_searches.get(target_id)
        if stop_event is not None and not stop_event.is_set():
            stop_event.set()
            _reply(event.reply_token, [_txt("🛑 正在停止搜尋…")])
        else:
            _reply(event.reply_token, [_txt("目前沒有正在執行的搜尋。")])
        return

    urls = list(dict.fromkeys(_DRIVE_LINK_RE.findall(text)))
    if not urls:
        # Doesn't match any command or contain a Drive link — ignore silently.
        return

    existing = _active_searches.get(target_id)
    if existing is not None and not existing.is_set():
        _reply(event.reply_token, [_txt("⚠️ 已有搜尋正在進行中，請稍候或傳送 /stop 停止。")])
        return

    stop_event = threading.Event()
    _active_searches[target_id] = stop_event

    _reply(event.reply_token, [_txt(
        f"🔍 開始搜尋 {len(urls)} 個資料夾…\n完成後會通知你！（傳送 /stop 可中止）"
    )])

    threading.Thread(
        target=_run_search,
        args=(target_id, urls, stop_event),
        daemon=True,
    ).start()


# ── Background search ──────────────────────────────────────────────────────────

def _run_search(target_id: str, urls: list[str], stop_event: threading.Event) -> None:
    public_url = os.environ.get("PUBLIC_URL", "").rstrip("/")
    album_id = _album_id(urls)

    try:
        album_dir = ALBUMS_DIR / album_id
        if album_dir.is_dir() and any(album_dir.iterdir()):
            logger.info("search cache hit target=%s album_id=%s", target_id, album_id)
            names = sorted(p.name for p in album_dir.iterdir() if p.is_file())
            _push(target_id, [_txt(
                _album_result_text(album_id, names, public_url) + "\n\n（先前搜尋過的快取結果）"
            )])
            return

        logger.info("search start target=%s urls=%d album_id=%s", target_id, len(urls), album_id)

        ref_paths = _reference_photo_paths()
        if not ref_paths:
            _push(target_id, [_txt(f"❌ 在 {_REF_DIR} 找不到參考照片")])
            return

        known = encode_references(ref_paths, detector=_DEFAULT_DETECTOR)
        if not known:
            _push(target_id, [_txt("⚠️ 參考照片中偵測不到人臉。")])
            return

        all_images: list[tuple[Path, str]] = []
        for url in urls:
            if stop_event.is_set():
                _push(target_id, [_txt("🛑 搜尋已停止。")])
                return

            try:
                folder_id = extract_folder_id(url)
            except ValueError as e:
                logger.warning("search target=%s invalid url=%s: %s", target_id, url, e)
                _push(target_id, [_txt(f"⚠️ 略過無效連結：{e}")])
                continue

            cache_dir = get_cache_dir(folder_id)
            cached = list_cached_images(cache_dir)

            if not cached:
                _push(target_id, [_txt("⬇️ 正在下載資料夾…請稍候。")])
                try:
                    download_images(url, cache_dir)
                except Exception as e:
                    logger.exception("search target=%s download failed url=%s", target_id, url)
                    _push(target_id, [_txt(f"⚠️ 下載失敗：{e}")])
                    continue
                cached = list_cached_images(cache_dir)

            all_images.extend((img, folder_id) for img in cached)

        if stop_event.is_set():
            _push(target_id, [_txt("🛑 搜尋已停止。")])
            return

        if not all_images:
            _push(target_id, [_txt("❌ 指定的資料夾中找不到圖片。")])
            return

        _push(target_id, [_txt(f"🔎 正在掃描 {len(all_images)} 張圖片…")])

        matches: list[tuple[Path, str]] = []
        for img_path, folder_id in all_images:
            if stop_event.is_set():
                _push(target_id, [_txt(f"🛑 搜尋已停止（已找到 {len(matches)} 張符合的照片）。")])
                return
            try:
                if is_match(str(img_path), known, tolerance=_DEFAULT_TOLERANCE, detector=_DEFAULT_DETECTOR):
                    matches.append((img_path, folder_id))
            except Exception:
                pass

        if len(matches) > _MAX_IMAGES_TO_SEND:
            _save_album(album_id, matches)
            names = sorted(p.name for p in album_dir.iterdir() if p.is_file())
            _push(target_id, [_txt(_album_result_text(album_id, names, public_url))])
        elif matches and public_url:
            for img_path, folder_id in matches:
                img_url = f"{public_url}/api/image/{folder_id}/{img_path.name}"
                _push(target_id, [ImageMessage(
                    original_content_url=img_url,
                    preview_image_url=img_url,
                )])
        elif matches:
            names = "\n".join(f"• {p.name}" for p, _ in matches)
            _push(target_id, [_txt(
                f"符合的檔案：\n{names}\n\n"
                "提示：在 .env 中設定 PUBLIC_URL 即可直接在聊天室收到圖片。"
            )])

        logger.info(
            "search done target=%s matches=%d scanned=%d",
            target_id, len(matches), len(all_images),
        )
        _push(target_id, [_txt(
            f"✅ 完成 — 在 {len(all_images)} 張照片中找到 {len(matches)} 張符合的照片。"
        )])

    except Exception as e:
        logger.exception("search failed target=%s", target_id)
        _push(target_id, [_txt(f"❌ 搜尋發生錯誤：{e}")])

    finally:
        if _active_searches.get(target_id) is stop_event:
            _active_searches.pop(target_id, None)
