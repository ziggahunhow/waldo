from pathlib import Path
from typing import List

import face_recognition
import numpy as np

_MAX_DIMENSION = 1800


def _load_image(path: str) -> np.ndarray:
    """Load an image file to an RGB numpy array.

    Handles HEIC/HEIF and MPO files, applies EXIF rotation, and downscales
    images larger than _MAX_DIMENSION so face detection works reliably.
    """
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


def encode_references(reference_paths: List[str]) -> List[np.ndarray]:
    """Load reference photos and encode each detected face.

    Prints a warning for photos where no face is found.
    Only the first detected face per photo is used.
    Returns a list of 128-dim face encoding arrays.
    """
    encodings: List[np.ndarray] = []
    for path in reference_paths:
        image = _load_image(path)
        found = _face_encodings(image)
        if not found:
            print(f"Warning: No face detected in {path!r} — skipping")
        else:
            encodings.append(found[0])
    return encodings


def _mediapipe_face_locations(image: np.ndarray) -> list:
    """Detect faces using MediaPipe and return locations in dlib's (top,right,bottom,left) format."""
    import mediapipe as mp

    h, w = image.shape[:2]
    detector = mp.solutions.face_detection.FaceDetection(
        model_selection=1,       # 1 = full-range model (handles tilted/distant faces)
        min_detection_confidence=0.4,
    )
    result = detector.process(image)
    detector.close()

    if not result.detections:
        return []

    locs = []
    for det in result.detections:
        bb = det.location_data.relative_bounding_box
        # clamp to [0,1] in case of out-of-frame detections
        x1 = max(0.0, bb.xmin)
        y1 = max(0.0, bb.ymin)
        x2 = min(1.0, bb.xmin + bb.width)
        y2 = min(1.0, bb.ymin + bb.height)
        top    = int(y1 * h)
        right  = int(x2 * w)
        bottom = int(y2 * h)
        left   = int(x1 * w)
        locs.append((top, right, bottom, left))
    return locs


def _face_encodings(image: np.ndarray) -> List[np.ndarray]:
    """Detect faces with MediaPipe and return dlib encodings."""
    locs = _mediapipe_face_locations(image)
    if not locs:
        return []
    return face_recognition.face_encodings(image, known_face_locations=locs)


def is_match(
    image_path: str,
    known_encodings: List[np.ndarray],
    tolerance: float = 0.25,
) -> bool:
    """Return True if any face in the image matches any of the known encodings.

    Loads the image, detects all faces, and compares each against known_encodings.
    Returns False if the image contains no faces.
    """
    image = _load_image(image_path)
    candidates = _face_encodings(image)
    for candidate in candidates:
        results = face_recognition.compare_faces(known_encodings, candidate, tolerance=tolerance)
        if any(results):
            return True
    return False
