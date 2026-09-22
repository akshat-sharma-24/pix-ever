"""Face model registry and file-system plumbing.

This module must stay importable with neither OpenCV nor numpy installed, and
with no model files on disk. Two things depend on that:

  * the lean build target, which ships without OpenCV and simply has tagging
    disabled (see README "Packaging"),
  * tools/fetch_models.py, which has to run *before* any model exists.

So the OpenCV-backed engine is imported lazily, not at module load.

CODE_DIR is resolved here rather than imported from server.py because
server.py opens the tkinter folder picker at import time; a command-line tool
importing it would pop a dialog.
"""

import os
import sys

try:
    import cv2
    import numpy as np
    IMPORT_ERROR = None
except ImportError as e:          # lean build, or a checkout without the extras
    cv2 = None
    np = None
    IMPORT_ERROR = e

if getattr(sys, 'frozen', False):
    CODE_DIR = os.path.dirname(sys.executable)
else:
    CODE_DIR = os.path.dirname(os.path.abspath(__file__))

# Sits beside the exe when frozen and beside the source when not — the same
# place config.json lives. Models are never downloaded at runtime.
MODELS_DIR = os.path.join(CODE_DIR, "models")

# Everything PixEver derives from the photos — reference images, thumbnails —
# lives under this folder inside the user's chosen STORAGE_DIR, so it travels
# with the drive and never mixes into the dated photo folders. Originals are
# never written to; derived data only ever goes here or into the database.
PIXEVER_DIRNAME = ".pixever"

# --- Model registry ---
# A model id names an exact pair of files. It is stored in Meta.face_model_id
# on the first scan; embeddings produced by different ids are not comparable,
# so changing the active model is a hard error rather than a migration.
#
# Each file carries the size and sha256 that tools/fetch_models.py verifies
# against. Those are not guesses: opencv_zoo keeps its .onnx files in git-lfs,
# and both values are copied from the LFS pointers at ZOO_COMMIT.

# Pinned so a later upstream commit can never change what a fetch produces.
ZOO_COMMIT = "47534e27c9851bb1128ccc0102f1145e27f23f98"

def _zoo_url(model_dir: str, filename: str) -> str:
    # media.githubusercontent.com/media/... serves the real blob; the ordinary
    # raw.githubusercontent.com URL serves the 131-byte LFS pointer instead.
    return (f"https://media.githubusercontent.com/media/opencv/opencv_zoo/"
            f"{ZOO_COMMIT}/models/{model_dir}/{filename}")

_YUNET = {
    "file": "face_detection_yunet_2023mar.onnx",
    "url": _zoo_url("face_detection_yunet", "face_detection_yunet_2023mar.onnx"),
    "size": 232589,
    "sha256": "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
}

MODELS = {
    # The default. int8 is a quarter of the size but measured roughly HALF the
    # speed on the same photos, for identical accuracy (22 correct tags and 0
    # wrong, both models) — so the only thing its 29 MB saving buys is a
    # smaller download, paid for with double the library scan time.
    "yunet2023mar_sface2021dec_fp32": {
        "detector": _YUNET,
        "recognizer": {
            "file": "face_recognition_sface_2021dec.onnx",
            "url": _zoo_url("face_recognition_sface",
                            "face_recognition_sface_2021dec.onnx"),
            "size": 38696353,
            "sha256": "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
        },
    },
    # Kept so test/compare_faces.py can re-check that trade-off on new
    # photos, especially once lookalike relatives are enrolled.
    "yunet2023mar_sface2021dec_int8": {
        "detector": _YUNET,
        "recognizer": {
            "file": "face_recognition_sface_2021dec_int8.onnx",
            "url": _zoo_url("face_recognition_sface",
                            "face_recognition_sface_2021dec_int8.onnx"),
            "size": 9896933,
            "sha256": "2b0e941e6f16cc048c20aee0c8e31f569118f65d702914540f7bfdc14048d78a",
        },
    },
}

DEFAULT_MODEL_ID = "yunet2023mar_sface2021dec_fp32"


def model_files(model_id: str = DEFAULT_MODEL_ID) -> dict:
    """Absolute path of each file in a model, keyed by role."""
    if model_id not in MODELS:
        raise KeyError(f"unknown model id: {model_id}")
    return {
        role: os.path.join(MODELS_DIR, spec["file"])
        for role, spec in MODELS[model_id].items()
    }


def missing_models(model_id: str = DEFAULT_MODEL_ID) -> list:
    """Paths of this model's files that are not on disk. Empty means ready."""
    return [p for p in model_files(model_id).values() if not os.path.exists(p)]


def models_available(model_id: str = DEFAULT_MODEL_ID) -> bool:
    """True when every file this model needs is present.

    Only checks existence — tools/fetch_models.py is what verifies contents.
    """
    return not missing_models(model_id)


def engine_available(model_id: str = DEFAULT_MODEL_ID) -> bool:
    """True when a FaceEngine can actually be constructed."""
    return cv2 is not None and models_available(model_id)


# --- Availability reporting ---
# Face tagging is optional. It can be absent for two quite different reasons,
# and the fix differs, so they are reported separately rather than as one
# "unavailable". Backup never depends on any of this.

STATE_READY = "ready"
STATE_OPENCV_MISSING = "opencv_missing"    # lean build, or extras not installed
STATE_MODELS_MISSING = "models_missing"    # named in the plan's §7


def status(model_id: str = DEFAULT_MODEL_ID) -> dict:
    """Whether tagging works, and if not, what would fix it.

    `state` is for the client to branch on; `detail` is written to be shown
    to a person as-is. The scan-progress fields are added in face_worker.
    """
    if cv2 is None:
        return {
            "available": False,
            "state": STATE_OPENCV_MISSING,
            "model_id": model_id,
            "missing_files": [],
            "detail": ("Face tagging is not installed in this build. Backup is "
                       "unaffected. To enable it: pip install -r requirements-faces.txt"),
        }

    missing = missing_models(model_id)
    if missing:
        names = [os.path.basename(p) for p in missing]
        return {
            "available": False,
            "state": STATE_MODELS_MISSING,
            "model_id": model_id,
            "missing_files": names,
            "detail": (f"Face models are missing from {MODELS_DIR}: "
                       f"{', '.join(names)}. Backup is unaffected. "
                       f"To enable tagging: python tools/fetch_models.py"),
        }

    return {
        "available": True,
        "state": STATE_READY,
        "model_id": model_id,
        "missing_files": [],
        "detail": "Face tagging is available.",
    }


# --- Pipeline ---
# Constants for the per-photo pipeline. See the plan's §5.

# Longest side to downscale to before detecting. Big photos cost time without
# finding more faces, and it makes MIN_FACE_PX mean the same thing for a phone
# photo and a DSLR one.
MAX_SIDE = 1600

# Faces smaller than this *after* downscaling are dropped. Tiny faces embed
# unreliably and are the main source of false positives from background crowds.
MIN_FACE_PX = 40

# YuNet's own confidence floor for calling something a face at all.
# 0.9 is the value in most OpenCV samples and is far too strict for ordinary
# photos: measured on real family photos it threw away a third of the
# reference images, finding no face at all in them. 0.6 lost none of them and
# raised recall from 91% to 96% without producing a single wrong tag. Below
# ~0.4 spurious detections start matching real people, which is worse than
# missing a face, so this is a floor to lower carefully.
# Re-measure with: test/compare_faces.py <dir> --det-threshold N
DET_SCORE_THRESHOLD = 0.6
NMS_THRESHOLD = 0.3

# Cosine similarity above which two faces are considered the same person.
# SFace publishes 0.363 as a generic default; 0.370 was chosen after measuring
# real family photos, where anything from 0.35 to 0.55 produced identical
# results (22 correct tags, 0 wrong). Sitting just inside the low end of that
# band keeps recall while leaving a little more margin than the stock value.
# The band was measured WITHOUT any pair of lookalike relatives, which is the
# case most likely to narrow it — re-measure when such a pair is enrolled:
#   test/compare_faces.py test/testdata/people
COSINE_THRESHOLD = 0.370

# Decision 3: Android only in v1, so no HEIC. Anything else is marked skipped
# by the scanner rather than treated as an error.
SUPPORTED_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}

# A reference photo's largest face counts as its subject only when the next
# face is smaller than this fraction of it. See subject_face().
DOMINANCE_RATIO = 0.80


class NoSubjectFace(ValueError):
    """A reference photo has no single, identifiable subject.

    The message is written to be shown to a person as-is: they are about to
    pick a different photo, and need to know which problem they hit.
    """


def face_size(face) -> int:
    """Linear extent of a face, for comparing two faces in the same photo."""
    return max(face.w, face.h)


def subject_face(found: list, dominance: float = DOMINANCE_RATIO):
    """The one face a reference photo is 'of'. Raises NoSubjectFace otherwise.

    A name says *who* a photo is of, never *which face*, so a photo holding
    several faces has to be resolved rather than guessed at. The largest face
    is the subject when the next-largest is clearly smaller — that covers
    bystanders and background crowds. When two faces are comparable in size
    there is no honest way to choose, and choosing anyway is how someone's
    reference set ends up holding another person's face: silent, permanent,
    and very hard to diagnose later.

    Only for enrolment. Library scanning keeps every face it finds.
    """
    if not found:
        raise NoSubjectFace(
            "No face was found in this photo. Use a clearer, front-facing "
            "photo — and don't crop it tightly, the whole head and shoulders "
            "works best."
        )
    ordered = sorted(found, key=face_size, reverse=True)
    if len(ordered) > 1:
        ratio = face_size(ordered[1]) / face_size(ordered[0])
        if ratio >= dominance:
            raise NoSubjectFace(
                f"This photo has {len(ordered)} faces of similar size, so it is "
                f"not clear which one to enrol. Use a photo where this person "
                f"is clearly the closest to the camera, or crop the others out."
            )
    return ordered[0]


def is_supported(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in SUPPORTED_SUFFIXES


def decode_image(data: bytes):
    """Decode image bytes to a BGR array, applying EXIF rotation.

    Returns None if the bytes are not a decodable image.
    """
    if not data:
        return None
    return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)


def read_image(path: str):
    """Decode a file to a BGR array, applying EXIF rotation. None on failure.

    Reads the bytes itself and uses imdecode rather than imread, because
    imread cannot open non-ASCII paths on Windows — which is where this runs,
    over whatever folder names the user's photos happen to have. Verified on
    OpenCV 5: imdecode applies EXIF orientation just as imread does.
    """
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    return decode_image(data)


def to_blob(embedding) -> bytes:
    """An embedding as the raw float32 BLOB stored in SQLite."""
    return np.asarray(embedding, dtype=np.float32).tobytes()


def from_blob(blob: bytes):
    """An embedding back from its stored BLOB."""
    return np.frombuffer(blob, dtype=np.float32)


def cosine_similarity(a, b) -> float:
    """Similarity of two embeddings, 1.0 being identical.

    A plain dot product, valid only because embeddings are stored L2-normalised.
    """
    return float(np.dot(a, b))


class Face:
    """One detected face: where it is, and what it looks like as 128 numbers.

    x/y/w/h are in the coordinates of the *original* image, not the downscaled
    copy, so a crop or thumbnail can be taken from the file as it sits on disk.
    """

    __slots__ = ("x", "y", "w", "h", "det_score", "embedding")

    def __init__(self, x, y, w, h, det_score, embedding):
        self.x, self.y, self.w, self.h = int(x), int(y), int(w), int(h)
        self.det_score = float(det_score)
        self.embedding = embedding

    def __repr__(self):
        return (f"Face(x={self.x}, y={self.y}, w={self.w}, h={self.h}, "
                f"det_score={self.det_score:.3f})")


class FaceEngine:
    """Detects faces and turns each into a 128-float embedding.

    This class is the seam the plan keeps deliberately narrow: swapping YuNet
    and SFace for a hand-rolled onnxruntime pipeline later means reimplementing
    `faces_in_image` and nothing else.

    Models are loaded once per instance and are not thread-safe, so the
    background worker should keep its own.
    """

    def __init__(self, model_id: str = DEFAULT_MODEL_ID, det_threshold: float = None):
        if cv2 is None:
            raise RuntimeError(
                f"OpenCV is not installed, so face tagging is unavailable ({IMPORT_ERROR})"
            )
        missing = missing_models(model_id)
        if missing:
            raise RuntimeError(
                "missing model files: " + ", ".join(os.path.basename(p) for p in missing)
                + " — run: python tools/fetch_models.py"
            )

        self.model_id = model_id
        self.det_threshold = (DET_SCORE_THRESHOLD if det_threshold is None
                              else det_threshold)
        paths = model_files(model_id)
        self._detector = cv2.FaceDetectorYN.create(
            paths["detector"], "", (320, 320),      # size is reset per image
            self.det_threshold, NMS_THRESHOLD, 5000,
        )
        self._recognizer = cv2.FaceRecognizerSF.create(paths["recognizer"], "")

    def faces_in_image(self, path: str) -> list:
        """Every usable face in the file at `path`. Empty list if none.

        Raises ValueError if the file cannot be decoded at all.
        """
        image = read_image(path)
        if image is None:
            raise ValueError(f"could not decode image: {path}")
        return self.faces_in_array(image)

    def faces_in_array(self, image) -> list:
        h, w = image.shape[:2]
        scale = min(1.0, MAX_SIDE / max(h, w))
        small = (cv2.resize(image, (round(w * scale), round(h * scale)))
                 if scale < 1.0 else image)

        self._detector.setInputSize((small.shape[1], small.shape[0]))
        _, detections = self._detector.detect(small)
        if detections is None:
            return []

        faces = []
        for row in detections:
            # row = [x, y, w, h, 10 landmark coords, score]
            if row[2] < MIN_FACE_PX or row[3] < MIN_FACE_PX:
                continue
            # Align and crop from the full-resolution image rather than the
            # downscaled one: same transform, more detail to feed the
            # recogniser, which matters most for the small faces that are
            # hardest to identify. Only the geometry scales — not row[14],
            # which is the detection score.
            full = row.copy()
            full[:14] /= scale
            aligned = self._recognizer.alignCrop(image, full)
            embedding = self._recognizer.feature(aligned)[0]
            faces.append(Face(
                full[0], full[1], full[2], full[3], row[14],
                embedding / np.linalg.norm(embedding),
            ))
        return faces
