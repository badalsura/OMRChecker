# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for the portable OMR engine (GUI + API + bulk reader).

    pyinstaller --noconfirm --clean packaging/omr.spec

Build mode via the OMR_BUILD_MODE environment variable:
    onedir  (default) dist/OMRChecker/OMRChecker.exe + its files: fastest start-up,
            recommended for bulk work (no unpacking on every worker process start)
    onefile dist/OMRChecker-onefile.exe: one file to copy around
    both    builds both

OMR_CONSOLE=0 hides the console window (the Tk window is the UI); default 1
keeps it so --bulk / --selftest print progress.
OMR_NO_TK=1 leaves Tk out (the launcher then runs as a console server); needed
with Python builds whose Tcl/Tk is linked statically (e.g. uv's standalone
CPython on Linux), which PyInstaller 5.x cannot collect.

Works with PyInstaller 5.13.x (Python 3.8, Windows 7+) and 6.x.
"""

import os
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
)

ROOT = Path(SPECPATH).resolve().parent  # noqa: F821 (SPECPATH is set by PyInstaller)
MODE = os.environ.get("OMR_BUILD_MODE", "onedir").lower()
CONSOLE = os.environ.get("OMR_CONSOLE", "1") != "0"
WITH_TK = os.environ.get("OMR_NO_TK", "0") in ("0", "", "false")
NAME = "OMRChecker"


def tree(source, target, skip=("node_modules", "test", "tests", "__pycache__", ".git")):
    """(file, dest_dir) pairs for a folder, skipping dev-only subfolders."""
    source = ROOT / source
    pairs = []
    if not source.exists():
        return pairs
    for path in source.rglob("*"):
        if path.is_file() and not any(part in skip for part in path.relative_to(source).parts):
            pairs.append((str(path), str(Path(target) / path.relative_to(source).parent)))
    return pairs


def optional(fn, *args):
    try:
        return fn(*args)
    except Exception:
        return []


datas = []
datas += tree("src/api/static", "src/api/static")
datas += tree("src/schemas", "src/schemas")
datas += tree("src/defaults", "src/defaults")
# Client-side (in-browser) reader, if built: served at /browser
datas += tree("web/omr-browser", "web/omr-browser")
# Sample templates (JSON + marker images), handy for first runs
for path in (ROOT / "samples").rglob("*"):
    if path.is_file() and (
        path.suffix.lower() == ".json" or "marker" in path.name.lower() or "omr_marker" in path.name.lower()
    ):
        datas.append((str(path), str(Path("samples") / path.relative_to(ROOT / "samples").parent)))
# Optional bundled Tesseract (UB Mannheim build copied into packaging/tesseract)
datas += tree("packaging/tesseract", "tesseract", skip=())
# OCR models and choices from packaging/fetch_ocr_models.py (packaging/ocr_models.md)
datas += tree("packaging/tessdata", "tessdata", skip=())
datas += tree("packaging/models/paddleocr", "models/paddleocr", skip=())
if (ROOT / "packaging" / "ocr_build.json").is_file():
    datas.append((str(ROOT / "packaging" / "ocr_build.json"), "."))
datas += optional(collect_data_files, "zxingcpp")
datas += optional(collect_data_files, "onnxruntime")
datas += optional(collect_data_files, "pymupdf")
datas += optional(collect_data_files, "fitz")
# PDF exports: reportlab's standard fonts and encodings
datas += optional(collect_data_files, "reportlab")

binaries = []
binaries += optional(collect_dynamic_libs, "onnxruntime")
binaries += optional(collect_dynamic_libs, "zxingcpp")
# Optional ZBar engine: pyzbar's Windows wheel ships libzbar-64.dll/libiconv.dll
binaries += optional(collect_dynamic_libs, "pyzbar")

hiddenimports = []
# Processors and readers are discovered at run time with pkgutil. Walk the file
# tree rather than collect_submodules("src"): src/processors has no __init__.py
# (namespace package), which collect_submodules silently skips.
for path in sorted((ROOT / "src").rglob("*.py")):
    parts = path.relative_to(ROOT).with_suffix("").parts
    if "tests" in parts or parts[:3] == ("src", "ml", "train"):
        continue
    hiddenimports.append(".".join(parts[:-1] if parts[-1] == "__init__" else parts))
hiddenimports += collect_submodules("uvicorn")
hiddenimports += [
    "zxingcpp",
    "pytesseract",
    "fitz",
    "multipart",
    "python_multipart",
    "email.mime.multipart",
    "anyio._backends._asyncio",
]
if WITH_TK:
    hiddenimports.append("tkinter")
hiddenimports += optional(collect_submodules, "onnxruntime.capi")
# Exports: SQLAlchemy loads dialects by URL at run time; reportlab imports
# its font/encoding modules lazily
hiddenimports += optional(collect_submodules, "sqlalchemy.dialects")
hiddenimports += optional(collect_submodules, "reportlab.pdfbase")
hiddenimports += ["openpyxl", "et_xmlfile", "greenlet"]

excludes = [
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
    "tesserocr",  # no Windows wheels; pytesseract is used there
    "PyQt5",
    "PySide2",
    "PyQt6",
    "PySide6",
]
if not WITH_TK:
    excludes += ["tkinter", "_tkinter"]

a = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "omr_desktop.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data)  # noqa: F821

icon = ROOT / "packaging" / "omr.ico"
exe_options = dict(
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX-compressed DLLs trigger antivirus false positives and slow start-up
    console=CONSOLE,
    icon=str(icon) if icon.exists() else None,
)

if MODE in ("onedir", "both"):
    exe = EXE(  # noqa: F821
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name=NAME,
        **exe_options,
    )
    coll = COLLECT(  # noqa: F821
        exe,
        a.binaries,
        a.zipfiles,
        a.datas,
        strip=False,
        upx=False,
        name=NAME,
    )

if MODE in ("onefile", "both"):
    exe_onefile = EXE(  # noqa: F821
        pyz,
        a.scripts,
        a.binaries,
        a.zipfiles,
        a.datas,
        [],
        name=f"{NAME}-onefile",
        runtime_tmpdir=None,
        **exe_options,
    )
