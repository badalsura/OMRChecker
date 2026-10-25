"""
A stable identifier for the machine a license is locked to.

The goal is a value that stays the same across reboots, app updates and normal
use on one computer, but differs between computers, so a license file copied to
another machine stops working. It is NOT a secret and not meant to be
unforgeable on its own: the security comes from the vendor's signature over it.

We hash a primary OS-provided machine id (so the raw id never leaves the
customer's machine in the clear) together with a fixed salt and the app name.

    Windows  HKLM\\SOFTWARE\\Microsoft\\Cryptography\\MachineGuid, plus the
             system drive's volume serial number
    Linux    /etc/machine-id (or /var/lib/dbus/machine-id)
    macOS    IOPlatformUUID from ioreg

If none can be read we fall back to a hash of hostname + MAC so activation
still works, marked so the vendor can see it is a weak fingerprint.
"""

from __future__ import annotations

import hashlib
import os
import platform
import subprocess
import sys
import uuid
from dataclasses import dataclass
from typing import Optional

# Changing this salt invalidates every machine-bound license, so it is fixed
# for the life of the product line.
_FP_SALT = b"omrchecker-fingerprint-v1"


@dataclass
class MachineInfo:
    """What activation shows the user and sends to the vendor. `fingerprint`
    is the value a license is bound to; the rest is human-readable context."""

    fingerprint: str
    source: str  # which identifier was used ("windows", "linux", "macos", "fallback")
    hostname: str
    os: str
    strong: bool  # False when only the hostname/MAC fallback was available


def _windows_machine_guid() -> Optional[str]:
    try:
        import winreg  # noqa: PLC0415 (Windows only)

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
            0,
            winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0),
        ) as key:
            value, _ = winreg.QueryValueEx(key, "MachineGuid")
            return str(value).strip() or None
    except Exception:
        return None


def _windows_volume_serial() -> Optional[str]:
    try:
        import ctypes  # noqa: PLC0415

        system_drive = os.environ.get("SystemDrive", "C:") + "\\"
        serial = ctypes.c_uint(0)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(system_drive),
            None,
            0,
            ctypes.byref(serial),
            None,
            None,
            None,
            0,
        )
        return f"{serial.value:08X}" if ok else None
    except Exception:
        return None


def _linux_machine_id() -> Optional[str]:
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            with open(path, "r", encoding="ascii") as handle:
                value = handle.read().strip()
                if value:
                    return value
        except OSError:
            continue
    return None


def _macos_platform_uuid() -> Optional[str]:
    try:
        out = subprocess.check_output(
            ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).decode("utf-8", "replace")
    except Exception:
        return None
    for line in out.splitlines():
        if "IOPlatformUUID" in line:
            parts = line.split('"')
            if len(parts) >= 4:
                return parts[-2].strip() or None
    return None


def _raw_identifier() -> tuple[str, str, bool]:
    """(raw_id, source, strong). The raw id is hashed before it is exposed."""
    system = platform.system()
    if system == "Windows":
        guid = _windows_machine_guid()
        serial = _windows_volume_serial()
        if guid:
            return (f"{guid}|{serial or ''}", "windows", True)
    elif system == "Linux":
        machine_id = _linux_machine_id()
        if machine_id:
            return (machine_id, "linux", True)
    elif system == "Darwin":
        platform_uuid = _macos_platform_uuid()
        if platform_uuid:
            return (platform_uuid, "macos", True)
    # Fallback: hostname + MAC. Weaker (a MAC can be cloned), but lets the
    # app run where the primary id is unreadable.
    mac = uuid.getnode()
    return (f"{platform.node()}|{mac:012x}", "fallback", False)


def _hash(raw_id: str) -> str:
    digest = hashlib.sha256()
    digest.update(_FP_SALT)
    digest.update(b"\x00")
    digest.update(raw_id.encode("utf-8", "replace"))
    # 128 bits is plenty to tell machines apart and keeps the shown code short.
    return digest.hexdigest()[:32]


def machine_info() -> MachineInfo:
    """Gather this machine's fingerprint and the context shown at activation."""
    raw_id, source, strong = _raw_identifier()
    return MachineInfo(
        fingerprint=_hash(raw_id),
        source=source,
        hostname=platform.node() or "unknown",
        os=f"{platform.system()} {platform.release()}".strip(),
        strong=strong,
    )


def machine_fingerprint() -> str:
    """Just the fingerprint a license binds to."""
    return machine_info().fingerprint


def fingerprint_matches(bound: Optional[str]) -> bool:
    """True if `bound` is None (floating license) or equals this machine."""
    if bound is None:
        return True
    return bound == machine_fingerprint()


if __name__ == "__main__":
    info = machine_info()
    print(f"fingerprint : {info.fingerprint}")
    print(
        f"source      : {info.source} ({'strong' if info.strong else 'weak fallback'})"
    )
    print(f"hostname    : {info.hostname}")
    print(f"os          : {info.os}")
    sys.exit(0)
