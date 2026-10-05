"""
Generate a template from sample sheets.

    python -m src.template_gen --images sheets/ [--labels labels.csv] --out out/

labels.csv: first column file_name, the other columns are field labels (an
empty cell means "no bubble marked"; a missing column means "unknown").
Writes template.json, reference.png, overlay.png and generation_report.json.
"""

import argparse
import csv
import json
import sys
from pathlib import Path

import cv2

from src.template_gen.engine import generate_template

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}


def read_labels_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader)
        rows = {}
        for row in reader:
            if not row or not row[0].strip():
                continue
            values = {
                key.strip(): (row[k] if k < len(row) else "").strip("\r\n")
                for k, key in enumerate(header)
                if k > 0 and key.strip()
            }
            rows[row[0].strip()] = values
    return rows


def load_images(folder):
    paths = sorted(
        p for p in Path(folder).iterdir() if p.suffix.lower() in IMAGE_SUFFIXES
    )
    images = []
    for path in paths:
        image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if image is None:
            print(f"warning: could not read {path}", file=sys.stderr)
            continue
        images.append((path, image))
    return images


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m src.template_gen", description=__doc__.split("\n\n")[0]
    )
    parser.add_argument("--images", required=True, help="folder of sample sheets")
    parser.add_argument("--labels", help="CSV with file_name + one column per field")
    parser.add_argument("--out", required=True, help="output folder")
    parser.add_argument(
        "--page-size", nargs=2, type=int, metavar=("W", "H"), help="canonical page"
    )
    parser.add_argument("--max-page-width", type=int, default=1700)
    parser.add_argument("--no-self-check", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    loaded = load_images(args.images)
    if not loaded:
        print(f"No images found in {args.images}", file=sys.stderr)
        return 2
    labels = None
    if args.labels:
        table = read_labels_csv(args.labels)
        labels = [table.get(p.name, table.get(p.stem)) for p, _ in loaded]
        missing = [p.name for (p, _), lab in zip(loaded, labels) if lab is None]
        if missing:
            print(f"warning: no labels for {missing}", file=sys.stderr)
    options = {
        "page_size": args.page_size,
        "max_page_width": args.max_page_width,
        "self_check": not args.no_self_check,
    }
    result = generate_template([image for _, image in loaded], labels, options)
    result.report["input_files"] = [p.name for p, _ in loaded]
    out_dir = result.save(args.out)
    report = result.report
    summary = {
        "out": str(out_dir),
        "blocks": len(result.template["fieldBlocks"]),
        "zones": len(result.template.get("zones", {})),
        "label_agreement": report["label_agreement"],
        "needs_verification": len(report["needs_verification"]),
        "warnings": report["warnings"],
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
