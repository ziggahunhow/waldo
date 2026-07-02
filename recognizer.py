from pathlib import Path
from typing import List

import face_recognition
import numpy as np

_MAX_DIMENSION = 1800
DETECTORS = ("mediapipe", "hog", "cnn", "insightface")

# Detectors that produce their own embeddings (detect + align + embed fused),
# so they bypass the dlib face_recognition encoding path and use cosine matching.
_EMBEDDING_DETECTORS = ("insightface",)

# Lazy singletons — created once, reused for the lifetime of the process.
_mp_detector = None
_if_app = None


def _get_mp_detector():
    global _mp_detector
    if _mp_detector is None:
        import mediapipe as mp
        _mp_detector = mp.solutions.face_detection.FaceDetection(
            model_selection=1,
            min_detection_confidence=0.4,
        )
    return _mp_detector


def _get_if_app():
    global _if_app
    if _if_app is None:
        from insightface.app import FaceAnalysis
        # buffalo_l = SCRFD detector + landmarks + ArcFace-r50 (512-dim).
        # ctx_id=-1 and the CPU provider keep this GPU-free.
        _if_app = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])
        _if_app.prepare(ctx_id=-1, det_size=(640, 640))
    return _if_app


def _load_image(path: str) -> np.ndarray:
    from PIL import Image, ImageOps

    if Path(path).suffix.lower() in {".heic", ".heif"}:
        import pillow_heif
        pillow_heif.register_heif_opener()

    img = Image.open(path)
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")

    if max(img.size) > _MAX_DIMENSION:
        img.thumbnail((_MAX_DIMENSION, _MAX_DIMENSION), Image.LANCZOS)

    return np.array(img)


def _locations_hog(image: np.ndarray) -> list:
    return face_recognition.face_locations(image, model="hog", number_of_times_to_upsample=1)


def _locations_cnn(image: np.ndarray) -> list:
    return face_recognition.face_locations(image, model="cnn")


def _locations_mediapipe(image: np.ndarray) -> list:
    h, w = image.shape[:2]
    result = _get_mp_detector().process(image)

    if not result.detections:
        return []

    locs = []
    for det in result.detections:
        bb = det.location_data.relative_bounding_box
        x1 = max(0.0, bb.xmin)
        y1 = max(0.0, bb.ymin)
        x2 = min(1.0, bb.xmin + bb.width)
        y2 = min(1.0, bb.ymin + bb.height)
        locs.append((int(y1 * h), int(x2 * w), int(y2 * h), int(x1 * w)))
    return locs


def _encodings_insightface(image: np.ndarray) -> List[np.ndarray]:
    """Detect, align, and embed in one pass. Returns unit-norm 512-dim vectors."""
    # insightface expects BGR; _load_image gives RGB.
    faces = _get_if_app().get(image[:, :, ::-1])
    return [f.normed_embedding for f in faces]


def _face_encodings(image: np.ndarray, detector: str = "mediapipe") -> List[np.ndarray]:
    if detector == "insightface":
        return _encodings_insightface(image)

    if detector == "mediapipe":
        locs = _locations_mediapipe(image)
    elif detector == "cnn":
        locs = _locations_cnn(image)
    else:
        locs = _locations_hog(image)

    if not locs:
        return []
    return face_recognition.face_encodings(image, known_face_locations=locs)


def _cosine_threshold(tolerance: float) -> float:
    """Map the dlib-style Euclidean tolerance (0.1 strict … 1.0 loose) onto a
    cosine-similarity threshold for insightface (higher = stricter), so the same
    UI slider keeps its 'lower = stricter' meaning across all detectors."""
    return max(0.05, min(0.9, 0.68 - 0.8 * tolerance))


def encode_references(reference_paths: List[str], detector: str = "mediapipe") -> List[np.ndarray]:
    encodings: List[np.ndarray] = []
    for path in reference_paths:
        image = _load_image(path)
        found = _face_encodings(image, detector=detector)
        if not found:
            print(f"Warning: No face detected in {path!r} — skipping")
        else:
            encodings.append(found[0])
    return encodings


def is_match(
    image_path: str,
    known_encodings: List[np.ndarray],
    tolerance: float = 0.25,
    detector: str = "mediapipe",
) -> bool:
    image = _load_image(image_path)
    candidates = _face_encodings(image, detector=detector)

    if detector in _EMBEDDING_DETECTORS:
        # Cosine similarity on unit-norm embeddings: higher = more similar.
        threshold = _cosine_threshold(tolerance)
        for candidate in candidates:
            if any(float(np.dot(known, candidate)) >= threshold for known in known_encodings):
                return True
        return False

    for candidate in candidates:
        if any(face_recognition.compare_faces(known_encodings, candidate, tolerance=tolerance)):
            return True
    return False
