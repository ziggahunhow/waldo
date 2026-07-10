import pytest
from drive import _safe_dest, extract_folder_id


def test_extract_folder_id_standard_url():
    url = "https://drive.google.com/drive/folders/1aBcDeFgHiJkLmNoPqRsTuV"
    assert extract_folder_id(url) == "1aBcDeFgHiJkLmNoPqRsTuV"


def test_extract_folder_id_with_sharing_param():
    url = "https://drive.google.com/drive/folders/1aBcDeFgHiJkLmNoPqRsTuV?usp=sharing"
    assert extract_folder_id(url) == "1aBcDeFgHiJkLmNoPqRsTuV"


def test_extract_folder_id_with_view_suffix():
    url = "https://drive.google.com/drive/folders/1aBcDeFgHiJkLmNoPqRsTuV/view"
    assert extract_folder_id(url) == "1aBcDeFgHiJkLmNoPqRsTuV"


def test_extract_folder_id_with_view_and_params():
    url = "https://drive.google.com/drive/folders/1aBcDeFgHiJkLmNoPqRsTuV/view?usp=sharing"
    assert extract_folder_id(url) == "1aBcDeFgHiJkLmNoPqRsTuV"


def test_extract_folder_id_invalid_url_raises():
    with pytest.raises(ValueError, match="Invalid Google Drive folder URL"):
        extract_folder_id("https://docs.google.com/spreadsheets/d/abc123")


def test_extract_folder_id_non_url_raises():
    with pytest.raises(ValueError, match="Invalid Google Drive folder URL"):
        extract_folder_id("not-a-url")


def test_safe_dest_resolves_plain_filename(tmp_path):
    assert _safe_dest(tmp_path, "photo.jpg") == tmp_path.resolve() / "photo.jpg"


def test_safe_dest_rejects_parent_traversal(tmp_path):
    assert _safe_dest(tmp_path, "../../line_bot.py") is None


def test_safe_dest_rejects_traversal_to_env_file(tmp_path):
    assert _safe_dest(tmp_path, "../../.env") is None


def test_safe_dest_rejects_absolute_path_escape(tmp_path):
    assert _safe_dest(tmp_path, "/etc/passwd") is None
