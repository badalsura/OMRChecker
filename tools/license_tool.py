#!/usr/bin/env python3
"""
Vendor-side license tool for OMRChecker. Runs on YOUR machine only; it holds
the RSA private key that signs licenses. Never ship it or the private key.

    # 1. Once: make your key pair. Keep private_key.pem secret and backed up.
    python tools/license_tool.py keygen --out-dir ./license-keys

    #    Copy the public key into the app so builds can verify licenses:
    cp ./license-keys/public_key.pem src/licensing/public_key.pem

    # 2. A customer runs the app's "Activate" screen and sends you their
    #    request (JSON with their machine fingerprint). Issue a license:
    python tools/license_tool.py issue \
        --key ./license-keys/private_key.pem \
        --request customer_request.json \
        --customer "Acme Exam Center" \
        --expires 2027-01-01 --features core,ocr,api \
        --out acme.omrlic

    #    Send acme.omrlic back to the customer; they load it in Activate.

    # 3. Check what a license contains / that it verifies:
    python tools/license_tool.py inspect acme.omrlic --pubkey ./license-keys/public_key.pem

A license can also be issued without a request file:
    --machine <fingerprint>   bind to a fingerprint you were given another way
    --floating                not bound to any machine (use sparingly)
"""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

# Run from the repo root so `src` imports work.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding, rsa  # noqa: E402

from src.licensing.model import (  # noqa: E402
    FEATURE_CORE,
    LicensePayload,
    signing_bytes,
)

DEFAULT_BITS = 3072


def _iso_utc(date_text: str) -> str:
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
        raise SystemExit(f"Bad date '{date_text}': {error}")
    return dt.astimezone(timezone.utc).isoformat()


# --------------------------------------------------------------------------- keygen
def cmd_keygen(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    private_path = out_dir / "private_key.pem"
    public_path = out_dir / "public_key.pem"
    if private_path.exists() and not args.force:
        raise SystemExit(f"{private_path} already exists (use --force to overwrite)")

    if args.passphrase is not None:
        passphrase = args.passphrase
    elif args.no_passphrase:
        passphrase = ""
    else:
        passphrase = getpass.getpass(
            "Passphrase to encrypt the private key (blank = none): "
        )
        if passphrase and passphrase != getpass.getpass("Confirm passphrase: "):
            raise SystemExit("Passphrases did not match")

    print(f"Generating a {args.bits}-bit RSA key pair…")
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=args.bits)

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
    public_path.write_bytes(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    print(f"  private key -> {private_path}  (KEEP SECRET, BACK UP)")
    print(f"  public key  -> {public_path}")
    print()
    print("Next: copy the public key into the app so it can verify licenses:")
    print(f"  cp {public_path} src/licensing/public_key.pem")
    if not passphrase:
        print("\nWARNING: the private key is NOT encrypted. Store it somewhere safe.")
    return 0


# ---------------------------------------------------------------------------- issue
def _load_private_key(path: Path, passphrase: str | None) -> rsa.RSAPrivateKey:
    data = Path(path).read_bytes()
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except TypeError:
        # Encrypted: ask for the passphrase.
        pw = (
            passphrase
            if passphrase is not None
            else getpass.getpass("Private key passphrase: ")
        )
        key = serialization.load_pem_private_key(data, password=pw.encode("utf-8"))
    except ValueError as error:
        raise SystemExit(f"Could not read private key: {error}")
    if not isinstance(key, rsa.RSAPrivateKey):
        raise SystemExit("The key is not an RSA private key")
    return key


def _sign(payload: dict, private_key: rsa.RSAPrivateKey) -> str:
    signature = private_key.sign(
        signing_bytes(payload),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("ascii")


def cmd_issue(args: argparse.Namespace) -> int:
    machine = None
    if args.request:
        request = json.loads(Path(args.request).read_text(encoding="utf-8"))
        machine = request.get("fingerprint")
        if not machine:
            raise SystemExit("The request file has no 'fingerprint'")
        if not request.get("strong", True):
            print(
                "NOTE: the customer's machine reported only a weak fallback "
                "fingerprint (hostname/MAC).",
                file=sys.stderr,
            )
    elif args.machine:
        machine = args.machine
    elif not args.floating:
        raise SystemExit("Pass --request FILE, --machine FP, or --floating")

    features = [f.strip() for f in args.features.split(",") if f.strip()] or [
        FEATURE_CORE
    ]
    if FEATURE_CORE not in features:
        features.insert(0, FEATURE_CORE)

    payload = LicensePayload(
        license_id=args.license_id or secrets.token_hex(8),
        customer=args.customer,
        issued_at=datetime.now(timezone.utc).isoformat(),
        expires_at=_iso_utc(args.expires) if args.expires else None,
        machine=machine,
        features=features,
        seats=args.seats,
        edition=args.edition,
        note=args.note or "",
    )

    private_key = _load_private_key(Path(args.key), args.passphrase)
    document = {
        "payload": payload.to_dict(),
        "signature": _sign(payload.to_dict(), private_key),
        "key_id": args.key_id,
    }
    text = json.dumps(document, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"Wrote {args.out}")
    else:
        print(text)
    print(
        f"  customer : {payload.customer}\n"
        f"  machine  : {machine or '(floating)'}\n"
        f"  expires  : {payload.expires_at or 'never'}\n"
        f"  features : {', '.join(features)}",
        file=sys.stderr,
    )
    return 0


# -------------------------------------------------------------------------- inspect
def cmd_inspect(args: argparse.Namespace) -> int:
    document = json.loads(Path(args.license).read_text(encoding="utf-8"))
    payload = document.get("payload", {})
    print(json.dumps(payload, indent=2))
    print(f"key_id    : {document.get('key_id')}")

    pubkey_path = args.pubkey or "src/licensing/public_key.pem"
    try:
        from src.licensing.verify import load_public_key, verify_signature

        key = load_public_key(Path(pubkey_path))
        if key is None:
            print(f"signature : CANNOT CHECK (no public key at {pubkey_path})")
            return 1
        ok = verify_signature(payload, base64.b64decode(document["signature"]), key)
        print(
            f"signature : {'VALID' if ok else 'INVALID'} (checked against {pubkey_path})"
        )
        return 0 if ok else 1
    except Exception as error:
        print(f"signature : CANNOT CHECK ({error})")
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    kg = sub.add_parser("keygen", help="Create an RSA key pair")
    kg.add_argument("--out-dir", default="./license-keys")
    kg.add_argument("--bits", type=int, default=DEFAULT_BITS)
    kg.add_argument(
        "--passphrase", help="Encrypt the private key with this (else prompt)"
    )
    kg.add_argument(
        "--no-passphrase", action="store_true", help="Do not encrypt the private key"
    )
    kg.add_argument("--force", action="store_true")
    kg.set_defaults(func=cmd_keygen)

    iss = sub.add_parser("issue", help="Sign a license")
    iss.add_argument("--key", required=True, help="Path to private_key.pem")
    iss.add_argument(
        "--passphrase", help="Private key passphrase (else prompt if needed)"
    )
    iss.add_argument("--request", help="Customer's activation request JSON")
    iss.add_argument("--machine", help="Bind to this fingerprint directly")
    iss.add_argument("--floating", action="store_true", help="Do not bind to a machine")
    iss.add_argument("--customer", required=True)
    iss.add_argument("--expires", help="YYYY-MM-DD or ISO (default: never)")
    iss.add_argument(
        "--features", default=FEATURE_CORE, help="Comma-separated (default: core)"
    )
    iss.add_argument("--seats", type=int, default=1)
    iss.add_argument("--edition", default="standard")
    iss.add_argument("--note", help="Free-form note stored in the license")
    iss.add_argument("--license-id", help="Override the generated id")
    iss.add_argument(
        "--key-id", default="v1", help="Which embedded public key verifies this"
    )
    iss.add_argument("--out", help="Write here (default: stdout)")
    iss.set_defaults(func=cmd_issue)

    ins = sub.add_parser("inspect", help="Show and verify a license file")
    ins.add_argument("license")
    ins.add_argument("--pubkey", help="Public key to verify against")
    ins.set_defaults(func=cmd_inspect)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
