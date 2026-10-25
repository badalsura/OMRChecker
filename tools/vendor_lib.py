#!/usr/bin/env python3
"""
Vendor-side licensing helpers: key generation, signing/issuing licenses, and
the revocation list.

This module holds everything that needs the RSA **private key**. It lives under
``tools/`` on purpose: the app bundles ``src/`` only (see ``packaging/omr.spec``
and ``packaging/build_nuitka.py``), so this code — and therefore any path that
could touch the private key — never ships to a customer. The CLI
(``tools/license_tool.py``) and the local web admin (``tools/license_admin``)
both import from here, so there is exactly one copy of the signing logic.

The signed bytes themselves are defined in ``src/licensing/model.py`` and shared
with the verifier, so the signer and the verifier can never drift apart.
"""

from __future__ import annotations

import base64
import json
import secrets

# Run from the repo root so ``src`` imports work when called as a script.
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.licensing.model import (  # noqa: E402
    FEATURE_CORE,
    LicensePayload,
    signing_bytes,
)

DEFAULT_BITS = 3072
PUBLIC_KEY_IN_APP = Path("src/licensing/public_key.pem")


# --------------------------------------------------------------------------- dates
def iso_utc(date_text: str) -> str:
    """Accept 'YYYY-MM-DD' or a full ISO timestamp; return ISO-8601 UTC."""
    text = date_text.strip()
    try:
        if len(text) == 10:
            dt = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        else:
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
    except ValueError as error:
        raise ValueError(f"Bad date '{date_text}': {error}")
    return dt.astimezone(timezone.utc).isoformat()


# --------------------------------------------------------------------------- keygen
def generate_keypair(
    out_dir: Path,
    *,
    bits: int = DEFAULT_BITS,
    passphrase: str = "",
    force: bool = False,
) -> tuple[Path, Path]:
    """Create an RSA key pair. Returns (private_path, public_path). Raises
    FileExistsError if the private key is already there and force is False."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    private_path = out_dir / "private_key.pem"
    public_path = out_dir / "public_key.pem"
    if private_path.exists() and not force:
        raise FileExistsError(f"{private_path} already exists")

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=bits)
    encryption = (
        serialization.BestAvailableEncryption(passphrase.encode("utf-8"))
        if passphrase
        else serialization.NoEncryption()
    )
    private_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=encryption,
        )
    )
    try:
        private_path.chmod(0o600)
    except OSError:
        pass
    public_path.write_bytes(public_key_pem(private_key))
    return private_path, public_path


def public_key_pem(private_key: rsa.RSAPrivateKey) -> bytes:
    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


# ---------------------------------------------------------------------------- keys
def load_private_key(path: Path, passphrase: Optional[str] = None) -> rsa.RSAPrivateKey:
    """Load a PEM private key. Pass the passphrase for an encrypted key; a
    TypeError from cryptography means the passphrase was required but missing."""
    data = Path(path).read_bytes()
    pw = passphrase.encode("utf-8") if passphrase else None
    key = serialization.load_pem_private_key(data, password=pw)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError("The key is not an RSA private key")
    return key


def private_key_is_encrypted(path: Path) -> bool:
    """True if the PEM private key needs a passphrase to open."""
    try:
        serialization.load_pem_private_key(Path(path).read_bytes(), password=None)
        return False
    except TypeError:
        return True
    except Exception:
        # Malformed or not a key: let the caller's load surface the real error.
        return False


# --------------------------------------------------------------------------- issue
def sign_payload(payload: dict, private_key: rsa.RSAPrivateKey) -> str:
    signature = private_key.sign(
        signing_bytes(payload),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("ascii")


def normalize_features(features) -> list[str]:
    """Accept a comma string or a list; always include the core feature."""
    if isinstance(features, str):
        items = [f.strip() for f in features.split(",") if f.strip()]
    else:
        items = [str(f).strip() for f in (features or []) if str(f).strip()]
    if FEATURE_CORE not in items:
        items.insert(0, FEATURE_CORE)
    return items


def issue_license(
    private_key: rsa.RSAPrivateKey,
    *,
    customer: str,
    machine: Optional[str] = None,
    floating: bool = False,
    expires: Optional[str] = None,
    features="core",
    seats: int = 1,
    edition: str = "standard",
    note: str = "",
    license_id: Optional[str] = None,
    key_id: str = "v1",
) -> dict:
    """Build and sign a license document. ``machine`` binds it to a fingerprint;
    pass floating=True for an unbound license (use sparingly)."""
    if not machine and not floating:
        raise ValueError("Provide a machine fingerprint or set floating=True")
    payload = LicensePayload(
        license_id=license_id or secrets.token_hex(8),
        customer=customer,
        issued_at=datetime.now(timezone.utc).isoformat(),
        expires_at=iso_utc(expires) if expires else None,
        machine=None if floating else machine,
        features=normalize_features(features),
        seats=int(seats),
        edition=edition,
        note=note or "",
    )
    return {
        "payload": payload.to_dict(),
        "signature": sign_payload(payload.to_dict(), private_key),
        "key_id": key_id,
    }


def machine_from_request(request: dict) -> str:
    """Pull the machine fingerprint out of a customer's activation request."""
    machine = request.get("fingerprint")
    if not machine:
        raise ValueError("The request has no 'fingerprint'")
    return machine


# -------------------------------------------------------------------------- inspect
def inspect_license(document: dict, pubkey_path: Optional[Path] = None):
    """Return (payload_dict, key_id, signature_ok). signature_ok is None if the
    public key could not be loaded."""
    payload = document.get("payload", {})
    key_id = document.get("key_id")
    path = Path(pubkey_path) if pubkey_path else PUBLIC_KEY_IN_APP
    try:
        from src.licensing.verify import load_public_key, verify_signature

        key = load_public_key(path)
        if key is None:
            return payload, key_id, None
        ok = verify_signature(payload, base64.b64decode(document["signature"]), key)
        return payload, key_id, ok
    except Exception:
        return payload, key_id, None


# ----------------------------------------------------------------------- revocation
# The revocation list is the seed of the future licensing server: today it is a
# local JSON file the admin maintains; later the same records are what an online
# server serves to apps that check in (see src/licensing/online.py).
def load_revocations(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {"revoked": {}}
    data.setdefault("revoked", {})
    return data


def save_revocations(path: Path, data: dict) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")


def revoke(path: Path, license_id: str, reason: str = "") -> dict:
    data = load_revocations(path)
    data["revoked"][license_id] = {
        "reason": reason,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    save_revocations(path, data)
    return data


def unrevoke(path: Path, license_id: str) -> dict:
    data = load_revocations(path)
    data["revoked"].pop(license_id, None)
    save_revocations(path, data)
    return data


def is_revoked(path: Path, license_id: str) -> bool:
    return license_id in load_revocations(path).get("revoked", {})
