"""
Verify a license file against the embedded public key and decide whether it
entitles this machine, right now, to a feature.

Signature scheme: RSA-PSS with SHA-256 (MGF1-SHA256), over signing_bytes() of
the payload. RSA because the key you issue licenses with stays on your machine
and nothing an attacker can read from the app lets them forge a signature.

The result is a LicenseStatus describing exactly why a license is or is not
valid, so the UI can tell the user what to do (missing, expired, wrong
machine, tampered) instead of a bare pass/fail.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from src.licensing import paths
from src.licensing.fingerprint import fingerprint_matches, machine_info
from src.licensing.model import LicensePayload, now_utc, parse_utc, signing_bytes

# If the system clock is ever found to be this far behind the last time the app
# ran, we treat it as a rollback attempt and refuse, rather than silently
# honouring an expired-then-rolled-back license. A couple of days of slack
# avoids false positives from minor clock corrections.
_ROLLBACK_SLACK_SECONDS = 2 * 24 * 3600


class Reason(str, Enum):
    VALID = "valid"
    NO_LICENSE = "no_license"
    NO_PUBLIC_KEY = "no_public_key"
    MALFORMED = "malformed"
    BAD_SIGNATURE = "bad_signature"
    EXPIRED = "expired"
    NOT_YET_VALID = "not_yet_valid"
    WRONG_MACHINE = "wrong_machine"
    CLOCK_ROLLBACK = "clock_rollback"
    FEATURE_MISSING = "feature_missing"
    REVOKED = "revoked"


_MESSAGES = {
    Reason.VALID: "License is valid.",
    Reason.NO_LICENSE: "No license is installed on this machine.",
    Reason.NO_PUBLIC_KEY: "The application is missing its license public key.",
    Reason.MALFORMED: "The license file is not readable.",
    Reason.BAD_SIGNATURE: "The license signature is invalid (file edited or not issued for this product).",
    Reason.EXPIRED: "The license has expired.",
    Reason.NOT_YET_VALID: "The license is not valid yet (issued for a later date).",
    Reason.WRONG_MACHINE: "The license was issued for a different machine.",
    Reason.CLOCK_ROLLBACK: "The system clock has been set back; please correct the date and time.",
    Reason.FEATURE_MISSING: "The license does not include this feature.",
    Reason.REVOKED: "This license has been revoked. Please contact your vendor.",
}


@dataclass
class LicenseStatus:
    valid: bool
    reason: Reason
    payload: Optional[LicensePayload] = None
    detail: str = ""

    @property
    def message(self) -> str:
        return self.detail or _MESSAGES.get(self.reason, "License error.")

    def to_public_dict(self) -> dict:
        """Safe to show the user / return from the API (no signature bytes)."""
        data = {
            "valid": self.valid,
            "reason": self.reason.value,
            "message": self.message,
        }
        if self.payload is not None:
            p = self.payload
            data["license"] = {
                "license_id": p.license_id,
                "customer": p.customer,
                "edition": p.edition,
                "features": list(p.features),
                "seats": p.seats,
                "issued_at": p.issued_at,
                "expires_at": p.expires_at,
                "machine_bound": p.machine is not None,
            }
        return data


class LicenseError(Exception):
    """Raised by the gate when a required license is not valid."""

    def __init__(self, status: LicenseStatus):
        super().__init__(status.message)
        self.status = status


def load_public_key(path: Optional[Path] = None) -> Optional[rsa.RSAPublicKey]:
    key_path = path or paths.public_key_path()
    try:
        data = Path(key_path).read_bytes()
    except OSError:
        return None
    try:
        key = serialization.load_pem_public_key(data)
    except Exception:
        return None
    return key if isinstance(key, rsa.RSAPublicKey) else None


def verify_signature(
    payload: dict, signature: bytes, public_key: rsa.RSAPublicKey
) -> bool:
    try:
        public_key.verify(
            signature,
            signing_bytes(payload),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return True
    except InvalidSignature:
        return False
    except Exception:
        return False


# --------------------------------------------------------------------------- state
def _read_state() -> dict:
    try:
        return json.loads(paths.state_file_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_last_seen(now_epoch: float) -> None:
    try:
        state = _read_state()
        state["last_seen"] = max(float(state.get("last_seen", 0)), now_epoch)
        paths.state_file_path().write_text(json.dumps(state), encoding="utf-8")
    except Exception:
        # The state file is a best-effort rollback speed bump; never let a
        # failure to write it block a legitimate user.
        pass


def _clock_rolled_back(now_epoch: float) -> bool:
    last_seen = float(_read_state().get("last_seen", 0))
    return last_seen > 0 and now_epoch < last_seen - _ROLLBACK_SLACK_SECONDS


# --------------------------------------------------------------------------- verify
def verify_license_data(
    data: dict,
    public_key: Optional[rsa.RSAPublicKey],
    *,
    feature: Optional[str] = None,
    check_machine: bool = True,
    check_clock: bool = True,
) -> LicenseStatus:
    """Verify an already-parsed license document. Pure except for the optional
    clock-state read/write, so it is easy to unit-test."""
    if public_key is None:
        return LicenseStatus(False, Reason.NO_PUBLIC_KEY)

    try:
        payload_dict = data["payload"]
        signature = base64.b64decode(data["signature"], validate=True)
        payload = LicensePayload.from_dict(payload_dict)
    except Exception:
        return LicenseStatus(False, Reason.MALFORMED)

    if not verify_signature(payload_dict, signature, public_key):
        return LicenseStatus(False, Reason.BAD_SIGNATURE, payload)

    # Signature is good from here on: the payload is authentic and unmodified.
    now = now_utc()
    now_epoch = time.time()

    if check_clock and _clock_rolled_back(now_epoch):
        return LicenseStatus(False, Reason.CLOCK_ROLLBACK, payload)

    issued = _safe_parse(payload.issued_at)
    if issued is not None and now < issued:
        # Small future-dating is fine; a license issued clearly in the future
        # means the clock is wrong or the file is bogus.
        if (issued - now).total_seconds() > _ROLLBACK_SLACK_SECONDS:
            return LicenseStatus(False, Reason.NOT_YET_VALID, payload)

    expiry = payload.expiry_datetime()
    if expiry is not None and now > expiry:
        return LicenseStatus(False, Reason.EXPIRED, payload)

    if check_machine and not fingerprint_matches(payload.machine):
        return LicenseStatus(False, Reason.WRONG_MACHINE, payload)

    if feature is not None and feature not in payload.features:
        return LicenseStatus(
            False,
            Reason.FEATURE_MISSING,
            payload,
            detail=f"The license does not include the '{feature}' feature.",
        )

    if check_clock:
        _write_last_seen(now_epoch)
    return LicenseStatus(True, Reason.VALID, payload)


def verify_license_file(
    path: Optional[Path] = None,
    *,
    feature: Optional[str] = None,
    public_key: Optional[rsa.RSAPublicKey] = None,
    check_machine: bool = True,
    check_clock: bool = True,
) -> LicenseStatus:
    """Load and verify the installed license file."""
    license_path = Path(path) if path else paths.license_file_path()
    if not license_path.exists():
        return LicenseStatus(False, Reason.NO_LICENSE)
    try:
        data = json.loads(license_path.read_text(encoding="utf-8"))
    except Exception:
        return LicenseStatus(False, Reason.MALFORMED)
    key = public_key if public_key is not None else load_public_key()
    return verify_license_data(
        data,
        key,
        feature=feature,
        check_machine=check_machine,
        check_clock=check_clock,
    )


# --------------------------------------------------------------------------- helpers
def _safe_parse(text: Optional[str]):
    try:
        return parse_utc(text) if text else None
    except Exception:
        return None


def current_machine_summary() -> dict:
    info = machine_info()
    return {
        "fingerprint": info.fingerprint,
        "hostname": info.hostname,
        "os": info.os,
        "strong": info.strong,
    }
