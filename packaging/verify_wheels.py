"""
Check that every pin in requirements-win7.txt has a wheel installable on
CPython 3.8 / Windows x64 (cp38-win_amd64, abi3 <= cp38, or py3-none-any/win_amd64),
using the PyPI JSON API. Exit code 1 if any pin is missing a wheel.

    python packaging/verify_wheels.py [requirements file]
"""

import json
import re
import sys
import urllib.request
from pathlib import Path

ABI3_OK = ("cp32", "cp33", "cp34", "cp35", "cp36", "cp37", "cp38")


def wheel_ok(filename):
    if not filename.endswith(".whl"):
        return False
    name = filename[:-4]
    python_tag, abi_tag, platform_tag = name.split("-")[-3:]
    platform_ok = platform_tag in (
        "any",
        "win_amd64",
    ) or "win_amd64" in platform_tag.split(".")
    if not platform_ok:
        return False
    tags = python_tag.split(".")
    if "cp38" in tags and abi_tag in ("cp38", "none", "abi3"):
        return True
    if abi_tag == "abi3" and any(t in ABI3_OK for t in tags):
        return True
    return abi_tag == "none" and any(t in ("py3", "py2.py3", "py38") for t in tags)


def requires_python_ok(spec):
    if not spec:
        return True
    try:
        from packaging.specifiers import SpecifierSet

        return SpecifierSet(spec).contains("3.8.10")
    except ImportError:
        return True


def main(path=None):
    path = Path(path or Path(__file__).with_name("requirements-win7.txt"))
    failures = 0
    for line in path.read_text().splitlines():
        line = line.split("#")[0].split(";")[0].strip()
        match = re.match(r"^([A-Za-z0-9_.\-]+)==([^\s]+)$", line)
        if not match:
            continue
        name, version = match.groups()
        with urllib.request.urlopen(
            f"https://pypi.org/pypi/{name}/{version}/json"
        ) as r:
            data = json.load(r)
        wheels = [
            f
            for f in data["urls"]
            if wheel_ok(f["filename"])
            and not f.get("yanked")
            and requires_python_ok(f.get("requires_python"))
        ]
        status = "OK  " if wheels else "MISS"
        failures += not wheels
        print(
            f"{status} {name}=={version:<12} {wheels[0]['filename'] if wheels else '-'}"
        )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
