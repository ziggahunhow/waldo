"""LINE Messaging API bot for FaceFind.

Flow
────
  Group/room chats only. A 1:1 DM gets one explanatory reply and nothing
  else happens.

  Access control: an explicit per-group allowlist (approved_groups.json).
  When added to a group/room the bot stays but does nothing useful until the
  admin (ADMIN_LINE_USER_ID) sends /approve in that chat; /revoke disables it
  again. Searching and reference-photo commands are gated on approval, so an
  unapproved group can't run searches or touch its reference set.
  We identify the admin by the message sender's user id (present in group
  message events) rather than the group member-list API, which requires a
  verified/premium LINE account.

  Remote approval: the admin can also manage groups they aren't a member of
  from their 1:1 chat with the bot — /approve <group id>, /revoke <group id>,
  /groups (list enabled ids). To surface the id, the bot DMs the admin the
  group id whenever it's added to an unapproved group (JoinEvent) or when such
  a group first attempts a search (once per group per process).

  Commands: /help (show usage), /approve … /revoke (admin: enable/disable
  this group; in a DM take a group id argument), /groups (admin DM: list
  enabled groups), /setref … /done (collect this group's own reference photos
  in-chat), /stop (cancel the caller's active search).

  Any other text message: extract Google Drive folder link(s) → download
  images to cache → match against this group's reference photos → reply with
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
  ADMIN_LINE_USER_ID        – your own LINE user ID. Only this user can
                              /approve or /revoke a group. DM the bot once and
                              check logs/server.log for "on_text target=<your
                              id>" to find it.
  PUBLIC_URL                – publicly reachable base URL of this server
                              (e.g. https://abc123.ngrok.io)
                              Required to send matched images; without it the
                              bot lists filenames only.
"""

import hashlib
import json
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

from cache import CACHE_ROOT, get_cache_dir, list_cached_images, prune_old_entries
from drive import count_folder_images, download_images, extract_folder_id
from recognizer import encode_references, is_match

_DRIVE_LINK_RE = re.compile(
    r"https://drive\.google\.com/drive/(?:u/\d+/)?folders/[a-zA-Z0-9_-]+"
)

# Reference photos are collected in-chat via /setref … /done and kept per group
# (or room): each conversation has its own subdirectory under REFS_DIR, so
# different groups can target different people. REFS_DIR/<conv id> is the live
# set read at search time; REFS_STAGING_DIR/<conv id> holds that group's
# in-progress collection so an abandoned or empty /setref can never break its
# live set. Groups are fully isolated — a group with no subdirectory yet simply
# has no reference photos and must run /setref before it can search.
REFS_DIR = Path(__file__).parent / "refs"
REFS_STAGING_DIR = Path(__file__).parent / "refs_staging"

# LINE group/room ids are already filesystem-safe ([A-Za-z0-9]); anything else
# is hashed so a crafted id can never escape REFS_DIR (defense in depth).
_SAFE_CONV_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_DEFAULT_TOLERANCE = 0.25
_DEFAULT_DETECTOR = "insightface"

_MAX_IMAGES_TO_SEND = 10

ALBUMS_DIR = Path(__file__).parent / "albums"

# Cached Drive downloads and saved albums are pruned once older than this on
# every search, so disk doesn't grow without bound.
_MAX_CACHE_AGE_SECONDS = 3 * 24 * 60 * 60

# One active search per user at a time — /stop signals the matching Event.
_active_searches: dict[str, threading.Event] = {}

# One reference-photo collection at a time per group/room (each writes its own
# staging subdir, so different groups can collect concurrently). Maps conv id →
# {"user_id": str, "count": int} while that group's /setref session is open.
# In-memory only: a server restart mid-collection (e.g. the dev auto-reloader)
# drops it, and the initiator just re-runs /setref — the staging subdir keeps
# this from ever corrupting a group's live set. The lock guards the whole map.
_ref_collectors: dict[str, dict] = {}
_ref_lock = threading.Lock()

# Per-group allowlist: only groups/rooms the admin has /approve'd may run
# searches or reference-photo commands. Persisted to disk so approvals survive
# restarts; loaded once at import and kept in sync on every change.
APPROVED_GROUPS_FILE = Path(__file__).parent / "approved_groups.json"
_approved_lock = threading.Lock()


def _load_approved_groups() -> set[str]:
    try:
        with APPROVED_GROUPS_FILE.open() as f:
            data = json.load(f)
        return {str(x) for x in data} if isinstance(data, list) else set()
    except (OSError, ValueError):
        return set()


_approved_groups: set[str] = _load_approved_groups()


def _is_approved(conv_id: str) -> bool:
    return conv_id in _approved_groups


def _set_approved(conv_id: str, approved: bool) -> None:
    """Add or remove conv_id from the persisted allowlist (atomic write)."""
    with _approved_lock:
        if approved:
            _approved_groups.add(conv_id)
        else:
            _approved_groups.discard(conv_id)
        try:
            tmp = APPROVED_GROUPS_FILE.with_name(APPROVED_GROUPS_FILE.name + ".tmp")
            tmp.write_text(json.dumps(sorted(_approved_groups)))
            tmp.replace(APPROVED_GROUPS_FILE)
        except OSError:
            logger.exception("failed to persist approved groups")

_HELP_TEXT = (
    "📖 可用指令：\n"
    "/help — 顯示這個說明\n"
    "/approve — （管理員）在這個群組啟用搜尋功能\n"
    "/revoke — （管理員）停用這個群組\n"
    "/setref — 開始設定參考照片（接著傳送人像照片）\n"
    "/done — 完成設定參考照片\n"
    "/stop — 停止目前正在執行的搜尋\n\n"
    "使用方式：直接傳送包含 Google Drive 資料夾連結的訊息，我就會開始搜尋符合的照片。"
)

# Admin-only commands usable in a 1:1 chat with the bot, so the admin can
# approve/revoke groups they aren't a member of (they can't type /approve there).
_ADMIN_DM_HELP = (
    "📖 管理員指令（私訊）：\n"
    "/groups — 列出已啟用的群組 ID\n"
    "/approve <群組 ID> — 啟用指定群組\n"
    "/revoke <群組 ID> — 停用指定群組\n\n"
    "當我被加入尚未啟用的群組時，會私訊你該群組的 ID。"
)


def _conv_dirname(conv_id: str) -> str:
    """Filesystem-safe subdirectory name for a conversation's reference set."""
    if _SAFE_CONV_ID_RE.match(conv_id):
        return conv_id
    return hashlib.sha256(conv_id.encode()).hexdigest()[:32]


def _group_refs_dir(conv_id: str) -> Path:
    return REFS_DIR / _conv_dirname(conv_id)


def _group_staging_dir(conv_id: str) -> Path:
    return REFS_STAGING_DIR / _conv_dirname(conv_id)


def _reference_photo_paths(conv_id: str) -> list[str]:
    """Live reference photos for one group/room, or [] if it has none set."""
    refs_dir = _group_refs_dir(conv_id)
    if not refs_dir.exists():
        return []
    return sorted(
        str(p) for p in refs_dir.iterdir()
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
        # Admins can manage remote groups from their 1:1 chat with the bot.
        if _is_admin(event.source) and _handle_admin_dm(event, text):
            return
        _reply(event.reply_token, [_txt("此機器人僅限群組使用，請將我加入群組後再試。")])
        return

    low = text.lower()

    if low == "/help":
        _reply(event.reply_token, [_txt(_HELP_TEXT)])
        return

    if low == "/approve":
        _handle_approval(event, approve=True)
        return

    if low == "/revoke":
        _handle_approval(event, approve=False)
        return

    if low == "/setref":
        if not _require_approved(event):
            return
        _start_ref_collection(event, _sender_id(event.source))
        return

    if low == "/done":
        if not _require_approved(event):
            return
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

    if not _require_approved(event):
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

    conv_id = _group_or_room_id(event.source)
    staging = _group_staging_dir(conv_id)
    with _ref_lock:
        busy = conv_id in _ref_collectors
        if not busy:
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True, exist_ok=True)
            _ref_collectors[conv_id] = {"user_id": sender_id, "count": 0}

    if busy:
        _reply(event.reply_token, [_txt("這個群組已有人正在收集參考照片，請稍候。")])
        return
    logger.info("setref start conv=%s user=%s", conv_id, sender_id)
    _reply(event.reply_token, [_txt(
        "📸 開始收集參考照片。請傳送 3-5 張人像照片（只有你傳送的照片會被使用），完成後輸入 /done。"
    )])


def _finish_ref_collection(event: MessageEvent, sender_id: Optional[str]) -> None:
    conv_id = _group_or_room_id(event.source)
    live = _group_refs_dir(conv_id)
    staging = _group_staging_dir(conv_id)
    count = 0
    with _ref_lock:
        collector = _ref_collectors.get(conv_id)
        if collector is None:
            action = "none"
        elif collector["user_id"] != sender_id:
            action = "not_owner"
        elif collector["count"] == 0:
            del _ref_collectors[conv_id]
            action = "empty"
        else:
            count = collector["count"]
            # Atomically swap staging into this group's live set: drop its old
            # refs subdir, then promote its staging subdir in place.
            shutil.rmtree(live, ignore_errors=True)
            live.parent.mkdir(parents=True, exist_ok=True)
            staging.rename(live)
            del _ref_collectors[conv_id]
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
    conv_id = _group_or_room_id(event.source)

    collector = _ref_collectors.get(conv_id)
    if collector is None or not sender_id or sender_id != collector["user_id"]:
        return

    # Fetch the (slow) image bytes outside the lock, then commit under it so a
    # concurrent /done or second photo can't hand out a duplicate index.
    try:
        content = _get_image_bytes(event.message.id)
    except Exception:
        logger.exception("on_image failed to fetch content")
        return

    staging = _group_staging_dir(conv_id)
    with _ref_lock:
        collector = _ref_collectors.get(conv_id)
        if collector is None or sender_id != collector["user_id"]:
            return  # collection ended or changed hands while we were fetching
        idx = collector["count"]
        staging.mkdir(parents=True, exist_ok=True)
        (staging / f"ref_{idx}.jpg").write_bytes(content)
        collector["count"] = idx + 1
        count = collector["count"]

    _reply(event.reply_token, [_txt(
        f"已收到第 {count} 張參考照片，傳送更多或輸入 /done 完成。"
    )])


# ── Join handler & approval (access control) ─────────────────────────────────────

def _group_or_room_id(source) -> str:
    return source.group_id if source.type == "group" else source.room_id


def _is_admin(source) -> bool:
    """True only for the configured ADMIN_LINE_USER_ID. Group message events
    carry the sender's user id even on unverified accounts, so this doesn't need
    the verified/premium-only member-list API."""
    admin_id = os.environ.get("ADMIN_LINE_USER_ID", "").strip()
    return bool(admin_id) and _sender_id(source) == admin_id


# Groups we've already DM'd the admin about (in-memory; a fresh reminder after
# a restart is fine). Covers groups the bot was already in before remote
# approval existed, where no JoinEvent fires to trigger the notification.
_pending_notified: set[str] = set()


def _require_approved(event) -> bool:
    """Gate for operational commands. Replies with how to enable and returns
    False when this group/room isn't on the allowlist. Also pings the admin
    once (per group, per process) so they can approve it remotely."""
    conv_id = _group_or_room_id(event.source)
    if _is_approved(conv_id):
        return True
    _reply(event.reply_token, [_txt(
        "⚠️ 這個群組尚未啟用，請管理員在這裡輸入 /approve 以啟用。"
    )])
    if conv_id not in _pending_notified:
        _pending_notified.add(conv_id)
        _notify_admin(_pending_group_notice(conv_id, joined=False))
    return False


def _pending_group_notice(conv_id: str, joined: bool) -> str:
    lead = "🔔 我被加入了一個尚未啟用的群組。" if joined else \
        "🔔 有一個尚未啟用的群組嘗試使用搜尋功能。"
    return (
        f"{lead}\n"
        f"群組 ID：\n{conv_id}\n\n"
        f"回覆「/approve {conv_id}」即可啟用。"
    )


def _handle_approval(event, approve: bool) -> None:
    source = event.source
    if not _is_admin(source):
        _reply(event.reply_token, [_txt("⚠️ 只有管理員可以執行此操作。")])
        return
    conv_id = _group_or_room_id(source)
    _set_approved(conv_id, approve)
    logger.info("approval set conv=%s approved=%s by admin", conv_id, approve)
    if approve:
        _reply(event.reply_token, [_txt(
            "✅ 已啟用！這個群組現在可以使用搜尋功能了，傳送 Google Drive 資料夾連結即可開始。"
        )])
    else:
        _reply(event.reply_token, [_txt("🚫 已停用，這個群組已無法使用搜尋功能。")])


def _notify_admin(text: str) -> None:
    """Best-effort DM to the configured admin. No-ops if no admin is set, and
    swallows push errors (e.g. the admin has never opened a 1:1 chat, so the
    bot has no permission to message them)."""
    admin_id = os.environ.get("ADMIN_LINE_USER_ID", "").strip()
    if not admin_id:
        return
    try:
        _push(admin_id, [_txt(text)])
    except Exception:
        logger.exception("failed to notify admin")


def _handle_admin_dm(event, text: str) -> bool:
    """Handle admin-only commands sent in the 1:1 chat with the bot. This lets
    the admin approve/revoke groups they aren't a member of by naming the group
    id explicitly. Returns True if the text was an admin command (and handled),
    False otherwise so the caller can fall through to the default DM reply.

    Caller must have already confirmed the sender is the admin."""
    parts = text.split()
    cmd = parts[0].lower() if parts else ""

    if cmd == "/help":
        _reply(event.reply_token, [_txt(_ADMIN_DM_HELP)])
        return True

    if cmd == "/groups":
        with _approved_lock:
            groups = sorted(_approved_groups)
        if groups:
            listing = "\n".join(groups)
            _reply(event.reply_token, [_txt(
                f"✅ 已啟用的群組（{len(groups)}）：\n{listing}"
            )])
        else:
            _reply(event.reply_token, [_txt("目前沒有已啟用的群組。")])
        return True

    if cmd in ("/approve", "/revoke"):
        approve = cmd == "/approve"
        if len(parts) < 2:
            _reply(event.reply_token, [_txt(
                f"用法：{cmd} <群組 ID>\n輸入 /groups 查看已啟用的群組 ID。"
            )])
            return True
        conv_id = parts[1].strip()
        _set_approved(conv_id, approve)
        logger.info(
            "approval set conv=%s approved=%s by admin (remote DM)", conv_id, approve
        )
        if approve:
            _reply(event.reply_token, [_txt(f"✅ 已啟用群組：\n{conv_id}")])
            # Let the group itself know it's live (best-effort — the bot may
            # not be in that group, in which case the push simply fails).
            try:
                _push(conv_id, [_txt(
                    "✅ 已啟用！這個群組現在可以使用搜尋功能了，"
                    "傳送 Google Drive 資料夾連結即可開始。"
                )])
            except Exception:
                logger.exception("failed to announce approval to group %s", conv_id)
        else:
            _reply(event.reply_token, [_txt(f"🚫 已停用群組：\n{conv_id}")])
        return True

    return False


@handler.add(JoinEvent)
def on_join(event: JoinEvent) -> None:
    source = event.source
    if source.type not in ("group", "room"):
        return

    conv_id = _group_or_room_id(source)
    approved = _is_approved(conv_id)
    logger.info("on_join type=%s id=%s approved=%s", source.type, conv_id, approved)

    if approved:
        _reply(event.reply_token, [_txt(
            "👋 哈囉！這個群組已啟用，傳送 Google Drive 資料夾連結即可開始搜尋，輸入 /help 查看指令。"
        )])
    else:
        _reply(event.reply_token, [_txt(
            "👋 哈囉！請管理員在這個群組輸入 /approve 以啟用搜尋功能。"
        )])
        # DM the admin the group id so they can approve it even if they aren't
        # a member here. Mark it notified so _require_approved doesn't re-ping.
        _pending_notified.add(conv_id)
        _notify_admin(_pending_group_notice(conv_id, joined=True))


# ── Background search ──────────────────────────────────────────────────────────

# Rough per-image throughput, used only for the heads-up estimate. Calibrated
# from a real run (362 imgs: ~10 min download + ~11 min scan ≈ 21 min total).
# Every image is scanned; only not-yet-cached images are downloaded.
_DOWNLOAD_SECONDS_PER_IMAGE = 1.8
_SCAN_SECONDS_PER_IMAGE = 2.0


def _download_notice(download_urls: list[str], cached_count: int) -> str:
    """One notice covering the whole search, with a rough time estimate for the
    full pipeline: downloading the not-yet-cached folders, then face-scanning
    every image (downloaded + already cached). Falls back to a generic message
    if a folder's image count can't be determined."""
    download_count = 0
    for url in download_urls:
        count = count_folder_images(url)
        if not count:
            return "⬇️ 開始下載資料夾，請稍候…"
        download_count += count
    scan_count = download_count + cached_count
    seconds = (
        download_count * _DOWNLOAD_SECONDS_PER_IMAGE
        + scan_count * _SCAN_SECONDS_PER_IMAGE
    )
    minutes = max(1, round(seconds / 60))
    return f"⬇️ 開始下載並比對（約 {download_count} 張照片，預計約 {minutes} 分鐘）…"


def _run_search(target_id: str, urls: list[str], stop_event: threading.Event) -> None:
    public_url = os.environ.get("PUBLIC_URL", "").rstrip("/")
    album_id = _album_id(urls)

    # Every triggered search first sweeps out cache/album folders whose own
    # timestamp is older than the retention window, so old downloads and albums
    # don't accumulate on disk.
    for root in (CACHE_ROOT, ALBUMS_DIR):
        removed = prune_old_entries(root, _MAX_CACHE_AGE_SECONDS)
        if removed:
            logger.info("pruned %d stale entries from %s", removed, root)

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

        ref_paths = _reference_photo_paths(target_id)
        if not ref_paths:
            _push(target_id, [_txt("❌ 這個群組尚未設定參考照片，請先使用 /setref 設定。")])
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
        cached_count = sum(len(cached) for _, _, _, cached in resolved if cached)
        if to_download:
            _push(target_id, [_txt(_download_notice(to_download, cached_count))])

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
