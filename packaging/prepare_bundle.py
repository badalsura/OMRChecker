"""
Put everything the exe bundles in place before PyInstaller runs, or stop the
build with what is missing. Called by packaging/build_windows.bat.

    python packaging/prepare_bundle.py                 # everything (local build)
    python packaging/prepare_bundle.py --only cloudflared

What a build always ships:
    packaging/tesseract/       tesseract.exe + DLLs (UB Mannheim build)
    packaging/tessdata/        Tesseract "best" models: English, Hindi
    packaging/models/paddleocr PaddleOCR PP-OCRv5, mobile and server: detection,
                               the main recogniser (also handwriting / ICR),
                               English and Devanagari
    packaging/cloudflared/     cloudflared.exe for "Start remote" in the launcher

Tesseract is copied from an installed UB Mannheim build (installed with winget
if missing). The OCR models come from packaging/fetch_ocr_models.py; converting
the PaddleOCR models needs paddle2onnx, which has no Python 3.8 wheels, so this
runs it with a newer Python (py -3.12 / 3.11 / 3.10 / 3.9) in build\\venv_models.

Standard library only; runs on Python 3.8.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TESSERACT_DIR = HERE / "tesseract"
CLOUDFLARED_DIR = HERE / "cloudflared"
BUILD_FILE = HERE / "ocr_build.json"

# What every build bundles (the GitHub workflow uses the same defaults)
OCR_ARGS = [
    "--tessdata", "best",
    "--langs", "eng", "hin",
    "--paddle", "mobile", "server",
    "--paddle-langs", "en", "devanagari", "ch",
    "--default-engine", "tesseract",
    "--fallback-engine", "paddle",
]
CLOUDFLARED_URL = (
    "https://github.com/cloudflare/cloudflared/releases/{release}/cloudflared-windows-amd64.exe"
)


def fail(message):
    print(f"\nBUILD STOPPED: {message}", file=sys.stderr)
    sys.exit(1)


def tesseract():
    if (TESSERACT_DIR / "tesseract.exe").is_file():
        print(f"  have {TESSERACT_DIR / 'tesseract.exe'}")
        return
    installed = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tesseract-OCR",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Tesseract-OCR",
    ]
    source = next((p for p in installed if (p / "tesseract.exe").is_file()), None)
    if source is None and shutil.which("winget"):
        print("  installing Tesseract with winget (UB-Mannheim.TesseractOCR)")
        subprocess.call([
            "winget", "install", "-e", "--id", "UB-Mannheim.TesseractOCR", "--silent",
            "--accept-package-agreements", "--accept-source-agreements",
        ])
        source = next((p for p in installed if (p / "tesseract.exe").is_file()), None)
    if source is None:
        fail(
            "Tesseract is not installed. Install the UB Mannheim build "
            "(https://github.com/UB-Mannheim/tesseract/wiki) or copy its folder to "
            f"{TESSERACT_DIR}"
        )
    (TESSERACT_DIR / "tessdata").mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(source / "tesseract.exe"), str(TESSERACT_DIR))
    for dll in source.glob("*.dll"):
        shutil.copy2(str(dll), str(TESSERACT_DIR))
    for name in ("eng.traineddata", "osd.traineddata"):
        if (source / "tessdata" / name).is_file():
            shutil.copy2(str(source / "tessdata" / name), str(TESSERACT_DIR / "tessdata"))
    if (source / "tessdata" / "configs").is_dir():
        shutil.copytree(
            str(source / "tessdata" / "configs"),
            str(TESSERACT_DIR / "tessdata" / "configs"),
            dirs_exist_ok=True,
        )
    print(f"  copied Tesseract from {source}")


def models_complete():
    if not BUILD_FILE.is_file():
        return False
    bundled = json.loads(BUILD_FILE.read_text(encoding="utf-8")).get("bundled", {})
    wanted = OCR_ARGS[OCR_ARGS.index("--langs") + 1 : OCR_ARGS.index("--paddle")]
    sizes = OCR_ARGS[OCR_ARGS.index("--paddle") + 1 : OCR_ARGS.index("--paddle-langs")]
    paddle_langs = OCR_ARGS[OCR_ARGS.index("--paddle-langs") + 1 : OCR_ARGS.index("--default-engine")]
    paddle = bundled.get("paddle") or []
    paddle = [paddle] if isinstance(paddle, str) else paddle
    return bool(
        set(wanted) <= set(bundled.get("langs") or [])
        and set(sizes) <= set(paddle)
        and set(paddle_langs) <= set(bundled.get("paddle_langs") or [])
    )


def newer_python():
    """A Python >= 3.9 to run paddle2onnx: the py launcher, else python3 on PATH."""
    candidates = []
    if shutil.which("py"):
        candidates += [["py", f"-3.{minor}"] for minor in (12, 11, 10, 9)]
    candidates += [["python3"], ["python"]]
    for command in candidates:
        try:
            out = subprocess.run(
                command + ["-c", "import sys; print(sys.version_info[:2] >= (3, 9))"],
                capture_output=True, text=True, timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode == 0 and out.stdout.strip() == "True":
            return command
    return None


def ocr_models():
    if models_complete():
        print(f"  have the OCR models ({BUILD_FILE.name})")
        return
    python = newer_python()
    if python is None:
        fail(
            "Converting the PaddleOCR models needs Python 3.9 or newer next to 3.8 "
            "(paddle2onnx has no 3.8 wheels). Install Python 3.11 from python.org, "
            "or copy packaging\\tessdata, packaging\\models and packaging\\ocr_build.json "
            "from a GitHub Actions build."
        )
    venv = ROOT / "build" / "venv_models"
    exe = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not exe.is_file():
        subprocess.check_call(python + ["-m", "venv", str(venv)])
    subprocess.check_call([str(exe), "-m", "pip", "install", "-q", "paddlepaddle", "paddle2onnx", "packaging"])
    subprocess.check_call([str(exe), str(HERE / "fetch_ocr_models.py")] + OCR_ARGS)
    if not models_complete():
        fail("the OCR models were not all fetched")


def cloudflared(release="latest"):
    target = CLOUDFLARED_DIR / "cloudflared.exe"
    if target.is_file():
        print(f"  have {target}")
        return
    CLOUDFLARED_DIR.mkdir(parents=True, exist_ok=True)
    local = ROOT / "cloudflared.exe"
    if release == "latest" and local.is_file():
        shutil.copy2(str(local), str(target))
        print(f"  copied {local}")
        return
    release = "latest/download" if release == "latest" else f"download/{release}"
    url = CLOUDFLARED_URL.format(release=release)
    print(f"  downloading {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "omr-build"})
    with urllib.request.urlopen(request, timeout=300) as response:
        data = response.read()
    if not data.startswith(b"MZ"):
        fail("the cloudflared download is not a Windows program")
    target.write_bytes(data)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--only", nargs="+", choices=["tesseract", "models", "cloudflared"],
        help="Prepare only these parts",
    )
    parser.add_argument(
        "--cloudflared-release", default="latest", help="e.g. 2025.9.1 (default: latest)"
    )
    args = parser.parse_args(argv)
    parts = args.only or ["tesseract", "models", "cloudflared"]
    if "tesseract" in parts:
        print("Tesseract")
        tesseract()
    if "models" in parts:
        print("OCR models")
        ocr_models()
    if "cloudflared" in parts:
        print("cloudflared")
        cloudflared(args.cloudflared_release)


if __name__ == "__main__":
    main()
