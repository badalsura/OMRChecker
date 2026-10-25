"""
The one licensing layer every entry point uses.

    from src.licensing import gate

    gate.require_license()                 # raises LicenseError if not licensed
    status = gate.current_status()         # never raises; for the UI / API
    gate.install_license_bytes(data)       # activate: validate + save a license

`require_license` is cheap to call repeatedly: the verified result is cached
for the process and re-checked periodically, so a license that expires while a
long-running server is up is caught without re-reading the file on every
request.

When the gate runs
------------------
Licensing gates the **packaged desktop app** (the thing you distribute) — not a
plain ``python main.py`` server run, which is meant to sit behind account/API-key
permissions you control, not a per-machine license file. So by default the gate
is active only in a frozen build (PyInstaller sets ``sys.frozen``, Nuitka
``__compiled__``). Two
env vars override that single decision in ``licensing_active()``:

* ``OMR_LICENSE_DISABLE=1`` — force the gate OFF (local dev, the test suite).
  Ignored by a packaged build.
* ``OMR_LICENSE_ENFORCE=1`` — force the gate ON from a source run (to test the
  gate, or to run a licensed server deployment deliberately).
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


def _env_true(name: str) -> bool:
    return os.environ.get(name, "") not in ("", "0", "false", "False")


def _disabled() -> bool:
    # Never in a packaged build: an environment variable must not unlock it
    from src.licensing import paths

    return _env_true("OMR_LICENSE_DISABLE") and not paths._frozen()


def licensing_active() -> bool:
    """Whether the license gate is enforced in this process. See the module
    docstring: active in a packaged desktop build, or when explicitly forced
    on; off for a plain source/server run or when explicitly disabled."""
    if _disabled():
        return False
    if _env_true("OMR_LICENSE_ENFORCE"):
        return True
    from src.licensing import paths

    return paths._frozen()


def _inactive_status() -> LicenseStatus:
    return LicenseStatus(
        True, Reason.VALID, None, detail="Licensing is not enforced in this mode."
    )


def current_status(
    feature: str = FEATURE_CORE, *, refresh: bool = False
) -> LicenseStatus:
    """The license status for a feature, cached per process. Never raises."""
    if not licensing_active():
        return _inactive_status()
    now = time.time()
    with _lock:
        cached = _cache.get(feature)
        if cached and not refresh and now - cached[0] < _CACHE_SECONDS:
            return cached[1]
    status = verify_license_file(feature=feature)
    # Dormant unless a licensing server is configured: when one is, a valid
    # license is additionally checked against remote revocation, within a grace
    # window so a dropped connection never locks out a legitimate user.
    if status.valid:
        from src.licensing import online

        if online.is_configured():
            status = online.apply_revocation(status)
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
