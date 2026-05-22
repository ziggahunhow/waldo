from pathlib import Path
from typing import List


_CACHE_ROOT = Path(".cache")
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}


def get_cache_dir(folder_id: str) -> Path:
    """Return the local cache directory for a given Drive folder ID.

    Example: folder_id "abc123" → Path(".cache/abc123")
    """
    return _CACHE_ROOT / folder_id


def list_cached_images(cache_dir: Path) -> List[Path]:
    """Return all image files (.jpg, .jpeg, .png) in cache_dir.

    Returns an empty list if cache_dir does not exist yet.
    """
    if not cache_dir.exists():
        return []
    return [p for p in cache_dir.iterdir() if p.suffix.lower() in _IMAGE_EXTENSIONS]
