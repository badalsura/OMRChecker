"""
The one licensing layer every entry point uses.

    from src.licensing import gate

    gate.require_license()                 # raises LicenseError if not licensed
    status = gate.current_status()         # never raises; for the UI / API
    gate.install_license_bytes(data)       # activate: validate + save a license

`require_license` is cheap to call repeatedly: the verified result is cached
for the process and re-checked periodically, so a license that expires while a
long-running server is up is caught without re-reading the file on every
request. Set OMR_LICENSE_DISABLE=1 to turn the gate off for local development
and source runs (never set in a shipped build).
"""

from __future__ import annotations

import os
import threading
import time
from typing import Optional

from src.licensing.model import FEATURE_CORE
from src.licensing.verify import (
    LicenseError,
    LicenseStatus,
    Reason,
    current_machine_summary,
    verify_license_file,
)

# How long a verified status is trusted before it is checked again.
_CACHE_SECONDS = 300

_lock = threading.Lock()
_cache: dict = {}  # feature -> (checked_at_epoch, LicenseStatus)


def _disabled() -> bool:
    return os.environ.get("OMR_LICENSE_DISABLE", "") not in ("", "0", "false", "False")


def _disabled_status() -> LicenseStatus:
    return LicenseStatus(
        True, Reason.VALID, None, detail="Licensing disabled for development."
    )


def current_status(
    feature: str = FEATURE_CORE, *, refresh: bool = False
) -> LicenseStatus:
    """The license status for a feature, cached per process. Never raises."""
    if _disabled():
        return _disabled_status()
    now = time.time()
    with _lock:
        cached = _cache.get(feature)
        if cached and not refresh and now - cached[0] < _CACHE_SECONDS:
            return cached[1]
    status = verify_license_file(feature=feature)
    with _lock:
        _cache[feature] = (now, status)
    return status


def is_licensed(feature: str = FEATURE_CORE) -> bool:
    return current_status(feature).valid


def require_license(feature: str = FEATURE_CORE) -> LicenseStatus:
    """Return the valid status, or raise LicenseError. Call this at the start
    of any major operation that the license must cover."""
    status = current_status(feature)
    if not status.valid:
        raise LicenseError(status)
    return status


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def activation_request() -> dict:
    """What the customer sends the vendor to get a license: this machine's
    fingerprint and human-readable context."""
    summary = current_machine_summary()
    summary["product"] = "OMRChecker"
    summary["request_version"] = 1
    return summary


def install_license_bytes(data: bytes, *, require_valid: bool = True) -> LicenseStatus:
    """Validate a license file's bytes and, if valid, install it as the active
    license. Returns the status; on failure nothing is written (unless
    require_valid is False, used only by tooling)."""
    import json

    from src.licensing import paths
    from src.licensing.verify import load_public_key, verify_license_data

    try:
        document = json.loads(data.decode("utf-8"))
    except Exception:
        return LicenseStatus(False, Reason.MALFORMED)

    status = verify_license_data(document, load_public_key(), check_clock=False)
    if require_valid and not status.valid:
        return status

    target = paths.license_file_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    clear_cache()
    # Re-read so the returned status reflects the installed file (incl. clock).
    return current_status(refresh=True)


def installed_license_path() -> Optional[str]:
    from src.licensing import paths

    path = paths.license_file_path()
    return str(path) if path.exists() else None
