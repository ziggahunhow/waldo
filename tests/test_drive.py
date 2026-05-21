import pytest
from drive import extract_folder_id


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
