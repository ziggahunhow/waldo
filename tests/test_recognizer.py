import numpy as np
from unittest.mock import patch, MagicMock

from recognizer import encode_references, is_match


# Synthetic 128-dim face encodings (same shape face_recognition returns)
_FACE_A = np.zeros(128, dtype=float)
_FACE_B = np.ones(128, dtype=float)


# encode_references / is_match load pixels via _load_image (PIL) and derive
# encodings via _face_encodings, so tests patch those rather than doing real
# file I/O or face detection.


@patch("recognizer._load_image", return_value=MagicMock())
@patch("recognizer._face_encodings")
def test_encode_references_returns_first_encoding(mock_encodings, mock_load):
    mock_encodings.return_value = [_FACE_A]

    result = encode_references(["ref.jpg"])

    assert len(result) == 1
    np.testing.assert_array_equal(result[0], _FACE_A)


@patch("recognizer._load_image", return_value=MagicMock())
@patch("recognizer._face_encodings")
def test_encode_references_warns_and_skips_when_no_face(mock_encodings, mock_load, capsys):
    mock_encodings.return_value = []  # no face in this photo

    result = encode_references(["bad.jpg"])

    assert result == []
    captured = capsys.readouterr()
    assert "no face detected" in captured.out.lower()


@patch("recognizer._load_image", return_value=MagicMock())
@patch("recognizer._face_encodings")
def test_encode_references_multiple_photos(mock_encodings, mock_load):
    mock_encodings.side_effect = [[_FACE_A], [_FACE_B]]

    result = encode_references(["ref1.jpg", "ref2.jpg"])

    assert len(result) == 2


@patch("recognizer._load_image", return_value=MagicMock())
@patch("recognizer._face_encodings")
@patch("recognizer.face_recognition")
def test_is_match_returns_true_for_matching_face(mock_fr, mock_encodings, mock_load):
    mock_encodings.return_value = [_FACE_A]
    mock_fr.compare_faces.return_value = [True]

    assert is_match("photo.jpg", [_FACE_A], tolerance=0.5) is True


@patch("recognizer._load_image", return_value=MagicMock())
@patch("recognizer._face_encodings")
@patch("recognizer.face_recognition")
def test_is_match_returns_false_for_different_face(mock_fr, mock_encodings, mock_load):
    mock_encodings.return_value = [_FACE_B]
    mock_fr.compare_faces.return_value = [False]

    assert is_match("photo.jpg", [_FACE_A], tolerance=0.5) is False


@patch("recognizer._load_image", return_value=MagicMock())
@patch("recognizer._face_encodings")
def test_is_match_returns_false_when_image_has_no_faces(mock_encodings, mock_load):
    mock_encodings.return_value = []  # landscape photo, no faces

    assert is_match("landscape.jpg", [_FACE_A], tolerance=0.5) is False


@patch("recognizer._load_image", return_value=MagicMock())
@patch("recognizer._face_encodings")
def test_is_match_insightface_uses_cosine_similarity(mock_encodings, mock_load):
    # insightface bypasses face_recognition entirely: unit-norm embeddings
    # compared by cosine similarity against _cosine_threshold(tolerance).
    same = np.array([1.0, 0.0, 0.0])
    mock_encodings.return_value = [same]

    # identical vector → cosine 1.0, above threshold → match
    assert is_match("p.jpg", [same], tolerance=0.25, detector="insightface") is True
    # orthogonal reference → cosine 0.0, below threshold → no match
    orthogonal = np.array([0.0, 1.0, 0.0])
    assert is_match("p.jpg", [orthogonal], tolerance=0.25, detector="insightface") is False
