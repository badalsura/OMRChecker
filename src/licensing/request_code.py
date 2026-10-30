"""
Request codes: this machine's fingerprint as a short code a person can copy,
read out or type, in place of a JSON request.

    OMR1-ABCDE-FGHIJ-KLMNO-PQRST-UVWXY-Z2345

The code is the 16-byte fingerprint plus a 2-byte checksum in base32, so a
mistyped code is refused instead of binding a license to the wrong machine.
Decoding forgives case, spaces, missing dashes and the digits 0/1/8 typed for
the letters O/I/B. The vendor side finds a code anywhere in pasted text (an
email, a saved request file), and still takes the older JSON request or a bare
fingerprint.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from typing import Optional

PREFIX = "OMR1"
_GROUP = 5
_CODE_RE = re.compile(r"OMR1(?:[\s\-]*[A-Z2-7018]){29}", re.IGNORECASE)
_FINGERPRINT_RE = re.compile(r"\b[0-9a-f]{32}\b", re.IGNORECASE)


class RequestCodeError(ValueError):
    pass


def _checksum(raw: bytes) -> bytes:
    return hashlib.sha256(b"omr-request" + raw).digest()[:2]


def encode(fingerprint: str) -> str:
    """The request code for a 32-hex-digit fingerprint."""
    raw = bytes.fromhex(fingerprint)
    if len(raw) != 16:
        raise RequestCodeError("A fingerprint is 32 hex digits")
    text = base64.b32encode(raw + _checksum(raw)).decode("ascii").rstrip("=")
    groups = [text[i : i + _GROUP] for i in range(0, len(text), _GROUP)]
    return "-".join([PREFIX, *groups])


def decode(code: str) -> str:
    """The fingerprint in a request code; RequestCodeError if it is mistyped."""
    text = re.sub(r"[\s\-]", "", code or "").upper()
    if not text.startswith(PREFIX):
        raise RequestCodeError("A request code starts with OMR1")
    body = text[len(PREFIX) :].translate(str.maketrans("018", "OIB"))
    if len(body) != 29 or not re.fullmatch(r"[A-Z2-7]+", body):
        raise RequestCodeError("That request code is incomplete or has a wrong character")
    try:
        data = base64.b32decode(body + "=" * (-len(body) % 8))
    except Exception as error:
        raise RequestCodeError("That request code is not readable") from error
    raw, check = data[:16], data[16:18]
    if _checksum(raw) != check:
        raise RequestCodeError("That request code has a typo: check it against the one shown in the app")
    return raw.hex()


def fingerprint_from_text(text: str) -> str:
    """The machine fingerprint in whatever the customer sent: a request code
    (anywhere in the text), an older JSON request, or a bare fingerprint."""
    text = (text or "").strip()
    if not text:
        raise RequestCodeError("Paste the customer's request code")
    match = _CODE_RE.search(text)
    if match:
        return decode(match.group(0))
    if text.startswith("{"):
        try:
            request = json.loads(text)
        except ValueError as error:
            raise RequestCodeError("That request is not readable") from error
        if isinstance(request, dict) and request.get("code"):
            return decode(str(request["code"]))
        if isinstance(request, dict) and request.get("fingerprint"):
            return str(request["fingerprint"]).lower()
        raise RequestCodeError("The request has no request code")
    match = _FINGERPRINT_RE.search(text)
    if match:
        return match.group(0).lower()
    if "OMR1" in text.upper():
        # Starts like a code but is too short or long to be one
        decode(text[text.upper().index("OMR1") :].split()[0])
    raise RequestCodeError("No request code found: it looks like OMR1-XXXXX-XXXXX-…")


def request_file_text(code: str, hostname: str = "", system: str = "", product: str = "OMRChecker") -> str:
    """A short plain-text request file the customer can attach to an email."""
    lines = [
        f"{product} license request",
        "",
        f"Request code: {code}",
    ]
    if hostname:
        lines.append(f"Computer: {hostname}")
    if system:
        lines.append(f"System: {system}")
    lines += ["", "Send this to your vendor. They reply with a license file (.omrlic) for this computer."]
    return "\n".join(lines) + "\n"


def describe(text: str) -> Optional[str]:
    """The computer name in a pasted request file, if it has one."""
    match = re.search(r"^Computer:\s*(.+)$", text or "", re.MULTILINE)
    if match:
        return match.group(1).strip()
    try:
        request = json.loads(text)
        return request.get("hostname") if isinstance(request, dict) else None
    except ValueError:
        return None
