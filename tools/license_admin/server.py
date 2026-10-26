"""
The local license-admin web server. See tools/license_admin/__init__.py.

Everything that needs the private key stays here on the vendor's machine. The
server binds to 127.0.0.1 only; it is a single-operator local tool, not a
network service, so the loaded private key lives in process memory for the
session and is never written anywhere by this server.
"""

from __future__ import annotations

import sys
import json
from pathlib import Path

# Repo root on the path so `src` and `tools` import whether this is run as
# `python -m tools.license_admin` or directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from tools import vendor_lib as vl  # noqa: E402

STATIC_DIR = Path(__file__).resolve().parent / "static"
DEFAULT_REVOCATIONS = Path("./license-keys/revocations.json")

# In-memory session state for this single-operator tool.
_state: dict = {
    "private_key": None,
    "key_path": None,
    "revocations_file": str(DEFAULT_REVOCATIONS),
    # Set by configure(): a key that comes with this copy (the issuer app),
    # and the folder issued licenses are also saved to
    "offered_key": None,
    "issued_dir": None,
}


def configure(key=None, home=None) -> None:
    """Point the admin at a private key to offer at start and a working folder
    (revocation list, copies of issued licenses). An unencrypted key is loaded
    at once; an encrypted one waits for its passphrase in the page."""
    if home:
        home = Path(home)
        _state["revocations_file"] = str(home / "revocations.json")
        _state["issued_dir"] = str(home / "issued")
    if key and Path(key).exists():
        _state["offered_key"] = str(key)
        if not vl.private_key_is_encrypted(Path(key)):
            _state["private_key"] = vl.load_private_key(Path(key))
            _state["key_path"] = str(key)


def _check_signature(document: dict):
    """(payload, key_id, signature_ok, checked_against): against the loaded
    private key's public half when there is one, else the app's public key."""
    key = _state["private_key"]
    if key is None:
        payload, key_id, ok = vl.inspect_license(document)
        return payload, key_id, ok, str(vl.PUBLIC_KEY_IN_APP)
    import base64

    from src.licensing.verify import verify_signature

    payload = document.get("payload", {})
    try:
        ok = verify_signature(payload, base64.b64decode(document["signature"]), key.public_key())
    except Exception:
        ok = False
    return payload, document.get("key_id"), ok, "the loaded private key"


class KeygenBody(BaseModel):
    out_dir: str = "./license-keys"
    bits: int = vl.DEFAULT_BITS
    passphrase: str = ""
    force: bool = False
    copy_to_app: bool = True


class LoadKeyBody(BaseModel):
    path: str
    passphrase: str = ""


class IssueBody(BaseModel):
    customer: str
    request_json: str = ""  # the customer's request code (or request file text)
    machine: str = ""
    floating: bool = False
    expires: str = ""  # YYYY-MM-DD or ISO, "" = never
    features: str = "core"
    seats: int = 1
    edition: str = "standard"
    note: str = ""
    license_id: str = ""
    key_id: str = "v1"


class InspectBody(BaseModel):
    text: str  # the .omrlic file contents


class RevokeBody(BaseModel):
    license_id: str
    reason: str = ""


def _revocations_path() -> Path:
    return Path(_state["revocations_file"])


def create_app() -> FastAPI:
    app = FastAPI(title="OMRChecker License Admin", docs_url=None, redoc_url=None)

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/state")
    def state():
        key_path = _state["key_path"]
        revoked = vl.load_revocations(_revocations_path()).get("revoked", {})
        return {
            "key_loaded": _state["private_key"] is not None,
            "key_path": key_path,
            "offered_key": _state["offered_key"],
            "issued_dir": _state["issued_dir"],
            "public_key_in_app": vl.PUBLIC_KEY_IN_APP.exists(),
            "public_key_in_app_path": str(vl.PUBLIC_KEY_IN_APP),
            "revocations_file": _state["revocations_file"],
            "revoked": revoked,
        }

    @app.post("/api/keygen")
    def keygen(body: KeygenBody):
        try:
            priv, pub = vl.generate_keypair(
                Path(body.out_dir),
                bits=body.bits,
                passphrase=body.passphrase,
                force=body.force,
            )
        except FileExistsError as error:
            raise HTTPException(409, str(error))
        except Exception as error:  # pragma: no cover - defensive
            raise HTTPException(400, str(error))
        copied = False
        if body.copy_to_app:
            vl.PUBLIC_KEY_IN_APP.parent.mkdir(parents=True, exist_ok=True)
            vl.PUBLIC_KEY_IN_APP.write_bytes(Path(pub).read_bytes())
            copied = True
        return {
            "private_key": str(priv),
            "public_key": str(pub),
            "copied_to_app": copied,
            "encrypted": bool(body.passphrase),
        }

    @app.post("/api/load-key")
    def load_key(body: LoadKeyBody):
        path = Path(body.path)
        if not path.exists():
            raise HTTPException(404, f"No such file: {path}")
        try:
            key = vl.load_private_key(path, body.passphrase or None)
        except TypeError:
            raise HTTPException(400, "This key is encrypted; a passphrase is required.")
        except Exception as error:
            raise HTTPException(400, f"Could not load key: {error}")
        _state["private_key"] = key
        _state["key_path"] = str(path)
        return {"ok": True, "key_path": str(path)}

    @app.post("/api/issue")
    def issue(body: IssueBody):
        if _state["private_key"] is None:
            raise HTTPException(400, "Load your private key first.")
        machine = body.machine.strip() or None
        if body.request_json.strip():
            try:
                machine = vl.machine_from_text(body.request_json)
            except ValueError as error:
                raise HTTPException(400, f"Request code: {error}")
        try:
            document = vl.issue_license(
                _state["private_key"],
                customer=body.customer,
                machine=machine,
                floating=body.floating,
                expires=body.expires.strip() or None,
                features=body.features,
                seats=body.seats,
                edition=body.edition,
                note=body.note,
                license_id=body.license_id.strip() or None,
                key_id=body.key_id,
            )
        except ValueError as error:
            raise HTTPException(400, str(error))
        customer_slug = (
            "".join(
                c
                for c in body.customer.lower().replace(" ", "-")
                if c.isalnum() or c == "-"
            )
            or "license"
        )
        text = json.dumps(document, indent=2)
        filename = f"{customer_slug}.omrlic"
        saved = None
        if _state["issued_dir"]:
            # A copy in the working folder: the app window may not download
            folder = Path(_state["issued_dir"])
            folder.mkdir(parents=True, exist_ok=True)
            saved = folder / f"{customer_slug}-{document['payload']['license_id']}.omrlic"
            saved.write_text(text, encoding="utf-8")
        return {
            "document": document,
            "text": text,
            "filename": filename,
            "saved_to": str(saved) if saved else None,
        }

    @app.post("/api/inspect")
    def inspect(body: InspectBody):
        try:
            document = json.loads(body.text)
        except Exception as error:
            raise HTTPException(400, f"Not a license file: {error}")
        payload, key_id, ok, against = _check_signature(document)
        return {
            "payload": payload,
            "key_id": key_id,
            "signature_ok": ok,
            "checked_against": against,
        }

    @app.get("/api/revocations")
    def revocations():
        return vl.load_revocations(_revocations_path()).get("revoked", {})

    @app.post("/api/revocations")
    def add_revocation(body: RevokeBody):
        vl.revoke(_revocations_path(), body.license_id.strip(), body.reason)
        return {"ok": True}

    @app.delete("/api/revocations/{license_id}")
    def remove_revocation(license_id: str):
        vl.unrevoke(_revocations_path(), license_id)
        return {"ok": True}

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    return app


def main(argv=None) -> int:
    import argparse
    import threading
    import webbrowser

    import uvicorn

    parser = argparse.ArgumentParser(description="OMRChecker local license admin")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8777)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--key", help="Private key to offer at start")
    parser.add_argument(
        "--revocations",
        help="Revocation list JSON (default ./license-keys/revocations.json)",
    )
    args = parser.parse_args(argv)
    configure(key=args.key)
    if args.revocations:
        _state["revocations_file"] = args.revocations

    url = f"http://{args.host}:{args.port}"
    print(f"License admin on {url}  (vendor tool — keep this machine private)")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
