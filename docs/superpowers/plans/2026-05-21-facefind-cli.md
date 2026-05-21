# facefind CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a CLI tool that scans a public Google Drive folder and finds all images containing a specific person using face recognition.

**Architecture:** Two-stage pipeline — Stage 1 downloads images from a public Drive folder into a local cache (gdown skips already-present files); Stage 2 encodes reference photos into face vectors and searches the cache for matches. Results are printed to console and copied to a local output folder.

**Tech Stack:** Python 3.9+, `click` (CLI), `gdown` (Drive download), `face_recognition` + `dlib` (face encoding/matching), `rich` (console output), `Pillow` (image handling), `pytest` (testing)

---

## File Map

| File | Responsibility |
|---|---|
| `requirements.txt` | All Python dependencies |
| `drive.py` | Parse Drive URL → folder ID; download folder to cache dir |
| `cache.py` | Resolve `.cache/<folder_id>/` path; list cached image files |
| `recognizer.py` | Encode reference photos; check if an image contains the target |
| `output.py` | Print match table (rich); copy matches to output folder (shutil) |
| `main.py` | CLI entry point (click); orchestrate all modules |
| `tests/__init__.py` | Make tests a package |
| `tests/test_drive.py` | Unit tests for URL parsing |
| `tests/test_recognizer.py` | Unit tests for face matching logic |
| `README.md` | Setup and usage instructions |

---

### Task 1: Project Setup

**Files:**
- Create: `requirements.txt`
- Create: `tests/__init__.py`

- [ ] **Step 1: Create `requirements.txt`**

```
click>=8.1
gdown>=5.1
face_recognition>=1.3
Pillow>=10.0
rich>=13.0
pytest>=8.0
```

- [ ] **Step 2: Install dependencies**

```bash
pip install -r requirements.txt
```

> If `dlib` (required by `face_recognition`) fails via pip, install via conda first:
> ```bash
> conda install -c conda-forge dlib
> pip install face_recognition
> ```

- [ ] **Step 3: Create the tests package**

```bash
mkdir -p tests && touch tests/__init__.py
```

- [ ] **Step 4: Verify pytest finds nothing (no errors)**

```bash
pytest tests/ -v
```

Expected: `no tests ran` with exit code 0.

- [ ] **Step 5: Commit**

```bash
git add requirements.txt tests/__init__.py
git commit -m "chore: project setup and dependencies"
```

---

### Task 2: `drive.py` — URL Parsing (TDD)

**Files:**
- Create: `tests/test_drive.py`
- Create: `drive.py`

- [ ] **Step 1: Write failing tests for `extract_folder_id()`**

Create `tests/test_drive.py`:

```python
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
```

- [ ] **Step 2: Run tests — verify they fail**

```bash
pytest tests/test_drive.py -v
```

Expected: `ModuleNotFoundError: No module named 'drive'`

- [ ] **Step 3: Implement `drive.py`**

Create `drive.py`:

```python
import re
from pathlib import Path
from typing import List

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
```

- [ ] **Step 4: Run tests — verify they pass**

```bash
pytest tests/test_drive.py -v
```

Expected: all 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add drive.py tests/test_drive.py
git commit -m "feat: drive URL parsing and folder download"
```

---

### Task 3: `cache.py` — Cache Directory Management

**Files:**
- Create: `cache.py`

- [ ] **Step 1: Create `cache.py`**

```python
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
```

- [ ] **Step 2: Verify no import errors**

```bash
python -c "from cache import get_cache_dir, list_cached_images; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add cache.py
git commit -m "feat: cache directory management"
```

---

### Task 4: `recognizer.py` — Face Encoding and Matching (TDD)

**Files:**
- Create: `tests/test_recognizer.py`
- Create: `recognizer.py`

- [ ] **Step 1: Write failing tests for `encode_references()` and `is_match()`**

Create `tests/test_recognizer.py`:

```python
import numpy as np
import pytest
from unittest.mock import patch, MagicMock

from recognizer import encode_references, is_match


# Synthetic 128-dim face encodings (same shape face_recognition returns)
_FACE_A = np.zeros(128, dtype=float)
_FACE_B = np.ones(128, dtype=float)


@patch("recognizer.face_recognition")
def test_encode_references_returns_first_encoding(mock_fr):
    mock_fr.load_image_file.return_value = MagicMock()
    mock_fr.face_encodings.return_value = [_FACE_A]

    result = encode_references(["ref.jpg"])

    assert len(result) == 1
    np.testing.assert_array_equal(result[0], _FACE_A)


@patch("recognizer.face_recognition")
def test_encode_references_warns_and_skips_when_no_face(mock_fr, capsys):
    mock_fr.load_image_file.return_value = MagicMock()
    mock_fr.face_encodings.return_value = []  # no face in this photo

    result = encode_references(["bad.jpg"])

    assert result == []
    captured = capsys.readouterr()
    assert "no face detected" in captured.out.lower()


@patch("recognizer.face_recognition")
def test_encode_references_multiple_photos(mock_fr):
    mock_fr.load_image_file.return_value = MagicMock()
    mock_fr.face_encodings.side_effect = [[_FACE_A], [_FACE_B]]

    result = encode_references(["ref1.jpg", "ref2.jpg"])

    assert len(result) == 2


@patch("recognizer.face_recognition")
def test_is_match_returns_true_for_matching_face(mock_fr):
    mock_fr.load_image_file.return_value = MagicMock()
    mock_fr.face_encodings.return_value = [_FACE_A]
    mock_fr.compare_faces.return_value = [True]

    assert is_match("photo.jpg", [_FACE_A], tolerance=0.5) is True


@patch("recognizer.face_recognition")
def test_is_match_returns_false_for_different_face(mock_fr):
    mock_fr.load_image_file.return_value = MagicMock()
    mock_fr.face_encodings.return_value = [_FACE_B]
    mock_fr.compare_faces.return_value = [False]

    assert is_match("photo.jpg", [_FACE_A], tolerance=0.5) is False


@patch("recognizer.face_recognition")
def test_is_match_returns_false_when_image_has_no_faces(mock_fr):
    mock_fr.load_image_file.return_value = MagicMock()
    mock_fr.face_encodings.return_value = []  # landscape photo, no faces

    assert is_match("landscape.jpg", [_FACE_A], tolerance=0.5) is False
```

- [ ] **Step 2: Run tests — verify they fail**

```bash
pytest tests/test_recognizer.py -v
```

Expected: `ModuleNotFoundError: No module named 'recognizer'`

- [ ] **Step 3: Implement `recognizer.py`**

Create `recognizer.py`:

```python
from typing import List

import face_recognition
import numpy as np


def encode_references(reference_paths: List[str]) -> List[np.ndarray]:
    """Load reference photos and encode each detected face.

    Prints a warning for photos where no face is found.
    Only the first detected face per photo is used.
    Returns a list of 128-dim face encoding arrays.
    """
    encodings: List[np.ndarray] = []
    for path in reference_paths:
        image = face_recognition.load_image_file(path)
        found = face_recognition.face_encodings(image)
        if not found:
            print(f"Warning: No face detected in {path!r} — skipping")
        else:
            encodings.append(found[0])
    return encodings


def is_match(
    image_path: str,
    known_encodings: List[np.ndarray],
    tolerance: float = 0.5,
) -> bool:
    """Return True if any face in the image matches any of the known encodings.

    Loads the image, detects all faces, and compares each against known_encodings.
    Returns False if the image contains no faces.
    """
    image = face_recognition.load_image_file(image_path)
    candidates = face_recognition.face_encodings(image)
    for candidate in candidates:
        results = face_recognition.compare_faces(known_encodings, candidate, tolerance=tolerance)
        if any(results):
            return True
    return False
```

- [ ] **Step 4: Run tests — verify they pass**

```bash
pytest tests/test_recognizer.py -v
```

Expected: all 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add recognizer.py tests/test_recognizer.py
git commit -m "feat: face encoding and matching"
```

---

### Task 5: `output.py` — Console Output and File Copying

**Files:**
- Create: `output.py`

- [ ] **Step 1: Create `output.py`**

```python
import shutil
from pathlib import Path
from typing import List

from rich.console import Console
from rich.table import Table


console = Console()


def print_matches(matches: List[Path], total: int) -> None:
    """Print a rich table of matched images, then a summary line."""
    if not matches:
        console.print(f"\n[yellow]No matches found[/yellow] in {total} image(s) scanned.")
        return

    table = Table(title=f"Matched Images ({len(matches)} of {total})")
    table.add_column("Filename", style="cyan")
    table.add_column("Cache Path", style="dim")

    for path in matches:
        table.add_row(path.name, str(path))

    console.print(table)
    console.print(f"\n[green]✓[/green] {len(matches)} of {total} image(s) matched.")


def copy_matches(matches: List[Path], output_dir: Path) -> None:
    """Copy matched image files into output_dir, creating it if needed."""
    output_dir.mkdir(parents=True, exist_ok=True)
    for path in matches:
        shutil.copy2(path, output_dir / path.name)
    console.print(
        f"[green]✓[/green] Copied {len(matches)} file(s) to [bold]{output_dir}[/bold]"
    )
```

- [ ] **Step 2: Verify no import errors**

```bash
python -c "from output import print_matches, copy_matches; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add output.py
git commit -m "feat: console output and file copying"
```

---

### Task 6: `main.py` — CLI Entry Point

**Files:**
- Create: `main.py`

- [ ] **Step 1: Create `main.py`**

```python
import shutil
import sys
from pathlib import Path
from typing import List

import click
from rich.console import Console
from rich.progress import track

from cache import get_cache_dir, list_cached_images
from drive import download_images, extract_folder_id
from output import copy_matches, print_matches
from recognizer import encode_references, is_match


console = Console()


@click.command()
@click.option("--url", required=True, help="Public Google Drive folder URL")
@click.option(
    "--reference",
    "references",
    required=True,
    multiple=True,
    help="Local path to a reference photo (repeat for multiple)",
)
@click.option(
    "--output",
    "output_dir",
    default="./results",
    show_default=True,
    help="Folder to copy matched images into",
)
@click.option(
    "--tolerance",
    default=0.5,
    show_default=True,
    type=click.FloatRange(0.1, 1.0),
    help="Face match threshold — lower is stricter",
)
@click.option(
    "--no-cache",
    is_flag=True,
    default=False,
    help="Force re-download even if folder is already cached",
)
def main(
    url: str,
    references: List[str],
    output_dir: str,
    tolerance: float,
    no_cache: bool,
) -> None:
    """Find photos containing a specific person in a public Google Drive folder."""

    # 1. Parse the Drive URL
    try:
        folder_id = extract_folder_id(url)
    except ValueError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        sys.exit(1)

    # 2. Resolve cache directory
    cache_dir = get_cache_dir(folder_id)

    # 3. Optionally clear cache
    if no_cache and cache_dir.exists():
        shutil.rmtree(cache_dir)
        console.print(f"[dim]Cache cleared for folder {folder_id}[/dim]")

    # 4. STAGE 1: Download
    console.print(f"\n[bold]Stage 1:[/bold] Syncing images → {cache_dir}")
    try:
        download_images(url, cache_dir)
    except Exception as exc:
        console.print(f"[red]Download failed:[/red] {exc}")
        console.print(
            "Make sure the folder sharing is set to 'Anyone with the link can view'."
        )
        sys.exit(1)

    cached_images = list_cached_images(cache_dir)
    if not cached_images:
        console.print("[yellow]No images (.jpg/.jpeg/.png) found in the Drive folder.[/yellow]")
        sys.exit(0)

    console.print(f"[green]✓[/green] {len(cached_images)} image(s) ready.\n")

    # 5. Encode reference photos
    console.print(f"[bold]Stage 2:[/bold] Encoding {len(references)} reference photo(s)...")
    known_encodings = encode_references(list(references))
    if not known_encodings:
        console.print(
            "[red]Error:[/red] No faces detected in any reference photo. "
            "Try a clearer, well-lit photo."
        )
        sys.exit(1)
    console.print(f"[green]✓[/green] {len(known_encodings)} face encoding(s) loaded.\n")

    # 6. STAGE 2: Search
    console.print("[bold]Stage 3:[/bold] Searching for matches...")
    matches: List[Path] = []
    for image_path in track(cached_images, description="Scanning..."):
        try:
            if is_match(str(image_path), known_encodings, tolerance=tolerance):
                matches.append(image_path)
        except Exception as exc:
            console.print(f"[yellow]Skipping {image_path.name}:[/yellow] {exc}")

    # 7. Output results
    print_matches(matches, total=len(cached_images))
    if matches:
        copy_matches(matches, Path(output_dir))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify CLI help text renders**

```bash
python main.py --help
```

Expected output (abbreviated):
```
Usage: main.py [OPTIONS]

  Find photos containing a specific person in a public Google Drive folder.

Options:
  --url TEXT              Public Google Drive folder URL  [required]
  --reference TEXT        Local path to a reference photo ...  [required]
  --output TEXT           Folder to copy matched images into  [default: ./results]
  --tolerance FLOAT RANGE Face match threshold ...  [default: 0.5]
  --no-cache              Force re-download even if folder is already cached
  --help                  Show this message and exit.
```

- [ ] **Step 3: Run full test suite**

```bash
pytest tests/ -v
```

Expected: all 12 tests PASS, 0 failures.

- [ ] **Step 4: Commit**

```bash
git add main.py
git commit -m "feat: CLI entry point and full orchestration"
```

---

### Task 7: `README.md`

**Files:**
- Create: `README.md`

- [ ] **Step 1: Create `README.md`**

````markdown
# facefind

Find photos containing a specific person in a public Google Drive folder.

## Requirements

- Python 3.9+
- `dlib` (required by `face_recognition`)

## Setup

```bash
pip install -r requirements.txt
```

> If `dlib` install fails via pip, use conda first:
> ```bash
> conda install -c conda-forge dlib
> pip install face_recognition
> ```

## Usage

```bash
python main.py \
  --url "https://drive.google.com/drive/folders/YOUR_FOLDER_ID" \
  --reference me1.jpg me2.jpg \
  --output ./results
```

The Drive folder must be set to **"Anyone with the link can view"**.

## Options

| Flag | Default | Description |
|---|---|---|
| `--url` | required | Public Google Drive folder URL |
| `--reference` | required | Reference photo(s) of the target person — repeat for multiple |
| `--output` | `./results` | Folder to copy matched images into |
| `--tolerance` | `0.5` | Match threshold: lower = stricter (range 0.1–1.0) |
| `--no-cache` | off | Force re-download even if the folder is already cached |

## Tips

- Use 3–5 reference photos in varied lighting and angles for best accuracy
- False positives? Lower `--tolerance` (try `0.4`)
- Missing matches? Raise `--tolerance` (try `0.6`)
- Re-runs on the same folder skip download — images are cached in `.cache/`

## How it works

1. **Sync** — downloads all images from the Drive folder into `.cache/<folder_id>/`
2. **Encode** — encodes reference photos into 128-dim face vectors
3. **Search** — compares every cached image against the reference vectors
4. **Output** — prints a match table and copies matched images to `--output`
````

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: README with setup, usage, and tips"
```

---

### Task 8: Smoke Test (Manual)

No code changes — manual end-to-end validation.

- [ ] **Step 1: Prepare test assets**

  1. Create a small public Google Drive folder containing 5–10 images: some with you in them, some without.
  2. Set folder sharing to **"Anyone with the link can view"**.
  3. Save 2–3 reference photos of yourself locally as `ref1.jpg`, `ref2.jpg`, etc.

- [ ] **Step 2: First run (downloads + searches)**

```bash
python main.py \
  --url "https://drive.google.com/drive/folders/YOUR_TEST_FOLDER_ID" \
  --reference ref1.jpg ref2.jpg \
  --output ./smoke-results
```

Verify:
- Progress bar appears during download
- Match table shows correct filenames
- `./smoke-results/` contains only images with you in them
- Summary line shows correct count (e.g. "3 of 8 images matched")

- [ ] **Step 3: Second run (cache hit)**

Run the same command again. The download stage should complete instantly — no re-downloading.

- [ ] **Step 4: Test `--no-cache` flag**

```bash
python main.py \
  --url "https://drive.google.com/drive/folders/YOUR_TEST_FOLDER_ID" \
  --reference ref1.jpg \
  --no-cache \
  --output ./smoke-results-2
```

Expected: images are re-downloaded from scratch.

- [ ] **Step 5: Test invalid URL error**

```bash
python main.py \
  --url "https://docs.google.com/spreadsheets/d/abc123" \
  --reference ref1.jpg
```

Expected: `Error: Invalid Google Drive folder URL: ...` and clean exit.
