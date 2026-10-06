"""
Generate parity fixtures for the browser engine.

Renders synthetic sheets with known answers (src/synth), reads them with the
Python engine (src/pipeline.OMREngine) and writes, per scenario:

    <out>/<scenario>/template.json
    <out>/<scenario>/<file_id>.pgm      binary PGM (no image library needed in Node),
                                        or .ppm (binary RGB) for colour sheets
    <out>/<scenario>/config.json        when the scenario sets config options
    <out>/<scenario>/expected.json      {file_id: {"truth": {...}, "python": ScanResult.to_dict()}}
    <out>/units.json                    direct comparisons of building blocks
                                        (colour dropout, thick ellipses, the
                                        built-in linear barcode decoder)

Usage (from the repository root):
    python web/omr-browser/test/make_fixtures.py --out /tmp/omr_fixtures [--n 6]
"""

import argparse
import base64
import json
import random
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from src.benchmark.runner import PRESETS  # noqa: E402
from src.pipeline import OMREngine  # noqa: E402
from src.synth import (  # noqa: E402
    augment,
    default_spec,
    random_answers,
    render_sheet,
)
from src.color import apply_dropout, normalize_dropout  # noqa: E402
from src.readers import linear  # noqa: E402
from src.synth.render import ZoneSpec  # noqa: E402

PINK = "#E8618C"  # form print colour (src/tests/test_color_dropout.py)
BLUE_PEN = "#1F3C9A"


def write_pgm(path, image):
    h, w = image.shape[:2]
    with open(path, "wb") as handle:
        handle.write(f"P5\n{w} {h}\n255\n".encode("ascii"))
        handle.write(image.tobytes())


def write_ppm(path, image):
    """Binary PPM, RGB order (the image is BGR)."""
    h, w = image.shape[:2]
    with open(path, "wb") as handle:
        handle.write(f"P6\n{w} {h}\n255\n".encode("ascii"))
        handle.write(np.ascontiguousarray(image[:, :, ::-1]).tobytes())


def scenario(
    out_dir,
    name,
    n,
    preset,
    seed,
    register=True,
    flip_every=0,
    zones=False,
    pre_processors=None,
    zone_specs=None,
    template_extra=None,
    config=None,
    render=None,
    after_render=None,
    border=0,
    capture=None,
):
    """
    Render n sheets, run the Python engine, write the fixture folder.

    zone_specs: [ZoneSpec] drawn and read; template_extra: top-level template keys
    ("fieldBlocks" entries update the blocks); config: config.json contents;
    render(index, rng) -> extra render_sheet kwargs (plus "border", "zone_values");
    after_render(index, image, truth) edits the rendered sheet; border: printed
    block borders (px); capture(index, image, rng) replaces the preset's augment.
    """
    spec = default_spec(questions=40, with_zones=False)
    spec.block_border = border
    if zones:
        spec.zones = [
            ZoneSpec("sheet_id", "barcode", [700, 110], [460, 120], {"formats": ["Code128"]}),
            ZoneSpec("qr", "qrcode", [560, 90], [130, 130]),
        ]
    if zone_specs:
        spec.zones = list(zone_specs)
    folder = Path(out_dir, name)
    folder.mkdir(parents=True, exist_ok=True)
    if pre_processors is None and not register:
        pre_processors = []
    template = spec.to_template(pre_processors=pre_processors)
    for key, value in (template_extra or {}).items():
        if key == "fieldBlocks":
            for block, options in value.items():
                template["fieldBlocks"][block].update(options)
        else:
            template[key] = value
    template_path = folder / "template.json"
    template_path.write_text(json.dumps(template, indent=1))
    if config:
        (folder / "config.json").write_text(json.dumps(config, indent=1))
    engine = OMREngine(template_path)
    rng = random.Random(seed)
    options = PRESETS[preset]["augment"]
    expected = {}
    for index in range(n):
        answers = random_answers(spec, rng, blank_rate=0.05, multi_rate=0.03)
        extra = render(index, rng) if render else {}
        spec.block_border = extra.pop("border", border)
        zone_values = extra.pop("zone_values", None)
        image, truth = render_sheet(
            spec, answers, zone_values, rng=rng, mark_style="mixed", erasures=4, **extra
        )
        spec.block_border = border
        if after_render:
            image = after_render(index, image, truth)
        if capture:
            image = capture(index, image, rng)
        elif options:
            flip = bool(flip_every) and index % flip_every == flip_every - 1
            image, _ = augment(image, rng, flip_180=flip, **options)
        file_id = f"{name}_{index:03d}"
        if image.ndim == 3:
            write_ppm(folder / f"{file_id}.ppm", image)
        else:
            write_pgm(folder / f"{file_id}.pgm", image)
        result = engine.scan(image, file_id, keep_images=False).to_dict()
        values = dict(truth["answers"])
        values.update(truth["zones"])
        expected[file_id] = {"truth": values, "python": result}
    (folder / "expected.json").write_text(json.dumps(expected))
    return folder


def ean13(digits12):
    """12 digits -> the 13-digit EAN with its check digit."""
    total = sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(digits12))
    return digits12 + str((10 - total % 10) % 10)


def builtin_zone(name, origin, dimensions, fmt, **options):
    """
    A barcode zone read by the built-in scanline decoder only. The synth draws
    codes scaled to fit the zone; these zone sizes give ~3 px modules.
    """
    return ZoneSpec(name, "barcode", origin, dimensions, {"formats": [fmt], "engines": ["builtin"], **options})


def barcode_values(index, rng):
    return {
        "zone_values": {
            "code128": f"SHEET-{rng.randint(100000, 999999)}",
            "code39": f"ID-{rng.randint(10000, 99999)}",
            "itf": "".join(rng.choice("0123456789") for _ in range(14)),
            "ean": ean13("".join(rng.choice("0123456789") for _ in range(12))),
        }
    }


def colour_sheet(index, rng):
    # Pink print; blue pen on every other sheet, black/pencil marks otherwise
    out = {"print_color": PINK}
    if index % 2:
        out["ink_color"] = BLUE_PEN
    return out


def rules_sheet(index, rng):
    value = f"SHEET-{rng.randint(100000, 999999)}"
    return {"zone_values": {"sheet_id": value, "sheet_id_copy": value}}


def erase_first_barcode(index, image, truth):
    """Every third sheet loses its main barcode: the lazy fallback zone is read."""
    if index % 3 == 0:
        image[80:240, 680:1200] = 255
        truth["zones"]["sheet_id"] = truth["zones"]["sheet_id_copy"]
    return image


def light_background(index, image, rng):
    """The page on a light desk: CropPage's fixed search fails, its adaptive retry works."""
    h, w = image.shape[:2]
    pad = 120
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    dst = src + pad + np.float32([[rng.uniform(-30, 30), rng.uniform(-30, 30)] for _ in range(4)])
    background = [150, 190, 215][index % 3]
    canvas = cv2.warpPerspective(
        image, cv2.getPerspectiveTransform(src, dst), (w + 2 * pad, h + 2 * pad), borderValue=background
    )
    noise = np.random.default_rng(index).normal(0, 3, canvas.shape)
    return np.clip(canvas + noise, 0, 255).astype(np.uint8)


def rectify_sheet(index, rng):
    # Local misprints of the MCQ blocks; every fourth sheet has no printed border
    warps = {
        "MCQ_1": (rng.uniform(-7, 7), rng.uniform(-7, 7), rng.uniform(-1, 1)),
        "MCQ_2": (rng.uniform(-5, 5), rng.uniform(-5, 5), rng.uniform(-0.6, 0.6)),
    }
    out = {"block_warps": warps}
    if index % 4 == 3:
        out["border"] = 0
    return out


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


RULES_TEMPLATE = {
    "customLabels": {"roll": ["roll1..6"]},
    "validate": {
        "roll": {"length": 6, "allowGaps": False, "pattern": "\\d+"},
        "q1": {"allowed": ["A", "B", "C"], "onFail": "flag"},
        "q2": {"allowed": ["A", "B", "C"], "onFail": "blank"},
        "q3": {"required": True, "onFail": "both"},
        "sheet_id": {"pattern": "SHEET-\\d{6}"},
        "q_pair": {"pattern": "[AB]", "onFail": "flag"},
        "q6": {"range": [1, 3], "onFail": "flag"},
        "q7": {"length": [2, 3], "onFail": "flag"},
        "q8": {"leadingZeros": "forbid", "allowed": ["A", "B", "C", "D"], "onFail": "flag"},
    },
    "checks": [
        {"name": "q_pair", "sources": ["q4", "q24"], "onConflict": "review"},
        {
            "name": "id_digits",
            "sources": ["sheet_id", "roll"],
            "normalize": "digits",
            "reviewOnConflict": False,
        },
        {
            "name": "q5_strict",
            "sources": ["q5", "q25"],
            "onConflict": "error",
            "onMissing": "review",
            "normalize": "upper",
        },
    ],
}

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
    # Light backgrounds: two sheets in three need CropPage's adaptive page search
    "croppage_light": dict(
        preset="clean",
        seed=16,
        capture=light_background,
        pre_processors=[{"name": "CropPage", "options": {"morphKernel": [10, 10]}}],
    ),
    # Colour sheets: pink print dropped by the red channel (partial strength),
    # a too_few_marks sheet review and a zone with its own (grey) dropout
    "color_red": dict(
        preset="scan",
        seed=21,
        render=colour_sheet,
        template_extra={"colorDropout": {"mode": "red", "strength": 0.8}},
        config={"review_params": {"min_marked_bubbles": 46}},
        zone_specs=[builtin_zone("sheet_id", [680, 80], [520, 160], "Code128", colorDropout="grey")],
    ),
    # Lab colour-distance dropout on phone photos (rotated sheets included)
    "color_lab": dict(
        preset="phone",
        seed=22,
        flip_every=3,
        render=colour_sheet,
        template_extra={"colorDropout": {"mode": "color", "color": PINK, "tolerance": 35}},
        zone_specs=[builtin_zone("sheet_id", [680, 80], [520, 160], "Code128", colorDropout={"mode": "max"})],
    ),
    # Block borders: rectification onto the printed border, local misprints
    "rectify": dict(
        preset="scan",
        seed=23,
        border=8,
        render=rectify_sheet,
        config={"alignment_params": {"rectify_on_border": True, "rectify_search_px": 20}},
        template_extra={"fieldBlocks": {"Roll": {"rectifyOnBorder": False}, "MCQ_2": {"borderPadding": 8}}},
    ),
    "fixed": dict(
        preset="scan",
        seed=24,
        config={"threshold_params": {"mode": "fixed", "fixed_threshold": 140, "fixed_min_fill_ratio": 0.15}},
    ),
    # validate / checks, and a barcode with a lazy fallback zone
    "rules": dict(
        preset="scan",
        seed=25,
        template_extra=RULES_TEMPLATE,
        zone_specs=[
            builtin_zone("sheet_id", [680, 80], [520, 160], "Code128", fallbackZone="sheet_id_copy"),
            builtin_zone("sheet_id_copy", [680, 1450], [520, 160], "Code128"),
        ],
        render=rules_sheet,
        after_render=erase_first_barcode,
    ),
    # The built-in linear decoder (zxing-wasm not needed): Code 128, Code 39, ITF, EAN-13
    "barcodes": dict(
        preset="scan",
        seed=26,
        zone_specs=[
            builtin_zone("code128", [130, 80], [520, 160], "Code128"),
            builtin_zone("code39", [680, 80], [470, 160], "Code39"),
            builtin_zone("itf", [130, 1450], [480, 160], "ITF"),
            builtin_zone("ean", [700, 1450], [360, 175], "EAN13"),
        ],
        render=barcode_values,
    ),
}


def b64(array):
    return base64.b64encode(np.ascontiguousarray(array).tobytes()).decode("ascii")


def render_code(text, fmt, scale=2.0, height=60, quiet=12, pad=20):
    """A zxing-generated linear code (as in src/tests/test_barcode_engines.py)."""
    import zxingcpp

    symbol = zxingcpp.create_barcode(text, zxingcpp.barcode_format_from_str(fmt))
    image = np.array(zxingcpp.write_barcode_to_image(symbol, scale=1), np.uint8)
    row = image[image.shape[0] // 2]
    row = np.concatenate([np.full(quiet, 255, np.uint8), row, np.full(quiet, 255, np.uint8)])
    image = np.repeat(row[None, :], height, axis=0)
    width = int(round(image.shape[1] * scale))
    image = cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA)
    return cv2.copyMakeBorder(image, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=255)


def unit_fixtures(out_dir, seed=7):
    """Building-block outputs compared one to one with the browser engine."""
    rs = np.random.RandomState(seed)
    units = {}

    # Colour dropout: random colours plus bands of print / pen colours
    h, w = 48, 64
    image = rs.randint(0, 256, (h, w, 3)).astype(np.uint8)
    palette = ["#E8618C", "#1F3C9A", "#202020", "#6E6E6E", "#2A3F7A", "#F2A900", "#D0021B", "#7ED321", "#FFFFFF", "#9B9B9B"]
    for i, colour in enumerate(palette):
        text = colour.lstrip("#")
        rgb = [int(text[k : k + 2], 16) for k in (0, 2, 4)]
        jitter = rs.randint(-20, 21, (4, w, 3))
        image[i * 4 : i * 4 + 4] = np.clip(np.array(rgb[::-1])[None, None] + jitter, 0, 255)
    specs = [
        "grey",
        "red",
        "green",
        "blue",
        "max",
        {"mode": "red", "strength": 0.35},
        {"mode": "max", "strength": 0.8},
        {"mode": "color", "color": "#E8618C"},
        {"mode": "color", "color": "#E8618C", "tolerance": 35, "strength": 0.7},
        {"mode": "color", "color": "#1F3C9A", "tolerance": 80},
        {"mode": "color", "color": "#F2A900", "tolerance": 12.5},
    ]
    units["dropout"] = {
        "width": w,
        "height": h,
        "rgb": b64(image[:, :, ::-1]),
        "cases": [{"spec": spec, "gray": b64(apply_dropout(image, normalize_dropout(spec)))} for spec in specs],
    }

    # cv2.ellipse(thickness=2) masks, partly outside the image too
    ellipses = []
    for _ in range(60):
        mw, mh = int(rs.randint(20, 90)), int(rs.randint(20, 90))
        cx, cy = int(rs.randint(-10, mw + 10)), int(rs.randint(-10, mh + 10))
        ax, ay = int(rs.randint(1, 30)), int(rs.randint(1, 30))
        mask = np.zeros((mh, mw), np.uint8)
        cv2.ellipse(mask, (cx, cy), (ax, ay), 0, 0, 360, 255, 2)
        ellipses.append({"w": mw, "h": mh, "cx": cx, "cy": cy, "ax": ax, "ay": ay, "mask": b64(mask)})
    units["ellipses"] = ellipses

    # Built-in linear decoder on clean and degraded renderings
    rng = random.Random(seed)
    makers = {
        "Code128": lambda: rng.choice(["SHEET-", "ab", "Z9-", ""]) + str(rng.randint(1, 10 ** rng.randint(1, 12))),
        "Code39": lambda: rng.choice(["ID-", "A ", "X$/", ""]) + str(rng.randint(1, 99999)),
        "ITF": lambda: "".join(rng.choice("0123456789") for _ in range(2 * rng.randint(3, 8))),
        "EAN13": lambda: ean13("".join(rng.choice("0123456789") for _ in range(12))),
        "EAN8": lambda: "".join(rng.choice("0123456789") for _ in range(7)),
        "UPCA": lambda: "".join(rng.choice("0123456789") for _ in range(11)),
    }
    codes = []
    for fmt, make in makers.items():
        for k in range(8):
            text = make()
            try:
                image = render_code(text, fmt, scale=rng.choice([1.6, 2.0, 2.5, 3.0]))
            except Exception:
                continue
            if k % 4 == 1:
                image = cv2.GaussianBlur(image, (0, 0), 0.9)
            elif k % 4 == 2:
                noise = np.random.default_rng(k).normal(0, 12, image.shape)
                image = np.clip(image + noise, 0, 255).astype(np.uint8)
            elif k % 4 == 3:
                ih, iw = image.shape
                matrix = cv2.getRotationMatrix2D((iw / 2, ih / 2), rng.uniform(-4, 4), 1)
                image = cv2.warpAffine(image, matrix, (iw, ih), borderValue=255)
            if k == 5:
                image = np.ascontiguousarray(cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE))
            formats = [fmt] if k % 2 else None
            found = linear.decode(image, formats, {})
            codes.append(
                {
                    "format": fmt,
                    "formats": formats,
                    "width": int(image.shape[1]),
                    "height": int(image.shape[0]),
                    "gray": b64(image),
                    "python": found,
                }
            )
    units["linear"] = codes
    (Path(out_dir) / "units.json").write_text(json.dumps(units))



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
    unit_fixtures(out)
    samples = sample_scenarios(out) if args.samples else []
    (out / "index.json").write_text(json.dumps({"scenarios": names, "samples": samples}))
    print(out)


if __name__ == "__main__":
    main()
