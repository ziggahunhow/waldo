# LINE Bot Postback Triggers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Handle the LINE Rich Menu's `更改參考照片` (change reference photo) and `貼上文字串` (paste text) buttons via `postback` actions, so button-label edits in the LINE console never silently break matching logic.

**Architecture:** Add a `PostbackEvent` handler to `line_bot.py` (the bot currently only handles `MessageEvent` for images and text). It dispatches on `event.postback.data` to two small helper functions that reuse the existing session state machine (`sess["state"]`, `sess["ref_paths"]`) already used by `on_text`/`on_image`/`_run_search` — no new state, no new search logic.

**Tech Stack:** Python 3.9, `line-bot-sdk` v3 (`linebot.v3.webhooks.PostbackEvent`/`PostbackContent`), `pytest` + `unittest.mock`.

## Global Constraints

- Reply strings, verbatim, from `docs/superpowers/specs/2026-07-10-line-bot-chinese-triggers-design.md`:
  - Change-ref prompt: `請上傳3-5張人像照片`
  - Paste-text prompt: `請貼上包含 Google Drive 資料夾連結的文字`
- Postback `data` contract (must match whatever the Rich Menu is configured with in the LINE console): `action=change_ref` and `action=paste_text`.
- Do not translate `on_image`'s confirmation text or any other existing quick-reply/settings/reset/help copy — out of scope per the spec.
- Do not touch `server.py` or `index.html` (web UI) — this is LINE-bot-only.
- No hard cap on reference photo count — matches existing `on_image` behavior.

---

### Task 1: Add `PostbackEvent` handler for Rich Menu triggers

**Files:**
- Modify: `line_bot.py:1-21` (module docstring), `line_bot.py:50` (import), `line_bot.py:171-198` (`on_image`, one-line change), `line_bot.py:379-381` (insert new section between `on_text` and `_run_search`)
- Create: `tests/test_line_bot.py`

**Interfaces:**
- Consumes (existing, from `line_bot.py`): `_session(user_id) -> dict`, `_reset_session(user_id) -> None`, `_reply(reply_token, messages) -> None`, `_txt(text) -> TextMessage`. Consumes `PostbackEvent`/`PostbackContent` from `linebot.v3.webhooks` (fields: `event.source.user_id`, `event.reply_token`, `event.postback.data`).
- Produces: `on_postback(event: PostbackEvent) -> None` (registered via `@handler.add(PostbackEvent)`), module constants `_SEARCHING_MSG`, `_REF_PHOTO_PROMPT`, `_PASTE_TEXT_PROMPT`, `_POSTBACK_CHANGE_REF`, `_POSTBACK_PASTE_TEXT`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_line_bot.py`:

```python
from types import SimpleNamespace
from unittest.mock import patch

import line_bot
from line_bot import _PASTE_TEXT_PROMPT, _REF_PHOTO_PROMPT, _SEARCHING_MSG, on_postback


def _fake_event(user_id: str, data: str, reply_token: str = "reply-token"):
    return SimpleNamespace(
        source=SimpleNamespace(user_id=user_id),
        reply_token=reply_token,
        postback=SimpleNamespace(data=data),
    )


def _reply_text(mock_reply) -> str:
    """Text of the single TextMessage passed to the last _reply() call."""
    _, messages = mock_reply.call_args[0]
    return messages[0].text


@patch("line_bot._reply")
def test_change_ref_clears_existing_refs_and_prompts(mock_reply):
    user_id = "U_change_ref_1"
    sess = line_bot._session(user_id)
    sess["ref_paths"] = ["old1.jpg", "old2.jpg"]
    sess["state"] = "collecting_refs"

    try:
        on_postback(_fake_event(user_id, "action=change_ref"))

        assert sess["ref_paths"] == []
        assert sess["state"] == "idle"
        mock_reply.assert_called_once()
        assert _reply_text(mock_reply) == _REF_PHOTO_PROMPT
    finally:
        line_bot._reset_session(user_id)


@patch("line_bot._reply")
def test_change_ref_blocked_while_searching(mock_reply):
    user_id = "U_change_ref_2"
    sess = line_bot._session(user_id)
    sess["ref_paths"] = ["old1.jpg"]
    sess["state"] = "searching"

    try:
        on_postback(_fake_event(user_id, "action=change_ref"))

        assert sess["ref_paths"] == ["old1.jpg"]
        assert sess["state"] == "searching"
        assert _reply_text(mock_reply) == _SEARCHING_MSG
    finally:
        line_bot._reset_session(user_id)


@patch("line_bot._reply")
def test_paste_text_without_refs_prompts_for_photos(mock_reply):
    user_id = "U_paste_1"
    sess = line_bot._session(user_id)
    assert sess["ref_paths"] == []

    try:
        on_postback(_fake_event(user_id, "action=paste_text"))

        assert sess["state"] == "idle"
        assert _reply_text(mock_reply) == _REF_PHOTO_PROMPT
    finally:
        line_bot._reset_session(user_id)


@patch("line_bot._reply")
def test_paste_text_with_refs_sets_awaiting_url(mock_reply):
    user_id = "U_paste_2"
    sess = line_bot._session(user_id)
    sess["ref_paths"] = ["ref1.jpg"]
    sess["state"] = "collecting_refs"

    try:
        on_postback(_fake_event(user_id, "action=paste_text"))

        assert sess["state"] == "awaiting_url"
        assert _reply_text(mock_reply) == _PASTE_TEXT_PROMPT
    finally:
        line_bot._reset_session(user_id)


@patch("line_bot._reply")
def test_paste_text_blocked_while_searching(mock_reply):
    user_id = "U_paste_3"
    sess = line_bot._session(user_id)
    sess["ref_paths"] = ["ref1.jpg"]
    sess["state"] = "searching"

    try:
        on_postback(_fake_event(user_id, "action=paste_text"))

        assert sess["state"] == "searching"
        assert _reply_text(mock_reply) == _SEARCHING_MSG
    finally:
        line_bot._reset_session(user_id)


@patch("line_bot._reply")
def test_unrecognized_postback_data_is_ignored(mock_reply):
    user_id = "U_unknown_1"
    sess = line_bot._session(user_id)
    sess["ref_paths"] = ["ref1.jpg"]
    sess["state"] = "collecting_refs"

    try:
        on_postback(_fake_event(user_id, "action=unknown"))

        mock_reply.assert_not_called()
        assert sess["state"] == "collecting_refs"
        assert sess["ref_paths"] == ["ref1.jpg"]
    finally:
        line_bot._reset_session(user_id)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_line_bot.py -v`
Expected: FAIL at collection — `ImportError: cannot import name '_PASTE_TEXT_PROMPT' from 'line_bot'` (none of the new names exist yet).

- [ ] **Step 3: Add `PostbackEvent` import**

Modify `line_bot.py:50`:

```python
# Before:
from linebot.v3.webhooks import ImageMessageContent, MessageEvent, TextMessageContent

# After:
from linebot.v3.webhooks import (
    ImageMessageContent,
    MessageEvent,
    PostbackEvent,
    TextMessageContent,
)
```

- [ ] **Step 4: Document the Rich Menu postback contract in the module docstring**

Modify `line_bot.py:10-13` — insert a new section between the existing "Settings sub-flow" block and "Setup" block:

```python
# Before (lines 10-13):
Settings sub-flow (available any time refs exist):
  settings → set tolerance | set model → back to collecting_refs

Setup
─────

# After:
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
```

- [ ] **Step 5: Add constants, helpers, and the `on_postback` handler**

Modify `line_bot.py`, inserting a new section between the end of `on_text` (line 379, the blank line after its closing `else` block) and the `# ── Background search ──` header (line 381):

```python
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

```

- [ ] **Step 6: Reuse `_SEARCHING_MSG` in `on_image`**

Modify `line_bot.py:178` (this line currently duplicates the same literal string the new handlers now share):

```python
# Before:
        _reply(event.reply_token, [_txt("Search in progress — please wait.")])

# After:
        _reply(event.reply_token, [_txt(_SEARCHING_MSG)])
```

- [ ] **Step 7: Run the new tests to verify they pass**

Run: `python3 -m pytest tests/test_line_bot.py -v`
Expected: `6 passed`

- [ ] **Step 8: Run the full test suite to check for regressions**

Run: `python3 -m pytest tests/ -q`
Expected: the 6 new `test_line_bot.py` tests pass, plus the existing `test_drive.py` tests (6) pass. `test_recognizer.py` has 6 pre-existing failures (`FileNotFoundError: landscape.jpg`) unrelated to this change — confirm the failure count/names match what's on `main` before this task (i.e. no *new* failures introduced), don't attempt to fix them here.

- [ ] **Step 9: Commit**

```bash
git add line_bot.py tests/test_line_bot.py
git commit -m "$(cat <<'EOF'
feat: handle LINE Rich Menu postback triggers for ref-photo/paste-text

更改參考照片 and 貼上文字串 buttons now dispatch via PostbackEvent
(action=change_ref / action=paste_text) instead of message-text
matching, so editing a button's display label in the LINE console
can't silently break the trigger.
EOF
)"
```
