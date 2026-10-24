"""
License status and activation endpoints, plus the dependency that gates every
working endpoint of the API.

The gate is added to the same `secured` dependency list that protects the real
endpoints, so one server-side choke point covers scans, jobs, exports, compare,
review and OCR at once: an unlicensed station cannot do real work no matter
which call it makes, and that cannot be bypassed by editing the browser code.

These endpoints themselves are always open (no license, no login), so a fresh
install can show the activation screen and accept a license file:

    GET  /license/status     is this station licensed? (+ safe license details)
    GET  /license/request    this machine's fingerprint, to send the vendor
    POST /license/activate    upload a license file (multipart) to activate
"""

from __future__ import annotations

from fastapi import HTTPException, Request, UploadFile

from src.licensing import gate
from src.licensing.model import FEATURE_CORE

# Max license file we will read (they are a few KB).
_MAX_LICENSE_BYTES = 64 * 1024


def make_license_guard(feature: str = FEATURE_CORE):
    """A FastAPI dependency that refuses the request (HTTP 402) when the
    station is not licensed for `feature`."""

    def require_licensed(request: Request):
        status = gate.current_status(feature)
        if not status.valid:
            raise HTTPException(
                status_code=402,
                detail={
                    "error": "unlicensed",
                    "reason": status.reason.value,
                    "message": status.message,
                },
            )

    return require_licensed


def register(app, ctx):
    @app.get("/license/status", tags=["license"])
    def license_status():
        """The current license status and, if present, safe details (no
        signature). `valid` says whether the station may work."""
        return gate.current_status(refresh=True).to_public_dict()

    @app.get("/license/request", tags=["license"])
    def license_request():
        """This machine's fingerprint and context. The user sends this to the
        vendor, who returns a license file bound to it."""
        return gate.activation_request()

    @app.post("/license/activate", tags=["license"])
    async def license_activate(file: UploadFile):
        """Install an uploaded license file. The file is verified before it is
        saved; an invalid file changes nothing and returns why."""
        data = await file.read(_MAX_LICENSE_BYTES + 1)
        if len(data) > _MAX_LICENSE_BYTES:
            raise HTTPException(413, "That file is too large to be a license.")
        status = gate.install_license_bytes(data)
        if not status.valid:
            # A plain-string detail so the UI shows a clean message.
            raise HTTPException(400, detail=status.message)
        return {"ok": True, **status.to_public_dict()}
