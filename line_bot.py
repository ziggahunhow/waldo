"""LINE Messaging API bot for FaceFind.

Flow
────
  Group/room chats only. A 1:1 DM gets one explanatory reply and nothing
  else happens.

  Access control: when the bot is added to a group/room (JoinEvent), it
  checks whether ADMIN_LINE_USER_ID is a member of that group/room via the
  Messaging API. If not present — or if that check itself fails for any
  reason (e.g. account tier doesn't support member listing) — the bot
  replies with why, then leaves immediately (fail-closed: unable to verify
  means don't stay). LINE's join event doesn't tell us who actually invited
  the bot, so this is a proxy: "stay only in groups/rooms the admin is
  also in," not a literal invite-permission check. This is checked once at
  join time only — if the admin later leaves a group, the bot doesn't
  re-check and won't auto-leave.

  Commands: /help (show usage), /setref … /done (collect the shared
  reference photos in-chat), /stop (cancel the caller's active search).

  Any other text message: extract Google Drive folder link(s) → download
  images to cache → match against the collected reference photos → reply with
  matched images, followed by a concluding "done" message (or an error
  message if something fails along the way). A repeat search with the exact
  same set of Drive URLs (an album already saved from a prior >10-match run)
  is served straight from that cache instead of re-downloading/re-matching.
  Only one search runs at a time per group/room; a second link while one is
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
  ADMIN_LINE_USER_ID        – your own LINE user ID. The bot only stays in
                              groups/rooms you're a member of; DM the bot
                              once and check logs/server.log for
                              "on_text target=<your id>" to find it.
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
from typing import Optional

import requests

logger = logging.getLogger(__name__)

from linebot.v3 import WebhookHandler
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    ImageMessage,
    MessagingApi,
    MessagingApiBlob,
    PushMessageRequest,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import (
    ImageMessageContent,
    JoinEvent,
    MessageEvent,
    TextMessageContent,
)

from cache import get_cache_dir, list_cached_images
from drive import count_folder_images, download_images, extract_folder_id
from recognizer import encode_references, is_match

_DRIVE_LINK_RE = re.compile(
    r"https://drive\.google\.com/drive/(?:u/\d+/)?folders/[a-zA-Z0-9_-]+"
)

# Reference photos are collected in-chat via /setref … /done. REFS_DIR is the
# live set read at search time; REFS_STAGING_DIR holds an in-progress
# collection so an abandoned or empty /setref can never break the live set.
REFS_DIR = Path(__file__).parent / "refs"
REFS_STAGING_DIR = Path(__file__).parent / "refs_staging"
_DEFAULT_TOLERANCE = 0.25
_DEFAULT_DETECTOR = "insightface"

_MAX_IMAGES_TO_SEND = 10

ALBUMS_DIR = Path(__file__).parent / "albums"

# One active search per user at a time — /stop signals the matching Event.
_active_searches: dict[str, threading.Event] = {}

# One reference-photo collection at a time, system-wide (the reference set is
# global, so concurrent collections would all write the same staging dir).
# {"user_id": str, "count": int} while a /setref session is open, else None.
# In-memory only: a server restart mid-collection (e.g. the dev auto-reloader)
# drops it, and the initiator just re-runs /setref — refs_staging/ keeps this
# from ever corrupting the live refs/ set.
_ref_collector: Optional[dict] = None
_ref_lock = threading.Lock()

_HELP_TEXT = (
    "📖 可用指令：\n"
    "/help — 顯示這個說明\n"
    "/setref — 開始設定參考照片（接著傳送人像照片）\n"
    "/done — 完成設定參考照片\n"
    "/stop — 停止目前正在執行的搜尋\n\n"
    "使用方式：直接傳送包含 Google Drive 資料夾連結的訊息，我就會開始搜尋符合的照片。"
)


def _reference_photo_paths() -> list[str]:
    if not REFS_DIR.exists():
        return []
    return sorted(
        str(p) for p in REFS_DIR.iterdir()
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


def _sender_id(source):
    """The individual LINE user who sent the event (may be None for some
    clients), as opposed to _push_target()'s group/room delivery id."""
    return getattr(source, "user_id", None)


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

    if event.source.type == "user":
        _reply(event.reply_token, [_txt("此機器人僅限群組使用，請將我加入群組後再試。")])
        return

    low = text.lower()

    if low == "/help":
        _reply(event.reply_token, [_txt(_HELP_TEXT)])
        return

    if low == "/setref":
        _start_ref_collection(event, _sender_id(event.source))
        return

    if low == "/done":
        _finish_ref_collection(event, _sender_id(event.source))
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


# ── Reference photo collection (/setref … images … /done) ────────────────────────

def _start_ref_collection(event: MessageEvent, sender_id: Optional[str]) -> None:
    if not sender_id:
        _reply(event.reply_token, [_txt("無法識別你的使用者 ID，請改用其他裝置再試一次。")])
        return

    global _ref_collector
    with _ref_lock:
        busy = _ref_collector is not None
        if not busy:
            shutil.rmtree(REFS_STAGING_DIR, ignore_errors=True)
            REFS_STAGING_DIR.mkdir(parents=True, exist_ok=True)
            _ref_collector = {"user_id": sender_id, "count": 0}

    if busy:
        _reply(event.reply_token, [_txt("已有人正在收集參考照片，請稍候。")])
        return
    logger.info("setref start user=%s", sender_id)
    _reply(event.reply_token, [_txt(
        "📸 開始收集參考照片。請傳送 3-5 張人像照片（只有你傳送的照片會被使用），完成後輸入 /done。"
    )])


def _finish_ref_collection(event: MessageEvent, sender_id: Optional[str]) -> None:
    global _ref_collector
    count = 0
    with _ref_lock:
        collector = _ref_collector
        if collector is None:
            action = "none"
        elif collector["user_id"] != sender_id:
            action = "not_owner"
        elif collector["count"] == 0:
            _ref_collector = None
            action = "empty"
        else:
            count = collector["count"]
            # Atomically swap staging into the live set: drop the old refs/,
            # promote refs_staging/ → refs/, then recreate an empty staging dir.
            shutil.rmtree(REFS_DIR, ignore_errors=True)
            REFS_STAGING_DIR.rename(REFS_DIR)
            REFS_STAGING_DIR.mkdir(parents=True, exist_ok=True)
            _ref_collector = None
            action = "done"

    if action == "none":
        _reply(event.reply_token, [_txt("目前沒有正在收集的參考照片。")])
    elif action == "not_owner":
        _reply(event.reply_token, [_txt("只有發起 /setref 的人可以使用 /done。")])
    elif action == "empty":
        _reply(event.reply_token, [_txt("尚未收到任何照片，保留原本的參考照片。")])
    else:
        logger.info("setref done user=%s count=%d", sender_id, count)
        _reply(event.reply_token, [_txt(f"✅ 已更新參考照片（共 {count} 張）。")])


def _get_image_bytes(message_id: str) -> bytes:
    with ApiClient(_cfg()) as client:
        return bytes(MessagingApiBlob(client).get_message_content(message_id))


@handler.add(MessageEvent, message=ImageMessageContent)
def on_image(event: MessageEvent) -> None:
    # Only meaningful mid-collection, and only in groups/rooms (DMs rejected
    # like on_text). Anything else — no collection, or a photo from someone
    # who isn't the initiator — is ignored silently.
    if event.source.type == "user":
        return
    sender_id = _sender_id(event.source)

    collector = _ref_collector
    if collector is None or not sender_id or sender_id != collector["user_id"]:
        return

    # Fetch the (slow) image bytes outside the lock, then commit under it so a
    # concurrent /done or second photo can't hand out a duplicate index.
    try:
        content = _get_image_bytes(event.message.id)
    except Exception:
        logger.exception("on_image failed to fetch content")
        return

    with _ref_lock:
        collector = _ref_collector
        if collector is None or sender_id != collector["user_id"]:
            return  # collection ended or changed hands while we were fetching
        idx = collector["count"]
        REFS_STAGING_DIR.mkdir(parents=True, exist_ok=True)
        (REFS_STAGING_DIR / f"ref_{idx}.jpg").write_bytes(content)
        collector["count"] = idx + 1
        count = collector["count"]

    _reply(event.reply_token, [_txt(
        f"已收到第 {count} 張參考照片，傳送更多或輸入 /done 完成。"
    )])


# ── Join handler (access control) ────────────────────────────────────────────────

def _group_or_room_id(source) -> str:
    return source.group_id if source.type == "group" else source.room_id


def _member_present(source, target_user_id: str) -> bool:
    """Paginate through the group/room's member list checking for
    target_user_id. Raises on API failure (e.g. account tier doesn't
    support member listing) rather than swallowing it — the caller treats
    any failure as fail-closed."""
    with ApiClient(_cfg()) as client:
        api = MessagingApi(client)
        start = None
        while True:
            if source.type == "group":
                resp = api.get_group_members_ids(source.group_id, start=start)
            else:
                resp = api.get_room_members_ids(source.room_id, start=start)
            if target_user_id in resp.member_ids:
                return True
            start = resp.next
            if not start:
                return False


def _leave(source) -> None:
    with ApiClient(_cfg()) as client:
        api = MessagingApi(client)
        if source.type == "group":
            api.leave_group(source.group_id)
        else:
            api.leave_room(source.room_id)


@handler.add(JoinEvent)
def on_join(event: JoinEvent) -> None:
    source = event.source
    if source.type not in ("group", "room"):
        return

    conv_id = _group_or_room_id(source)
    admin_id = os.environ.get("ADMIN_LINE_USER_ID", "").strip()
    logger.info("on_join type=%s id=%s", source.type, conv_id)

    try:
        if not admin_id:
            raise RuntimeError("ADMIN_LINE_USER_ID not configured")
        if not _member_present(source, admin_id):
            raise RuntimeError("admin not a member of this group/room")
    except Exception as e:
        logger.warning("on_join leaving %s (%s): %s", conv_id, source.type, e)
        _reply(event.reply_token, [_txt("此機器人僅限管理員邀請使用，即將離開。")])
        try:
            _leave(source)
        except Exception:
            logger.exception("on_join failed to leave %s", conv_id)
        return

    _reply(event.reply_token, [_txt(
        "👋 哈囉！在群組中傳送 Google Drive 資料夾連結即可開始搜尋，輸入 /help 查看指令。"
    )])


# ── Background search ──────────────────────────────────────────────────────────

# Rough download throughput used only to give the user a heads-up estimate.
_SECONDS_PER_IMAGE = 2


def _download_notice(urls: list[str]) -> str:
    """One 'downloading' notice covering every folder that needs fetching, with
    a rough time estimate from the total image count across all of them. Falls
    back to a generic message if the count can't be determined for any folder."""
    total = 0
    for url in urls:
        count = count_folder_images(url)
        if not count:
            return "⬇️ 開始下載資料夾，請稍候…"
        total += count
    minutes = max(1, round(total * _SECONDS_PER_IMAGE / 60))
    return f"⬇️ 開始下載資料夾（約 {total} 張照片，預計約 {minutes} 分鐘）…"


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
            _push(target_id, [_txt("❌ 尚未設定參考照片，請先使用 /setref 設定。")])
            return

        known = encode_references(ref_paths, detector=_DEFAULT_DETECTOR)
        if not known:
            _push(target_id, [_txt("⚠️ 參考照片中偵測不到人臉。")])
            return

        # Resolve every URL up front so we can announce all downloads in one
        # message instead of one noisy notice per folder.
        resolved: list[tuple[str, str, Path, list[Path]]] = []
        for url in urls:
            try:
                folder_id = extract_folder_id(url)
            except ValueError as e:
                logger.warning("search target=%s invalid url=%s: %s", target_id, url, e)
                _push(target_id, [_txt(f"⚠️ 略過無效連結：{e}")])
                continue
            cache_dir = get_cache_dir(folder_id)
            resolved.append((url, folder_id, cache_dir, list_cached_images(cache_dir)))

        to_download = [url for url, _, _, cached in resolved if not cached]
        if to_download:
            _push(target_id, [_txt(_download_notice(to_download))])

        all_images: list[tuple[Path, str]] = []
        for url, folder_id, cache_dir, cached in resolved:
            if stop_event.is_set():
                _push(target_id, [_txt("🛑 搜尋已停止。")])
                return

            if not cached:
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
