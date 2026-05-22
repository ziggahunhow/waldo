import re
from pathlib import Path

import gdown


_FOLDER_URL_RE = re.compile(
    r"https://drive\.google\.com/drive/folders/([a-zA-Z0-9_-]+)"
)


def extract_folder_id(url: str) -> str:
    """Extract the folder ID from a Google Drive folder URL.

    Raises ValueError for non-folder or malformed URLs.
    """
    match = _FOLDER_URL_RE.search(url)
    if not match:
        raise ValueError(f"Invalid Google Drive folder URL: {url!r}")
    return match.group(1)


def download_images(url: str, cache_dir: Path) -> None:
    """Download all files from a public Google Drive folder into cache_dir.

    Uses gdown which skips files that already exist locally.
    cache_dir is created if it does not exist.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    gdown.download_folder(url, output=str(cache_dir), quiet=False, use_cookies=False)
