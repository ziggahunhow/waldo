from pathlib import Path
from typing import List, Optional

import face_recognition
import numpy as np

_MAX_DIMENSION = 1800
DETECTORS = ("mediapipe", "hog", "cnn")

# Lazy singleton — created once, reused for the lifetime of the process.
_mp_detector = None


def _get_mp_detector():
    global _mp_detector
    if _mp_detector is None:
        import mediapipe as mp
        _mp_detector = mp.solutions.face_detection.FaceDetection(
            model_selection=1,
            min_detection_confidence=0.4,
        )
    return _mp_detector


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


def _face_encodings(image: np.ndarray, detector: str = "mediapipe") -> List[np.ndarray]:
    if detector == "mediapipe":
        locs = _locations_mediapipe(image)
    elif detector == "cnn":
        locs = _locations_cnn(image)
    else:
        locs = _locations_hog(image)

    if not locs:
        return []
    return face_recognition.face_encodings(image, known_face_locations=locs)


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
    for candidate in candidates:
        if any(face_recognition.compare_faces(known_encodings, candidate, tolerance=tolerance)):
            return True
    return False
