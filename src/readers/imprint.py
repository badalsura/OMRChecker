"""
Scanner imprint numbers: the short code a scanner's imprinter prints on each
sheet as it feeds (for example "S100001"), in dot-matrix characters from a
small set such as "S0123456789".

The print lands wherever the paper was when the imprinter fired, often
outside the timing marks, so an "imprint" zone is a search strip rather than
a box: the reader finds the line of dots inside it, cuts it out and reads it
with a small CTC line recogniser (ONNX) trained on that scanner's print.

Optional module: nothing loads until a template has an imprint zone, and the
model is a separate file (packaging/models/imprint/<name>.onnx plus a .json
sidecar, see src/ml/train_imprint.py). Without a model the zone is read by
Tesseract and always goes to review (no_imprint_model).

Zone options:
    model          model name in packaging/models/imprint (default "imprint")
                   or a path to the .onnx file
    direction      how the print reads in the template (as for OCR zones);
                   default: the model's own
    charHeight     character height in template px (default: found)
    whitelist      characters allowed (default: the model's charset)
    pattern        regular expression the value must match, e.g. "S\\d{6}"
    minConfidence  as for other zones
"""

import json
import os
import sys
import threading
from pathlib import Path

import cv2
import numpy as np

from src.logger import logger
from src.readers.base import ZoneReadResult

DEFAULT_MODEL = "imprint"
DEFAULT_CHARSET = "S0123456789"
INPUT_HEIGHT = 32
# A component this full of ink is a solid mark (timing mark, edge shadow),
# not dot-matrix print, which covers well under half of its box
MAX_FILL = 0.6
# Line box padding, as a fraction of the line height
PAD = 0.25

_MODELS = {}
_LOCK = threading.Lock()


# ------------------------------------------------------------------ locating
def ink_mask(grey):
    """Dark pixels: an adaptive threshold, so a faint print on a bright page
    and a dark one on a grey scan both come through."""
    block = max(15, (min(grey.shape[:2]) // 4) | 1)
    return cv2.adaptiveThreshold(
        grey, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, block, 12
    )


def drop_solid(mask):
    """Remove solid blobs (timing marks, edge shadows, filled boxes): ink
    covering most of a box much bigger than a typical dot or stroke."""
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    if count <= 2:
        return mask
    sizes = np.minimum(stats[1:, cv2.CC_STAT_WIDTH], stats[1:, cv2.CC_STAT_HEIGHT])
    typical = max(float(np.median(sizes)), 1.0)
    fill = stats[1:, cv2.CC_STAT_AREA] / np.maximum(
        stats[1:, cv2.CC_STAT_WIDTH] * stats[1:, cv2.CC_STAT_HEIGHT], 1
    )
    solid = np.nonzero((fill > MAX_FILL) & (sizes > 3 * typical))[0] + 1
    if solid.size:
        mask = mask.copy()
        mask[np.isin(labels, solid)] = 0
    return mask


def estimate_char_height(mask):
    """Height of the text from the tallest rows of joined dots; None if blank."""
    rows = np.nonzero(mask.sum(axis=1) > 0)[0]
    if rows.size == 0:
        return None
    count, _, stats, _ = cv2.connectedComponentsWithStats(
        cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8)), 8
    )
    heights = sorted(stats[1:, cv2.CC_STAT_HEIGHT])
    if not heights:
        return None
    return float(np.percentile(heights, 90))


def locate_line(grey, char_height=None):
    """
    (x, y, w, h) of the dot-matrix line in a horizontal strip, or None.

    1. ink_mask, then join the dots into strokes (closing ~0.15 char height)
       and the strokes of a line into blobs (closing ~0.8 char height wide);
    2. drop blobs that are solid (fill > MAX_FILL in the stroke mask: timing
       marks) or far from the character height;
    3. start from the blob with the most ink and add the blobs level with it
       and less than 1.5 character heights away (a wide gap between two
       characters splits a line);
    4. the line must be at least as wide as tall.
    """
    mask = drop_solid(ink_mask(grey))
    char_height = char_height or estimate_char_height(mask)
    if not char_height:
        return None
    dot = max(int(round(char_height * 0.15)), 2)
    strokes = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((dot, dot), np.uint8))
    join = max(int(round(char_height * 0.8)), 3)
    lines = cv2.morphologyEx(
        strokes, cv2.MORPH_CLOSE, np.ones((max(dot, 3), join), np.uint8)
    )
    count, _, stats, _ = cv2.connectedComponentsWithStats(lines, 8)
    blobs = []
    for i in range(1, count):
        x, y, w, h, _ = (int(v) for v in stats[i])
        if not 0.5 * char_height <= h <= 2.0 * char_height:
            continue
        fill = np.count_nonzero(strokes[y : y + h, x : x + w]) / float(w * h)
        if fill > MAX_FILL:
            continue
        blobs.append([x, y, w, h, int(np.count_nonzero(mask[y : y + h, x : x + w]))])
    if not blobs:
        return None
    x, y, w, h, _ = max(blobs, key=lambda b: b[4])
    grown = True
    while grown:
        grown = False
        for bx, by, bw, bh, _ in blobs:
            overlap = min(y + h, by + bh) - max(y, by)
            gap = max(bx - (x + w), x - (bx + bw))
            inside = bx >= x and bx + bw <= x + w and by >= y and by + bh <= y + h
            if inside or overlap < 0.5 * min(h, bh) or gap > 1.5 * char_height:
                continue
            x0, y0 = min(x, bx), min(y, by)
            x, y, w, h = x0, y0, max(x + w, bx + bw) - x0, max(y + h, by + bh) - y0
            grown = True
    if w < h:
        return None
    return x, y, w, h


def cut_line(grey, box):
    x, y, w, h = box
    pad = int(round(h * PAD))
    H, W = grey.shape[:2]
    return grey[max(y - pad, 0) : min(y + h + pad, H), max(x - pad, 0) : min(x + w + pad, W)]


def model_input(line, height=INPUT_HEIGHT, max_width=None):
    """Grey line crop -> (1, 1, height, W) float32, paper ~1 and ink ~0."""
    h, w = line.shape[:2]
    width = max(int(round(w * height / float(h))), 1)
    if max_width:
        width = min(width, max_width)
    resized = cv2.resize(line, (width, height), interpolation=cv2.INTER_AREA)
    return (resized.astype(np.float32) / 255.0)[None, None]


# ------------------------------------------------------------------ model
def model_dirs():
    roots = [Path(__file__).resolve().parents[2]]
    if getattr(sys, "frozen", False):
        roots.insert(0, Path(sys.executable).resolve().parent)
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            roots.insert(1, Path(meipass))
    dirs = [Path(os.environ["OMR_IMPRINT_MODEL_DIR"])] if os.environ.get("OMR_IMPRINT_MODEL_DIR") else []
    for root in roots:
        dirs += [root / "models" / "imprint", root / "packaging" / "models" / "imprint"]
    return dirs


def find_model(name):
    name = name or DEFAULT_MODEL
    path = Path(name)
    if path.suffix == ".onnx":
        return path if path.is_file() else None
    for directory in model_dirs():
        candidate = directory / f"{name}.onnx"
        if candidate.is_file():
            return candidate
    return None


class ImprintModel:
    """A CTC line recogniser: input "x" [N, 1, 32, W], output [N, T, 1 + charset]
    with class 0 the CTC blank and class i the charset's character i - 1."""

    def __init__(self, path):
        import onnxruntime as ort

        self.path = Path(path)
        meta = json.loads(self.path.with_suffix(".json").read_text())
        self.meta = meta
        self.charset = meta.get("charset", DEFAULT_CHARSET)
        self.blank = int(meta.get("blank", 0))
        self.height = int(meta.get("input_height", INPUT_HEIGHT))
        self.max_width = meta.get("max_width")
        self.direction = meta.get("direction", "horizontal")
        options = ort.SessionOptions()
        threads = int(os.environ.get("OMR_ONNX_THREADS", "0") or 0)
        if threads:
            options.intra_op_num_threads = threads
            options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(
            str(self.path), options, providers=["CPUExecutionProvider"]
        )
        self.input_name = self.session.get_inputs()[0].name

    def read(self, line, allowed=None):
        """(text, per-character confidences) for one line crop."""
        logits = self.session.run(
            None, {self.input_name: model_input(line, self.height, self.max_width)}
        )[0][0]
        return ctc_greedy(logits, self.charset, self.blank, allowed)


def softmax(logits):
    shifted = logits - logits.max(axis=-1, keepdims=True)
    exp = np.exp(shifted)
    return exp / exp.sum(axis=-1, keepdims=True)


def ctc_greedy(logits, charset, blank=0, allowed=None):
    """Greedy CTC decode: (text, confidence per character = mean top
    probability over the frames of that character)."""
    probs = softmax(np.asarray(logits, dtype=np.float32))
    # Classes other than the blank are the charset, in order
    classes = [c for c in range(probs.shape[-1]) if c != blank]
    char_of = {c: charset[i] for i, c in enumerate(classes[: len(charset)])}
    if allowed:
        for c, ch in char_of.items():
            if ch not in allowed:
                probs[:, c] = 0
    best = probs.argmax(axis=-1)
    text, confidences, run, previous = [], [], [], blank
    for t, index in enumerate(best):
        if index != previous and run:
            confidences.append(float(np.mean(run)))
            run = []
        if index != blank:
            if index != previous:
                text.append(char_of.get(int(index), ""))
            run.append(float(probs[t, index]))
        previous = index
    if run:
        confidences.append(float(np.mean(run)))
    return "".join(text), confidences


def get_model(name):
    path = find_model(name)
    if path is None:
        return None
    key = str(path)
    with _LOCK:
        if key not in _MODELS:
            try:
                _MODELS[key] = ImprintModel(path)
            except Exception as error:
                logger.warning(f"Imprint model {path} did not load: {error}")
                _MODELS[key] = None
        return _MODELS[key]


def reset_cache():
    _MODELS.clear()


def installed_models():
    """Names of the imprint models found (each .onnx with its .json)."""
    names = set()
    for directory in model_dirs():
        if directory.is_dir():
            names |= {p.stem for p in directory.glob("*.onnx") if p.with_suffix(".json").is_file()}
    return sorted(names)


# ------------------------------------------------------------------ zone
def read_imprint_zone(zone, image, ocr_params=None):
    from src.readers import text_reader

    options = zone.options
    crop = zone.crop(image)
    if crop.ndim == 3:
        crop = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    model = get_model(options.get("model"))
    direction = options.get("direction") or (model.direction if model else "horizontal")
    strip = text_reader.rotate_crop(crop, direction)
    details = {"direction": direction}
    box = locate_line(strip, options.get("charHeight"))
    if box is None:
        return ZoneReadResult(zone.name, zone.type, "", 0.0, ["not_found"], details=details)
    details["line"] = list(box)
    line = cut_line(strip, box)
    whitelist = options.get("whitelist")
    if model is None:
        return read_without_model(zone, line, whitelist or DEFAULT_CHARSET, details)
    text, confidences = model.read(line, set(whitelist) if whitelist else None)
    details.update(
        engine="imprint",
        model=model.path.stem,
        char_confidences=[round(c, 3) for c in confidences],
    )
    confidence = float(min(confidences)) if confidences else 0.0
    flags = [] if text else ["empty"]
    return ZoneReadResult(
        zone.name, zone.type, text, confidence, flags, details=details, engine="imprint"
    )


def read_without_model(zone, line, whitelist, details):
    """Tesseract on the line with its dots joined; always reviewed."""
    from src.readers import ocr

    flags = ["no_imprint_model"]
    if not ocr.tesseract_available():
        return ZoneReadResult(
            zone.name, zone.type, "", 0.0, flags + ["engine_unavailable"], details=details
        )
    dot = max(int(round(line.shape[0] * 0.08)), 2)
    joined = cv2.morphologyEx(line, cv2.MORPH_OPEN, np.ones((dot, dot), np.uint8))
    text, confidence = ocr.recognize_text(
        ocr.prepare_for_ocr(joined), psm=7, whitelist=whitelist
    )
    return ZoneReadResult(
        zone.name, zone.type, text.replace(" ", ""), confidence, flags,
        details=details, engine="tesseract",
    )
