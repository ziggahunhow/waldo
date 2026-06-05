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
| `--tolerance` | `0.25` | Match threshold: lower = stricter (range 0.1–1.0) |
| `--no-cache` | off | Force re-download even if the folder is already cached |

## Tips

- Use 3–5 reference photos in varied lighting and angles for best accuracy
- False positives? Lower `--tolerance` (try `0.2`)
- Missing matches? Raise `--tolerance` (try `0.3`)
- Re-runs on the same folder skip download — images are cached in `.cache/`

## Limitations

- **Small or distant faces are skipped.** The face detector (HOG) requires a face to be reasonably large in the frame — roughly 80×80 pixels or larger after the image is scaled to 1800px on its longest side. Photos where the subject is far from the camera or faces are very small will not be detected and will be silently excluded from results.
- **Folders larger than 50 files require a Google API key.** Set `GOOGLE_API_KEY` in a `.env` file at the project root. Without it, only the first 50 files are downloaded (gdown limitation).

## How it works

1. **Sync** — downloads all images from the Drive folder into `.cache/<folder_id>/`
2. **Encode** — encodes reference photos into 128-dim face vectors
3. **Search** — compares every cached image against the reference vectors
4. **Output** — prints a match table and copies matched images to `--output`
