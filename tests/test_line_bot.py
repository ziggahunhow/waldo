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
    monkeypatch.setattr(line_bot, "REFS_DIR", tmp_path)

    paths = line_bot._reference_photo_paths()

    assert sorted(Path(p).name for p in paths) == ["a.jpg", "b.jpg"]


def test_reference_photo_paths_missing_dir_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(line_bot, "REFS_DIR", tmp_path / "does-not-exist")

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


def _fake_text_event(group_id: str, text: str, reply_token: str = "reply-token"):
    """Fake a group-chat event by default — on_text now only acts on
    group/room sources, so this is the shape most tests need."""
    return SimpleNamespace(
        source=SimpleNamespace(type="group", group_id=group_id, user_id="U_sender"),
        reply_token=reply_token,
        message=SimpleNamespace(text=text),
    )


def _reply_text(mock_reply) -> str:
    """Text of the single TextMessage passed to the last _reply() call."""
    _, messages = mock_reply.call_args[0]
    return messages[0].text


def test_push_target_uses_user_id_for_1to1_chat():
    source = SimpleNamespace(type="user", user_id="U123")
    assert line_bot._push_target(source) == "U123"


def test_push_target_uses_group_id_for_group_chat():
    source = SimpleNamespace(type="group", group_id="G123", user_id="U123")
    assert line_bot._push_target(source) == "G123"


def test_push_target_uses_room_id_for_multi_person_room():
    source = SimpleNamespace(type="room", room_id="R123", user_id="U123")
    assert line_bot._push_target(source) == "R123"


@patch("line_bot.threading.Thread")
@patch("line_bot._reply")
def test_on_text_in_group_pushes_results_to_group_not_sender(mock_reply, mock_thread):
    group_id = "G_search_1"
    line_bot._active_searches.pop(group_id, None)
    event = SimpleNamespace(
        source=SimpleNamespace(type="group", group_id=group_id, user_id="U_sender"),
        reply_token="reply-token",
        message=SimpleNamespace(text="https://drive.google.com/drive/folders/abc123"),
    )

    line_bot.on_text(event)

    _, kwargs = mock_thread.call_args
    assert kwargs["args"][0] == group_id
    assert group_id in line_bot._active_searches
    line_bot._active_searches.pop(group_id, None)


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


# ── DM rejection ─────────────────────────────────────────────────────────────────

@patch("line_bot.threading.Thread")
@patch("line_bot._reply")
def test_on_text_in_dm_is_rejected_and_ignores_content(mock_reply, mock_thread):
    url = "https://drive.google.com/drive/folders/abc123"
    event = SimpleNamespace(
        source=SimpleNamespace(type="user", user_id="U_dm"),
        reply_token="reply-token",
        message=SimpleNamespace(text=url),
    )
    line_bot.on_text(event)

    mock_reply.assert_called_once()
    assert "僅限群組使用" in _reply_text(mock_reply)
    mock_thread.assert_not_called()


# ── Join handler (access control) ─────────────────────────────────────────────────

def _fake_join_event(source, reply_token: str = "reply-token"):
    return SimpleNamespace(source=source, reply_token=reply_token)


@patch("line_bot._leave")
@patch("line_bot._member_present", return_value=True)
@patch.dict("os.environ", {"ADMIN_LINE_USER_ID": "U_admin"}, clear=True)
@patch("line_bot._reply")
def test_on_join_stays_when_admin_present(mock_reply, mock_member_present, mock_leave):
    source = SimpleNamespace(type="group", group_id="G1", user_id=None)
    line_bot.on_join(_fake_join_event(source))

    mock_member_present.assert_called_once_with(source, "U_admin")
    mock_leave.assert_not_called()
    assert "僅限管理員" not in _reply_text(mock_reply)


@patch("line_bot._leave")
@patch("line_bot._member_present", return_value=False)
@patch.dict("os.environ", {"ADMIN_LINE_USER_ID": "U_admin"}, clear=True)
@patch("line_bot._reply")
def test_on_join_leaves_when_admin_not_a_member(mock_reply, mock_member_present, mock_leave):
    source = SimpleNamespace(type="group", group_id="G2", user_id=None)
    line_bot.on_join(_fake_join_event(source))

    mock_leave.assert_called_once_with(source)
    assert "僅限管理員" in _reply_text(mock_reply)


@patch("line_bot._leave")
@patch("line_bot._member_present")
@patch.dict("os.environ", {}, clear=True)
@patch("line_bot._reply")
def test_on_join_leaves_when_admin_not_configured(mock_reply, mock_member_present, mock_leave):
    source = SimpleNamespace(type="room", room_id="R1", user_id=None)
    line_bot.on_join(_fake_join_event(source))

    mock_member_present.assert_not_called()  # no point checking with no id to check for
    mock_leave.assert_called_once_with(source)
    assert "僅限管理員" in _reply_text(mock_reply)


@patch("line_bot._leave")
@patch("line_bot._member_present", side_effect=RuntimeError("account tier doesn't support this"))
@patch.dict("os.environ", {"ADMIN_LINE_USER_ID": "U_admin"}, clear=True)
@patch("line_bot._reply")
def test_on_join_fails_closed_when_membership_check_errors(mock_reply, mock_member_present, mock_leave):
    source = SimpleNamespace(type="group", group_id="G3", user_id=None)
    line_bot.on_join(_fake_join_event(source))

    mock_leave.assert_called_once_with(source)
    assert "僅限管理員" in _reply_text(mock_reply)


@patch("line_bot._member_present")
@patch("line_bot._reply")
def test_on_join_ignores_non_group_non_room_source(mock_reply, mock_member_present):
    source = SimpleNamespace(type="user", user_id="U1")
    line_bot.on_join(_fake_join_event(source))

    mock_reply.assert_not_called()
    mock_member_present.assert_not_called()


@patch("line_bot._cfg")
def test_member_present_paginates_until_found(mock_cfg):
    page1 = SimpleNamespace(member_ids=["Ux", "Uy"], next="token2")
    page2 = SimpleNamespace(member_ids=["U_admin"], next=None)
    mock_api = MagicMock()
    mock_api.get_group_members_ids.side_effect = [page1, page2]

    with patch("line_bot.ApiClient"), patch("line_bot.MessagingApi", return_value=mock_api):
        source = SimpleNamespace(type="group", group_id="G1")
        assert line_bot._member_present(source, "U_admin") is True

    assert mock_api.get_group_members_ids.call_count == 2


@patch("line_bot._cfg")
def test_member_present_returns_false_when_exhausted(mock_cfg):
    page1 = SimpleNamespace(member_ids=["Ux"], next=None)
    mock_api = MagicMock()
    mock_api.get_group_members_ids.side_effect = [page1]

    with patch("line_bot.ApiClient"), patch("line_bot.MessagingApi", return_value=mock_api):
        source = SimpleNamespace(type="group", group_id="G1")
        assert line_bot._member_present(source, "U_admin") is False


# ── Reference photo collection (/setref … images … /done) ────────────────────────

def _fake_ref_text_event(user_id, text, reply_token="reply-token"):
    return SimpleNamespace(
        source=SimpleNamespace(type="group", group_id="G_ref", user_id=user_id),
        reply_token=reply_token,
        message=SimpleNamespace(text=text),
    )


def _fake_image_event(user_id, message_id="M1", reply_token="reply-token"):
    return SimpleNamespace(
        source=SimpleNamespace(type="group", group_id="G_ref", user_id=user_id),
        reply_token=reply_token,
        message=SimpleNamespace(id=message_id),
    )


def _stage_refs(tmp_path, monkeypatch):
    """Point REFS_DIR/REFS_STAGING_DIR at tmp dirs and reset collector state."""
    refs = tmp_path / "refs"
    staging = tmp_path / "refs_staging"
    staging.mkdir()
    monkeypatch.setattr(line_bot, "REFS_DIR", refs)
    monkeypatch.setattr(line_bot, "REFS_STAGING_DIR", staging)
    monkeypatch.setattr(line_bot, "_ref_collector", None)
    return refs, staging


@patch("line_bot._reply")
def test_setref_starts_collection(mock_reply, tmp_path, monkeypatch):
    _stage_refs(tmp_path, monkeypatch)

    line_bot.on_text(_fake_ref_text_event("U_init", "/setref"))

    assert line_bot._ref_collector == {"user_id": "U_init", "count": 0}
    assert "開始收集參考照片" in _reply_text(mock_reply)


@patch("line_bot._reply")
def test_setref_rejected_while_another_collection_active(mock_reply, tmp_path, monkeypatch):
    _stage_refs(tmp_path, monkeypatch)
    monkeypatch.setattr(line_bot, "_ref_collector", {"user_id": "U_other", "count": 2})

    line_bot.on_text(_fake_ref_text_event("U_init", "/setref"))

    # untouched — the other person's session survives
    assert line_bot._ref_collector == {"user_id": "U_other", "count": 2}
    assert "已有人正在收集" in _reply_text(mock_reply)


@patch("line_bot._reply")
def test_setref_errors_when_sender_id_missing(mock_reply, tmp_path, monkeypatch):
    _stage_refs(tmp_path, monkeypatch)

    line_bot.on_text(_fake_ref_text_event(None, "/setref"))

    assert line_bot._ref_collector is None
    assert "無法識別你的使用者 ID" in _reply_text(mock_reply)


@patch("line_bot._get_image_bytes", return_value=b"jpegbytes")
@patch("line_bot._reply")
def test_on_image_from_initiator_is_saved(mock_reply, mock_get, tmp_path, monkeypatch):
    _, staging = _stage_refs(tmp_path, monkeypatch)
    monkeypatch.setattr(line_bot, "_ref_collector", {"user_id": "U_init", "count": 0})

    line_bot.on_image(_fake_image_event("U_init"))

    assert (staging / "ref_0.jpg").read_bytes() == b"jpegbytes"
    assert line_bot._ref_collector["count"] == 1
    assert "已收到第 1 張" in _reply_text(mock_reply)


@patch("line_bot._get_image_bytes")
@patch("line_bot._reply")
def test_on_image_from_non_initiator_is_ignored(mock_reply, mock_get, tmp_path, monkeypatch):
    _, staging = _stage_refs(tmp_path, monkeypatch)
    monkeypatch.setattr(line_bot, "_ref_collector", {"user_id": "U_init", "count": 0})

    line_bot.on_image(_fake_image_event("U_someone_else"))

    mock_get.assert_not_called()
    mock_reply.assert_not_called()
    assert line_bot._ref_collector["count"] == 0
    assert not any(staging.iterdir())


@patch("line_bot._get_image_bytes")
@patch("line_bot._reply")
def test_on_image_ignored_when_no_collection_active(mock_reply, mock_get, tmp_path, monkeypatch):
    _stage_refs(tmp_path, monkeypatch)  # collector is None

    line_bot.on_image(_fake_image_event("U_init"))

    mock_get.assert_not_called()
    mock_reply.assert_not_called()


@patch("line_bot._reply")
def test_done_promotes_staging_into_live_refs(mock_reply, tmp_path, monkeypatch):
    refs, staging = _stage_refs(tmp_path, monkeypatch)
    (staging / "ref_0.jpg").write_bytes(b"a")
    (staging / "ref_1.jpg").write_bytes(b"b")
    monkeypatch.setattr(line_bot, "_ref_collector", {"user_id": "U_init", "count": 2})

    line_bot.on_text(_fake_ref_text_event("U_init", "/done"))

    assert line_bot._ref_collector is None
    assert sorted(p.name for p in refs.iterdir()) == ["ref_0.jpg", "ref_1.jpg"]
    assert line_bot.REFS_STAGING_DIR.exists()
    assert not any(line_bot.REFS_STAGING_DIR.iterdir())  # recreated empty
    assert "已更新參考照片（共 2 張）" in _reply_text(mock_reply)


@patch("line_bot._reply")
def test_done_by_non_initiator_leaves_state_untouched(mock_reply, tmp_path, monkeypatch):
    refs, staging = _stage_refs(tmp_path, monkeypatch)
    (staging / "ref_0.jpg").write_bytes(b"a")
    monkeypatch.setattr(line_bot, "_ref_collector", {"user_id": "U_init", "count": 1})

    line_bot.on_text(_fake_ref_text_event("U_intruder", "/done"))

    assert line_bot._ref_collector == {"user_id": "U_init", "count": 1}
    assert not refs.exists()  # not promoted
    assert "只有發起 /setref 的人" in _reply_text(mock_reply)


@patch("line_bot._reply")
def test_done_with_no_photos_keeps_existing_refs(mock_reply, tmp_path, monkeypatch):
    refs, staging = _stage_refs(tmp_path, monkeypatch)
    refs.mkdir()
    (refs / "old.jpg").write_bytes(b"keep-me")
    monkeypatch.setattr(line_bot, "_ref_collector", {"user_id": "U_init", "count": 0})

    line_bot.on_text(_fake_ref_text_event("U_init", "/done"))

    assert line_bot._ref_collector is None
    assert (refs / "old.jpg").read_bytes() == b"keep-me"  # untouched
    assert "尚未收到任何照片" in _reply_text(mock_reply)


@patch("line_bot._reply")
def test_done_with_no_active_collection(mock_reply, tmp_path, monkeypatch):
    _stage_refs(tmp_path, monkeypatch)  # collector is None

    line_bot.on_text(_fake_ref_text_event("U_init", "/done"))

    assert "目前沒有正在收集的參考照片" in _reply_text(mock_reply)
