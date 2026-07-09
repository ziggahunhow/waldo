# LINE Bot: Chinese Rich Menu Triggers (`更改參考照片` / `貼上文字串`)

## Context

The bot's LINE Rich Menu (configured externally in the LINE Developers
console, outside this repo) presents users with Traditional Chinese buttons.
Two of these — `更改參考照片` (change reference photo) and `貼上文字串`
(paste text string) — need to trigger new behavior in `line_bot.py`.

**Revised from the original draft of this spec:** these buttons will use
LINE `postback` actions rather than `message` actions. A `message` action
sends its label as literal chat text, coupling the button's display label to
the code's matching logic — rename the button and the match silently stops
firing. A `postback` action instead sends a developer-defined `data` string
via a separate `PostbackEvent`, independent of whatever label is shown to
the user. `line_bot.py` currently has no handler for `PostbackEvent` at all
(only `MessageEvent` for images and text), so this is new.

This is still a narrow addition: wire these two actions into the existing
state machine. It does not localize the rest of the bot (quick reply
labels, `on_image` confirmations, `settings`/`reset`/`help` flows stay in
English) — that would be a separate, larger pass if wanted later.

The web UI (`server.py` `/api/search`, `index.html`) is a fully separate
module with its own message flow; nothing here touches it.

## External config change required (outside this repo)

The Rich Menu itself is configured via the LINE Developers console / Rich
Menu API, not in this codebase. Whoever owns that config needs to set both
buttons to `postback` actions with:

| Button | `data` | `displayText` |
|---|---|---|
| 更改參考照片 | `action=change_ref` | `更改參考照片` |
| 貼上文字串 | `action=paste_text` | `貼上文字串` |

`displayText` makes the tap still echo as a visible chat bubble (matching
today's UX under `message` actions) even though the actual payload is the
silent `data` string. Until the Rich Menu is updated to match, these code
changes have no effect — the two `data` values above are the contract
between the Rich Menu config and this handler.

## Behavior

### New: `PostbackEvent` handler

Add `@handler.add(PostbackEvent)` (imported from `linebot.v3.webhooks`,
alongside the existing `MessageEvent`/`ImageMessageContent`/
`TextMessageContent` imports) dispatching on `event.postback.data`:

- `action=change_ref` → change-reference-photo behavior (below)
- `action=paste_text` → paste-text behavior (below)
- unrecognized `data` → ignored (log and no-op; mirrors how unrecognized
  text falls through to `on_text`'s fallback branch)

### `action=change_ref` (change reference photo)

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

### `action=paste_text` (paste text)

Same guard:

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
- Actually updating the Rich Menu config in the LINE console — that's a
  manual step outside this repo (see table above), called out but not
  performed as part of this work.
- Enforcing a hard 3-5 photo cap on reference uploads — matches existing
  permissive behavior of `on_image`, which has never capped uploads.

## Logging

`on_text` already logs `user_id`, `state`, and `text` on every call. The new
`PostbackEvent` handler needs its own log line (postback events don't go
through `on_text`), logging `user_id` and `event.postback.data` on receipt,
consistent with the existing logging conventions in this file.
