# facefind CLI — Design Spec
**Date:** 2026-05-21  
**Status:** Approved  

---

## Overview

`facefind` is a Python CLI tool that takes a public Google Drive folder URL and a set of local reference photos, then finds all images in that folder containing a specific person. Matches are printed to the console and copied to a local output folder.

This is the CLI MVP. The long-term goal is a web app; the architecture is chosen to make that transition straightforward.

---

## Goals

- Accept a public Google Drive folder URL and download its images to a local cache
- Accept 1–5 local reference photos of a target person
- Run face recognition against cached images and identify matches
- Output: console list of matched filenames (+ local cache path), and a local copy of matched images
- Fast re-runs via cache (skip already-downloaded images)

## Non-Goals (MVP)

- Private Drive folders (no OAuth / service account)
- Recursive subfolder scanning
- Web UI
- Batch processing of multiple people simultaneously
- GPU acceleration

---

## Architecture

### Command

```bash
python main.py \
  --url "https://drive.google.com/drive/folders/FOLDER_ID" \
  --reference me1.jpg me2.jpg me3.jpg \
  --output ./results \
  [--tolerance 0.5]
```

### File Layout

```
febe/
├── main.py           # CLI entry point (click)
├── drive.py          # Drive URL parsing + image downloading (gdown)
├── cache.py          # Local cache read/write/skip logic
├── recognizer.py     # Face encoding + comparison (face_recognition)
├── output.py         # Console printing (rich) + file copying (shutil)
├── requirements.txt
└── README.md
```

### Key Libraries

| Library | Purpose |
|---|---|
| `click` | CLI argument parsing |
| `gdown` | Downloading public Google Drive folders |
| `face_recognition` | Face encoding and comparison (dlib-based) |
| `rich` | Progress bars and formatted console output |
| `Pillow` | Image file handling |

---

## Data Flow

```
1. Parse CLI args
       │
2. Extract folder ID from Drive URL
   e.g. .../folders/1aBcDeFg... → "1aBcDeFg..."
       │
3. SYNC — download images → .cache/<folder_id>/
   • Skip files already present (by filename)
   • Supported formats: .jpg, .jpeg, .png, .heic
   • Show download progress bar (rich)
       │
4. Encode reference photos → list of face vectors
   • Warn per photo if no face detected
   • Exit early if no reference photo yields a face
       │
5. SEARCH — for each cached image:
   • Detect all faces in the image
   • Compare each face to reference vectors (at given tolerance)
   • If any face matches → mark image as a hit
       │
6. OUTPUT
   • Print matched filenames + local cache paths to console
   • Copy matched images to --output folder (created if missing)
   • Print summary: "X of Y images matched"
   Note: Drive URLs per file are not available in MVP (gdown doesn't return file IDs).
   Linking back to Drive is a post-MVP enhancement requiring the Drive API.
```

---

## Module Responsibilities

### `drive.py`
- Parse folder ID from various Drive URL formats
- Use `gdown` to download folder contents to the cache directory
- Return list of downloaded file paths

### `cache.py`
- Resolve the cache path for a given folder ID: `.cache/<folder_id>/`
- Check which files are already downloaded (skip list)
- No expiry logic in MVP

### `recognizer.py`
- Load and encode reference photos into face vectors (once, at startup)
- Expose `is_match(image_path: str, tolerance: float) -> bool`
- Internally: detect all faces in the image, compare each to reference vectors

### `output.py`
- Print match list to console using `rich` (filename, local cache path)
- Copy matched files to output folder via `shutil`
- Print final summary line

### `main.py`
- Wire up all modules via `click` CLI
- Orchestrate: sync → encode references → search → output

---

## Error Handling

| Situation | Behaviour |
|---|---|
| Invalid / non-folder Drive URL | Print clear error message, exit |
| Drive folder is private / inaccessible | Print "make sure folder is set to 'Anyone with the link'", exit |
| Reference photo has no detectable face | Warn per photo; exit if none yield a face |
| Cached image can't be opened | Skip with warning, continue |
| Image has no faces | Skip silently (not a match) |
| Output folder doesn't exist | Create automatically |
| Re-run with same Drive URL | Cache hit — skip download, run search directly |
| `--tolerance` out of range | Clamp to 0.1–1.0 with a warning |

No retry logic on download failures in MVP — `gdown` handles transient errors internally.

---

## CLI Flags

| Flag | Default | Description |
|---|---|---|
| `--url` | required | Public Google Drive folder URL |
| `--reference` | required | 1 or more local reference image paths |
| `--output` | `./results` | Local folder to copy matched images into |
| `--tolerance` | `0.5` | Face match threshold (lower = stricter) |
| `--no-cache` | off | Force re-download even if cache exists |

---

## Testing

### Manual Smoke Test
- Prepare a small public Drive folder with known test images (some containing the target person, some not)
- Run the CLI with reference photos of the target person
- Verify console output and `./results` folder contain correct matches

### Unit Tests (pytest)

**`test_drive.py`**
- `extract_folder_id()` correctly parses various Drive URL formats:
  - `.../folders/ID`
  - `.../folders/ID?usp=sharing`
  - `.../folders/ID/view`

**`test_recognizer.py`**
- Given two encodings of the same face, `is_match()` returns `True` at tolerance 0.5
- Given two encodings of different faces, `is_match()` returns `False` at tolerance 0.5
- Tolerance boundary: strict tolerance (0.1) rejects borderline matches

No unit tests for `output.py` or `cache.py` in MVP (thin wrappers around stdlib).

---

## Future (Web App Phase)

- Cache layer becomes persistent server-side storage (S3 or local volume)
- Stage 1 (sync) and Stage 2 (search) become async background jobs
- OAuth added for private Drive folders
- Recursive subfolder support
- Web UI: paste Drive URL, upload reference photos, view matched photos in browser
