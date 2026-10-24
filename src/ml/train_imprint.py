"""
Train the scanner imprint reader (src/readers/imprint.py).

1. Cut the imprint line out of labelled scans, exactly as the reader does:

    python -m src.ml.train_imprint crops --template inputs/teri/template.json \\
        --input scans/ --label "S{stem}" --out data/imprint

   --label builds each sheet's truth from its file: {stem} is the file name
   without extension, {name} with it (a scanner that numbers its files the
   way it numbers its imprints labels every sheet for free). --truth reads a
   CSV of file,label instead. Crops go to <out>/<group>/<file>.png, where the
   group is the sheet's parent folder (a scanning batch), and manifest.csv.

2. Train (PyTorch, requirements.ml.txt) and export ONNX + JSON sidecar:

    python -m src.ml.train_imprint train --data data/imprint \\
        --out packaging/models/imprint/teri_imprint --direction rot90ccw

   Real crops are mixed with synthetic dot-matrix lines of random strings so
   the model reads any number, not only the ones it saw. --holdout <group>
   keeps one batch out and reports its exact-match rate (leave-one-batch-out:
   run once per group); --holdout all does every group in turn.

3. Score a model on crops:

    python -m src.ml.train_imprint eval --data data/imprint \\
        --model packaging/models/imprint/teri_imprint.onnx
"""

import argparse
import csv
import json
import random
import time
from pathlib import Path

import cv2
import numpy as np

from src.readers import imprint

# 5 x 7 dot-matrix glyphs, one string per row, "#" = dot
GLYPHS = {
    "0": [".###.", "#...#", "#..##", "#.#.#", "##..#", "#...#", ".###."],
    "1": ["..#..", ".##..", "..#..", "..#..", "..#..", "..#..", ".###."],
    "2": [".###.", "#...#", "....#", "...#.", "..#..", ".#...", "#####"],
    "3": ["#####", "...#.", "..#..", "...#.", "....#", "#...#", ".###."],
    "4": ["...#.", "..##.", ".#.#.", "#..#.", "#####", "...#.", "...#."],
    "5": ["#####", "#....", "####.", "....#", "....#", "#...#", ".###."],
    "6": ["..##.", ".#...", "#....", "####.", "#...#", "#...#", ".###."],
    "7": ["#####", "....#", "...#.", "..#..", ".#...", ".#...", ".#..."],
    "8": [".###.", "#...#", "#...#", ".###.", "#...#", "#...#", ".###."],
    "9": [".###.", "#...#", "#...#", ".####", "....#", "...#.", ".##.."],
    "S": [".####", "#....", "#....", ".###.", "....#", "....#", "####."],
}


# ------------------------------------------------------------------ crops
def label_for(path, pattern=None, truth=None):
    if truth is not None:
        return truth.get(path.name) or truth.get(path.stem)
    return pattern.format(stem=path.stem, name=path.name)


def export_crops(template, inputs, out, label=None, truth_csv=None, zone_name=None):
    from src.pipeline import STATUS_ERROR, OMREngine
    from src.readers import text_reader

    truth = None
    if truth_csv:
        with open(truth_csv, newline="") as handle:
            truth = {row[0]: row[1] for row in csv.reader(handle) if len(row) >= 2}
    engine = OMREngine(template)
    zones = [z for z in engine.template.zones if z.type == "imprint"]
    if zone_name:
        zones = [z for z in zones if z.name == zone_name]
    if not zones:
        raise SystemExit("the template has no imprint zone")
    zone = zones[0]
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    rows, missed = [], []
    paths = sorted(
        p for root in inputs for p in (Path(root).rglob("*") if Path(root).is_dir() else [Path(root)])
        if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".tif", ".tiff")
    )
    for path in paths:
        text = label_for(path, label, truth)
        if not text:
            missed.append((str(path), "no label"))
            continue
        for result in engine.scan_path(path, keep_images=True):
            if result.status == STATUS_ERROR or result.aligned_image is None:
                missed.append((str(path), result.error or "error"))
                continue
            crop = zone.crop(result.aligned_image)
            if crop.ndim == 3:
                crop = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
            model = imprint.get_model(zone.options.get("model"))
            direction = zone.options.get("direction") or (
                model.direction if model else "horizontal"
            )
            strip = text_reader.rotate_crop(crop, direction)
            box = imprint.locate_line(strip, zone.options.get("charHeight"))
            if box is None:
                missed.append((str(path), "line not found"))
                continue
            group = path.parent.name
            target = out / group / f"{path.stem}.png"
            target.parent.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(target), imprint.cut_line(strip, box))
            rows.append((str(target.relative_to(out)), text, group, path.name))
    with open(out / "manifest.csv", "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["path", "label", "group", "source"])
        writer.writerows(rows)
    print(f"{len(rows)} crops in {out}; {len(missed)} sheets skipped")
    for path, reason in missed[:20]:
        print(f"  skipped {path}: {reason}")
    return rows, missed


def load_manifest(data):
    data = Path(data)
    with open(data / "manifest.csv", newline="") as handle:
        return [
            (data / row["path"], row["label"], row.get("group", ""))
            for row in csv.DictReader(handle)
        ]


# ------------------------------------------------------------------ synthetic
def render_dot_line(text, rng, height=None):
    """A grey dot-matrix line of text, roughly like an imprinter's."""
    pitch = rng.uniform(3.0, 5.0)  # px between dot centres
    radius = pitch * rng.uniform(0.3, 0.5)
    gap = rng.uniform(1.2, 2.2)  # dots between characters
    width = int((len(text) * (5 + gap) + 2) * pitch) + 8
    rows = int(9 * pitch) + 8
    image = np.full((rows, width), 255, np.float32)
    ink = rng.uniform(0, 140)
    x0 = 4 + pitch
    for ch in text:
        for r, line in enumerate(GLYPHS[ch]):
            for c, cell in enumerate(line):
                if cell != "#" or rng.random() < 0.04:  # a missing dot now and then
                    continue
                cx = x0 + c * pitch + rng.gauss(0, 0.15 * pitch)
                cy = 4 + pitch + r * pitch + rng.gauss(0, 0.15 * pitch)
                shade = ink + rng.uniform(0, 60)
                cv2.circle(image, (int(round(cx * 4)), int(round(cy * 4))), max(int(round(radius * 4)), 1),
                           shade, -1, lineType=cv2.LINE_AA, shift=2)
        x0 += (5 + gap) * pitch
    image = cv2.GaussianBlur(image, (0, 0), rng.uniform(0.3, 1.2))
    return np.clip(image, 0, 255).astype(np.uint8)


def random_label(rng, charset, lengths=(5, 9)):
    digits = [c for c in charset if c.isdigit()]
    letters = [c for c in charset if not c.isdigit()]
    length = rng.randint(*lengths)
    text = "".join(rng.choice(digits) for _ in range(length))
    if letters and rng.random() < 0.8:
        text = rng.choice(letters) + text[1:]
    return text


def augment(image, rng):
    image = image.astype(np.float32)
    # Contrast, brightness and a faint print
    image = 255 - (255 - image) * rng.uniform(0.35, 1.1)
    image += rng.uniform(-25, 25)
    h, w = image.shape
    # A little rotation / scale / shift, as the line box is cut on each sheet
    angle = rng.uniform(-2.0, 2.0)
    scale = rng.uniform(0.92, 1.08)
    matrix = cv2.getRotationMatrix2D((w / 2, h / 2), angle, scale)
    matrix[:, 2] += (rng.uniform(-0.05, 0.05) * w, rng.uniform(-0.08, 0.08) * h)
    image = cv2.warpAffine(image, matrix, (w, h), borderMode=cv2.BORDER_REPLICATE)
    if rng.random() < 0.3:
        image = cv2.GaussianBlur(image, (0, 0), rng.uniform(0.5, 1.2))
    image += np.random.default_rng(rng.randint(0, 1 << 30)).normal(0, rng.uniform(0, 10), image.shape)
    return np.clip(image, 0, 255).astype(np.uint8)


# ------------------------------------------------------------------ model
def build_model(classes):
    import torch.nn as nn

    def block(cin, cout, pool):
        return [nn.Conv2d(cin, cout, 3, padding=1, bias=False), nn.BatchNorm2d(cout),
                nn.ReLU(inplace=True), nn.MaxPool2d(pool)]

    class Net(nn.Module):
        def __init__(self):
            super().__init__()
            self.features = nn.Sequential(
                *block(1, 32, (2, 2)),     # 16 x W/2
                *block(32, 64, (2, 2)),    # 8 x W/4
                *block(64, 96, (2, 1)),    # 4
                *block(96, 128, (4, 1)),   # 1
            )
            self.head = nn.Sequential(
                nn.Conv1d(128, 128, 3, padding=1), nn.ReLU(inplace=True),
                nn.Dropout(0.2), nn.Conv1d(128, classes, 1),
            )

        def forward(self, x):
            features = self.features(x).squeeze(2)  # N, C, T
            return self.head(features).permute(0, 2, 1)  # N, T, classes

    return Net()


def batch_tensor(images, height):
    inputs = [imprint.model_input(image, height)[0, 0] for image in images]
    width = max(i.shape[1] for i in inputs)
    width = (width + 3) // 4 * 4
    batch = np.ones((len(inputs), 1, height, width), np.float32)
    for k, item in enumerate(inputs):
        batch[k, 0, :, : item.shape[1]] = item
    return batch


def train(data, out, charset="S0123456789", direction="horizontal", holdout=None,
          epochs=40, synthetic=0.5, seed=0, height=32, trained_on=""):
    import torch
    import torch.nn.functional as F

    rng = random.Random(seed)
    torch.manual_seed(seed)
    samples = load_manifest(data)
    groups = sorted({g for _, _, g in samples})
    if holdout == "all":
        scores = {}
        for group in groups:
            scores[group] = train(data, f"{out}_holdout_{group}", charset, direction, group,
                                  epochs, synthetic, seed, height, trained_on)["holdout_exact"]
        print(json.dumps({"leave_one_group_out": scores}, indent=2))
        return {"leave_one_group_out": scores}
    test = [s for s in samples if holdout and s[2] == holdout]
    fit = [s for s in samples if not holdout or s[2] != holdout]
    images = {p: cv2.imread(str(p), cv2.IMREAD_GRAYSCALE) for p, _, _ in samples}
    index = {ch: i + 1 for i, ch in enumerate(charset)}
    model = build_model(len(charset) + 1)
    optimiser = torch.optim.AdamW(model.parameters(), lr=2e-3, weight_decay=1e-4)
    steps_per_epoch = max(len(fit) // 32, 50)
    scheduler = torch.optim.lr_scheduler.OneCycleLR(
        optimiser, max_lr=2e-3, total_steps=epochs * steps_per_epoch
    )
    started = time.time()
    for epoch in range(epochs):
        model.train()
        total = 0.0
        for _ in range(steps_per_epoch):
            crops, labels = [], []
            for _ in range(32):
                if not fit or rng.random() < synthetic:
                    text = random_label(rng, charset)
                    crops.append(augment(render_dot_line(text, rng), rng))
                else:
                    path, text, _ = rng.choice(fit)
                    crops.append(augment(images[path], rng))
                labels.append(text)
            x = torch.from_numpy(batch_tensor(crops, height))
            logits = model(x)  # N, T, C
            log_probs = F.log_softmax(logits, dim=2).permute(1, 0, 2)  # T, N, C
            targets = torch.tensor([index[c] for t in labels for c in t], dtype=torch.long)
            target_lengths = torch.tensor([len(t) for t in labels], dtype=torch.long)
            input_lengths = torch.full((len(labels),), log_probs.shape[0], dtype=torch.long)
            loss = F.ctc_loss(log_probs, targets, input_lengths, target_lengths,
                              blank=0, zero_infinity=True)
            optimiser.zero_grad()
            loss.backward()
            optimiser.step()
            scheduler.step()
            total += float(loss)
        print(f"epoch {epoch + 1}/{epochs} loss {total / steps_per_epoch:.4f} ({time.time() - started:.0f}s)", flush=True)
    model.eval()
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    dummy = torch.ones(1, 1, height, 160)
    torch.onnx.export(
        model, dummy, str(out.with_suffix(".onnx")), input_names=["x"], output_names=["logits"],
        dynamic_axes={"x": {0: "n", 3: "w"}, "logits": {0: "n", 1: "t"}}, opset_version=17,
        dynamo=False,
    )
    meta = {
        "charset": charset, "blank": 0, "input_height": height, "input_channels": 1,
        "invert": False, "max_width": 1024, "direction": direction,
        "trained_on": trained_on or str(data), "holdout": holdout,
        "samples": len(fit), "epochs": epochs, "synthetic_share": synthetic,
    }
    out.with_suffix(".json").write_text(json.dumps(meta, indent=2))
    imprint.reset_cache()
    if test:
        score = evaluate(test, out.with_suffix(".onnx"))
        meta["holdout_exact"] = score["exact"]
        meta["metrics"] = score
        out.with_suffix(".json").write_text(json.dumps(meta, indent=2))
        print(json.dumps({"holdout": holdout, **score}, indent=2))
    return meta


def evaluate(samples, model_path):
    model = imprint.ImprintModel(model_path)
    right, errors, started = 0, [], time.perf_counter()
    for path, label, _ in samples:
        text, _ = model.read(cv2.imread(str(path), cv2.IMREAD_GRAYSCALE))
        if text == label:
            right += 1
        else:
            errors.append({"file": str(path), "truth": label, "read": text})
    ms = (time.perf_counter() - started) * 1000 / max(len(samples), 1)
    return {"n": len(samples), "exact": round(right / max(len(samples), 1), 4),
            "ms_per_crop": round(ms, 2), "errors": errors[:50]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    crops = sub.add_parser("crops")
    crops.add_argument("--template", required=True)
    crops.add_argument("--input", nargs="+", required=True)
    crops.add_argument("--out", required=True)
    crops.add_argument("--label", help='e.g. "S{stem}"')
    crops.add_argument("--truth", help="CSV of file,label")
    crops.add_argument("--zone")
    fit = sub.add_parser("train")
    fit.add_argument("--data", required=True)
    fit.add_argument("--out", required=True)
    fit.add_argument("--charset", default="S0123456789")
    fit.add_argument("--direction", default="horizontal")
    fit.add_argument("--holdout")
    fit.add_argument("--epochs", type=int, default=40)
    fit.add_argument("--synthetic", type=float, default=0.5)
    fit.add_argument("--seed", type=int, default=0)
    fit.add_argument("--trained-on", default="")
    score = sub.add_parser("eval")
    score.add_argument("--data", required=True)
    score.add_argument("--model", required=True)
    args = parser.parse_args(argv)
    if args.command == "crops":
        if not args.label and not args.truth:
            parser.error("crops needs --label or --truth")
        export_crops(args.template, args.input, args.out, args.label, args.truth, args.zone)
    elif args.command == "train":
        train(args.data, args.out, args.charset, args.direction, args.holdout,
              args.epochs, args.synthetic, args.seed, trained_on=args.trained_on)
    else:
        print(json.dumps(evaluate(load_manifest(args.data), args.model), indent=2))


if __name__ == "__main__":
    main()
