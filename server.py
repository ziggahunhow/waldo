import io
import json
import os
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, Response, abort, request, send_file, stream_with_context

load_dotenv()
sys.path.insert(0, str(Path(__file__).parent))

from cache import get_cache_dir, list_cached_images
from drive import download_images, extract_folder_id
from recognizer import encode_references, is_match

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
                    return

                cache_dir = get_cache_dir(folder_id)
                cached = list_cached_images(cache_dir)

                if cached:
                    yield sse({"type": "stage", "msg": f"Using {len(cached)} cached images ({label})…"})
                else:
                    yield sse({"type": "stage", "msg": f"Downloading {label}…"})
                    try:
                        download_images(url, cache_dir)
                    except Exception as e:
                        yield sse({"type": "error", "msg": f"Download failed ({label}): {e}"})
                        return
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


@app.route("/api/image/<folder_id>/<filename>")
def serve_image(folder_id, filename):
    """Serve a cached image, converting HEIC → JPEG for browser compatibility."""
    img_path = get_cache_dir(folder_id) / filename
    if not img_path.exists():
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


@app.route("/line/webhook", methods=["POST"])
def line_webhook():
    from line_bot import handler as _line_handler
    from linebot.v3.exceptions import InvalidSignatureError

    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        _line_handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400, "Invalid LINE signature")
    except Exception as e:
        abort(500, str(e))
    return "OK"


if __name__ == "__main__":
    app.run(debug=True, port=5001)
