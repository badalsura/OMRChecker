"""
PaddleOCR (PP-OCRv5) text detection and recognition on ONNX Runtime.

The pretrained PP-OCRv5 models are used as they are, converted to ONNX, and run
by the onnxruntime the app already ships, so the PaddlePaddle framework is not
needed. Only the two small pre/post-processing steps PaddleOCR does in Python
are re-implemented here with NumPy and OpenCV:

- detection (DB, "Differentiable Binarization"): the model returns a text
  probability map; it is thresholded, each connected region becomes a rotated
  box, scored by its mean probability and grown ("unclipped") by
  area * unclip_ratio / perimeter, as in PaddleOCR's DBPostProcess;
- recognition (SVTR/PP-LCNet + CTC): the crop is resized to a fixed height,
  normalised to [-1, 1], and the per-timestep character probabilities are CTC
  decoded (best path: argmax, merge repeats, drop the blank) with the model's
  character dictionary.

Models are not committed: packaging/fetch_ocr_models.py downloads them at build
time (see packaging/ocr_models.md). Without the model files, or when
onnxruntime cannot load (e.g. Windows 7 without the UCRT updates), the engine
reports itself unavailable and the OCR reader falls back to Tesseract.
"""

import os
import sys
import threading
from pathlib import Path

import cv2
import numpy as np

from src.logger import logger

# (kind, size, lang) -> (onnx file, dictionary file or None). The dictionary may
# also be embedded in the ONNX metadata under "character" (one char per line).
MODEL_FILES = {
    ("det", "mobile", None): ("PP-OCRv5_mobile_det.onnx", None),
    ("det", "server", None): ("PP-OCRv5_server_det.onnx", None),
    # Multilingual model (Chinese, English, Japanese, ... ); covers English
    ("rec", "mobile", "ch"): ("PP-OCRv5_mobile_rec.onnx", "ppocrv5_dict.txt"),
    ("rec", "server", "ch"): ("PP-OCRv5_server_rec.onnx", "ppocrv5_dict.txt"),
    ("rec", "mobile", "en"): ("en_PP-OCRv5_mobile_rec.onnx", "ppocrv5_en_dict.txt"),
    # Devanagari script (Hindi, Marathi, Nepali, ...)
    ("rec", "mobile", "devanagari"): (
        "devanagari_PP-OCRv5_mobile_rec.onnx",
        "ppocrv5_devanagari_dict.txt",
    ),
}

# Where to look for models when config ocr_params.paddle_model_dir is not set
MODEL_DIR_ENV = "OMR_PADDLE_MODELS"

DET_MEAN = np.array([0.485, 0.456, 0.406], np.float32)
DET_STD = np.array([0.229, 0.224, 0.225], np.float32)
REC_HEIGHT = 48
REC_MAX_WIDTH = 3200
DET_LIMIT_SIDE = 960
DET_MIN_SIDE = 64
DB_THRESH = 0.3
DB_BOX_THRESH = 0.6
DB_UNCLIP_RATIO = 1.5
DB_MIN_SIZE = 3

_LOCK = threading.Lock()
_ORT = None  # module, False when it failed to import
_ORT_ERROR = None
_SESSIONS = {}  # path -> InferenceSession or None (failed)
_DICTS = {}
_WARNED = set()


def _warn_once(key, message):
    if key not in _WARNED:
        _WARNED.add(key)
        logger.warning(message)


def onnxruntime_module():
    """onnxruntime, or None when it cannot be imported (logged once)."""
    global _ORT, _ORT_ERROR
    if _ORT is None:
        try:
            import onnxruntime  # noqa: F401

            _ORT = onnxruntime
        except Exception as error:  # ImportError, or a DLL load failure on Win7
            _ORT = False
            _ORT_ERROR = str(error)
            _warn_once(
                "ort",
                f"PaddleOCR disabled: onnxruntime could not be loaded ({error}); "
                "OCR uses Tesseract only",
            )
    return _ORT or None


def default_model_dirs():
    dirs = []
    env = os.environ.get(MODEL_DIR_ENV)
    if env:
        dirs.append(Path(env))
    roots = [Path(__file__).resolve().parents[2]]
    if getattr(sys, "frozen", False):
        roots.insert(0, Path(sys.executable).resolve().parent)
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            roots.insert(1, Path(meipass))
    for root in roots:
        dirs += [root / "models" / "paddleocr", root / "packaging" / "models" / "paddleocr"]
    return dirs


def find_model_file(name, model_dir=None):
    candidates = [Path(model_dir)] if model_dir else []
    candidates += default_model_dirs()
    for directory in candidates:
        path = directory / name
        if path.is_file():
            return path
    return None


def rec_model_key(size, lang):
    """Best available recognition model key for a size and language."""
    lang = (lang or "en").lower()
    if lang in ("hin", "hi", "mar", "nep", "san", "devanagari"):
        lang = "devanagari"
    elif lang in ("eng", "en", "latin"):
        lang = "en"
    keys = [("rec", size, lang), ("rec", "mobile", lang)]
    if lang == "en":
        # The multilingual model also reads English
        keys += [("rec", size, "ch"), ("rec", "mobile", "ch")]
    return [k for k in keys if k in MODEL_FILES]


def _session(path):
    path = str(path)
    with _LOCK:
        if path in _SESSIONS:
            return _SESSIONS[path]
        ort = onnxruntime_module()
        session = None
        if ort is not None:
            try:
                options = ort.SessionOptions()
                threads = int(os.environ.get("OMR_ONNX_THREADS", "0") or 0)
                if threads:
                    options.intra_op_num_threads = threads
                    options.inter_op_num_threads = 1
                session = ort.InferenceSession(
                    path, options, providers=["CPUExecutionProvider"]
                )
            except Exception as error:
                _warn_once(path, f"PaddleOCR model '{path}' could not be loaded: {error}")
                session = None
        _SESSIONS[path] = session
        return session


def load_dictionary(dict_path=None, session=None):
    """Character list for CTC: index 0 is the blank, then the dictionary, then ' '."""
    key = str(dict_path) if dict_path else id(session)
    if key in _DICTS:
        return _DICTS[key]
    chars = None
    if dict_path and Path(dict_path).is_file():
        text = Path(dict_path).read_text(encoding="utf-8")
        chars = [line.rstrip("\r\n") for line in text.splitlines()]
    elif session is not None:
        meta = session.get_modelmeta().custom_metadata_map or {}
        if meta.get("character"):
            chars = meta["character"].splitlines()
    if chars is None:
        return None
    chars = [c for c in chars if c != ""]
    table = ["<blank>"] + chars + [" "]
    _DICTS[key] = table
    return table


def ctc_decode(probs, characters, allowed=None):
    """
    Best-path CTC decoding of one (T, C) probability matrix.

    allowed: optional set of characters; others (except the blank) are masked
    out before the argmax, like Tesseract's whitelist.
    Returns (text, [per-character probability]).
    """
    probs = np.asarray(probs, dtype=np.float32)
    if allowed is not None:
        mask = np.zeros(probs.shape[1], dtype=bool)
        mask[0] = True
        for index, char in enumerate(characters[: probs.shape[1]]):
            if index and char in allowed:
                mask[index] = True
        probs = np.where(mask[None, :], probs, 0.0)
    best = probs.argmax(axis=1)
    scores = probs.max(axis=1)
    text, confidences, previous = [], [], -1
    for index, score in zip(best, scores):
        if index != previous and index != 0 and index < len(characters):
            text.append(characters[index])
            confidences.append(float(score))
        previous = index
    # Strip edge spaces with their scores
    while text and text[0] == " ":
        text.pop(0)
        confidences.pop(0)
    while text and text[-1] == " ":
        text.pop()
        confidences.pop()
    return "".join(text), confidences


def _to_bgr(image):
    if image.ndim == 2:
        return cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    if image.shape[2] == 4:
        return cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)
    return image


def rec_preprocess(image, height=REC_HEIGHT):
    image = _to_bgr(image)
    h, w = image.shape[:2]
    width = int(np.ceil(height * w / float(max(h, 1))))
    width = min(max(width, 8), REC_MAX_WIDTH)
    resized = cv2.resize(image, (width, height)).astype(np.float32)
    resized = (resized / 255.0 - 0.5) / 0.5
    return resized.transpose(2, 0, 1)[None]


def det_preprocess(image, limit_side=DET_LIMIT_SIDE):
    image = _to_bgr(image)
    h, w = image.shape[:2]
    ratio = 1.0
    if max(h, w) > limit_side:
        ratio = float(limit_side) / max(h, w)
    elif min(h, w) < DET_MIN_SIDE:
        ratio = float(DET_MIN_SIDE) / max(min(h, w), 1)
    nh = max(int(round(h * ratio / 32) * 32), 32)
    nw = max(int(round(w * ratio / 32) * 32), 32)
    resized = cv2.resize(image, (nw, nh)).astype(np.float32) / 255.0
    resized = (resized - DET_MEAN) / DET_STD
    return resized.transpose(2, 0, 1)[None], (h / float(nh), w / float(nw))


def _order_points(points):
    points = np.asarray(points, dtype=np.float32)
    s = points.sum(axis=1)
    d = np.diff(points, axis=1).ravel()
    return np.array(
        [points[np.argmin(s)], points[np.argmin(d)], points[np.argmax(s)], points[np.argmax(d)]],
        dtype=np.float32,
    )


def db_postprocess(prob_map, scale=(1.0, 1.0), thresh=DB_THRESH, box_thresh=DB_BOX_THRESH, unclip_ratio=DB_UNCLIP_RATIO):
    """Boxes (4x2 TL,TR,BR,BL in original pixels) and scores from a DB probability map."""
    prob_map = np.asarray(prob_map, dtype=np.float32)
    while prob_map.ndim > 2:
        prob_map = prob_map[0]
    bitmap = (prob_map > thresh).astype(np.uint8)
    contours, _ = cv2.findContours(bitmap, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    boxes, scores = [], []
    for contour in contours:
        if len(contour) < 3:
            continue
        rect = cv2.minAreaRect(contour)
        if min(rect[1]) < DB_MIN_SIZE:
            continue
        mask = np.zeros_like(bitmap)
        cv2.fillPoly(mask, [contour.reshape(-1, 2).astype(np.int32)], 1)
        score = float(prob_map[mask.astype(bool)].mean()) if mask.any() else 0.0
        if score < box_thresh:
            continue
        # Unclip: grow the box by area * ratio / perimeter on every side
        area = cv2.contourArea(contour)
        perimeter = cv2.arcLength(contour, True)
        distance = area * unclip_ratio / max(perimeter, 1e-6)
        (cx, cy), (bw, bh), angle = rect
        grown = ((cx, cy), (bw + 2 * distance, bh + 2 * distance), angle)
        if min(grown[1]) < DB_MIN_SIZE + 2:
            continue
        points = _order_points(cv2.boxPoints(grown))
        points[:, 0] = np.clip(points[:, 0] * scale[1], 0, prob_map.shape[1] * scale[1] - 1)
        points[:, 1] = np.clip(points[:, 1] * scale[0], 0, prob_map.shape[0] * scale[0] - 1)
        boxes.append(points)
        scores.append(score)
    # Reading order: top to bottom, then left to right on the same line
    order = sorted(range(len(boxes)), key=lambda i: (round(boxes[i][0][1] / 10.0), boxes[i][0][0]))
    return [boxes[i] for i in order], [scores[i] for i in order]


def crop_box(image, box):
    box = np.asarray(box, dtype=np.float32)
    width = int(max(np.linalg.norm(box[0] - box[1]), np.linalg.norm(box[2] - box[3])))
    height = int(max(np.linalg.norm(box[0] - box[3]), np.linalg.norm(box[1] - box[2])))
    width, height = max(width, 1), max(height, 1)
    target = np.array([[0, 0], [width, 0], [width, height], [0, height]], np.float32)
    matrix = cv2.getPerspectiveTransform(box, target)
    crop = cv2.warpPerspective(image, matrix, (width, height), borderMode=cv2.BORDER_REPLICATE)
    if height > width * 1.5:
        crop = np.rot90(crop)
    return crop


class PaddleOCR:
    """One detection + recognition model pair; sessions are shared per process."""

    def __init__(self, det_size="mobile", rec_size="mobile", lang="en", model_dir=None):
        self.det_size, self.rec_size, self.lang = det_size, rec_size, lang
        self.model_dir = model_dir
        self.unavailable_reason = None
        self.rec_path = self.dict_path = self.det_path = None
        if onnxruntime_module() is None:
            self.unavailable_reason = f"onnxruntime unavailable ({_ORT_ERROR})"
            return
        for key in rec_model_key(rec_size, lang):
            model_name, dict_name = MODEL_FILES[key]
            path = find_model_file(model_name, model_dir)
            if path:
                self.rec_path = path
                self.dict_path = find_model_file(dict_name, model_dir) if dict_name else None
                break
        if self.rec_path is None:
            self.unavailable_reason = "PaddleOCR recognition model not installed"
            return
        det_name = MODEL_FILES[("det", det_size, None)][0]
        self.det_path = find_model_file(det_name, model_dir) or find_model_file(
            MODEL_FILES[("det", "mobile", None)][0], model_dir
        )

    @property
    def available(self):
        if self.unavailable_reason:
            return False
        session = _session(self.rec_path)
        if session is None:
            self.unavailable_reason = "PaddleOCR recognition model failed to load"
            return False
        if load_dictionary(self.dict_path, session) is None:
            self.unavailable_reason = "PaddleOCR character dictionary missing"
            return False
        return True

    def recognize_line(self, image, allowed=None):
        """(text, per-char confidences) for one text line image."""
        session = _session(self.rec_path)
        characters = load_dictionary(self.dict_path, session)
        name = session.get_inputs()[0].name
        output = session.run(None, {name: rec_preprocess(image).astype(np.float32)})[0]
        # (1, T, C) per-timestep character probabilities
        return ctc_decode(output[0], characters, allowed)

    def detect(self, image):
        if not self.det_path:
            return []
        session = _session(self.det_path)
        if session is None:
            return []
        tensor, scale = det_preprocess(image)
        output = session.run(None, {session.get_inputs()[0].name: tensor})[0]
        boxes, _ = db_postprocess(output, scale)
        return boxes

    def read(self, image, allowed=None, single_line=True):
        """
        Read a zone crop. single_line: recognise the whole crop as one line
        (the usual OCR zone); otherwise detect text lines first and read each.
        Returns (text, confidence, char_confidences, details).
        """
        image = _to_bgr(image)
        boxes = [] if single_line else self.detect(image)
        lines, confidences = [], []
        if boxes:
            for box in boxes:
                text, chars = self.recognize_line(crop_box(image, box), allowed)
                if text:
                    lines.append(text)
                    confidences.extend(chars)
        else:
            text, chars = self.recognize_line(image, allowed)
            if text:
                lines.append(text)
                confidences.extend(chars)
        text = " ".join(lines)
        confidence = float(np.mean(confidences)) if confidences else 0.0
        details = {"lines": len(lines), "detected_boxes": len(boxes)}
        return text, confidence, confidences, details


_ENGINES = {}


def get_engine(det_size="mobile", rec_size="mobile", lang="en", model_dir=None):
    key = (det_size, rec_size, (lang or "en").lower(), str(model_dir or ""))
    if key not in _ENGINES:
        _ENGINES[key] = PaddleOCR(det_size, rec_size, lang, model_dir)
    return _ENGINES[key]


def reset_cache():
    """Forget loaded sessions and engines (tests, or after models were installed)."""
    global _ORT
    with _LOCK:
        _SESSIONS.clear()
        _DICTS.clear()
        _ENGINES.clear()
    if _ORT is False:
        _ORT = None


def paddle_available(det_size="mobile", rec_size="mobile", lang="en", model_dir=None):
    """True when onnxruntime loads and the PaddleOCR models are present; never raises."""
    try:
        return bool(get_engine(det_size, rec_size, lang, model_dir).available)
    except Exception:
        return False
