"""
Nuitka build of the OMR desktop app (Windows), alongside the PyInstaller spec.

Nuitka compiles the Python to C and then to a native exe, which is harder to
reverse-engineer than PyInstaller's bundled bytecode. This driver assembles the
Nuitka command with the SAME bundled resources as packaging/omr.spec (static
GUI, schemas, defaults, branding, docs, samples, in-browser reader, the license
public key, Tesseract, OCR models, cloudflared), so the two builds ship the
same files.

    # 1. Put the bundled resources in place (Tesseract, models, cloudflared):
    python packaging/prepare_bundle.py
    # 2. Make sure the license public key is in place (see docs/licensing.md):
    #    copy license-keys\\public_key.pem src\\licensing\\public_key.pem
    # 3. Build:
    python packaging/build_nuitka.py                 # app window (WebView2), onedir
    python packaging/build_nuitka.py --mode onefile  # single exe
    python packaging/build_nuitka.py --launcher      # console launcher (omr_desktop.py)
    python packaging/build_nuitka.py --dry-run       # print the command, build nothing

Output: dist-nuitka/OMRChecker.dist/OMRChecker.exe (onedir) or
dist-nuitka/OMRChecker.exe (onefile).

Requirements on the BUILD machine (Windows):
    pip install nuitka
    A C compiler: MSVC (Visual Studio Build Tools) is recommended; Nuitka can
    also fetch a bundled MinGW64. First build may download the dependency
    walker and (with --onefile) the onefile bootstrap; --assume-yes-for-downloads
    allows that.

NOTE: this must be built and tested on Windows. It has not been run on the
machine that generated it (Linux). Treat the first Windows build as the real
validation: run OMRChecker.exe --selftest in dist-nuitka to confirm the engine,
OCR and process pool work before shipping.

Standard library only; runs on Python 3.8.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

NAME = "OMRChecker"
# Keep in sync with packaging/omr_desktop.py LAUNCHER_VERSION and the installer.
VERSION = "1.0.0"
COMPANY = "OMRChecker"
PRODUCT = "OMRChecker"

# App entry (WebView2 window) vs the console launcher (server + browser / --bulk).
ENTRY_APP = HERE / "omr_app.py"
ENTRY_LAUNCHER = HERE / "omr_desktop.py"


def data_dirs(app_mode):
    """(source dir, bundled dest) pairs, mirroring packaging/omr.spec. Only the
    ones that exist are passed (optional bundles stay optional)."""
    pairs = [
        (ROOT / "src" / "api" / "static", "src/api/static"),
        (ROOT / "src" / "schemas", "src/schemas"),
        (ROOT / "src" / "defaults", "src/defaults"),
        (ROOT / "branding", "branding"),
        (ROOT / "web" / "omr-browser", "web/omr-browser"),
        (ROOT / "packaging" / "tesseract", "tesseract"),
        (ROOT / "packaging" / "tessdata", "tessdata"),
        (ROOT / "packaging" / "models" / "paddleocr", "models/paddleocr"),
    ]
    if os.environ.get("OMR_BUNDLE_IMPRINT", "1") != "0":
        pairs.append((ROOT / "packaging" / "models" / "imprint", "models/imprint"))
    # cloudflared is a launcher (server-build) feature; the app window omits it.
    if not app_mode:
        pairs.append((ROOT / "packaging" / "cloudflared", "cloudflared"))
    return [(src, dest) for src, dest in pairs if src.is_dir()]


def data_files():
    """Single files to bundle at an explicit destination."""
    files = [
        (ROOT / "docs" / "user-guide.md", "docs/user-guide.md"),
    ]
    ocr_build = ROOT / "packaging" / "ocr_build.json"
    if ocr_build.is_file():
        files.append((ocr_build, "ocr_build.json"))
    # Sample templates (JSON + marker images) handy for first runs.
    for path in (ROOT / "samples").rglob("*"):
        if path.is_file() and (
            path.suffix.lower() == ".json"
            or "marker" in path.name.lower()
            or "omr_marker" in path.name.lower()
        ):
            dest = Path("samples") / path.relative_to(ROOT / "samples")
            files.append((path, str(dest).replace(os.sep, "/")))
    return files


# Packages whose data files / DLLs Nuitka should collect (mirrors the spec's
# collect_data_files / collect_dynamic_libs).
PACKAGE_DATA = ["onnxruntime", "zxingcpp", "reportlab"]
# PyMuPDF imports as fitz; include both names Nuitka may see.
PACKAGE_DATA_OPTIONAL = ["pymupdf", "fitz", "pyzbar"]

# src is force-included whole (processors/readers are discovered at run time
# with pkgutil, so import-following alone would miss them); tests and the ML
# training code are dropped via NOFOLLOW below. uvicorn loads workers/loops
# dynamically too.
INCLUDE_PACKAGES = ["src", "uvicorn"]
INCLUDE_MODULES = [
    "zxingcpp",
    "pytesseract",
    "multipart",
    "python_multipart",
    "email.mime.multipart",
    "anyio._backends._asyncio",
    "openpyxl",
    "et_xmlfile",
    "greenlet",
]

# Never follow these imports into the build (mirrors the spec's excludes).
NOFOLLOW = [
    "src.tests",
    "src.ml.train",
    "src.ml.train_imprint",
    "torch",
    "torchvision",
    "onnx",
    "onnxscript",
    "tensorflow",
    "pytest",
    "IPython",
    "jupyter",
    "notebook",
    "sphinx",
    "tesserocr",
    "PyQt5",
    "PySide2",
    "PyQt6",
    "PySide6",
]


def installed(module):
    import importlib.util

    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def build_command(args):
    app_mode = not args.launcher
    entry = ENTRY_LAUNCHER if args.launcher else ENTRY_APP

    cmd = [sys.executable, "-m", "nuitka", "--standalone"]
    if args.mode == "onefile":
        cmd.append("--onefile")
    if args.msvc:
        # Prefer MSVC (Visual Studio Build Tools) over the bundled MinGW64.
        cmd.append("--msvc=latest")
    cmd += [
        f"--output-dir={args.output_dir}",
        f"--output-filename={NAME}.exe",
        "--assume-yes-for-downloads",
        "--remove-output",
        # numpy/scipy-heavy; anti-bloat trims known-unused imports.
        "--enable-plugin=anti-bloat",
        f"--company-name={COMPANY}",
        f"--product-name={PRODUCT}",
        f"--file-version={VERSION}",
        f"--product-version={VERSION}",
        f"--file-description={PRODUCT}",
    ]

    icon = ROOT / "branding" / "icon.ico"
    if icon.is_file():
        cmd.append(f"--windows-icon-from-ico={icon}")

    # No console for the app window; the launcher keeps one for --bulk/--selftest.
    cmd.append(
        "--windows-console-mode=disable" if app_mode else "--windows-console-mode=force"
    )

    if app_mode:
        # pywebview + WebView2 through pythonnet/WinForms.
        # Nuitka's pywebview plugin picks the Windows backends itself (the
        # app imports winforms and edgechromium) and refuses any webview
        # module asked for by hand
        cmd.append("--include-module=clr")

    if not app_mode:
        # The launcher's window is Tk
        cmd.append("--enable-plugin=tk-inter")

    for pkg in INCLUDE_PACKAGES:
        cmd.append(f"--include-package={pkg}")
    for pkg in PACKAGE_DATA:
        cmd.append(f"--include-package-data={pkg}")
    for pkg in PACKAGE_DATA_OPTIONAL:
        cmd.append(f"--include-package-data={pkg}")
    for mod in INCLUDE_MODULES:
        # Nuitka stops on a module that is not installed (greenlet comes only
        # with some SQLAlchemy installs)
        if installed(mod):
            cmd.append(f"--include-module={mod}")
    # PyMuPDF's SWIG wrapper (pymupdf.mupdf) is millions of lines of C once
    # compiled, and MSVC runs out of heap on it: ship PyMuPDF as bytecode
    for pkg in ("pymupdf", "fitz"):
        if installed(pkg):
            cmd.append(f"--noinclude-custom-mode={pkg}:bytecode")
    for name in NOFOLLOW:
        cmd.append(f"--nofollow-import-to={name}")

    for src, dest in data_dirs(app_mode):
        cmd.append(f"--include-data-dir={src}={dest}")
    for src, dest in data_files():
        cmd.append(f"--include-data-files={src}={dest}")

    cmd.append(str(entry))
    return cmd


def main(argv=None):
    parser = argparse.ArgumentParser(description="Nuitka build of OMRChecker")
    parser.add_argument("--mode", choices=["onedir", "onefile"], default="onedir")
    parser.add_argument(
        "--launcher",
        action="store_true",
        help="Build the console launcher (omr_desktop.py) instead of the app window",
    )
    parser.add_argument("--output-dir", default=str(ROOT / "dist-nuitka"))
    parser.add_argument(
        "--msvc",
        action="store_true",
        help="Use MSVC (--msvc=latest) instead of letting Nuitka pick a compiler",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print the command and exit"
    )
    args = parser.parse_args(argv)

    # The license public key is compiled in (src/licensing/embedded_key.py)
    sys.path.insert(0, str(HERE))
    from embed_public_key import embed

    embed()

    if os.name != "nt":
        print(
            "WARNING: this is a Windows build. Running it elsewhere will not "
            "produce a Windows exe.",
            file=sys.stderr,
        )

    cmd = build_command(args)
    printable = " ".join(f'"{c}"' if " " in c or "=" in c else c for c in cmd)
    print(printable + "\n")
    if args.dry_run:
        return 0
    return subprocess.call(cmd, cwd=str(ROOT))


if __name__ == "__main__":
    raise SystemExit(main())
