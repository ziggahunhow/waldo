from server import _safe_child


def test_safe_child_resolves_plain_filename(tmp_path):
    (tmp_path / "a.jpg").write_bytes(b"x")

    result = _safe_child(tmp_path, "a.jpg")

    assert result == tmp_path / "a.jpg"


def test_safe_child_rejects_parent_traversal(tmp_path):
    assert _safe_child(tmp_path, "..") is None


def test_safe_child_rejects_multi_segment_traversal(tmp_path):
    assert _safe_child(tmp_path, "../../server.py") is None


def test_safe_child_rejects_absolute_path_escape(tmp_path):
    assert _safe_child(tmp_path, "/etc/passwd") is None
