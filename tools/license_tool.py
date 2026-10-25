#!/usr/bin/env python3
"""
Vendor-side license tool for OMRChecker. Runs on YOUR machine only; it holds
the RSA private key that signs licenses. Never ship it or the private key.

Prefer the local web admin for day-to-day issuing (a GUI over this same logic):

    python -m tools.license_admin        # opens http://127.0.0.1:8777

This CLI remains the scriptable/offline equivalent:

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

    # 4. Revocation list (used later by the licensing server; see docs):
    python tools/license_tool.py revoke LICENSE_ID --reason "refunded"
    python tools/license_tool.py revocations

A license can also be issued without a request file:
    --machine <fingerprint>   bind to a fingerprint you were given another way
    --floating                not bound to any machine (use sparingly)
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

# Run from the repo root so `src` and `tools` imports work.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import vendor_lib as vl  # noqa: E402
from tools.vendor_lib import FEATURE_CORE  # noqa: E402

DEFAULT_REVOCATIONS = "./license-keys/revocations.json"


# --------------------------------------------------------------------------- keygen
def cmd_keygen(args: argparse.Namespace) -> int:
    private_path = Path(args.out_dir) / "private_key.pem"
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
    priv, pub = vl.generate_keypair(
        Path(args.out_dir), bits=args.bits, passphrase=passphrase, force=args.force
    )
    print(f"  private key -> {priv}  (KEEP SECRET, BACK UP)")
    print(f"  public key  -> {pub}")
    print()
    print("Next: copy the public key into the app so it can verify licenses:")
    print(f"  cp {pub} src/licensing/public_key.pem")
    if not passphrase:
        print("\nWARNING: the private key is NOT encrypted. Store it somewhere safe.")
    return 0


# ---------------------------------------------------------------------------- issue
def _load_private_key(path: Path, passphrase):
    try:
        if passphrase is None and vl.private_key_is_encrypted(path):
            passphrase = getpass.getpass("Private key passphrase: ")
        return vl.load_private_key(path, passphrase)
    except Exception as error:
        raise SystemExit(f"Could not read private key: {error}")


def cmd_issue(args: argparse.Namespace) -> int:
    machine = None
    floating = args.floating
    if args.request:
        request = json.loads(Path(args.request).read_text(encoding="utf-8"))
        try:
            machine = vl.machine_from_request(request)
        except ValueError as error:
            raise SystemExit(str(error))
        if not request.get("strong", True):
            print(
                "NOTE: the customer's machine reported only a weak fallback "
                "fingerprint (hostname/MAC).",
                file=sys.stderr,
            )
    elif args.machine:
        machine = args.machine
    elif not floating:
        raise SystemExit("Pass --request FILE, --machine FP, or --floating")

    private_key = _load_private_key(Path(args.key), args.passphrase)
    try:
        document = vl.issue_license(
            private_key,
            customer=args.customer,
            machine=machine,
            floating=floating,
            expires=args.expires,
            features=args.features,
            seats=args.seats,
            edition=args.edition,
            note=args.note or "",
            license_id=args.license_id,
            key_id=args.key_id,
        )
    except ValueError as error:
        raise SystemExit(str(error))

    text = json.dumps(document, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"Wrote {args.out}")
    else:
        print(text)
    payload = document["payload"]
    print(
        f"  customer : {payload['customer']}\n"
        f"  machine  : {payload['machine'] or '(floating)'}\n"
        f"  expires  : {payload['expires_at'] or 'never'}\n"
        f"  features : {', '.join(payload['features'])}",
        file=sys.stderr,
    )
    return 0


# -------------------------------------------------------------------------- inspect
def cmd_inspect(args: argparse.Namespace) -> int:
    document = json.loads(Path(args.license).read_text(encoding="utf-8"))
    pubkey = Path(args.pubkey) if args.pubkey else None
    payload, key_id, ok = vl.inspect_license(document, pubkey)
    print(json.dumps(payload, indent=2))
    print(f"key_id    : {key_id}")
    where = args.pubkey or str(vl.PUBLIC_KEY_IN_APP)
    if ok is None:
        print(f"signature : CANNOT CHECK (no public key at {where})")
        return 1
    print(f"signature : {'VALID' if ok else 'INVALID'} (checked against {where})")
    return 0 if ok else 1


# ----------------------------------------------------------------------- revocation
def cmd_revoke(args: argparse.Namespace) -> int:
    vl.revoke(Path(args.file), args.license_id, args.reason or "")
    print(f"Revoked {args.license_id} in {args.file}")
    return 0


def cmd_unrevoke(args: argparse.Namespace) -> int:
    vl.unrevoke(Path(args.file), args.license_id)
    print(f"Removed {args.license_id} from {args.file}")
    return 0


def cmd_revocations(args: argparse.Namespace) -> int:
    data = vl.load_revocations(Path(args.file))
    print(json.dumps(data.get("revoked", {}), indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    kg = sub.add_parser("keygen", help="Create an RSA key pair")
    kg.add_argument("--out-dir", default="./license-keys")
    kg.add_argument("--bits", type=int, default=vl.DEFAULT_BITS)
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

    rev = sub.add_parser("revoke", help="Add a license id to the revocation list")
    rev.add_argument("license_id")
    rev.add_argument("--reason", help="Why it was revoked")
    rev.add_argument("--file", default=DEFAULT_REVOCATIONS)
    rev.set_defaults(func=cmd_revoke)

    unr = sub.add_parser("unrevoke", help="Remove a license id from the list")
    unr.add_argument("license_id")
    unr.add_argument("--file", default=DEFAULT_REVOCATIONS)
    unr.set_defaults(func=cmd_unrevoke)

    lst = sub.add_parser("revocations", help="Show the revocation list")
    lst.add_argument("--file", default=DEFAULT_REVOCATIONS)
    lst.set_defaults(func=cmd_revocations)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
