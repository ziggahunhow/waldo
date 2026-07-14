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
    monkeypatch.setattr(line_bot, "REFS_DIR", tmp_path)
    group_dir = tmp_path / "G_ref"
    group_dir.mkdir()
    (group_dir / "a.jpg").write_bytes(b"x")
    (group_dir / "b.jpg").write_bytes(b"x")
    (group_dir / ".DS_Store").write_bytes(b"x")
    (group_dir / "subdir").mkdir()

    paths = line_bot._reference_photo_paths("G_ref")

    assert sorted(Path(p).name for p in paths) == ["a.jpg", "b.jpg"]


def test_reference_photo_paths_missing_dir_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(line_bot, "REFS_DIR", tmp_path)

    # A group with no subdirectory of its own has no reference photos.
    assert line_bot._reference_photo_paths("G_never_set") == []


def test_reference_photo_paths_are_isolated_per_group(tmp_path, monkeypatch):
    monkeypatch.setattr(line_bot, "REFS_DIR", tmp_path)
    (tmp_path / "G_a").mkdir()
    (tmp_path / "G_a" / "ref_0.jpg").write_bytes(b"a")
    (tmp_path / "G_b").mkdir()
    (tmp_path / "G_b" / "ref_0.jpg").write_bytes(b"b")

    a = line_bot._reference_photo_paths("G_a")
    b = line_bot._reference_photo_paths("G_b")

    assert [Path(p).parent.name for p in a] == ["G_a"]
    assert [Path(p).parent.name for p in b] == ["G_b"]
    assert line_bot._reference_photo_paths("G_c") == []


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


@patch("line_bot._require_approved", return_value=True)
@patch("line_bot.threading.Thread")
@patch("line_bot._reply")
def test_on_text_in_group_pushes_results_to_group_not_sender(mock_reply, mock_thread, mock_gate):
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


@patch("line_bot._require_approved", return_value=True)
@patch("line_bot.threading.Thread")
@patch("line_bot._reply")
def test_on_text_with_drive_link_starts_search_thread(mock_reply, mock_thread, mock_gate):
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


@patch("line_bot._require_approved", return_value=True)
@patch("line_bot.threading.Thread")
@patch("line_bot._reply")
def test_on_text_dedupes_repeated_drive_links(mock_reply, mock_thread, mock_gate):
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


@patch("line_bot._require_approved", return_value=True)
@patch("line_bot.threading.Thread")
@patch("line_bot._reply")
def test_on_text_blocks_second_search_while_one_is_running(mock_reply, mock_thread, mock_gate):
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


# ── Join handler & approval (access control) ──────────────────────────────────────

def _fake_join_event(source, reply_token: str = "reply-token"):
    return SimpleNamespace(source=source, reply_token=reply_token)


def _fake_cmd_event(text, group_id="G_appr", user_id="U_sender", reply_token="reply-token"):
    return SimpleNamespace(
        source=SimpleNamespace(type="group", group_id=group_id, user_id=user_id),
        reply_token=reply_token,
        message=SimpleNamespace(text=text),
    )


@patch("line_bot._reply")
def test_on_join_prompts_for_approval_when_not_approved(mock_reply, monkeypatch):
    monkeypatch.setattr(line_bot, "_approved_groups", set())
    source = SimpleNamespace(type="group", group_id="G_new", user_id=None)
    line_bot.on_join(_fake_join_event(source))

    assert "/approve" in _reply_text(mock_reply)


@patch("line_bot._reply")
def test_on_join_welcomes_when_already_approved(mock_reply, monkeypatch):
    monkeypatch.setattr(line_bot, "_approved_groups", {"G_known"})
    source = SimpleNamespace(type="group", group_id="G_known", user_id=None)
    line_bot.on_join(_fake_join_event(source))

    assert "已啟用" in _reply_text(mock_reply)


@patch("line_bot._reply")
def test_on_join_ignores_non_group_non_room_source(mock_reply):
    source = SimpleNamespace(type="user", user_id="U1")
    line_bot.on_join(_fake_join_event(source))

    mock_reply.assert_not_called()


@patch.dict("os.environ", {"ADMIN_LINE_USER_ID": "U_admin"}, clear=True)
@patch("line_bot._set_approved")
@patch("line_bot._reply")
def test_approve_by_admin_enables_group(mock_reply, mock_set, monkeypatch):
    line_bot.on_text(_fake_cmd_event("/approve", group_id="G_x", user_id="U_admin"))

    mock_set.assert_called_once_with("G_x", True)
    assert "已啟用" in _reply_text(mock_reply)


@patch.dict("os.environ", {"ADMIN_LINE_USER_ID": "U_admin"}, clear=True)
@patch("line_bot._set_approved")
@patch("line_bot._reply")
def test_approve_by_non_admin_is_rejected(mock_reply, mock_set):
    line_bot.on_text(_fake_cmd_event("/approve", group_id="G_x", user_id="U_other"))

    mock_set.assert_not_called()
    assert "只有管理員" in _reply_text(mock_reply)


@patch.dict("os.environ", {"ADMIN_LINE_USER_ID": "U_admin"}, clear=True)
@patch("line_bot._set_approved")
@patch("line_bot._reply")
def test_revoke_by_admin_disables_group(mock_reply, mock_set):
    line_bot.on_text(_fake_cmd_event("/revoke", group_id="G_x", user_id="U_admin"))

    mock_set.assert_called_once_with("G_x", False)
    assert "已停用" in _reply_text(mock_reply)


@patch("line_bot._notify_admin")
@patch("line_bot.threading.Thread")
@patch("line_bot._reply")
def test_search_blocked_in_unapproved_group(mock_reply, mock_thread, mock_notify, monkeypatch):
    monkeypatch.setattr(line_bot, "_approved_groups", set())
    monkeypatch.setattr(line_bot, "_pending_notified", set())
    url = "https://drive.google.com/drive/folders/abc123"
    line_bot.on_text(_fake_cmd_event(url, group_id="G_unapproved"))

    mock_thread.assert_not_called()
    assert "尚未啟用" in _reply_text(mock_reply)


# ── Remote (DM) admin approval for groups the admin isn't in ──────────────────────

def _fake_dm_event(text, user_id="U_admin", reply_token="reply-token"):
    return SimpleNamespace(
        source=SimpleNamespace(type="user", user_id=user_id),
        reply_token=reply_token,
        message=SimpleNamespace(text=text),
    )


@patch.dict("os.environ", {"ADMIN_LINE_USER_ID": "U_admin"}, clear=True)
@patch("line_bot._push")
@patch("line_bot._set_approved")
@patch("line_bot._reply")
def test_admin_dm_approve_enables_named_group(mock_reply, mock_set, mock_push):
    line_bot.on_text(_fake_dm_event("/approve G_remote"))

    mock_set.assert_called_once_with("G_remote", True)
    assert "G_remote" in _reply_text(mock_reply)


@patch.dict("os.environ", {"ADMIN_LINE_USER_ID": "U_admin"}, clear=True)
@patch("line_bot._push")
@patch("line_bot._set_approved")
@patch("line_bot._reply")
def test_admin_dm_revoke_disables_named_group(mock_reply, mock_set, mock_push):
    line_bot.on_text(_fake_dm_event("/revoke G_remote"))

    mock_set.assert_called_once_with("G_remote", False)
    assert "已停用" in _reply_text(mock_reply)


@patch.dict("os.environ", {"ADMIN_LINE_USER_ID": "U_admin"}, clear=True)
@patch("line_bot._set_approved")
@patch("line_bot._reply")
def test_admin_dm_approve_without_id_shows_usage(mock_reply, mock_set):
    line_bot.on_text(_fake_dm_event("/approve"))

    mock_set.assert_not_called()
    assert "用法" in _reply_text(mock_reply)


@patch.dict("os.environ", {"ADMIN_LINE_USER_ID": "U_admin"}, clear=True)
@patch("line_bot._set_approved")
@patch("line_bot._reply")
def test_non_admin_dm_approve_falls_through_to_group_only_notice(mock_reply, mock_set):
    line_bot.on_text(_fake_dm_event("/approve G_remote", user_id="U_other"))

    mock_set.assert_not_called()
    assert "僅限群組使用" in _reply_text(mock_reply)


@patch.dict("os.environ", {"ADMIN_LINE_USER_ID": "U_admin"}, clear=True)
@patch("line_bot._reply")
def test_admin_dm_groups_lists_approved(mock_reply, monkeypatch):
    monkeypatch.setattr(line_bot, "_approved_groups", {"G_one", "G_two"})
    line_bot.on_text(_fake_dm_event("/groups"))

    text = _reply_text(mock_reply)
    assert "G_one" in text and "G_two" in text


@patch.dict("os.environ", {"ADMIN_LINE_USER_ID": "U_admin"}, clear=True)
@patch("line_bot._notify_admin")
@patch("line_bot._reply")
def test_on_join_notifies_admin_with_group_id(mock_reply, mock_notify, monkeypatch):
    monkeypatch.setattr(line_bot, "_approved_groups", set())
    monkeypatch.setattr(line_bot, "_pending_notified", set())
    source = SimpleNamespace(type="group", group_id="G_new2", user_id=None)
    line_bot.on_join(_fake_join_event(source))

    mock_notify.assert_called_once()
    assert "G_new2" in mock_notify.call_args[0][0]


@patch("line_bot._notify_admin")
@patch("line_bot.threading.Thread")
@patch("line_bot._reply")
def test_unapproved_search_notifies_admin_once(mock_reply, mock_thread, mock_notify, monkeypatch):
    monkeypatch.setattr(line_bot, "_approved_groups", set())
    monkeypatch.setattr(line_bot, "_pending_notified", set())
    url = "https://drive.google.com/drive/folders/abc123"
    line_bot.on_text(_fake_cmd_event(url, group_id="G_pending"))
    line_bot.on_text(_fake_cmd_event(url, group_id="G_pending"))

    mock_notify.assert_called_once()
    assert "G_pending" in mock_notify.call_args[0][0]


def test_set_approved_persists_atomically(tmp_path, monkeypatch):
    f = tmp_path / "approved_groups.json"
    monkeypatch.setattr(line_bot, "APPROVED_GROUPS_FILE", f)
    monkeypatch.setattr(line_bot, "_approved_groups", set())

    line_bot._set_approved("G_a", True)
    assert line_bot._load_approved_groups() == {"G_a"}
    assert line_bot._is_approved("G_a")

    line_bot._set_approved("G_a", False)
    assert line_bot._load_approved_groups() == set()
    assert not line_bot._is_approved("G_a")


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
    """Point REFS_DIR/REFS_STAGING_DIR at tmp dirs and reset collector state.
    Returns the ``G_ref`` group's own (live refs, staging) subdirectories,
    which is what the per-group flow reads and writes."""
    refs_root = tmp_path / "refs"
    staging_root = tmp_path / "refs_staging"
    refs_root.mkdir()
    staging_root.mkdir()
    monkeypatch.setattr(line_bot, "REFS_DIR", refs_root)
    monkeypatch.setattr(line_bot, "REFS_STAGING_DIR", staging_root)
    monkeypatch.setattr(line_bot, "_ref_collectors", {})
    monkeypatch.setattr(line_bot, "_approved_groups", {"G_ref"})  # setref/done are gated
    return refs_root / "G_ref", staging_root / "G_ref"


@patch("line_bot._reply")
def test_setref_starts_collection(mock_reply, tmp_path, monkeypatch):
    _stage_refs(tmp_path, monkeypatch)

    line_bot.on_text(_fake_ref_text_event("U_init", "/setref"))

    assert line_bot._ref_collectors == {"G_ref": {"user_id": "U_init", "count": 0}}
    assert "開始收集參考照片" in _reply_text(mock_reply)


@patch("line_bot._reply")
def test_setref_rejected_while_another_collection_active(mock_reply, tmp_path, monkeypatch):
    _stage_refs(tmp_path, monkeypatch)
    monkeypatch.setattr(line_bot, "_ref_collectors", {"G_ref": {"user_id": "U_other", "count": 2}})

    line_bot.on_text(_fake_ref_text_event("U_init", "/setref"))

    # untouched — the other person's session in this group survives
    assert line_bot._ref_collectors == {"G_ref": {"user_id": "U_other", "count": 2}}
    assert "已有人正在收集" in _reply_text(mock_reply)


@patch("line_bot._reply")
def test_setref_errors_when_sender_id_missing(mock_reply, tmp_path, monkeypatch):
    _stage_refs(tmp_path, monkeypatch)

    line_bot.on_text(_fake_ref_text_event(None, "/setref"))

    assert line_bot._ref_collectors == {}
    assert "無法識別你的使用者 ID" in _reply_text(mock_reply)


@patch("line_bot._get_image_bytes", return_value=b"jpegbytes")
@patch("line_bot._reply")
def test_on_image_from_initiator_is_saved(mock_reply, mock_get, tmp_path, monkeypatch):
    _, staging = _stage_refs(tmp_path, monkeypatch)
    monkeypatch.setattr(line_bot, "_ref_collectors", {"G_ref": {"user_id": "U_init", "count": 0}})

    line_bot.on_image(_fake_image_event("U_init"))

    assert (staging / "ref_0.jpg").read_bytes() == b"jpegbytes"
    assert line_bot._ref_collectors["G_ref"]["count"] == 1
    assert "已收到第 1 張" in _reply_text(mock_reply)


@patch("line_bot._get_image_bytes")
@patch("line_bot._reply")
def test_on_image_from_non_initiator_is_ignored(mock_reply, mock_get, tmp_path, monkeypatch):
    _, staging = _stage_refs(tmp_path, monkeypatch)
    monkeypatch.setattr(line_bot, "_ref_collectors", {"G_ref": {"user_id": "U_init", "count": 0}})

    line_bot.on_image(_fake_image_event("U_someone_else"))

    mock_get.assert_not_called()
    mock_reply.assert_not_called()
    assert line_bot._ref_collectors["G_ref"]["count"] == 0
    assert not staging.exists()  # nothing written, so the group staging dir was never created


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
    staging.mkdir()  # this group's staging subdir
    (staging / "ref_0.jpg").write_bytes(b"a")
    (staging / "ref_1.jpg").write_bytes(b"b")
    monkeypatch.setattr(line_bot, "_ref_collectors", {"G_ref": {"user_id": "U_init", "count": 2}})

    line_bot.on_text(_fake_ref_text_event("U_init", "/done"))

    assert line_bot._ref_collectors == {}
    assert sorted(p.name for p in refs.iterdir()) == ["ref_0.jpg", "ref_1.jpg"]
    assert not staging.exists()  # its staging subdir was renamed into the live set
    assert line_bot.REFS_STAGING_DIR.exists()  # the shared staging root remains
    assert "已更新參考照片（共 2 張）" in _reply_text(mock_reply)


@patch("line_bot._reply")
def test_done_by_non_initiator_leaves_state_untouched(mock_reply, tmp_path, monkeypatch):
    refs, staging = _stage_refs(tmp_path, monkeypatch)
    staging.mkdir()
    (staging / "ref_0.jpg").write_bytes(b"a")
    monkeypatch.setattr(line_bot, "_ref_collectors", {"G_ref": {"user_id": "U_init", "count": 1}})

    line_bot.on_text(_fake_ref_text_event("U_intruder", "/done"))

    assert line_bot._ref_collectors == {"G_ref": {"user_id": "U_init", "count": 1}}
    assert not refs.exists()  # not promoted
    assert "只有發起 /setref 的人" in _reply_text(mock_reply)


@patch("line_bot._reply")
def test_done_with_no_photos_keeps_existing_refs(mock_reply, tmp_path, monkeypatch):
    refs, staging = _stage_refs(tmp_path, monkeypatch)
    refs.mkdir()
    (refs / "old.jpg").write_bytes(b"keep-me")
    monkeypatch.setattr(line_bot, "_ref_collectors", {"G_ref": {"user_id": "U_init", "count": 0}})

    line_bot.on_text(_fake_ref_text_event("U_init", "/done"))

    assert line_bot._ref_collectors == {}
    assert (refs / "old.jpg").read_bytes() == b"keep-me"  # untouched
    assert "尚未收到任何照片" in _reply_text(mock_reply)


@patch("line_bot._reply")
def test_done_with_no_active_collection(mock_reply, tmp_path, monkeypatch):
    _stage_refs(tmp_path, monkeypatch)  # no collectors

    line_bot.on_text(_fake_ref_text_event("U_init", "/done"))

    assert "目前沒有正在收集的參考照片" in _reply_text(mock_reply)


@patch("line_bot._get_image_bytes", return_value=b"z")
@patch("line_bot._reply")
def test_collections_in_different_groups_are_independent(mock_reply, mock_get, tmp_path, monkeypatch):
    refs_root = tmp_path / "refs"
    staging_root = tmp_path / "refs_staging"
    refs_root.mkdir()
    staging_root.mkdir()
    monkeypatch.setattr(line_bot, "REFS_DIR", refs_root)
    monkeypatch.setattr(line_bot, "REFS_STAGING_DIR", staging_root)
    monkeypatch.setattr(line_bot, "_ref_collectors", {})
    monkeypatch.setattr(line_bot, "_approved_groups", {"G_a", "G_b"})

    def _grp_text(group_id, user_id, text):
        return SimpleNamespace(
            source=SimpleNamespace(type="group", group_id=group_id, user_id=user_id),
            reply_token="reply-token",
            message=SimpleNamespace(text=text),
        )

    def _grp_image(group_id, user_id):
        return SimpleNamespace(
            source=SimpleNamespace(type="group", group_id=group_id, user_id=user_id),
            reply_token="reply-token",
            message=SimpleNamespace(id="M1"),
        )

    # Two groups open collections concurrently — neither blocks the other.
    line_bot.on_text(_grp_text("G_a", "U1", "/setref"))
    line_bot.on_text(_grp_text("G_b", "U2", "/setref"))
    assert set(line_bot._ref_collectors) == {"G_a", "G_b"}

    # A photo in G_a advances only G_a and lands in G_a's own staging dir.
    line_bot.on_image(_grp_image("G_a", "U1"))
    assert line_bot._ref_collectors["G_a"]["count"] == 1
    assert line_bot._ref_collectors["G_b"]["count"] == 0
    assert (staging_root / "G_a" / "ref_0.jpg").read_bytes() == b"z"
    assert not any((staging_root / "G_b").iterdir())  # G_b's collection has no photos yet
