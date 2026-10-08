"""
Printed-text OCR using Tesseract's LSTM engine (OEM 1).

Tesseract is the standard open-source OCR engine; the zone is upscaled to a
text height Tesseract handles well and binarised before recognition. Word
confidences reported by Tesseract are averaged into the zone confidence, and
per-character confidences are kept in the zone details.

For throughput, the in-process tesserocr binding is used when installed (one
engine per thread, ~10 ms per zone); otherwise pytesseract runs the tesseract
executable per call, which is roughly 50x slower.

Models: ocr_params.tessdata picks the "best" (accurate, larger) or "fast"
traineddata set when the build bundled it (packaging/fetch_ocr_models.py puts
them in tessdata/best and tessdata/fast next to the app); otherwise whatever the
Tesseract install ships is used, as before.

The zone reader itself (text direction, engine choice, PaddleOCR fallback,
layout-mode retries and the second clean-up pass) is src/readers/text_reader.py;
read_ocr_zone below delegates to it.
"""

import glob
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

import cv2
import numpy as np

from src.logger import logger
from src.readers.base import ZoneReadResult

try:
    import tesserocr
except (ImportError, ValueError):  # pragma: no cover
    # ValueError: cysignals refuses to install handlers outside the main thread
    tesserocr = None

try:
    import pytesseract
except ImportError:  # pragma: no cover
    pytesseract = None

TARGET_TEXT_HEIGHT = 48
_THREAD_LOCAL = threading.local()
_WARNED = set()


def _system_tessdata():
    configured = os.environ.get("TESSDATA_PREFIX")
    if configured and glob.glob(os.path.join(configured, "*.traineddata")):
        return configured
    for pattern in (
        "/usr/share/tesseract-ocr/*/tessdata",
        "/usr/share/tessdata",
        "/usr/local/share/tessdata",
        "/opt/homebrew/share/tessdata",
    ):
        for path in sorted(glob.glob(pattern), reverse=True):
            if glob.glob(os.path.join(path, "*.traineddata")):
                return path
    return None


def bundled_tessdata_dirs(variant):
    """Candidate folders for a bundled "best" / "fast" traineddata set."""
    if variant not in ("best", "fast"):
        return []
    dirs = []
    env = os.environ.get(f"OMR_TESSDATA_{variant.upper()}")
    if env:
        dirs.append(Path(env))
    roots = [Path(__file__).resolve().parents[2]]
    if getattr(sys, "frozen", False):
        roots.insert(0, Path(sys.executable).resolve().parent)
        if getattr(sys, "_MEIPASS", None):
            roots.insert(1, Path(sys._MEIPASS))
    for root in roots:
        dirs += [root / "tessdata" / variant, root / "packaging" / "tessdata" / variant]
    return [d for d in dirs if d.is_dir()]


def _has_langs(directory, lang):
    return all(
        os.path.isfile(os.path.join(str(directory), f"{part}.traineddata"))
        for part in (lang or "eng").split("+")
    )


def find_tessdata(variant=None, lang=None):
    """
    The tessdata folder to use. With a variant ("best"/"fast") a bundled set
    holding every language in `lang` wins; otherwise the system install.
    """
    for directory in bundled_tessdata_dirs(variant):
        if lang is None or _has_langs(directory, lang):
            return str(directory)
    return _system_tessdata()


def installed_languages(variant=None):
    """Sorted traineddata language codes available (system + bundled)."""
    found = set()
    dirs = [_system_tessdata()] + [str(d) for d in bundled_tessdata_dirs("best")]
    dirs += [str(d) for d in bundled_tessdata_dirs("fast")]
    for directory in dirs:
        if directory:
            for path in glob.glob(os.path.join(directory, "*.traineddata")):
                found.add(os.path.basename(path)[: -len(".traineddata")])
    if not found and pytesseract is not None and shutil.which("tesseract"):
        try:
            found.update(pytesseract.get_languages(config=""))
        except Exception:  # pragma: no cover
            pass
    found.discard("osd")
    return sorted(found)


def tesseract_available():
    if tesserocr is not None and find_tessdata():
        return True
    if _capi_tessdata() is not None:
        return True
    return pytesseract is not None and shutil.which("tesseract") is not None


def _capi_tessdata(tessdata=None):
    """tessdata for the ctypes engine, or None when that engine can't load."""
    from src.readers import tess_capi

    if tess_capi.library() is None:
        return None
    path = tessdata or find_tessdata()
    if path is None:
        # A Windows Tesseract install keeps tessdata next to the DLL
        exe = shutil.which("tesseract")
        if exe and os.path.isdir(os.path.join(os.path.dirname(exe), "tessdata")):
            path = os.path.join(os.path.dirname(exe), "tessdata")
    return path


def _tesserocr_api(lang, psm, path=None, patterns_file=None):
    apis = getattr(_THREAD_LOCAL, "apis", None)
    if apis is None:
        apis = _THREAD_LOCAL.apis = {}
    path = path or find_tessdata()
    key = (path, lang, psm, patterns_file)
    if key not in apis:
        variables = {}
        if patterns_file:
            variables["user_patterns_file"] = patterns_file
        apis[key] = tesserocr.PyTessBaseAPI(
            path=path.rstrip("/\\") + os.sep,
            lang=lang,
            psm=psm,
            oem=tesserocr.OEM.LSTM_ONLY,
            variables=variables,
        )
    return apis[key]


def prepare_for_ocr(crop, expected_lines=1):
    if crop.ndim == 3:
        crop = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    h = crop.shape[0]
    line_height = max(h / max(expected_lines, 1), 1)
    scale = TARGET_TEXT_HEIGHT / line_height
    if scale > 1.2:
        crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    crop = cv2.GaussianBlur(crop, (3, 3), 0)
    _, binary = cv2.threshold(crop, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    # Tesseract expects dark text on a light background with a margin
    return cv2.copyMakeBorder(binary, 10, 10, 10, 10, cv2.BORDER_CONSTANT, value=255)


def prepare_for_ocr_cleanup(crop, expected_lines=1):
    """
    Second clean-up for low-confidence reads: denoise, then a local (adaptive)
    threshold, which copes with uneven lighting and smudges better than Otsu.
    """
    if crop.ndim == 3:
        crop = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    h = crop.shape[0]
    line_height = max(h / max(expected_lines, 1), 1)
    scale = TARGET_TEXT_HEIGHT / line_height
    if scale > 1.2:
        crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    crop = cv2.fastNlMeansDenoising(crop, None, 12, 7, 21)
    block = int(TARGET_TEXT_HEIGHT * 0.75) | 1
    binary = cv2.adaptiveThreshold(
        crop, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, block, 12
    )
    binary = cv2.medianBlur(binary, 3)
    return cv2.copyMakeBorder(binary, 10, 10, 10, 10, cv2.BORDER_CONSTANT, value=255)


def tesseract_config(psm, whitelist=None, tessdata=None, patterns_file=None):
    config = f"--oem 1 --psm {psm}"
    # pytesseract splits the config string; paths with spaces cannot be quoted
    # portably (Windows), so they are skipped and the defaults apply
    if tessdata and " " not in tessdata:
        config = f"--tessdata-dir {tessdata} " + config
    if patterns_file and " " not in patterns_file:
        config += f" --user-patterns {patterns_file}"
    if whitelist:
        config += f" -c tessedit_char_whitelist={whitelist}"
    return config


# ---------------------------------------------------------------- user patterns
_PATTERN_FILES = {}
_CLASS_TOKENS = {
    "0-9": r"\d",
    "A-Z": r"\A",
    "a-z": r"\a",
    "A-Za-z": r"\c",
    "a-zA-Z": r"\c",
    "A-Za-z0-9": r"\n",
    "0-9A-Za-z": r"\n",
    "a-zA-Z0-9": r"\n",
    "0-9A-Z": None,
    "A-Z0-9": None,
}
MAX_USER_PATTERNS = 16


def regex_to_user_patterns(regex):
    """
    Convert a simple field regex (e.g. "^[0-9]{7}$", "\\d{2}[A-Z]\\d+") to
    Tesseract user patterns (\\d digit, \\A upper, \\a lower, \\c letter,
    \\n alphanumeric, \\* repeat). Returns None for regexes it cannot express.
    """
    if not regex:
        return None
    regex = regex.strip()
    if regex.startswith("^"):
        regex = regex[1:]
    if regex.endswith("$") and not regex.endswith("\\$"):
        regex = regex[:-1]
    if "(" in regex or "|" in regex:
        return None
    tokens, i = [], 0  # each token: [pattern string, min, max or None]
    while i < len(regex):
        c = regex[i]
        if c == "\\" and i + 1 < len(regex):
            n = regex[i + 1]
            if n == "d":
                tokens.append([r"\d", 1, 1])
            elif n in ".-+/:#()[]{}*?\\":
                tokens.append(["\\\\" if n == "\\" else n, 1, 1])
            else:
                return None
            i += 2
        elif c == "[":
            end = regex.find("]", i)
            if end < 0:
                return None
            body = regex[i + 1 : end]
            token = _CLASS_TOKENS.get(body)
            if token is None:
                return None
            tokens.append([token, 1, 1])
            i = end + 1
        elif c in "{+*?":
            if not tokens or tokens[-1][1] != 1 or tokens[-1][2] != 1:
                return None
            if c == "{":
                end = regex.find("}", i)
                if end < 0:
                    return None
                parts = regex[i + 1 : end].split(",")
                try:
                    low = int(parts[0])
                    high = int(parts[1]) if len(parts) > 1 and parts[1] else (low if len(parts) == 1 else None)
                except ValueError:
                    return None
                tokens[-1][1], tokens[-1][2] = low, high
                i = end + 1
            else:
                tokens[-1][1], tokens[-1][2] = {"+": (1, None), "*": (0, None), "?": (0, 1)}[c]
                i += 1
        elif c in ".^$|)":
            return None
        else:
            tokens.append(["\\\\" if c == "\\" else c, 1, 1])
            i += 1
    patterns = [""]
    for token, low, high in tokens:
        if high is None:
            # Tesseract's \* repeats the previous item zero or more times
            if low == 0:
                options = ["", token + r"\*"]
            else:
                options = [token * low + r"\*"]
        else:
            options = [token * count for count in range(low, high + 1)]
        patterns = [p + o for p in patterns for o in options]
        if len(patterns) > MAX_USER_PATTERNS:
            return None
    patterns = [p for p in patterns if p]
    return patterns or None


def user_patterns_file(patterns):
    """A temp file with the patterns (one per line), reused for the same set."""
    if not patterns:
        return None
    key = tuple(patterns)
    path = _PATTERN_FILES.get(key)
    if path and os.path.isfile(path):
        return path
    handle, path = tempfile.mkstemp(prefix="omr_patterns_", suffix=".txt")
    with os.fdopen(handle, "w", encoding="utf-8") as out:
        out.write("\n".join(patterns) + "\n")
    _PATTERN_FILES[key] = path
    return path


# ------------------------------------------------------------------ recognition
def recognize_text(image, psm=7, whitelist=None, lang="eng"):
    """Return (text, confidence in [0, 1]) for an already-prepared image."""
    text, confidence, _ = recognize_text_detailed(image, psm, whitelist, lang)
    return text, confidence


def recognize_text_detailed(
    image, psm=7, whitelist=None, lang="eng", tessdata=None, patterns_file=None
):
    """(text, confidence in [0, 1], per-character confidences in [0, 1])."""
    if tesserocr is not None and find_tessdata():
        try:
            return _recognize_in_process(
                image, psm, whitelist, lang, tessdata, patterns_file
            )
        except RuntimeError as error:  # e.g. a language missing from tessdata
            if pytesseract is None:
                raise
            if "tesserocr" not in _WARNED:
                _WARNED.add("tesserocr")
                logger.warning(f"tesserocr failed ({error}); using pytesseract")
    elif ("capi", lang, tessdata) not in _WARNED:
        capi_path = _capi_tessdata(tessdata)
        if capi_path is not None:
            from src.readers import tess_capi

            try:
                return tess_capi.recognize(
                    image, psm, whitelist, lang, capi_path, patterns_file
                )
            except Exception as error:  # e.g. a language missing from tessdata
                if pytesseract is None:
                    raise
                # Only this language and data folder fall back to the executable
                _WARNED.add(("capi", lang, tessdata))
                logger.warning(f"in-process Tesseract failed ({error}); using pytesseract")
    data = pytesseract.image_to_data(
        image,
        lang=lang,
        config=tesseract_config(psm, whitelist, tessdata, patterns_file),
        output_type=pytesseract.Output.DICT,
    )
    words, confidences, chars = [], [], []
    for text, conf in zip(data["text"], data["conf"]):
        conf = float(conf)
        if text.strip() and conf >= 0:
            words.append(text.strip())
            confidences.append(conf / 100.0)
            # The executable reports word confidences only
            chars.extend([conf / 100.0] * len(text.strip()))
    if not words:
        return "", 0.0, []
    return " ".join(words), float(np.mean(confidences)), chars


def _utf8(item, level):
    # tesserocr raises RuntimeError("No text returned") on an empty page
    try:
        return item.GetUTF8Text(level)
    except RuntimeError:
        return ""


def _recognize_in_process(image, psm, whitelist, lang, tessdata=None, patterns_file=None):
    from PIL import Image

    api = _tesserocr_api(lang, psm, tessdata, patterns_file)
    api.SetVariable("tessedit_char_whitelist", whitelist or "")
    api.SetImage(Image.fromarray(image))
    api.Recognize()
    words, confidences = [], []
    iterator = api.GetIterator()
    level = tesserocr.RIL.WORD
    for word in tesserocr.iterate_level(iterator, level):
        text = _utf8(word, level)
        if text and text.strip():
            words.append(text.strip())
            confidences.append(word.Confidence(level) / 100.0)
    if not words:
        return "", 0.0, []
    chars = []
    symbol = tesserocr.RIL.SYMBOL
    for item in tesserocr.iterate_level(api.GetIterator(), symbol):
        text = _utf8(item, symbol)
        if text and text.strip():
            chars.append(item.Confidence(symbol) / 100.0)
    return " ".join(words), float(np.mean(confidences)), chars


def read_ocr_zone(zone, image, ocr_params=None):
    from src.readers.text_reader import read_text_zone

    return read_text_zone(zone, image, ocr_params)


def legacy_read_ocr_zone(zone, image):
    """Today's single-pass Tesseract read (kept for comparison and tests)."""
    if not tesseract_available():
        return ZoneReadResult(zone.name, zone.type, "", 0.0, ["engine_unavailable"])
    options = zone.options
    prepared = prepare_for_ocr(zone.crop(image, padding=2))
    text, confidence = recognize_text(
        prepared,
        psm=options.get("psm", 7),
        whitelist=options.get("whitelist"),
        lang=options.get("lang", "eng"),
    )
    flags = [] if text else ["empty"]
    return ZoneReadResult(zone.name, zone.type, text, confidence, flags)


__all__ = [
    "find_tessdata",
    "installed_languages",
    "prepare_for_ocr",
    "prepare_for_ocr_cleanup",
    "recognize_text",
    "recognize_text_detailed",
    "regex_to_user_patterns",
    "read_ocr_zone",
    "tesseract_available",
]
