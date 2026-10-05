"""
Generate parity fixtures for the browser engine.

Renders synthetic sheets with known answers (src/synth), reads them with the
Python engine (src/pipeline.OMREngine) and writes, per scenario:

    <out>/<scenario>/template.json
    <out>/<scenario>/<file_id>.pgm      binary PGM (no image library needed in Node)
    <out>/<scenario>/expected.json      {file_id: {"truth": {...}, "python": ScanResult.to_dict()}}

Usage (from the repository root):
    python web/omr-browser/test/make_fixtures.py --out /tmp/omr_fixtures [--n 6]
"""

import argparse
import json
import random
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402

from src.benchmark.runner import PRESETS  # noqa: E402
from src.pipeline import OMREngine  # noqa: E402
from src.synth import (  # noqa: E402
    augment,
    default_spec,
    random_answers,
    render_sheet,
)
from src.synth.render import ZoneSpec  # noqa: E402


def write_pgm(path, image):
    h, w = image.shape[:2]
    with open(path, "wb") as handle:
        handle.write(f"P5\n{w} {h}\n255\n".encode("ascii"))
        handle.write(image.tobytes())


def scenario(out_dir, name, n, preset, seed, register=True, flip_every=0, zones=False, pre_processors=None):
    """Render n sheets, run the Python engine, write the fixture folder."""
    spec = default_spec(questions=40, with_zones=False)
    if zones:
        spec.zones = [
            ZoneSpec("sheet_id", "barcode", [700, 110], [460, 120], {"formats": ["Code128"]}),
            ZoneSpec("qr", "qrcode", [560, 90], [130, 130]),
        ]
    folder = Path(out_dir, name)
    folder.mkdir(parents=True, exist_ok=True)
    if pre_processors is None and not register:
        pre_processors = []
    template = spec.to_template(pre_processors=pre_processors)
    template_path = folder / "template.json"
    template_path.write_text(json.dumps(template, indent=1))
    engine = OMREngine(template_path)
    rng = random.Random(seed)
    options = PRESETS[preset]["augment"]
    expected = {}
    for index in range(n):
        answers = random_answers(spec, rng, blank_rate=0.05, multi_rate=0.03)
        image, truth = render_sheet(spec, answers, rng=rng, mark_style="mixed", erasures=4)
        if options:
            flip = bool(flip_every) and index % flip_every == flip_every - 1
            image, _ = augment(image, rng, flip_180=flip, **options)
        file_id = f"{name}_{index:03d}"
        write_pgm(folder / f"{file_id}.pgm", image)
        result = engine.scan(image, file_id, keep_images=False).to_dict()
        values = dict(truth["answers"])
        values.update(truth["zones"])
        expected[file_id] = {"truth": values, "python": result}
    (folder / "expected.json").write_text(json.dumps(expected))
    return folder


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"}


def sample_scenarios(out_dir, max_images=4):
    """Repository samples (real photos/scans): fixtures compare against the Python engine only."""
    names = []
    for template_path in sorted(ROOT.glob("samples/**/template.json")):
        folder = template_path.parent
        template = json.loads(template_path.read_text())
        images = sorted(
            p
            for p in folder.rglob("*")
            if p.suffix.lower() in IMAGE_SUFFIXES and "marker" not in p.name.lower() and "answer_key" not in p.name.lower()
        )[:max_images]
        if not images:
            continue
        name = "sample_" + "_".join(folder.relative_to(ROOT / "samples").parts)
        target = Path(out_dir, name)
        target.mkdir(parents=True, exist_ok=True)
        (target / "template.json").write_text(json.dumps(template))
        if (folder / "config.json").exists():
            (target / "config.json").write_text((folder / "config.json").read_text())
        assets = {}
        for processor in template.get("preProcessors", []):
            relative = processor.get("options", {}).get("relativePath")
            if relative:
                marker = cv2.imread(str(folder / relative), cv2.IMREAD_GRAYSCALE)
                asset_file = Path(relative).stem + ".asset.pgm"
                write_pgm(target / asset_file, marker)
                assets[relative] = asset_file
        (target / "assets.json").write_text(json.dumps(assets))
        try:
            engine = OMREngine(template_path)
        except Exception as error:  # templates the Python engine can't load either
            print(f"skip {name}: {error}", file=sys.stderr)
            continue
        expected = {}
        for image_path in images:
            image = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
            if image is None:
                continue
            file_id = image_path.stem
            write_pgm(target / f"{file_id}.pgm", image)
            result = engine.scan(image, file_id, keep_images=False).to_dict()
            expected[file_id] = {"truth": None, "python": result}
        (target / "expected.json").write_text(json.dumps(expected))
        names.append(name)
    return names


SCENARIOS = {
    # name: kwargs
    "clean": dict(preset="clean", seed=11, register=False),
    "scan": dict(preset="scan", seed=12),
    "phone": dict(preset="phone", seed=13, flip_every=3),
    "zones": dict(preset="scan", seed=14, zones=True),
    "croppage": dict(
        preset="scan",
        seed=15,
        pre_processors=[{"name": "CropPage", "options": {"morphKernel": [10, 10]}}],
    ),
}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=None, help="output folder (default: a new temp dir)")
    parser.add_argument("--n", type=int, default=6, help="sheets per scenario")
    parser.add_argument("--only", nargs="*", default=None, help="scenario names")
    parser.add_argument("--samples", action="store_true", help="also convert the repository samples")
    args = parser.parse_args(argv)
    out = Path(args.out or tempfile.mkdtemp(prefix="omr_browser_fixtures_"))
    out.mkdir(parents=True, exist_ok=True)
    names = args.only or list(SCENARIOS)
    for name in names:
        kwargs = dict(SCENARIOS[name])
        n = args.n * 2 if name == "phone" else args.n
        scenario(out, name, n, **kwargs)
    samples = sample_scenarios(out) if args.samples else []
    (out / "index.json").write_text(json.dumps({"scenarios": names, "samples": samples}))
    print(out)


if __name__ == "__main__":
    main()
