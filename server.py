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
        f'<div class="result-item" data-name="{name}">'
        f'<img src="/api/album/{album_id}/{name}" alt="{name}" loading="lazy">'
        f'<button class="result-select" title="Select photo">✓</button>'
        f'</div>'
        for name in filenames
    )
    return f"""<!doctype html>
<html><head><title>FaceFind album</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg:           #090805;
    --surface:      #111009;
    --surface-3:    #222015;
    --amber:        #C4862C;
    --amber-border: rgba(196, 134, 44, 0.25);
    --text:         #EDE6D6;
    --text-dim:     #7E7060;
    --text-muted:   #42392C;
    --border:       #211F15;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: var(--bg); color: var(--text);
    font-family: 'JetBrains Mono', monospace;
    padding: 1.5rem 1.5rem 6rem;
  }}
  h1 {{
    font-size: 0.65rem; font-weight: 400; letter-spacing: 0.12em;
    color: var(--text-dim); text-transform: uppercase; margin-bottom: 1rem;
  }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 3px; }}

  .result-item {{ position: relative; aspect-ratio: 1; overflow: hidden; background: var(--surface); cursor: pointer; }}
  .result-item img {{
    width: 100%; height: 100%; object-fit: cover; display: block;
    transition: transform 0.4s ease, filter 0.4s ease;
    filter: brightness(0.9) contrast(1.05) saturate(0.9);
  }}
  .result-item:hover img {{ transform: scale(1.04); filter: brightness(1) contrast(1.02) saturate(1); }}
  .result-item.selected img {{ filter: brightness(0.55) saturate(0.7); }}
  .result-item.selected {{ outline: 2px solid var(--amber); outline-offset: -2px; }}

  .result-select {{
    position: absolute; top: 6px; right: 6px; width: 22px; height: 22px;
    background: rgba(0,0,0,0.55); border: 1px solid rgba(255,255,255,0.45);
    color: transparent; font-size: 0.62rem; font-weight: 600;
    display: flex; align-items: center; justify-content: center;
    cursor: pointer; z-index: 3;
    transition: background 0.15s, color 0.15s, border-color 0.15s;
  }}
  .result-select:hover {{ border-color: var(--amber); color: var(--amber-border); }}
  .result-item.selected .result-select {{ background: var(--amber); border-color: var(--amber); color: var(--bg); }}

  /* ── Lightbox ── */
  .lightbox {{
    position: fixed; inset: 0; background: rgba(0,0,0,0.92); z-index: 500;
    display: flex; align-items: center; justify-content: center; flex-direction: column; gap: 1rem;
    opacity: 0; pointer-events: none; transition: opacity 0.2s;
  }}
  .lightbox.visible {{ opacity: 1; pointer-events: all; }}
  .lightbox img {{ max-width: min(90vw, 1200px); max-height: 82vh; object-fit: contain; display: block; box-shadow: 0 16px 64px rgba(0,0,0,0.8); }}
  .lightbox-name {{ font-size: 0.65rem; color: var(--text-muted); letter-spacing: 0.1em; }}
  .lightbox-close {{
    position: absolute; top: 1.5rem; right: 1.5rem; background: none; border: 1px solid var(--border);
    color: var(--text-muted); font-family: 'JetBrains Mono', monospace; font-size: 0.65rem;
    padding: 0.35rem 0.65rem; cursor: pointer; letter-spacing: 0.1em; transition: color 0.15s, border-color 0.15s;
  }}
  .lightbox-close:hover {{ color: var(--text); border-color: var(--text-dim); }}

  /* ── Selection bar ── */
  .selection-bar {{
    position: fixed; bottom: 2rem; left: 50%;
    transform: translateX(-50%) translateY(calc(100% + 2rem));
    transition: transform 0.35s cubic-bezier(0.4, 0, 0.2, 1);
    background: var(--surface-3); border: 1px solid var(--amber-border);
    padding: 0.7rem 0.9rem; display: flex; align-items: center; gap: 0.75rem;
    z-index: 200; white-space: nowrap; box-shadow: 0 8px 32px rgba(0,0,0,0.6);
  }}
  .selection-bar.visible {{ transform: translateX(-50%) translateY(0); }}
  .sel-count {{ font-size: 0.65rem; color: var(--amber); letter-spacing: 0.1em; min-width: 6rem; }}
  .sel-divider {{ width: 1px; height: 1rem; background: var(--border); }}
  .btn-sel {{
    background: none; border: none; font-family: 'JetBrains Mono', monospace;
    font-size: 0.62rem; font-weight: 300; letter-spacing: 0.1em; cursor: pointer;
    padding: 0.2rem 0.1rem; color: var(--text-muted); transition: color 0.15s;
  }}
  .btn-sel:hover {{ color: var(--text); }}
  .btn-download-sel {{
    background: var(--amber); border: none; color: var(--bg);
    font-family: 'JetBrains Mono', monospace; font-size: 0.62rem; font-weight: 400;
    letter-spacing: 0.12em; padding: 0.45rem 0.9rem; cursor: pointer; transition: opacity 0.15s;
  }}
  .btn-download-sel:hover {{ opacity: 0.85; }}
  .btn-download-sel:disabled {{ opacity: 0.4; cursor: default; }}
</style></head>
<body>
  <h1>{len(filenames)} matched photo(s)</h1>
  <div class="grid" id="grid">{items}</div>

  <div class="lightbox" id="lightbox">
    <button class="lightbox-close" id="lightbox-close">✕ close</button>
    <img id="lightbox-img" src="" alt="">
    <span class="lightbox-name" id="lightbox-name"></span>
  </div>

  <div class="selection-bar" id="selection-bar">
    <span class="sel-count" id="sel-count">0 selected</span>
    <div class="sel-divider"></div>
    <button class="btn-sel" id="btn-sel-all">select all</button>
    <button class="btn-sel" id="btn-sel-none">deselect</button>
    <div class="sel-divider"></div>
    <button class="btn-download-sel" id="btn-download-sel" disabled>↓ download zip</button>
  </div>

  <script>
    const albumId = {album_id!r};
    const grid = document.getElementById('grid');
    const selectionBar = document.getElementById('selection-bar');
    const selCount = document.getElementById('sel-count');
    const dlBtn = document.getElementById('btn-download-sel');
    const selected = new Set();

    // Prefer the native share sheet on phones (Save to Photos/Files) over a
    // zip, which most mobile browsers can't usefully "open" on download.
    const canShareFiles = !!(navigator.canShare && navigator.share);
    const dlLabel = canShareFiles ? '📤 share photos' : '↓ download zip';
    dlBtn.textContent = dlLabel;

    function setSelected(item, on) {{
      const name = item.dataset.name;
      item.classList.toggle('selected', on);
      if (on) selected.add(name); else selected.delete(name);
      selCount.textContent = selected.size === 0 ? '0 selected' : `${{selected.size}} selected`;
      selectionBar.classList.toggle('visible', selected.size > 0);
      dlBtn.disabled = selected.size === 0;
    }}

    grid.querySelectorAll('.result-item').forEach(item => {{
      item.querySelector('.result-select').addEventListener('click', e => {{
        e.stopPropagation();
        setSelected(item, !item.classList.contains('selected'));
      }});
      item.querySelector('img').addEventListener('click', () => {{
        openLightbox(item.querySelector('img').src, item.dataset.name);
      }});
    }});

    document.getElementById('btn-sel-all').addEventListener('click', () => {{
      grid.querySelectorAll('.result-item').forEach(item => setSelected(item, true));
    }});
    document.getElementById('btn-sel-none').addEventListener('click', () => {{
      grid.querySelectorAll('.result-item').forEach(item => setSelected(item, false));
    }});

    // ── Lightbox ──
    const lightbox = document.getElementById('lightbox');
    const lightboxImg = document.getElementById('lightbox-img');
    const lightboxName = document.getElementById('lightbox-name');

    function openLightbox(src, name) {{
      lightboxImg.src = src;
      lightboxName.textContent = name;
      lightbox.classList.add('visible');
    }}
    function closeLightbox() {{
      lightbox.classList.remove('visible');
      lightboxImg.src = '';
    }}
    document.getElementById('lightbox-close').addEventListener('click', closeLightbox);
    lightbox.addEventListener('click', e => {{ if (e.target === lightbox) closeLightbox(); }});

    // ── Download / share ──
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
        dlBtn.textContent = dlLabel;
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
    # Auto-reloader on for dev convenience. It restarts on any .py change,
    # which drops in-memory state: an in-progress /setref collection is lost
    # and the initiator just re-runs it (refs_staging/ keeps this from ever
    # corrupting the live refs/ set). The Werkzeug debugger stays opt-in
    # (FLASK_DEBUG=1): it's an RCE risk once this server is reachable through a
    # public tunnel.
    debug = os.environ.get("FLASK_DEBUG", "").lower() in ("1", "true", "yes")
    app.run(port=5565, debug=debug, use_reloader=True)
