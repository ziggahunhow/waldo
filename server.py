import io
import json
import logging
import os
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from flask import Flask, Response, abort, request, send_file, stream_with_context

load_dotenv()
sys.path.insert(0, str(Path(__file__).parent))

from cache import get_cache_dir, list_cached_images
from drive import download_images, extract_folder_id
from recognizer import encode_references, is_match

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
_log_handler = TimedRotatingFileHandler(LOG_DIR / "server.log", when="midnight", backupCount=14)
_log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
logging.getLogger().addHandler(_log_handler)
logging.getLogger().setLevel(logging.INFO)

ALBUMS_DIR = Path(__file__).parent / "albums"
ALBUMS_DIR.mkdir(exist_ok=True)

app = Flask(__name__)


def sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


@app.route("/")
def index():
    return send_file("index.html")


@app.route("/api/search", methods=["POST"])
def search():
    urls = [u.strip() for u in request.form.getlist("url") if u.strip()]
    tolerance = float(request.form.get("tolerance", 0.25))
    detector = request.form.get("detector", "mediapipe")
    if detector not in ("hog", "mediapipe", "cnn", "insightface"):
        detector = "mediapipe"
    output_dir = request.form.get("output", "results").strip() or "results"
    ref_files = request.files.getlist("references")

    # Save uploads before the generator runs (request context won't survive the stream)
    tmpdir = tempfile.mkdtemp()
    ref_paths = []
    for f in ref_files:
        dest = os.path.join(tmpdir, Path(f.filename).name)
        f.save(dest)
        ref_paths.append(dest)

    def generate():
        try:
            if not urls:
                yield sse({"type": "error", "msg": "At least one Drive URL is required"})
                return

            # Download each folder, collect (img_path, folder_id) pairs
            all_images = []
            for i, url in enumerate(urls):
                label = f"folder {i + 1}/{len(urls)}" if len(urls) > 1 else "folder"
                try:
                    folder_id = extract_folder_id(url)
                except ValueError as e:
                    yield sse({"type": "error", "msg": str(e)})
                    continue

                cache_dir = get_cache_dir(folder_id)
                cached = list_cached_images(cache_dir)

                if cached:
                    yield sse({"type": "stage", "msg": f"Using {len(cached)} cached images ({label})…"})
                else:
                    yield sse({"type": "stage", "msg": f"Downloading {label}…"})
                    try:
                        download_images(url, cache_dir)
                    except Exception as e:
                        app.logger.exception(f"Download failed ({label}): {url}")
                        yield sse({"type": "error", "msg": f"Download failed ({label}): {e}"})
                        continue
                    cached = list_cached_images(cache_dir)

                all_images.extend((img, folder_id) for img in cached)

            if not all_images:
                yield sse({"type": "error", "msg": "No images found in any Drive folder"})
                return

            yield sse({"type": "total", "count": len(all_images)})
            yield sse({"type": "stage", "msg": "Encoding reference photos…"})

            if not ref_paths:
                yield sse({"type": "error", "msg": "No reference photos uploaded"})
                return

            known = encode_references(ref_paths, detector=detector)
            if not known:
                yield sse({"type": "error", "msg": "No faces detected in any reference photo"})
                return

            yield sse({"type": "stage", "msg": f"Searching {len(all_images)} images…"})

            out_path = Path(output_dir)
            out_path.mkdir(parents=True, exist_ok=True)

            for i, (img_path, folder_id) in enumerate(all_images):
                try:
                    if is_match(str(img_path), known, tolerance=tolerance, detector=detector):
                        shutil.copy2(img_path, out_path / img_path.name)
                        yield sse({"type": "match", "filename": img_path.name, "folder_id": folder_id})
                except Exception:
                    pass
                yield sse({"type": "progress", "current": i + 1, "total": len(all_images)})

            yield sse({"type": "done", "output_dir": str(out_path)})

        except Exception as e:
            app.logger.exception("Search failed")
            yield sse({"type": "error", "msg": str(e)})
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    return Response(
        stream_with_context(generate()),
        content_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


@app.route("/api/cache", methods=["GET"])
def cache_info():
    root = Path(".cache")
    if not root.exists():
        return {"folders": 0, "images": 0}
    folders = [p for p in root.iterdir() if p.is_dir()]
    images = sum(len(list_cached_images(f)) for f in folders)
    return {"folders": len(folders), "images": images}


@app.route("/api/cache", methods=["DELETE"])
def cache_clear():
    root = Path(".cache")
    if root.exists():
        shutil.rmtree(root)
    return {"ok": True}


@app.route("/api/download", methods=["POST"])
def download_zip():
    items = request.get_json(force=True) or []
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        seen = set()
        for item in items:
            img_path = get_cache_dir(item["folder_id"]) / item["filename"]
            if not img_path.exists():
                continue
            name = item["filename"]
            if name in seen:
                stem = img_path.stem
                ext = img_path.suffix
                name = f"{stem}_{item['folder_id'][:6]}{ext}"
            seen.add(name)
            zf.write(img_path, name)
    buf.seek(0)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return send_file(
        buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"facefind_matches_{stamp}.zip",
    )


@app.route("/api/preview", methods=["POST"])
def preview():
    """Convert an uploaded reference image (incl. HEIC) to a JPEG thumbnail
    so the browser can display it — browsers can't render HEIC natively."""
    f = request.files.get("file")
    if not f:
        return "No file", 400

    import pillow_heif
    from PIL import Image, ImageOps

    pillow_heif.register_heif_opener()
    try:
        img = ImageOps.exif_transpose(Image.open(f.stream)).convert("RGB")
    except Exception as e:
        return f"Cannot read image: {e}", 400

    img.thumbnail((400, 400))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=82)
    buf.seek(0)
    return send_file(buf, mimetype="image/jpeg")


def _safe_child(parent_dir: Path, name: str) -> Optional[Path]:
    """Resolve `name` as a direct child of parent_dir, rejecting any path
    traversal (e.g. "..", "../../server.py"). Returns None if unsafe."""
    resolved_parent = parent_dir.resolve()
    candidate = (resolved_parent / name).resolve()
    if candidate.parent != resolved_parent:
        return None
    return candidate


def _serve_image_file(img_path: Path):
    """Serve an image file, converting HEIC → JPEG for browser compatibility."""
    if img_path is None or not img_path.exists():
        return "Not found", 404

    if img_path.suffix.lower() in {".heic", ".heif"}:
        import pillow_heif
        from PIL import Image, ImageOps

        pillow_heif.register_heif_opener()
        img = ImageOps.exif_transpose(Image.open(str(img_path))).convert("RGB")
        img.thumbnail((800, 800))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=82)
        buf.seek(0)
        return send_file(buf, mimetype="image/jpeg")

    return send_file(str(img_path))


@app.route("/api/image/<folder_id>/<filename>")
def serve_image(folder_id, filename):
    return _serve_image_file(_safe_child(get_cache_dir(folder_id), filename))


@app.route("/album/<album_id>")
def album_page(album_id):
    album_dir = _safe_child(ALBUMS_DIR, album_id)
    if album_dir is None or not album_dir.is_dir():
        return "Album not found", 404

    filenames = sorted(p.name for p in album_dir.iterdir() if p.is_file())
    items = "\n".join(
        f'<div class="item" data-name="{name}">'
        f'<img src="/api/album/{album_id}/{name}" loading="lazy">'
        f'<button class="sel" title="Select photo">✓</button>'
        f'</div>'
        for name in filenames
    )
    return f"""<!doctype html>
<html><head><title>FaceFind album</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ background:#111; color:#eee; font-family:sans-serif; margin:0; padding:16px 16px 80px; }}
  h1 {{ font-size:16px; font-weight:normal; opacity:0.7; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(180px,1fr)); gap:8px; }}
  .item {{ position:relative; border-radius:6px; overflow:hidden; }}
  .item img {{ display:block; width:100%; height:180px; object-fit:cover; cursor:pointer; }}
  .item.selected img {{ outline:3px solid #4caf50; outline-offset:-3px; }}
  .item .sel {{
    position:absolute; top:6px; right:6px; width:22px; height:22px;
    background:rgba(0,0,0,0.4); border:1px solid rgba(255,255,255,0.35); border-radius:5px;
    color:transparent; font-size:0.7rem; font-weight:700;
    display:flex; align-items:center; justify-content:center; cursor:pointer; padding:0;
  }}
  .item.selected .sel {{ background:#4caf50; border-color:#4caf50; color:#111; }}
  .bar {{
    position:fixed; left:0; right:0; bottom:0; padding:12px 16px;
    background:#1c1c1c; border-top:1px solid #333;
    display:flex; align-items:center; gap:12px; flex-wrap:wrap;
  }}
  .bar button {{
    background:#2a2a2a; color:#eee; border:1px solid #444; border-radius:6px;
    padding:8px 14px; cursor:pointer; font-size:14px;
  }}
  .bar button:disabled {{ opacity:0.4; cursor:default; }}
  .bar .count {{ opacity:0.7; font-size:13px; margin-right:auto; }}
</style></head>
<body>
  <h1>{len(filenames)} matched photo(s)</h1>
  <div class="grid" id="grid">{items}</div>

  <div class="bar">
    <span class="count" id="count">0 selected</span>
    <button id="sel-all">select all</button>
    <button id="sel-none">select none</button>
    <button id="dl" disabled>↓ download zip</button>
  </div>

  <script>
    const albumId = {album_id!r};
    const grid = document.getElementById('grid');
    const countEl = document.getElementById('count');
    const dlBtn = document.getElementById('dl');
    const selected = new Set();

    // Prefer the native share sheet on phones (Save to Photos/Files) over a
    // zip, which most mobile browsers can't usefully "open" on download.
    const canShareFiles = !!(navigator.canShare && navigator.share);
    dlBtn.textContent = canShareFiles ? '📤 share photos' : '↓ download zip';

    function setSelected(item, on) {{
      const name = item.dataset.name;
      item.classList.toggle('selected', on);
      if (on) selected.add(name); else selected.delete(name);
      countEl.textContent = selected.size === 0 ? '0 selected' : `${{selected.size}} selected`;
      dlBtn.disabled = selected.size === 0;
    }}

    grid.querySelectorAll('.item').forEach(item => {{
      item.querySelector('.sel').addEventListener('click', e => {{
        e.stopPropagation();
        setSelected(item, !item.classList.contains('selected'));
      }});
      item.querySelector('img').addEventListener('click', () => {{
        setSelected(item, !item.classList.contains('selected'));
      }});
    }});

    document.getElementById('sel-all').addEventListener('click', () => {{
      grid.querySelectorAll('.item').forEach(item => setSelected(item, true));
    }});
    document.getElementById('sel-none').addEventListener('click', () => {{
      grid.querySelectorAll('.item').forEach(item => setSelected(item, false));
    }});

    async function shareSelected() {{
      const files = await Promise.all([...selected].map(async name => {{
        const res = await fetch(`/api/album/${{albumId}}/${{encodeURIComponent(name)}}`);
        const blob = await res.blob();
        return new File([blob], name, {{ type: blob.type }});
      }}));
      if (!navigator.canShare({{ files }})) throw new Error('sharing not supported for these files');
      await navigator.share({{ files }});
    }}

    async function downloadZip() {{
      const res = await fetch(`/api/album/${{albumId}}/download`, {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ filenames: [...selected] }}),
      }});
      if (!res.ok) throw new Error('server error');
      const blob = await res.blob();
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'facefind_album.zip';
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 10000);
    }}

    dlBtn.addEventListener('click', async () => {{
      dlBtn.disabled = true;
      const original = dlBtn.textContent;
      dlBtn.textContent = canShareFiles ? 'preparing…' : 'zipping…';
      try {{
        if (canShareFiles) {{
          await shareSelected();
        }} else {{
          await downloadZip();
        }}
      }} catch (e) {{
        if (e.name !== 'AbortError') alert('Download failed: ' + e.message);
      }} finally {{
        dlBtn.disabled = selected.size === 0;
        dlBtn.textContent = original;
      }}
    }});
  </script>
</body></html>"""


@app.route("/api/album/<album_id>/<filename>")
def serve_album_image(album_id, filename):
    album_dir = _safe_child(ALBUMS_DIR, album_id)
    if album_dir is None:
        return "Not found", 404
    return _serve_image_file(_safe_child(album_dir, filename))


@app.route("/api/album/<album_id>/download", methods=["POST"])
def download_album_zip(album_id):
    album_dir = _safe_child(ALBUMS_DIR, album_id)
    if album_dir is None or not album_dir.is_dir():
        return "Album not found", 404

    filenames = (request.get_json(force=True) or {}).get("filenames", [])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for name in filenames:
            img_path = _safe_child(album_dir, name)
            if img_path is not None and img_path.exists():
                zf.write(img_path, img_path.name)
    buf.seek(0)
    return send_file(
        buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"facefind_album_{album_id}.zip",
    )


@app.route("/line/webhook", methods=["POST"])
def line_webhook():
    from line_bot import handler as _line_handler
    from linebot.v3.exceptions import InvalidSignatureError

    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    app.logger.info("LINE webhook hit: %d bytes body", len(body))
    try:
        _line_handler.handle(body, signature)
    except InvalidSignatureError:
        app.logger.warning("LINE webhook: invalid signature")
        abort(400, "Invalid LINE signature")
    except Exception as e:
        app.logger.exception("LINE webhook handler failed")
        abort(500, str(e))
    return "OK"


if __name__ == "__main__":
    app.run(debug=True, port=5565)
