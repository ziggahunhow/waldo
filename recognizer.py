from typing import List

import face_recognition
import numpy as np


def encode_references(reference_paths: List[str]) -> List[np.ndarray]:
    """Load reference photos and encode each detected face.

    Prints a warning for photos where no face is found.
    Only the first detected face per photo is used.
    Returns a list of 128-dim face encoding arrays.
    """
    encodings: List[np.ndarray] = []
    for path in reference_paths:
        image = face_recognition.load_image_file(path)
        found = face_recognition.face_encodings(image)
        if not found:
            print(f"Warning: No face detected in {path!r} — skipping")
        else:
            encodings.append(found[0])
    return encodings


def is_match(
    image_path: str,
    known_encodings: List[np.ndarray],
    tolerance: float = 0.5,
) -> bool:
    """Return True if any face in the image matches any of the known encodings.

    Loads the image, detects all faces, and compares each against known_encodings.
    Returns False if the image contains no faces.
    """
    image = face_recognition.load_image_file(image_path)
    candidates = face_recognition.face_encodings(image)
    for candidate in candidates:
        results = face_recognition.compare_faces(known_encodings, candidate, tolerance=tolerance)
        if any(results):
            return True
    return False
