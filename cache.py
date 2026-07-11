import shutil
import time
from pathlib import Path
from typing import List, Optional


CACHE_ROOT = Path(".cache")
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic"}


def get_cache_dir(folder_id: str) -> Path:
    """Return the local cache directory for a given Drive folder ID.

    Example: folder_id "abc123" → Path(".cache/abc123")
    """
    return CACHE_ROOT / folder_id


def prune_old_entries(
    root: Path, max_age_seconds: float, now: Optional[float] = None
) -> int:
    """Best-effort removal of each immediate child of `root` (a cache/album
    subfolder or a loose file) whose own timestamp is older than
    max_age_seconds. Subfolders are judged by the folder's own mtime, not by the
    files inside them. Returns the number of children removed."""
    if not root.is_dir():
        return 0
    cutoff = (time.time() if now is None else now) - max_age_seconds
    removed = 0
    for child in root.iterdir():
        try:
            if child.stat().st_mtime >= cutoff:
                continue
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def list_cached_images(cache_dir: Path) -> List[Path]:
    """Return all image files (.jpg, .jpeg, .png) in cache_dir.

    Returns an empty list if cache_dir does not exist yet.
    """
    if not cache_dir.exists():
        return []
    return [p for p in cache_dir.iterdir() if p.suffix.lower() in _IMAGE_EXTENSIONS]
