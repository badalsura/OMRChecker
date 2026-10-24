"""
The license payload and the exact bytes that get signed.

A license file is JSON with three top-level keys::

    {
      "payload":   { ... the entitlement, see LicensePayload ... },
      "signature": "<base64 RSA-PSS/SHA-256 over signing_bytes(payload)>",
      "key_id":    "v1"          # which embedded public key verifies it
    }

Only the vendor, who holds the RSA private key, can produce a `signature` that
the embedded public key accepts, so a license cannot be forged or a keygen
written without that key. The app ships the public key only.

The signed bytes are a *canonical* encoding of the payload (sorted keys, no
insignificant whitespace) behind a domain-separation prefix, so the signature
is stable regardless of how the JSON is re-serialised and cannot be replayed
against some other JSON document.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

# Bumped only on an incompatible change to the signed encoding. It is part of
# the signed bytes, so old apps reject newer formats rather than mis-reading
# them.
FORMAT_VERSION = 1

# Domain-separation prefix: signing these exact bytes means "this is an OMR
# license payload", so a signature can never be reused as if it signed some
# other kind of message.
SIGNING_PREFIX = b"omrchecker-license.v1\n"

# A machine-bound license may name several features; "core" is the base engine.
FEATURE_CORE = "core"


@dataclass
class LicensePayload:
    """The entitlement the vendor signs. Every field here is covered by the
    signature; changing any of them invalidates the license."""

    license_id: str
    customer: str
    issued_at: str  # ISO-8601 UTC, e.g. "2026-01-15T10:00:00+00:00"
    # None = perpetual. Otherwise ISO-8601 UTC; compared against the clock.
    expires_at: Optional[str] = None
    # None = not bound to a machine (floating). Otherwise the fingerprint from
    # src.licensing.fingerprint that this license is locked to.
    machine: Optional[str] = None
    features: List[str] = field(default_factory=lambda: [FEATURE_CORE])
    seats: int = 1
    edition: str = "standard"
    # Free-form, signed, for the vendor's own notes (order id, reseller…).
    note: str = ""
    format: int = FORMAT_VERSION

    def to_dict(self) -> dict:
        return {
            "format": self.format,
            "license_id": self.license_id,
            "customer": self.customer,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "machine": self.machine,
            "features": list(self.features),
            "seats": self.seats,
            "edition": self.edition,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "LicensePayload":
        return cls(
            license_id=str(data["license_id"]),
            customer=str(data["customer"]),
            issued_at=str(data["issued_at"]),
            expires_at=data.get("expires_at"),
            machine=data.get("machine"),
            features=list(data.get("features") or [FEATURE_CORE]),
            seats=int(data.get("seats", 1)),
            edition=str(data.get("edition", "standard")),
            note=str(data.get("note", "")),
            format=int(data.get("format", FORMAT_VERSION)),
        )

    def expiry_datetime(self) -> Optional[datetime]:
        return parse_utc(self.expires_at) if self.expires_at else None


def canonical_json(payload: dict) -> bytes:
    """Deterministic JSON: sorted keys, compact separators, UTF-8. Both the
    signer and the verifier must produce the same bytes for the same payload,
    so this is the single source of that encoding."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def signing_bytes(payload: dict) -> bytes:
    """The exact bytes the signature covers: prefix + canonical payload."""
    return SIGNING_PREFIX + canonical_json(payload)


def parse_utc(text: str) -> datetime:
    """Parse an ISO-8601 timestamp as timezone-aware UTC (accepts a trailing
    'Z')."""
    value = text.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
