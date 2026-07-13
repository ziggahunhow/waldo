# facefind

Find photos containing a specific person in a public Google Drive folder.
Three ways to use it: a CLI tool, a web UI, and a LINE bot.

## Requirements

- Python 3.9+
- `dlib` (required by `face_recognition`)

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # then fill in whichever variables the interface you're using needs
```

> If `dlib` install fails via pip, use conda first:
> ```bash
> conda install -c conda-forge dlib
> pip install face_recognition
> ```

All three interfaces share the same underlying search: download a public
Drive folder's images, encode reference photos into face vectors, and match
every downloaded image against them.

## CLI

```bash
python main.py \
  --url "https://drive.google.com/drive/folders/YOUR_FOLDER_ID" \
  --reference me1.jpg me2.jpg \
  --output ./results
```

The Drive folder must be set to **"Anyone with the link can view"**.

### Options

| Flag | Default | Description |
|---|---|---|
| `--url` | required | Public Google Drive folder URL |
| `--reference` | required | Reference photo(s) of the target person — repeat for multiple |
| `--output` | `./results` | Folder to copy matched images into |
| `--tolerance` | `0.25` | Match threshold: lower = stricter (range 0.1–1.0) |
| `--detector` | `mediapipe` | Face detection model — `mediapipe`, `hog`, `cnn`, or `insightface` |
| `--no-cache` | off | Force re-download even if the folder is already cached |

### Tips

- Use 3–5 reference photos in varied lighting and angles for best accuracy
- False positives? Lower `--tolerance` (try `0.2`)
- Missing matches? Raise `--tolerance` (try `0.3`)
- Re-runs on the same folder skip download — images are cached in `.cache/`
- `insightface` (SCRFD detection + ArcFace embeddings) is generally the most
  accurate option, especially on angled faces, at the cost of a slower first
  run (downloads its model, ~300MB)

## Web UI

```bash
python server.py
```

Browse to `http://localhost:5565`. Paste one or more Drive folder links (or
paste any text containing them — links get extracted automatically), drag
in reference photos, and search. Results support multi-select and zip
download; HEIC/HEIF reference photos and matches are previewed as JPEG
automatically.

## LINE bot

A group-chat bot exposed via `server.py`'s `/line/webhook` route — run the
same `python server.py` process, then point a LINE Messaging API channel's
webhook at `https://<your-public-url>/line/webhook`.

**Setup:** fill in `LINE_CHANNEL_SECRET` and either `LINE_CHANNEL_ACCESS_TOKEN`
or `LINE_CHANNEL_ID` in `.env` (see `.env.example` for details on each var,
including `ADMIN_LINE_USER_ID` and `PUBLIC_URL`).

**Access control:** the bot only responds in group/room chats, not 1:1 DMs, and
each group must be approved before it can do anything. When added to a group it
stays but stays inert until the admin (`ADMIN_LINE_USER_ID`) sends `/approve` in
that chat; `/revoke` disables it again. Approvals persist in `approved_groups.json`.
The admin is identified by the message sender's user id, so this works on an
unverified LINE account (no member-list API needed). See `line_bot.py`'s module
docstring for details.

**Usage:** in an approved group, send any message containing a Google Drive
folder link and the bot searches it against the shared reference photos, replying
with matches (or a link to a web album if there are more than 10). `/help` lists
commands; `/stop` cancels an in-progress search.

**Reference photos** are set in-chat, not hardcoded. Send `/setref`, then send
3–5 portrait photos of the target person (only the initiator's photos count),
then `/done` to commit them. The set is global — one shared set across every
group the bot is in — and is stored under `refs/` (staged in `refs_staging/`
until `/done`, so an abandoned collection never breaks the live set). Until a
set exists, searches reply asking you to run `/setref` first. See
`docs/superpowers/specs/2026-07-10-line-bot-dynamic-refs-design.md` for the
full design.

## Limitations

- **Small or distant faces are skipped.** Face detectors need a face to be
  reasonably large in the frame — roughly 80×80 pixels or larger after the
  image is scaled to 1800px on its longest side. Photos where the subject
  is far from the camera or faces are very small will not be detected and
  will be silently excluded from results.
- **Folders larger than 50 files require a Google API key.** Set
  `GOOGLE_API_KEY` in `.env`. Without it, only the first 50 files are
  downloaded (gdown limitation).

## How it works

1. **Sync** — downloads all images from the Drive folder into `.cache/<folder_id>/`
2. **Encode** — encodes reference photos into face vectors (128-dim for
   `hog`/`mediapipe`/`cnn`, 512-dim ArcFace embeddings for `insightface`)
3. **Search** — compares every cached image against the reference vectors
4. **Output** — CLI: prints a match table and copies matches to `--output`. Web UI: renders a results grid. LINE bot: replies with matched images or an album link.
