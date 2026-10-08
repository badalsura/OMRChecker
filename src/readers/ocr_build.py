"""
Build-time OCR choices (packaging/ocr_build.json, bundled as ocr_build.json at
the app root): the default engine, whether a fallback is used, model sizes and
languages. They become the config.json ocr_params defaults, so config.json
still overrides them per template without a rebuild.
"""

import json
import os
import sys
from pathlib import Path

_CACHE = None


def _candidates():
    env = os.environ.get("OMR_OCR_BUILD")
    if env:
        yield Path(env)
    roots = [Path(__file__).resolve().parents[2]]
    if getattr(sys, "frozen", False):
        roots.insert(0, Path(sys.executable).resolve().parent)
        if getattr(sys, "_MEIPASS", None):
            roots.insert(1, Path(sys._MEIPASS))
    for root in roots:
        yield root / "ocr_build.json"


def build_defaults():
    """{ocr_params key: value} chosen at build time ({} when there is no file)."""
    global _CACHE
    if _CACHE is None:
        _CACHE = {}
        for path in _candidates():
            if path.is_file():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    _CACHE = dict(data.get("ocr_params", {}))
                except Exception:  # a broken build file must not stop the app
                    _CACHE = {}
                break
    return _CACHE


def apply_build_defaults(config_defaults):
    """Overlay the build choices onto CONFIG_DEFAULTS.ocr_params (in place)."""
    params = config_defaults["ocr_params"]
    for key, value in build_defaults().items():
        if key in params:
            params[key] = value
    return config_defaults
