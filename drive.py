import os
import re
from pathlib import Path

import gdown
import requests


_FOLDER_URL_RE = re.compile(
    r"https://drive\.google\.com/drive/(?:u/\d+/)?folders/([a-zA-Z0-9_-]+)"
)
_DRIVE_API = "https://www.googleapis.com/drive/v3/files"
_FILE_DOWNLOAD = "https://drive.google.com/uc"


def extract_folder_id(url: str) -> str:
    """Extract the folder ID from a Google Drive folder URL.

    Raises ValueError for non-folder or malformed URLs.
    """
    match = _FOLDER_URL_RE.search(url)
    if not match:
        raise ValueError(f"Invalid Google Drive folder URL: {url!r}")
    return match.group(1)


def _list_all_files_api(folder_id: str, api_key: str) -> list[dict]:
    """List all files in a public Drive folder using the API (handles pagination)."""
    files = []
    page_token = None
    while True:
        params = {
            "q": f"'{folder_id}' in parents and trashed=false",
            "key": api_key,
            "pageSize": 100,
            "fields": "nextPageToken,files(id,name,mimeType)",
        }
        if page_token:
            params["pageToken"] = page_token
        r = requests.get(_DRIVE_API, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        files.extend(data.get("files", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return files


def download_images(url: str, cache_dir: Path) -> None:
    """Download all files from a public Google Drive folder into cache_dir.

    When GOOGLE_API_KEY is set, uses the Drive API v3 (supports >50 files).
    Otherwise falls back to gdown (capped at 50 files per folder page).
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    api_key = os.environ.get("GOOGLE_API_KEY")

    if api_key:
        folder_id = extract_folder_id(url)
        files = _list_all_files_api(folder_id, api_key)
        image_mime = {"image/jpeg", "image/png", "image/heif", "image/heic"}
        for f in files:
            if f.get("mimeType") in image_mime or any(
                f["name"].lower().endswith(ext)
                for ext in (".jpg", ".jpeg", ".png", ".heic", ".heif")
            ):
                dest = cache_dir / f["name"]
                if dest.exists():
                    print(f"Skipping (cached): {f['name']}")
                    continue
                print(f"Downloading: {f['name']}")
                r = requests.get(
                    _FILE_DOWNLOAD,
                    params={"id": f["id"], "export": "download"},
                    stream=True,
                    timeout=60,
                )
                r.raise_for_status()
                dest.write_bytes(r.content)
    else:
        print(
            "Tip: set GOOGLE_API_KEY to download folders with more than 50 files.\n"
            "     Get a free key at https://console.cloud.google.com/ → APIs & Services → Credentials\n"
        )
        gdown.download_folder(url, output=str(cache_dir), quiet=False, use_cookies=False, remaining_ok=True)
