# LINE Bot: Chinese Rich Menu Triggers (`更改參考照片` / `貼上文字串`)

## Context

The bot's LINE Rich Menu (configured externally in the LINE Developers
console, outside this repo) presents users with Traditional Chinese buttons
using "message" actions. Two of these buttons — `更改參考照片` (change
reference photo) and `貼上文字串` (paste text string) — send their label as
a plain text message to the webhook. `line_bot.py`'s `on_text` handler has
no recognition for either string today, so both fall through to the generic
fallback reply.

This is a narrow addition: recognize these two strings and wire them into
the existing state machine. It does not localize the rest of the bot (quick
reply labels, `on_image` confirmations, `settings`/`reset`/`help` flows stay
in English) — that would be a separate, larger pass if wanted later.

The web UI (`server.py` `/api/search`, `index.html`) is a fully separate
module with its own message flow; nothing here touches it.

## Behavior

### `更改參考照片` (change reference photo)

Recognized as a global command in `on_text`, checked alongside the existing
`reset`/`help` commands (i.e. regardless of current session state).

1. If `sess["state"] == "searching"`: reply `Search in progress — please
   wait.` and stop (mirrors the existing guard in `on_image`).
2. Otherwise:
   - Clear `sess["ref_paths"]` (discard any previously collected reference
     photos for this session).
   - Set `sess["state"] = "idle"`.
   - Reply with exactly: `請上傳3-5張人像照片`

No new state is introduced for collecting the photos themselves — the
existing `on_image` handler already accepts photo uploads in any
non-`searching` state and appends to `sess["ref_paths"]`, starting the
index fresh since the list was just cleared. Its existing (English)
confirmation reply is unchanged.

### `貼上文字串` (paste text)

Also recognized as a global command in `on_text`, same guard:

1. If `sess["state"] == "searching"`: reply `Search in progress — please
   wait.` and stop.
2. If `sess["ref_paths"]` is empty: reply `請上傳3-5張人像照片` (same
   string as above, redirecting the user to upload references first) and
   stop — no state change.
3. Otherwise: set `sess["state"] = "awaiting_url"` and reply with exactly:
   `請貼上包含 Google Drive 資料夾連結的文字`

The next text message the user sends is then handled by the existing
`awaiting_url` branch already present in `on_text` — unchanged logic:
extract Drive folder links via `_DRIVE_LINK_RE`, download, face-match
against the collected references, and push result images back via
`_run_search`. No new search logic is written; this trigger only reaches
the same code path the existing `search` quick-reply command reaches.

## Out of scope

- Translating `on_image`'s confirmation text, other quick-reply labels, or
  `settings`/`reset`/`help` flows to Chinese.
- Any change to the web UI (`server.py`, `index.html`).
- Handling LINE `PostbackEvent` (confirmed the Rich Menu uses `message`
  actions, so this isn't needed here).
- Enforcing a hard 3-5 photo cap on reference uploads — matches existing
  permissive behavior of `on_image`, which has never capped uploads.

## Logging

Both branches log via the existing `logger` in `line_bot.py`, consistent
with prior logging added to `on_text`/`on_image`/`_run_search` (already logs
`user_id`, `state`, and `text` on every `on_text` call, which covers these
two triggers without additional log lines).
