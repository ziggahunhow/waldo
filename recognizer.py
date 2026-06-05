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


def _face_encodings(image: np.ndarray) -> List[np.ndarray]:
    """Detect faces and return encodings. Images where no face is detected are skipped."""
    locs = face_recognition.face_locations(image, model="hog", number_of_times_to_upsample=1)
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
