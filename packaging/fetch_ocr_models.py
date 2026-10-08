"""
Fetch the OCR models a build bundles, and write the build's OCR choices.

Nothing downloaded here is committed to git. See packaging/ocr_models.md.

    python packaging/fetch_ocr_models.py                       # Tesseract "best" eng, no PaddleOCR
    python packaging/fetch_ocr_models.py --tessdata best fast --langs eng hin
    python packaging/fetch_ocr_models.py --paddle mobile --paddle-langs en devanagari \
        --default-engine tesseract --fallback-engine paddle
    python packaging/fetch_ocr_models.py --paddle server --paddle-onnx-dir D:\\converted

Outputs (all git-ignored, picked up by packaging/omr.spec and by a source
checkout at run time):
    packaging/tessdata/best/<lang>.traineddata     tessdata_best (accurate, larger)
    packaging/tessdata/fast/<lang>.traineddata     tessdata_fast (smaller, faster)
    packaging/models/paddleocr/*.onnx + *_dict.txt PP-OCRv5 det / rec models
    packaging/ocr_build.json                       default ocr_params for this build

PaddleOCR models: the official PP-OCRv5 inference models are downloaded from
PaddlePaddle's model server and converted to ONNX with paddle2onnx, which must
be installed on the build machine only (`pip install paddle2onnx`; the app
itself never needs PaddlePaddle). If you already have converted ONNX files,
pass --paddle-onnx-dir to copy them instead.

Standard library only; runs on Python 3.8.
"""

import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
TESSDATA_DIR = HERE / "tessdata"
PADDLE_DIR = HERE / "models" / "paddleocr"
BUILD_FILE = HERE / "ocr_build.json"

TESSDATA_URL = {
    "best": "https://github.com/tesseract-ocr/tessdata_best/raw/main/{lang}.traineddata",
    "fast": "https://github.com/tesseract-ocr/tessdata_fast/raw/main/{lang}.traineddata",
}

# Official PaddleOCR (PaddleX) inference model archives
PADDLE_URL = (
    "https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/"
    "paddle3.0.0/{name}_infer.tar"
)
# (kind, size, lang) -> official model name; file names match src/readers/paddle_ocr.py
PADDLE_MODELS = {
    ("det", "mobile", None): "PP-OCRv5_mobile_det",
    ("det", "server", None): "PP-OCRv5_server_det",
    ("rec", "mobile", "ch"): "PP-OCRv5_mobile_rec",
    ("rec", "server", "ch"): "PP-OCRv5_server_rec",
    ("rec", "mobile", "en"): "en_PP-OCRv5_mobile_rec",
    ("rec", "mobile", "devanagari"): "devanagari_PP-OCRv5_mobile_rec",
}
DICT_NAMES = {
    "PP-OCRv5_mobile_rec": "ppocrv5_dict.txt",
    "PP-OCRv5_server_rec": "ppocrv5_dict.txt",
    "en_PP-OCRv5_mobile_rec": "ppocrv5_en_dict.txt",
    "devanagari_PP-OCRv5_mobile_rec": "ppocrv5_devanagari_dict.txt",
}


def download(url, target=None):
    print(f"  downloading {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "omr-build"})
    with urllib.request.urlopen(request, timeout=120) as response:
        data = response.read()
    if target is not None:
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return data


def fetch_tessdata(variants, langs, force=False):
    for variant in variants:
        for lang in langs:
            target = TESSDATA_DIR / variant / f"{lang}.traineddata"
            if target.is_file() and not force:
                print(f"  have {target.relative_to(HERE)}")
                continue
            download(TESSDATA_URL[variant].format(lang=lang), target)


def dictionary_from_yml(text):
    """character_dict list from a PaddleX inference.yml (no PyYAML needed)."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith("character_dict:"):
            indent = len(line) - len(line.lstrip())
            chars = []
            for item in lines[i + 1 :]:
                stripped = item.strip()
                if not stripped.startswith("- ") and stripped != "-":
                    if len(item) - len(item.lstrip()) <= indent and stripped:
                        break
                    continue
                value = item.strip()[2:] if stripped != "-" else ""
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                    value = value[1:-1]
                    if '\\"' in value or "''" in value:
                        value = value.replace('\\"', '"').replace("''", "'")
                chars.append(value)
            return chars
    return None


def convert_to_onnx(model_dir, onnx_path):
    model_dir = Path(model_dir)
    model_file = "inference.json" if (model_dir / "inference.json").is_file() else "inference.pdmodel"
    command = [
        sys.executable, "-m", "paddle2onnx",
        "--model_dir", str(model_dir),
        "--model_filename", model_file,
        "--params_filename", "inference.pdiparams",
        "--save_file", str(onnx_path),
        "--opset_version", "14",
    ]
    exe = shutil.which("paddle2onnx")
    if exe:
        command = [exe] + command[3:]
    print("  converting: " + " ".join(command))
    subprocess.check_call(command)


def fetch_paddle(size, langs, onnx_dir=None, force=False):
    PADDLE_DIR.mkdir(parents=True, exist_ok=True)
    wanted = [PADDLE_MODELS[("det", size, None)]]
    for lang in langs:
        key = ("rec", size, lang)
        if key not in PADDLE_MODELS:
            key = ("rec", "mobile", lang)  # only mobile exists for en / devanagari
        wanted.append(PADDLE_MODELS[key])
    for name in wanted:
        onnx_path = PADDLE_DIR / f"{name}.onnx"
        dict_name = DICT_NAMES.get(name)
        if onnx_path.is_file() and not force:
            print(f"  have {onnx_path.relative_to(HERE)}")
            continue
        if onnx_dir:
            source = Path(onnx_dir)
            shutil.copy2(str(source / f"{name}.onnx"), str(onnx_path))
            if dict_name and (source / dict_name).is_file():
                shutil.copy2(str(source / dict_name), str(PADDLE_DIR / dict_name))
            continue
        with tempfile.TemporaryDirectory() as tmp:
            data = download(PADDLE_URL.format(name=name))
            with tarfile.open(fileobj=io.BytesIO(data)) as archive:
                for member in archive.getmembers():
                    # Only plain files, no absolute paths or ".."
                    if member.isfile() and ".." not in Path(member.name).parts and not os.path.isabs(member.name):
                        archive.extract(member, tmp)
            model_dir = next(
                (p.parent for p in Path(tmp).rglob("inference.pdiparams")), None
            )
            if model_dir is None:
                raise SystemExit(f"{name}: no inference model in the archive")
            if dict_name:
                yml = model_dir / "inference.yml"
                chars = dictionary_from_yml(yml.read_text(encoding="utf-8")) if yml.is_file() else None
                if not chars:
                    raise SystemExit(f"{name}: no character_dict in inference.yml")
                (PADDLE_DIR / dict_name).write_text("\n".join(chars) + "\n", encoding="utf-8")
            convert_to_onnx(model_dir, onnx_path)


def write_build_file(args):
    fallback = args.fallback_engine
    if fallback == "paddle" and not args.paddle:
        print("  no PaddleOCR models bundled: fallback engine set to none")
        fallback = "none"
    params = {
        "default_engine": args.default_engine,
        "fallback_engine": fallback,
        "tessdata": args.tessdata[0],
        "langs": ["eng"] if "eng" in args.langs else args.langs[:1],
    }
    if args.paddle:
        params["paddle_det_model"] = args.paddle
        params["paddle_rec_model"] = args.paddle
        params["paddle_lang"] = args.paddle_langs[0]
    elif args.default_engine == "paddle":
        raise SystemExit("--default-engine paddle needs --paddle mobile|server")
    BUILD_FILE.write_text(
        json.dumps({"ocr_params": params, "bundled": {
            "tessdata": args.tessdata, "langs": args.langs,
            "paddle": args.paddle, "paddle_langs": args.paddle_langs if args.paddle else [],
        }}, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {BUILD_FILE.relative_to(HERE)}: {json.dumps(params)}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tessdata", nargs="+", choices=["best", "fast"], default=["best"],
                        help="Tesseract model sets to bundle; the first is the default")
    parser.add_argument("--langs", nargs="+", default=["eng"],
                        help="Tesseract languages, e.g. eng hin (traineddata names)")
    parser.add_argument("--paddle", choices=["mobile", "server"], default=None,
                        help="Bundle PaddleOCR PP-OCRv5 models of this size (default: none)")
    parser.add_argument("--paddle-langs", nargs="+", default=["en"],
                        choices=["en", "ch", "devanagari"],
                        help="PaddleOCR recognition languages; the first is the default")
    parser.add_argument("--paddle-onnx-dir", default=None,
                        help="Copy already converted ONNX models (+ dict files) from here")
    parser.add_argument("--default-engine", choices=["tesseract", "paddle"], default="tesseract")
    parser.add_argument("--fallback-engine", choices=["none", "tesseract", "paddle"], default="none")
    parser.add_argument("--force", action="store_true", help="Download again")
    args = parser.parse_args(argv)
    for lang in args.langs:
        if not re.fullmatch(r"[A-Za-z_]+", lang):
            raise SystemExit(f"bad language name: {lang}")

    print("Tesseract models")
    fetch_tessdata(args.tessdata, args.langs, args.force)
    if args.paddle:
        print("PaddleOCR models")
        fetch_paddle(args.paddle, args.paddle_langs, args.paddle_onnx_dir, args.force)
    write_build_file(args)


if __name__ == "__main__":
    main()
