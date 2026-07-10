from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import line_bot


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


def test_reference_photo_paths_filters_hidden_files_and_dirs(tmp_path, monkeypatch):
    (tmp_path / "a.jpg").write_bytes(b"x")
    (tmp_path / "b.jpg").write_bytes(b"x")
    (tmp_path / ".DS_Store").write_bytes(b"x")
    (tmp_path / "subdir").mkdir()
    monkeypatch.setattr(line_bot, "_REF_DIR", tmp_path)

    paths = line_bot._reference_photo_paths()

    assert sorted(Path(p).name for p in paths) == ["a.jpg", "b.jpg"]


def test_reference_photo_paths_missing_dir_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(line_bot, "_REF_DIR", tmp_path / "does-not-exist")

    assert line_bot._reference_photo_paths() == []


def _fake_text_event(user_id: str, text: str, reply_token: str = "reply-token"):
    return SimpleNamespace(
        source=SimpleNamespace(user_id=user_id),
        reply_token=reply_token,
        message=SimpleNamespace(text=text),
    )


def _reply_text(mock_reply) -> str:
    """Text of the single TextMessage passed to the last _reply() call."""
    _, messages = mock_reply.call_args[0]
    return messages[0].text


@patch("line_bot.threading.Thread")
@patch("line_bot._reply")
def test_on_text_without_drive_links_replies_with_error(mock_reply, mock_thread):
    line_bot.on_text(_fake_text_event("U1", "hello there, no links here"))

    mock_reply.assert_called_once()
    assert "No Google Drive links found" in _reply_text(mock_reply)
    mock_thread.assert_not_called()


@patch("line_bot.threading.Thread")
@patch("line_bot._reply")
def test_on_text_with_drive_link_starts_search_thread(mock_reply, mock_thread):
    url = "https://drive.google.com/drive/folders/abc123"
    line_bot.on_text(_fake_text_event("U2", f"check this out {url}"))

    mock_reply.assert_called_once()
    mock_thread.assert_called_once()
    _, kwargs = mock_thread.call_args
    assert kwargs["args"] == ("U2", [url])
    assert kwargs["daemon"] is True
    mock_thread.return_value.start.assert_called_once()


@patch("line_bot.threading.Thread")
@patch("line_bot._reply")
def test_on_text_dedupes_repeated_drive_links(mock_reply, mock_thread):
    url = "https://drive.google.com/drive/folders/abc123"
    line_bot.on_text(_fake_text_event("U3", f"{url} and again {url}"))

    _, kwargs = mock_thread.call_args
    assert kwargs["args"] == ("U3", [url])
