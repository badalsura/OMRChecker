"""Which optional engines loaded in this install (plan item 19).

The portable Windows 7 build runs Python 3.8 with pinned libraries, and some
optional engines (ONNX Runtime, Tesseract, PaddleOCR, pyzbar) may not load
there. Every feature switches itself off when its engine is missing; this
module reports which ones are on, for the desktop ``--selftest`` and ``/health``.

Each entry is ``{"available": bool, "detail": str}``. Checks never raise.
"""

import importlib
import platform
import sys

# Modules E's PaddleOCR reader may live in, and the probe functions it may expose.
_PADDLE_MODULES = ("src.readers.paddle", "src.readers.paddle_ocr", "src.readers.paddleocr")
_PADDLE_PROBES = ("paddle_available", "available", "is_available")


def _entry(available, detail=""):
    return {"available": bool(available), "detail": str(detail)}


def _module_version(name):
    """Import ``name``; return (True, version) or (False, reason)."""
    try:
        module = importlib.import_module(name)
    except Exception as error:  # ImportError, or a DLL load failure on Windows 7
        return False, "unavailable ({}: {})".format(type(error).__name__, error)[:200]
    return True, str(getattr(module, "__version__", "") or "loaded")


def _onnxruntime():
    ok, detail = _module_version("onnxruntime")
    return _entry(ok, detail if ok else detail + "; bubbles use thresholding, ICR goes to review")


def _tesseract():
    try:
        from src.readers.ocr import tesseract_available

        ok = tesseract_available()
    except Exception as error:
        return _entry(False, "unavailable ({})".format(type(error).__name__))
    return _entry(ok, "found" if ok else "not found; OCR zones go to review")


def _paddle():
    for name in _PADDLE_MODULES:
        try:
            module = importlib.import_module(name)
        except ImportError:
            continue
        except Exception as error:
            return _entry(False, "unavailable ({})".format(type(error).__name__))
        for probe in _PADDLE_PROBES:
            check = getattr(module, probe, None)
            if callable(check):
                try:
                    ok = bool(check())
                except Exception as error:
                    return _entry(False, "unavailable ({})".format(type(error).__name__))
                return _entry(ok, "models loaded" if ok else "models or onnxruntime missing")
        return _entry(False, "reader present but no availability check")
    return _entry(False, "not included in this build")


def _barcodes():
    try:
        from src.readers.barcode import available_engines

        engines = available_engines()
    except Exception as error:
        return {"zxing": _entry(False, type(error).__name__), "pyzbar": _entry(False, "")}
    return {
        "zxing": _entry(engines.get("zxing"), "ZXing-C++" if engines.get("zxing") else "built-in decoder used"),
        "pyzbar": _entry(engines.get("pyzbar"), "optional, off unless barcode_params.pyzbar"),
    }


def engine_report():
    """Report which optional engines loaded. Cheap enough for /health."""
    report = {
        "python": platform.python_version(),
        "platform": sys.platform,
        "onnxruntime": _onnxruntime(),
        "tesseract": _tesseract(),
        "paddleocr": _paddle(),
    }
    report.update(_barcodes())
    for key, module in (("xlsx", "openpyxl"), ("pdf", "reportlab"), ("sql", "sqlalchemy")):
        ok, detail = _module_version(module)
        report[key] = _entry(ok, module + " " + detail if ok else detail)
    return report


def summary(report=None):
    """``{name: bool}`` for the engines in ``report`` (default: a fresh report)."""
    report = engine_report() if report is None else report
    return {k: v["available"] for k, v in report.items() if isinstance(v, dict)}


_CACHED = {}


def cached_summary():
    """``summary()`` computed once per process (for /health, which is polled)."""
    if "summary" not in _CACHED:
        _CACHED["summary"] = summary()
    return dict(_CACHED["summary"])
