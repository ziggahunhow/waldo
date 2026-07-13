# Face-recognition LINE bot / web server.
# Heavy native deps: dlib (built from source), mediapipe, insightface, opencv.
FROM python:3.13-slim-bookworm

# build-essential + cmake: compile dlib.
# libgl1 + libglib2.0-0: runtime libs for opencv / mediapipe.
RUN apt-get update -o Acquire::Retries=3 \
    && apt-get install -y --no-install-recommends -o Acquire::Retries=3 \
        build-essential \
        cmake \
        curl \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first so the (slow) dlib build is cached across code changes.
# setuptools is not bundled in the slim image, but face_recognition_models imports
# pkg_resources (setuptools) to locate its model files — without it face_recognition
# reports the models as missing. Pin <81 since pkg_resources is slated for removal.
COPY requirements.txt .
RUN pip install --no-cache-dir "setuptools<81" -r requirements.txt

# App code and data dirs (.cache, albums, refs, logs, approved_groups.json) are
# bind-mounted at runtime via docker-compose, so nothing else is copied in.
EXPOSE 5565

# Production run: no Werkzeug reloader (it drops in-memory state and needs a tty),
# threaded so a long search doesn't block webhook delivery. Bind loopback only —
# the container shares the host network namespace and cloudflared reaches it via
# 127.0.0.1:5565, so there's no need to expose it on any external interface.
CMD ["python", "-u", "-c", \
     "from server import app; app.run(host='127.0.0.1', port=5565, use_reloader=False, threaded=True)"]
