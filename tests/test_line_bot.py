from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import line_bot
from line_bot import _PASTE_TEXT_PROMPT, _REF_PHOTO_PROMPT, _SEARCHING_MSG, on_postback


def _reset_token_cache():
    line_bot._token_cache["token"] = None
    line_bot._token_cache["expires_at"] = 0.0


@patch.dict("os.environ", {"LINE_CHANNEL_ACCESS_TOKEN": "explicit-token"}, clear=True)
@patch("line_bot.requests")
def test_access_token_prefers_explicit_token(mock_requests):
    _reset_token_cache()

    assert line_bot._access_token() == "explicit-token"
    mock_requests.post.assert_not_called()


@patch.dict("os.environ", {"LINE_CHANNEL_ID": "cid", "LINE_CHANNEL_SECRET": "csecret"}, clear=True)
@patch("line_bot.requests")
def test_access_token_reuses_cached_token_before_expiry(mock_requests):
    _reset_token_cache()
    line_bot._token_cache["token"] = "cached-token"
    line_bot._token_cache["expires_at"] = line_bot.time.time() + 3600

    assert line_bot._access_token() == "cached-token"
    mock_requests.post.assert_not_called()


@patch.dict("os.environ", {"LINE_CHANNEL_ID": "cid", "LINE_CHANNEL_SECRET": "csecret"}, clear=True)
@patch("line_bot.requests")
def test_access_token_exchanges_and_caches_new_token(mock_requests):
    _reset_token_cache()
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"access_token": "fresh-token", "expires_in": 2592000}
    mock_requests.post.return_value = mock_resp

    result = line_bot._access_token()

    assert result == "fresh-token"
    mock_requests.post.assert_called_once()
    assert line_bot._token_cache["token"] == "fresh-token"

    # second call reuses the cache instead of exchanging again
    result2 = line_bot._access_token()
    assert result2 == "fresh-token"
    mock_requests.post.assert_called_once()


@patch.dict("os.environ", {}, clear=True)
@patch("line_bot.requests")
def test_access_token_raises_when_credentials_missing(mock_requests):
    _reset_token_cache()

    try:
        line_bot._access_token()
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "LINE credentials missing" in str(e)
    mock_requests.post.assert_not_called()


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
