"""
One OCR interface over several engines (Tesseract, PaddleOCR on ONNX Runtime).

Per zone (template.json zone options, all optional):

- direction: "horizontal" (default) | "rot90cw" | "rot90ccw" | "rot180" | "auto".
  The crop is turned before recognition: "rot90cw" for vertical text reading
  bottom to top, "rot90ccw" for top to bottom. "auto" reads all four and keeps
  the valid read with the highest confidence.
- engine: "default" (ocr_params.default_engine) | "tesseract" | "paddle".
- fallbackEngine: "default" (ocr_params.fallback_engine) | "none" | "tesseract" | "paddle".
  Used when the first read is empty, below minConfidence or fails the zone
  pattern; the better valid read wins, and two different non-empty reads send
  the zone to review (flag "engine_disagree") when disagree_to_review is on.
- psmRetry: retry other Tesseract layout modes on an empty / invalid read
  (default ocr_params.multi_psm).
- userPatterns: Tesseract user patterns (list); by default derived from the
  zone "pattern" when ocr_params.user_patterns is on.
- minCharConfidence: flag "low_char_confidence" (review) when any character is
  below it (default ocr_params.min_char_confidence; 0 = off).

An engine that is not available (PaddleOCR models missing, onnxruntime failing
to load on Windows 7, no Tesseract) is replaced by the other one; the zone
details say so.
"""

import re

import cv2

from src.readers.base import ZoneReadResult
from src.readers.ocr_build import build_defaults

DIRECTIONS = ("horizontal", "rot90cw", "rot90ccw", "rot180")
ENGINES = ("tesseract", "paddle")
PSM_RETRY_ORDER = (7, 8, 13, 6, 11)

OCR_DEFAULTS = {
    "default_engine": "tesseract",
    "fallback_engine": "none",
    "paddle_det_model": "mobile",
    "paddle_rec_model": "mobile",
    "paddle_lang": "en",
    "paddle_model_dir": None,
    "tessdata": "best",
    "langs": ["eng"],
    "multi_psm": True,
    "cleanup_pass": True,
    "disagree_to_review": True,
    "user_patterns": True,
    "min_char_confidence": 0,
    "icr_second_reader": "auto",
    "icr_engine": "auto",
}

def ocr_settings(params=None):
    settings = dict(OCR_DEFAULTS)
    for key, value in build_defaults().items():
        if key in OCR_DEFAULTS:
            settings[key] = value
    for key, value in (params or {}).items():
        settings[key] = value
    return settings


# -------------------------------------------------------------------- rotation
def rotate_crop(crop, direction):
    """Turn a zone crop so its text reads left to right."""
    if direction == "rot90cw":
        return cv2.rotate(crop, cv2.ROTATE_90_CLOCKWISE)
    if direction == "rot90ccw":
        return cv2.rotate(crop, cv2.ROTATE_90_COUNTERCLOCKWISE)
    if direction == "rot180":
        return cv2.rotate(crop, cv2.ROTATE_180)
    return crop


def zone_directions(zone):
    direction = zone.options.get("direction", "horizontal") or "horizontal"
    if direction == "auto":
        return list(DIRECTIONS)
    return [direction if direction in DIRECTIONS else "horizontal"]


# -------------------------------------------------------------------- engines
class TextRead:
    __slots__ = (
        "text", "confidence", "chars", "engine", "valid", "details", "direction",
    )

    def __init__(self, text, confidence, chars=None, engine=None, details=None):
        self.text = text or ""
        self.confidence = float(confidence or 0.0)
        self.chars = list(chars or [])
        self.engine = engine
        self.valid = False
        self.details = details or {}
        self.direction = "horizontal"

    def summary(self):
        return {
            "engine": self.engine,
            "text": self.text,
            "confidence": round(self.confidence, 4),
            "valid": self.valid,
            "direction": self.direction,
        }


def is_valid(text, zone, min_confidence=None, confidence=None):
    """Non-empty, matching the zone pattern and (if given) confident enough."""
    if not text:
        return False
    pattern = zone.options.get("pattern")
    if pattern:
        try:
            if not re.fullmatch(pattern, text):
                return False
        except re.error:
            pass
    whitelist = zone.options.get("whitelist")
    if whitelist and any(c not in whitelist for c in text if c != " "):
        return False
    if min_confidence is not None and confidence is not None:
        return confidence >= min_confidence
    return True


def better(a, b):
    """The better of two reads: valid beats invalid, then higher confidence."""
    if a is None:
        return b
    if b is None:
        return a
    key_a = (a.valid, bool(a.text), a.confidence)
    key_b = (b.valid, bool(b.text), b.confidence)
    return b if key_b > key_a else a


def paddle_engine(settings, lang=None):
    from src.readers import paddle_ocr

    paddle_lang = settings.get("paddle_lang") or "en"
    if lang:
        first = lang.split("+")[0]
        if first in ("hin", "mar", "nep", "san"):
            paddle_lang = "devanagari"
    return paddle_ocr.get_engine(
        settings.get("paddle_det_model", "mobile"),
        settings.get("paddle_rec_model", "mobile"),
        paddle_lang,
        settings.get("paddle_model_dir"),
    )


def engine_available(name, settings, lang=None):
    from src.readers import ocr

    if name == "tesseract":
        return ocr.tesseract_available()
    if name == "paddle":
        return paddle_engine(settings, lang).available
    return False


def zone_lang(zone, settings):
    lang = zone.options.get("lang")
    if lang:
        return lang
    langs = settings.get("langs") or ["eng"]
    return "+".join(langs) if isinstance(langs, (list, tuple)) else str(langs)


def tesseract_read(crop, zone, settings, min_confidence, expected_lines=1):
    """Tesseract with user patterns, layout-mode retries and a second clean-up."""
    from src.readers import ocr

    options = zone.options
    lang = zone_lang(zone, settings)
    tessdata = ocr.find_tessdata(settings.get("tessdata"), lang)
    whitelist = options.get("whitelist")
    patterns = options.get("userPatterns")
    if patterns is None and settings.get("user_patterns") and options.get("pattern"):
        patterns = ocr.regex_to_user_patterns(options.get("pattern"))
    patterns_file = ocr.user_patterns_file(patterns) if patterns else None
    psm = options.get("psm", 7)
    retry = options.get("psmRetry", settings.get("multi_psm", True))

    def run(image, mode, cleanup=False):
        text, confidence, chars = ocr.recognize_text_detailed(
            image, mode, whitelist, lang, tessdata, patterns_file
        )
        read = TextRead(text, confidence, chars, "tesseract", {"psm": mode})
        if cleanup:
            read.details["cleanup"] = True
        read.valid = is_valid(text, zone)
        return read

    prepared = ocr.prepare_for_ocr(crop, expected_lines)
    best = run(prepared, psm)
    tried = [psm]
    good = lambda r: r.valid and r.confidence >= min_confidence  # noqa: E731
    if not best.valid and retry:
        for mode in PSM_RETRY_ORDER:
            if mode in tried:
                continue
            tried.append(mode)
            best = better(best, run(prepared, mode))
            if good(best):
                break
    if not good(best) and settings.get("cleanup_pass", True):
        cleaned = ocr.prepare_for_ocr_cleanup(crop, expected_lines)
        best = better(best, run(cleaned, best.details.get("psm", psm), cleanup=True))
    best.details["psm_tried"] = tried
    if tessdata:
        best.details["tessdata"] = settings.get("tessdata")
    if patterns:
        best.details["user_patterns"] = list(patterns)
    return best


def paddle_read(crop, zone, settings):
    whitelist = zone.options.get("whitelist")
    engine = paddle_engine(settings, zone.options.get("lang"))
    single_line = zone.options.get("psm", 7) in (7, 8, 13)
    text, confidence, chars, details = engine.read(
        crop, set(whitelist) if whitelist else None, single_line
    )
    read = TextRead(text, confidence, chars, "paddle", details)
    read.valid = is_valid(text, zone)
    return read


def engine_read(name, crop, zone, settings, min_confidence):
    if name == "paddle":
        return paddle_read(crop, zone, settings)
    return tesseract_read(crop, zone, settings, min_confidence)


def choose_engines(zone, settings):
    """(primary, fallback or None, notes) after availability checks."""
    lang = zone.options.get("lang")
    notes = {}
    primary = zone.options.get("engine", "default") or "default"
    if primary == "default":
        primary = settings.get("default_engine", "tesseract")
    if primary not in ENGINES:
        primary = "tesseract"
    fallback = zone.options.get("fallbackEngine", "default") or "default"
    if fallback == "default":
        fallback = settings.get("fallback_engine", "none")
    if fallback not in ENGINES or fallback == primary:
        fallback = None
    if not engine_available(primary, settings, lang):
        other = "paddle" if primary == "tesseract" else "tesseract"
        notes["engine_unavailable"] = primary
        if primary == "paddle":
            notes["reason"] = paddle_engine(settings, lang).unavailable_reason
        if engine_available(other, settings, lang):
            primary, fallback = other, None
        else:
            return None, None, notes
    if fallback and not engine_available(fallback, settings, lang):
        notes["fallback_unavailable"] = fallback
        fallback = None
    return primary, fallback, notes


def normalise(text):
    return re.sub(r"\s+", "", text or "").upper()


def read_with_engines(crop, zone, settings, primary, fallback, min_confidence):
    """Primary read, then the fallback engine when the primary is not good enough."""
    first = engine_read(primary, crop, zone, settings, min_confidence)
    reads = [first]
    best, disagree = first, False
    if fallback and (not first.valid or first.confidence < min_confidence):
        second = engine_read(fallback, crop, zone, settings, min_confidence)
        reads.append(second)
        best = better(first, second)
        disagree = bool(first.text and second.text) and normalise(
            first.text
        ) != normalise(second.text)
    return best, reads, disagree


def read_text_crop(zone, crop, ocr_params=None, reader=None):
    """
    Read text from a zone crop with direction handling and engine fallback.
    reader(rotated_crop) -> (best TextRead, reads, disagree) can replace the
    engines (tests, ICR). Returns a ZoneReadResult.
    """
    settings = ocr_settings(ocr_params)
    min_confidence = zone.options.get("minConfidence", 0.6)
    details = {}
    if reader is None:
        primary, fallback, notes = choose_engines(zone, settings)
        details.update(notes)
        if primary is None:
            return ZoneReadResult(
                zone.name, zone.type, "", 0.0, ["engine_unavailable"], details=details
            )

        def reader(image):
            return read_with_engines(
                image, zone, settings, primary, fallback, min_confidence
            )

    best, all_reads, disagree_any = None, [], False
    for direction in zone_directions(zone):
        best_here, reads, disagree = reader(rotate_crop(crop, direction))
        for read in reads:
            read.direction = direction
        best_here.direction = direction
        all_reads.extend(reads)
        if best is None or better(best, best_here) is best_here:
            best, disagree_any = best_here, disagree
    flags = [] if best.text else ["empty"]
    if disagree_any and settings.get("disagree_to_review", True):
        flags.append("engine_disagree")
    min_char = zone.options.get("minCharConfidence", settings.get("min_char_confidence") or 0)
    if min_char and best.chars and min(best.chars) < min_char:
        flags.append("low_char_confidence")
    details.update(best.details)
    details["direction"] = best.direction
    if best.chars:
        details["char_confidences"] = [round(c, 3) for c in best.chars]
    if len(all_reads) > 1:
        details["reads"] = [r.summary() for r in all_reads]
    return ZoneReadResult(
        zone.name,
        zone.type,
        best.text,
        best.confidence,
        flags,
        details=details,
        engine=best.engine,
    )


def read_text_zone(zone, image, ocr_params=None):
    return read_text_crop(zone, zone.crop(image, padding=2), ocr_params)


def ocr_capabilities(ocr_params=None):
    """What the GUI needs: engines, their availability and languages."""
    from src.readers import ocr, paddle_ocr

    settings = ocr_settings(ocr_params)
    paddle = paddle_engine(settings)
    paddle_ok = paddle.available
    return {
        "engines": {
            "tesseract": ocr.tesseract_available(),
            "paddle": paddle_ok,
        },
        "paddle_reason": None if paddle_ok else paddle.unavailable_reason,
        "onnxruntime": paddle_ocr.onnxruntime_module() is not None,
        "tesseract_languages": ocr.installed_languages(),
        "paddle_languages": sorted(
            {k[2] for k in paddle_ocr.MODEL_FILES if k[0] == "rec"}
        ),
        "directions": list(DIRECTIONS) + ["auto"],
        "defaults": {k: settings[k] for k in OCR_DEFAULTS},
    }


