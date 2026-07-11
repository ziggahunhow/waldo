import time

from cache import prune_old_entries


_DAY = 24 * 60 * 60


def _set_age(path, seconds_old, now):
    ts = now - seconds_old
    import os

    os.utime(path, (ts, ts))


def test_prune_removes_old_folders_and_keeps_fresh(tmp_path):
    now = time.time()
    old = tmp_path / "old_folder"
    old.mkdir()
    old_img = old / "a.jpg"
    old_img.write_bytes(b"x")
    _set_age(old_img, 5 * _DAY, now)
    _set_age(old, 5 * _DAY, now)

    fresh = tmp_path / "fresh_folder"
    fresh.mkdir()
    (fresh / "b.jpg").write_bytes(b"x")  # just created → fresh

    removed = prune_old_entries(tmp_path, 3 * _DAY, now=now)

    assert removed == 1
    assert not old.exists()
    assert fresh.exists()


def test_prune_uses_folder_time_not_file_time(tmp_path):
    now = time.time()
    # The folder's own timestamp is old even though it holds a freshly-written
    # file — the folder time wins, so it is pruned.
    folder = tmp_path / "old_dir_fresh_file"
    folder.mkdir()
    (folder / "fresh.jpg").write_bytes(b"x")  # fresh file inside
    _set_age(folder, 5 * _DAY, now)           # but the folder itself is old

    removed = prune_old_entries(tmp_path, 3 * _DAY, now=now)

    assert removed == 1
    assert not folder.exists()


def test_prune_removes_old_loose_files(tmp_path):
    now = time.time()
    old_file = tmp_path / "stale.txt"
    old_file.write_bytes(b"x")
    _set_age(old_file, 4 * _DAY, now)

    removed = prune_old_entries(tmp_path, 3 * _DAY, now=now)

    assert removed == 1
    assert not old_file.exists()


def test_prune_missing_root_is_noop(tmp_path):
    assert prune_old_entries(tmp_path / "does_not_exist", 3 * _DAY, now=time.time()) == 0
