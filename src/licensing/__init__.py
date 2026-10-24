"""
Client-side license checking for OMRChecker.

The product is licensed with RSA-signed license files. The vendor holds the
private key (see tools/license_tool.py) and the app ships only the public key
(src/licensing/public_key.pem). A license cannot be forged or a keygen written
without the private key.

Public API (use `gate` for everything in the app):

    from src.licensing import gate
    gate.require_license()                 # raise LicenseError if unlicensed
    gate.current_status()                  # status for the UI / API
    gate.install_license_bytes(data)       # activate from a license file
    gate.activation_request()              # machine info to send the vendor
"""

from src.licensing.gate import (
    activation_request,
    current_status,
    install_license_bytes,
    is_licensed,
    require_license,
)
from src.licensing.model import FEATURE_CORE, LicensePayload
from src.licensing.verify import LicenseError, LicenseStatus, Reason

__all__ = [
    "require_license",
    "current_status",
    "is_licensed",
    "install_license_bytes",
    "activation_request",
    "LicenseError",
    "LicenseStatus",
    "Reason",
    "LicensePayload",
    "FEATURE_CORE",
]
