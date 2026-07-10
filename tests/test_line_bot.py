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


def test_album_id_is_deterministic_regardless_of_url_order():
    a = ["https://drive.google.com/drive/folders/aaa", "https://drive.google.com/drive/folders/bbb"]
    b = list(reversed(a))

    assert line_bot._album_id(a) == line_bot._album_id(b)


def test_album_id_differs_for_different_urls():
    a = ["https://drive.google.com/drive/folders/aaa"]
    b = ["https://drive.google.com/drive/folders/bbb"]

    assert line_bot._album_id(a) != line_bot._album_id(b)


def test_save_album_copies_files_and_dedupes_names(tmp_path, monkeypatch):
    monkeypatch.setattr(line_bot, "ALBUMS_DIR", tmp_path)

    src_a = tmp_path / "src_a"
    src_b = tmp_path / "src_b"
    src_a.mkdir()
    src_b.mkdir()
    img_a = src_a / "photo.jpg"
    img_b = src_b / "photo.jpg"
    img_a.write_bytes(b"aaa")
    img_b.write_bytes(b"bbb")

    line_bot._save_album("abc123", [(img_a, "folderAAA"), (img_b, "folderBBB")])

    album_dir = tmp_path / "abc123"
    names = sorted(p.name for p in album_dir.iterdir())
    assert names == ["photo.jpg", "photo_folder.jpg"]
    assert (album_dir / "photo.jpg").read_bytes() == b"aaa"
    assert (album_dir / "photo_folder.jpg").read_bytes() == b"bbb"


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
def test_on_text_without_drive_links_is_silently_ignored(mock_reply, mock_thread):
    line_bot.on_text(_fake_text_event("U1", "hello there, no links here"))

    mock_reply.assert_not_called()
    mock_thread.assert_not_called()


@patch("line_bot.threading.Thread")
@patch("line_bot._reply")
def test_on_text_with_drive_link_starts_search_thread(mock_reply, mock_thread):
    line_bot._active_searches.clear()
    url = "https://drive.google.com/drive/folders/abc123"
    line_bot.on_text(_fake_text_event("U2", f"check this out {url}"))

    mock_reply.assert_called_once()
    mock_thread.assert_called_once()
    _, kwargs = mock_thread.call_args
    assert kwargs["args"][0] == "U2"
    assert kwargs["args"][1] == [url]
    assert isinstance(kwargs["args"][2], line_bot.threading.Event)
    assert kwargs["daemon"] is True
    mock_thread.return_value.start.assert_called_once()


@patch("line_bot.threading.Thread")
@patch("line_bot._reply")
def test_on_text_dedupes_repeated_drive_links(mock_reply, mock_thread):
    line_bot._active_searches.clear()
    url = "https://drive.google.com/drive/folders/abc123"
    line_bot.on_text(_fake_text_event("U3", f"{url} and again {url}"))

    _, kwargs = mock_thread.call_args
    assert kwargs["args"][1] == [url]


@patch("line_bot._reply")
def test_on_text_help_command(mock_reply):
    line_bot.on_text(_fake_text_event("U4", "/help"))

    mock_reply.assert_called_once()
    assert _reply_text(mock_reply) == line_bot._HELP_TEXT


@patch("line_bot._reply")
def test_on_text_stop_with_no_active_search(mock_reply):
    line_bot._active_searches.clear()
    line_bot.on_text(_fake_text_event("U5", "/stop"))

    assert "沒有正在執行的搜尋" in _reply_text(mock_reply)


@patch("line_bot._reply")
def test_on_text_stop_signals_active_search(mock_reply):
    user_id = "U6"
    stop_event = line_bot.threading.Event()
    line_bot._active_searches[user_id] = stop_event

    try:
        line_bot.on_text(_fake_text_event(user_id, "/stop"))

        assert stop_event.is_set()
        assert "正在停止搜尋" in _reply_text(mock_reply)
    finally:
        line_bot._active_searches.pop(user_id, None)


@patch("line_bot.threading.Thread")
@patch("line_bot._reply")
def test_on_text_blocks_second_search_while_one_is_running(mock_reply, mock_thread):
    user_id = "U7"
    line_bot._active_searches[user_id] = line_bot.threading.Event()  # not yet set = running

    try:
        url = "https://drive.google.com/drive/folders/abc123"
        line_bot.on_text(_fake_text_event(user_id, url))

        assert "已有搜尋正在進行中" in _reply_text(mock_reply)
        mock_thread.assert_not_called()
    finally:
        line_bot._active_searches.pop(user_id, None)


@patch("line_bot._push")
def test_run_search_serves_cached_album_without_recomputing(mock_push, tmp_path, monkeypatch):
    monkeypatch.setattr(line_bot, "ALBUMS_DIR", tmp_path)
    monkeypatch.setattr(line_bot, "encode_references", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("should not recompute on a cache hit")
    ))

    urls = ["https://drive.google.com/drive/folders/cached"]
    album_id = line_bot._album_id(urls)
    album_dir = tmp_path / album_id
    album_dir.mkdir()
    (album_dir / "a.jpg").write_bytes(b"x")

    line_bot._run_search("U8", urls, line_bot.threading.Event())

    mock_push.assert_called_once()
    _, messages = mock_push.call_args[0]
    assert "先前搜尋過的快取結果" in messages[0].text
