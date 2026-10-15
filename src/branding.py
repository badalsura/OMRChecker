"""
The product's name and icon, kept in one place: branding/branding.json and
branding/icon.png. Change those (then run packaging/apply_branding.py for the
exe and installer icon) to rebrand; nothing else needs editing.
"""

import json
import sys
from pathlib import Path

DEFAULTS = {"name": "UNIC OMR Scanner", "publisher": "UNIC", "icon": "icon.png"}


def _branding_dir():
    # A PyInstaller build unpacks data files next to the program (_MEIPASS)
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    return base / "branding"


BRANDING_DIR = _branding_dir()


def load_branding():
    """branding.json merged over the defaults (a broken file keeps the defaults)."""
    brand = dict(DEFAULTS)
    try:
        data = json.loads((BRANDING_DIR / "branding.json").read_text(encoding="utf-8"))
        brand.update({k: v for k, v in data.items() if isinstance(v, str) and v.strip()})
    except (OSError, ValueError):
        pass
    return brand


def icon_path():
    path = BRANDING_DIR / load_branding()["icon"]
    return path if path.is_file() else None
