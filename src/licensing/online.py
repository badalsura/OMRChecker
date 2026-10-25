"""
Optional online check-in for remote revocation — DORMANT by default.

The offline license file (signed, verified against the embedded public key) is
the whole licensing system today. It cannot be revoked after issue, because the
app never contacts anyone. This module adds the *missing* piece for later: when
you host a licensing server and point the app at it, a valid license is also
checked against the server's revocation list on a periodic check-in.

It is dormant until ``OMR_LICENSE_SERVER`` is set, so nothing here touches the
network in the current offline product. When it is set:

* The app POSTs ``{license_id, machine, product}`` to ``<server>/check`` at most
  once per ``OMR_LICENSE_CHECK_INTERVAL_HOURS`` (default 24).
* The server replies ``{"revoked": true|false}``. A revoked license is refused.
* If the server cannot be reached, the app keeps working within a **grace
  window** of ``OMR_LICENSE_GRACE_DAYS`` (default 14) since the last successful
  check, so exam centres with flaky connectivity are never locked out by a blip.
  Past the grace window with no contact, it falls back to the offline result
  (still valid) rather than hard-failing — tighten this when you go online-only.

The check-in result is cached in the same ``.license_state`` file the
rollback guard uses. Network errors never raise; they degrade to grace.

Design note: the revocation list the server serves is exactly the list the
vendor maintains with ``tools/license_tool.py revoke`` / the web admin, so the
offline and online stories share one source of truth.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Optional

from src.licensing import paths
from src.licensing.verify import LicenseStatus, Reason

_DEFAULT_INTERVAL_HOURS = 24
_DEFAULT_GRACE_DAYS = 14
_TIMEOUT_SECONDS = 5


def server_url() -> Optional[str]:
    url = os.environ.get("OMR_LICENSE_SERVER", "").strip()
    return url.rstrip("/") or None


def is_configured() -> bool:
    """True only when a licensing server is configured. False keeps this whole
    module inert (the offline product's behaviour)."""
    return server_url() is not None


def _hours(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _check_interval_seconds() -> float:
    return _hours("OMR_LICENSE_CHECK_INTERVAL_HOURS", _DEFAULT_INTERVAL_HOURS) * 3600


def _grace_seconds() -> float:
    return _hours("OMR_LICENSE_GRACE_DAYS", _DEFAULT_GRACE_DAYS) * 24 * 3600


def _read_state() -> dict:
    try:
        return json.loads(paths.state_file_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_state(update: dict) -> None:
    try:
        state = _read_state()
        state.update(update)
        paths.state_file_path().write_text(json.dumps(state), encoding="utf-8")
    except Exception:
        pass


def _server_says_revoked(license_id: str, machine: Optional[str]) -> Optional[bool]:
    """Ask the server whether this license is revoked. Returns True/False, or
    None if the server could not be reached or gave an unusable answer."""
    url = server_url()
    if not url:
        return None
    body = json.dumps(
        {"license_id": license_id, "machine": machine, "product": "OMRChecker"}
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{url}/check",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            data = json.loads(response.read().decode("utf-8"))
        return bool(data.get("revoked", False))
    except (urllib.error.URLError, ValueError, OSError, TimeoutError):
        return None


def apply_revocation(status: LicenseStatus) -> LicenseStatus:
    """Given a locally-valid status, apply the online revocation check. Called
    only when ``is_configured()``. Never raises; on no contact it honours the
    grace window and otherwise returns the input status unchanged."""
    if not status.valid or status.payload is None:
        return status

    license_id = status.payload.license_id
    machine = status.payload.machine
    now = time.time()
    state = _read_state()
    last_ok = float(state.get("online_ok_at", 0) or 0)
    known_revoked = bool(state.get("online_revoked", False))

    if known_revoked:
        # A prior check already saw this license revoked; stay revoked until a
        # later check clears it.
        revoked_now = _server_says_revoked(license_id, machine)
        if revoked_now is False:
            _write_state({"online_revoked": False, "online_ok_at": now})
            return status
        return LicenseStatus(False, Reason.REVOKED, status.payload)

    due = (now - last_ok) >= _check_interval_seconds()
    if not due:
        return status  # checked recently; trust the local result

    revoked_now = _server_says_revoked(license_id, machine)
    if revoked_now is True:
        _write_state({"online_revoked": True, "online_ok_at": now})
        return LicenseStatus(False, Reason.REVOKED, status.payload)
    if revoked_now is False:
        _write_state({"online_revoked": False, "online_ok_at": now})
        return status

    # No contact. Keep working within the grace window since the last success.
    if last_ok and (now - last_ok) > _grace_seconds():
        # Past grace: do not hard-fail on connectivity alone (an offline exam
        # centre must keep working); fall back to the valid offline result.
        # Change this to a refusal if you move to strict online-only licensing.
        return status
    return status
