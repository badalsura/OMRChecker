"""
Where the license file, the public key and the small verification-state file
live, for both the packaged exe and a source checkout.

Packaged (frozen) layout, portable next to the exe::

    OMRChecker.exe
    license/
        public_key.pem     the vendor's public key (bundled read-only resource)
        license.omrlic     the customer's installed license (written at activation)
        .license_state     last-seen time etc. (anti clock-rollback)

If the exe folder is read-only the writable files fall back to
%LOCALAPPDATA%\\OMRChecker\\license (same fallback the data dir uses).

A packaged build verifies with the key compiled into it
(src/licensing/embedded_key.py, written at build time from public_key.pem);
OMR_LICENSE_PUBKEY and key files on disk count only in a source run, so a
user cannot swap in a key of their own.

Overrides, mainly for tests and for the vendor tool:

    OMR_LICENSE_PUBKEY   path to the public key to verify against (source runs)
    OMR_LICENSE_FILE     path to the installed license file
    OMR_LICENSE_DIR      base dir for the writable license/state files
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "OMRChecker"
LICENSE_FILENAME = "license.omrlic"
PUBLIC_KEY_FILENAME = "public_key.pem"
STATE_FILENAME = ".license_state"


def _frozen() -> bool:
    # PyInstaller sets sys.frozen; Nuitka marks every compiled module instead
    return bool(getattr(sys, "frozen", False)) or "__compiled__" in globals()


def _app_dir() -> Path:
    """Folder the user sees: next to the exe when frozen, else the repo root."""
    if _frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def _resource_dir() -> Path:
    """Folder with bundled read-only resources (onefile: the _MEIPASS temp dir)."""
    return Path(getattr(sys, "_MEIPASS", _app_dir()))


def _writable(folder: Path) -> bool:
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / ".write_probe"
        probe.write_text("ok", encoding="ascii")
        probe.unlink()
        return True
    except Exception:
        return False


def _localappdata_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_NAME / "license"


def writable_license_dir() -> Path:
    """Where the installed license and state are written. Prefers the folder
    next to the exe (portable); falls back to %LOCALAPPDATA% if it is
    read-only."""
    override = os.environ.get("OMR_LICENSE_DIR")
    if override:
        folder = Path(override)
        folder.mkdir(parents=True, exist_ok=True)
        return folder
    next_to_exe = _app_dir() / "license"
    if _writable(next_to_exe):
        return next_to_exe
    fallback = _localappdata_dir()
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def license_file_path() -> Path:
    """The installed license file (may not exist yet)."""
    override = os.environ.get("OMR_LICENSE_FILE")
    if override:
        return Path(override)
    return writable_license_dir() / LICENSE_FILENAME


def state_file_path() -> Path:
    return writable_license_dir() / STATE_FILENAME


def public_key_path() -> Path:
    """The public key the app verifies licenses with. An env override wins;
    otherwise the bundled resource, then the writable dir (so a key can be
    dropped in next to the exe)."""
    override = os.environ.get("OMR_LICENSE_PUBKEY")
    if override:
        return Path(override)
    candidates = [
        _resource_dir() / "license" / PUBLIC_KEY_FILENAME,
        _app_dir() / "license" / PUBLIC_KEY_FILENAME,
        Path(__file__).resolve().parent / PUBLIC_KEY_FILENAME,
    ]
    for path in candidates:
        if path.exists():
            return path
    # Return the first candidate so callers can report a clear "missing" error.
    return candidates[0]
