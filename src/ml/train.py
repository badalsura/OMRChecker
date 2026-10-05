"""
Train a crop classifier (bubble marked/empty, or ICR characters) and export it
to ONNX for src/ml/classifiers.py.

    python -m src.ml.train --data datasets/bubbles --out models/bubble_model.onnx --kind bubble
    python -m src.ml.train --data datasets/icr --out models/icr_model.onnx --kind icr

--data is a folder written by src/ml/dataset.py (manifest.csv) or a folder of
class sub-folders. The run:

* loads every crop, resized exactly as the runtime resizes it;
* splits train/validation by source sheet (so crops of one sheet never land on
  both sides) or, without sources, stratified by label;
* trains a small CNN with on-the-fly augmentation (shifts that mimic
  registration error, scale, rotation, contrast/brightness, gamma, blur, noise)
  and class-balanced loss, keeping the epoch with the best validation loss
  (early stopping);
* fits a softmax temperature on the validation set so that confidences are
  calibrated (the reader's review thresholds rely on them);
* exports <out>.onnx plus the <out>.json sidecar with labels, input size,
  temperature, validation metrics and dataset info.

PyTorch is only needed here (requirements.ml.txt); inference uses onnxruntime.
"""

import argparse
import json
import math
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import numpy as np

from src.ml.dataset import BLANK_LABEL, EMPTY, KIND_BUBBLE, MARKED, load_samples

ARCHITECTURES = {
    # channels per stage; every stage halves the resolution
    # Bubbles are an easy, high-volume problem: ~0.7 MFLOP per crop keeps a page of
    # a few hundred bubbles well under a few milliseconds on one CPU core
    KIND_BUBBLE: {"channels": (8, 16, 32), "convs_per_stage": 1, "dropout": 0.1},
    "icr": {"channels": (32, 64, 128), "convs_per_stage": 2, "dropout": 0.25},
}
AUGMENT = {
    KIND_BUBBLE: dict(shift=0.12, scale=0.12, rotate=10.0),
    "icr": dict(shift=0.08, scale=0.12, rotate=8.0),
}


# --------------------------------------------------------------------------- numpy helpers


def softmax_np(logits, temperature=1.0):
    logits = np.asarray(logits, dtype=np.float64) / temperature
    logits = logits - logits.max(axis=1, keepdims=True)
    exp = np.exp(logits)
    return exp / exp.sum(axis=1, keepdims=True)


def nll(logits, targets, temperature=1.0):
    probs = softmax_np(logits, temperature)
    picked = probs[np.arange(len(targets)), targets]
    return float(-np.mean(np.log(np.clip(picked, 1e-12, 1.0))))


def expected_calibration_error(probs, targets, bins=15):
    probs = np.asarray(probs)
    confidence = probs.max(axis=1)
    correct = probs.argmax(axis=1) == np.asarray(targets)
    edges = np.linspace(0.0, 1.0, bins + 1)
    ece = 0.0
    for low, high in zip(edges[:-1], edges[1:]):
        in_bin = (confidence > low) & (confidence <= high)
        if in_bin.any():
            ece += in_bin.mean() * abs(
                correct[in_bin].mean() - confidence[in_bin].mean()
            )
    return float(ece)


def fit_temperature(logits, targets, low=0.05, high=20.0, iterations=60):
    """Softmax temperature fitted on validation logits.

    Normally the temperature minimising the validation NLL (golden-section search
    on log T). When the validation set has no errors the NLL keeps falling as
    T -> 0, so instead T is chosen so that the mean confidence equals the
    rule-of-succession accuracy estimate (n + 1) / (n + 2): confident, but never
    more than the evidence supports.
    """
    if len(targets) == 0:
        return 1.0
    logits = np.asarray(logits, dtype=np.float64)
    targets = np.asarray(targets)
    if (logits.argmax(axis=1) == targets).all():
        n = len(targets)
        goal = (n + 1) / (n + 2)
        a, b = math.log(low), math.log(high)
        for _ in range(iterations):
            mid = (a + b) / 2
            confidence = softmax_np(logits, math.exp(mid)).max(axis=1).mean()
            # Confidence falls as the temperature rises
            if confidence > goal:
                a = mid
            else:
                b = mid
        return float(math.exp((a + b) / 2))
    a, b = math.log(low), math.log(high)
    ratio = (math.sqrt(5) - 1) / 2
    c, d = b - ratio * (b - a), a + ratio * (b - a)
    fc, fd = nll(logits, targets, math.exp(c)), nll(logits, targets, math.exp(d))
    for _ in range(iterations):
        if fc < fd:
            b, d, fd = d, c, fc
            c = b - ratio * (b - a)
            fc = nll(logits, targets, math.exp(c))
        else:
            a, c, fc = c, d, fd
            d = a + ratio * (b - a)
            fd = nll(logits, targets, math.exp(d))
    temperature = math.exp((a + b) / 2)
    # Never make things worse than the uncalibrated model
    if nll(logits, targets, temperature) > nll(logits, targets, 1.0):
        return 1.0
    return float(temperature)


def classification_metrics(targets, predictions, labels):
    """Accuracy, per-class precision/recall/F1/support and the confusion matrix."""
    targets = np.asarray(targets, dtype=int)
    predictions = np.asarray(predictions, dtype=int)
    k = len(labels)
    confusion = np.zeros((k, k), dtype=int)
    np.add.at(confusion, (targets, predictions), 1)
    per_class = {}
    for i, label in enumerate(labels):
        tp = int(confusion[i, i])
        predicted = int(confusion[:, i].sum())
        actual = int(confusion[i, :].sum())
        precision = tp / predicted if predicted else 0.0
        recall = tp / actual if actual else 0.0
        f1 = (
            2 * precision * recall / (precision + recall) if precision + recall else 0.0
        )
        per_class[label] = {
            "precision": round(precision, 5),
            "recall": round(recall, 5),
            "f1": round(f1, 5),
            "support": actual,
        }
    total = int(confusion.sum())
    return {
        "accuracy": round(float(np.trace(confusion)) / total, 5) if total else 0.0,
        "n": total,
        "per_class": per_class,
        "confusion_matrix": {
            "labels": list(labels),
            "rows_true_cols_pred": confusion.tolist(),
        },
    }


def split_indices(labels, groups=None, val_fraction=0.15, seed=0):
    """Train/validation split: by group (source sheet) when possible, else stratified."""
    rng = random.Random(seed)
    n = len(labels)
    unique_groups = sorted({g for g in groups if g}) if groups is not None else []
    if len(unique_groups) >= 5:
        rng.shuffle(unique_groups)
        n_val = max(1, int(round(len(unique_groups) * val_fraction)))
        val_groups = set(unique_groups[:n_val])
        val = [i for i in range(n) if groups[i] in val_groups]
        train = [i for i in range(n) if groups[i] not in val_groups]
        if val and train:
            return train, val
    by_label: Dict[str, List[int]] = {}
    for i, label in enumerate(labels):
        by_label.setdefault(label, []).append(i)
    train, val = [], []
    for indices in by_label.values():
        rng.shuffle(indices)
        n_val = int(round(len(indices) * val_fraction))
        if len(indices) >= 2:
            n_val = max(1, n_val)
        val += indices[:n_val]
        train += indices[n_val:]
    return sorted(train), sorted(val)


def order_labels(labels, kind):
    unique = sorted(set(labels))
    if kind == KIND_BUBBLE:
        if MARKED not in unique:
            raise ValueError("a bubble dataset needs crops labelled 'marked'")
        # Stable, readable order: empty first, marked second, extra classes after
        head = [label for label in (EMPTY, MARKED) if label in unique]
        return head + [label for label in unique if label not in head]
    if BLANK_LABEL in unique:
        unique.remove(BLANK_LABEL)
        unique.insert(0, BLANK_LABEL)
    return unique


# --------------------------------------------------------------------------- torch parts


def _torch():
    try:
        import torch  # noqa: F401
    except ImportError as error:  # pragma: no cover - depends on the environment
        raise ImportError(
            "Training needs PyTorch: pip install -r requirements.ml.txt "
            "(inference only needs onnxruntime)"
        ) from error
    return torch


def build_model(n_classes, kind=KIND_BUBBLE):
    torch = _torch()
    nn = torch.nn
    arch = ARCHITECTURES.get(kind, ARCHITECTURES["icr"])
    layers, in_channels = [], 1
    for out_channels in arch["channels"]:
        for _ in range(arch.get("convs_per_stage", 1)):
            layers += [
                nn.Conv2d(in_channels, out_channels, 3, padding=1, bias=False),
                nn.BatchNorm2d(out_channels),
                nn.ReLU(inplace=True),
            ]
            in_channels = out_channels
        layers.append(nn.MaxPool2d(2))
    layers += [
        nn.AdaptiveAvgPool2d(1),
        nn.Flatten(),
        nn.Dropout(arch["dropout"]),
        nn.Linear(in_channels, n_classes),
    ]
    return nn.Sequential(*layers)


def augment_batch(batch, generator, shift=0.1, scale=0.1, rotate=8.0):
    """Random geometric and photometric changes on an (N, 1, H, W) batch in [0, 1]."""
    torch = _torch()
    F = torch.nn.functional
    n = batch.shape[0]
    device = batch.device

    def uniform(low, high, size=(n,)):
        return torch.rand(size, generator=generator, device=device) * (high - low) + low

    angle = uniform(-rotate, rotate) * math.pi / 180
    zoom = uniform(1 - scale, 1 + scale)
    cos, sin = torch.cos(angle) / zoom, torch.sin(angle) / zoom
    theta = torch.stack(
        [
            torch.stack([cos, -sin, uniform(-shift, shift) * 2], dim=1),
            torch.stack([sin, cos, uniform(-shift, shift) * 2], dim=1),
        ],
        dim=1,
    )
    grid = F.affine_grid(theta, batch.shape, align_corners=False)
    out = F.grid_sample(batch, grid, padding_mode="border", align_corners=False)

    # Occasional blur (scanner optics, motion) and its opposite, plain resampling
    blur = uniform(0, 1) < 0.3
    if blur.any():
        blurred = F.avg_pool2d(out, 3, stride=1, padding=1, count_include_pad=False)
        out = torch.where(blur[:, None, None, None], blurred, out)

    # Paper tone, ink darkness and lighting
    contrast = uniform(0.6, 1.3)[:, None, None, None]
    brightness = uniform(-0.2, 0.2)[:, None, None, None]
    out = (out - 0.5) * contrast + 0.5 + brightness
    gamma = torch.exp(uniform(-0.4, 0.4))[:, None, None, None]
    out = out.clamp(1e-4, 1.0) ** gamma
    noise = uniform(0, 0.06)[:, None, None, None]
    out = out + torch.randn(out.shape, generator=generator, device=device) * noise
    return out.clamp(0.0, 1.0)


def _logits(model, images, batch_size=2048):
    torch = _torch()
    model.eval()
    outputs = []
    with torch.no_grad():
        for start in range(0, len(images), batch_size):
            outputs.append(model(images[start : start + batch_size]))
    if not outputs:
        return np.zeros((0, 0), dtype=np.float32)
    return torch.cat(outputs).numpy()


def export_onnx(model, path, input_size, opset=17):
    """Export with a dynamic batch dimension and check it against PyTorch.

    Uses the TorchScript exporter (small, plain graphs) and falls back to the
    torch.export-based exporter on PyTorch versions without it. Returns the
    largest absolute logit difference between ONNX Runtime and PyTorch.
    """
    import warnings

    import onnxruntime as ort

    torch = _torch()
    width, height = input_size
    model.eval()
    dummy = torch.zeros(1, 1, height, width)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            torch.onnx.export(
                model,
                dummy,
                str(path),
                input_names=["input"],
                output_names=["logits"],
                dynamic_axes={"input": {0: "batch"}, "logits": {0: "batch"}},
                opset_version=opset,
                dynamo=False,
            )
    except Exception:  # legacy exporter removed or failing: use torch.export
        batch = torch.export.Dim("batch", min=1, max=1 << 20)
        torch.onnx.export(
            model,
            (dummy,),
            str(path),
            input_names=["input"],
            output_names=["logits"],
            dynamic_shapes=({0: batch},),
            opset_version=max(opset, 18),
            dynamo=True,
        )
    session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
    probe = torch.rand(5, 1, height, width)
    with torch.no_grad():
        expected = model(probe).numpy()
    got = session.run(None, {session.get_inputs()[0].name: probe.numpy()})[0]
    difference = float(np.abs(expected - got).max())
    if difference > 1e-3:
        raise RuntimeError(
            f"ONNX export does not match PyTorch (max diff {difference})"
        )
    return difference


def measure_inference_speed(model_path, input_size, crop_shape=(30, 30), n=8192):
    """Crops/second through OnnxCropClassifier (resize + inference), batched."""
    from src.ml.classifiers import OnnxCropClassifier

    classifier = OnnxCropClassifier(model_path)
    rng = np.random.default_rng(0)
    crops = [
        rng.integers(0, 255, crop_shape, dtype=np.uint8) for _ in range(min(n, 2048))
    ]
    classifier.predict_proba(crops[:64])  # warm up
    started = time.perf_counter()
    done = 0
    while done < n:
        classifier.predict_proba(crops)
        done += len(crops)
    return round(done / (time.perf_counter() - started), 1)


# --------------------------------------------------------------------------- training


def train_from_arrays(
    images: np.ndarray,
    labels: Sequence[str],
    out_path,
    kind=KIND_BUBBLE,
    groups: Optional[Sequence[str]] = None,
    input_size=(32, 32),
    epochs=30,
    batch_size=256,
    lr=3e-3,
    weight_decay=1e-4,
    val_fraction=0.15,
    patience=5,
    balance="weights",
    augment=True,
    seed=0,
    threads=0,
    dataset_info=None,
    verbose=True,
):
    """Train on in-memory crops (uint8, (N, H, W) at input_size) and export ONNX.

    Returns the metadata written to the JSON sidecar.
    """
    torch = _torch()
    torch.manual_seed(seed)
    if threads:
        torch.set_num_threads(threads)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()

    label_names = order_labels(labels, kind)
    index_of = {label: i for i, label in enumerate(label_names)}
    targets = np.array([index_of[label] for label in labels], dtype=np.int64)
    train_idx, val_idx = split_indices(list(labels), groups, val_fraction, seed)
    if not val_idx:
        raise ValueError("not enough data for a validation split")

    data = torch.from_numpy(images.astype(np.float32) / 255.0).unsqueeze(1)
    y = torch.from_numpy(targets)
    x_train, y_train = data[train_idx], y[train_idx]
    x_val, y_val = data[val_idx], y[val_idx]

    counts = np.bincount(targets[train_idx], minlength=len(label_names)).astype(float)
    if balance == "weights":
        weights = counts.sum() / (len(label_names) * np.maximum(counts, 1))
        class_weights = torch.tensor(weights, dtype=torch.float32)
    else:
        class_weights = None

    model = build_model(len(label_names), kind)
    optimizer = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=weight_decay)
    steps_per_epoch = max(1, math.ceil(len(train_idx) / batch_size))
    scheduler = torch.optim.lr_scheduler.OneCycleLR(
        optimizer, max_lr=lr, epochs=epochs, steps_per_epoch=steps_per_epoch
    )
    loss_fn = torch.nn.CrossEntropyLoss(weight=class_weights)
    val_loss_fn = torch.nn.CrossEntropyLoss()
    generator = torch.Generator().manual_seed(seed)
    aug_params = AUGMENT.get(kind, AUGMENT["icr"])

    best = {"loss": float("inf"), "epoch": -1, "state": None}
    history = []
    for epoch in range(epochs):
        model.train()
        order = torch.randperm(len(train_idx), generator=generator)
        total_loss = 0.0
        for start in range(0, len(order), batch_size):
            batch_index = order[start : start + batch_size]
            if len(batch_index) < 2:
                continue  # BatchNorm needs more than one sample
            batch = x_train[batch_index]
            if augment:
                batch = augment_batch(batch, generator, **aug_params)
            loss = loss_fn(model(batch), y_train[batch_index])
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            scheduler.step()
            total_loss += loss.item() * len(batch_index)
        val_logits = torch.from_numpy(_logits(model, x_val))
        val_loss = float(val_loss_fn(val_logits, y_val))
        val_acc = float((val_logits.argmax(1) == y_val).float().mean())
        history.append(
            {
                "epoch": epoch + 1,
                "train_loss": round(total_loss / max(len(train_idx), 1), 5),
                "val_loss": round(val_loss, 5),
                "val_accuracy": round(val_acc, 5),
            }
        )
        if verbose:
            print(
                f"epoch {epoch + 1:3d}  train_loss {history[-1]['train_loss']:.4f}  "
                f"val_loss {val_loss:.4f}  val_acc {val_acc:.4f}",
                flush=True,
            )
        if val_loss < best["loss"] - 1e-5:
            best = {
                "loss": val_loss,
                "epoch": epoch + 1,
                "state": {k: v.clone() for k, v in model.state_dict().items()},
            }
        elif epoch + 1 - best["epoch"] >= patience:
            if verbose:
                print(f"early stop: no improvement for {patience} epochs", flush=True)
            break
    model.load_state_dict(best["state"])

    val_logits = _logits(model, x_val)
    val_targets = y_val.numpy()
    temperature = fit_temperature(val_logits, val_targets)
    probs_raw = softmax_np(val_logits)
    probs_cal = softmax_np(val_logits, temperature)
    metrics = classification_metrics(val_targets, probs_cal.argmax(1), label_names)
    metrics["calibration"] = {
        "temperature": round(temperature, 5),
        "nll_before": round(nll(val_logits, val_targets), 5),
        "nll_after": round(nll(val_logits, val_targets, temperature), 5),
        "ece_before": round(expected_calibration_error(probs_raw, val_targets), 5),
        "ece_after": round(expected_calibration_error(probs_cal, val_targets), 5),
    }
    # How many validation crops would the reader be unsure about (p < 0.9)?
    metrics["low_confidence_rate_p90"] = round(
        float((probs_cal.max(1) < 0.9).mean()), 5
    )

    export_difference = export_onnx(model, out_path, input_size)
    metadata = {
        "labels": label_names,
        "input_size": list(input_size),
        "temperature": round(temperature, 6),
        "kind": kind,
        "architecture": {
            "type": "cnn",
            **{
                k: list(v) if isinstance(v, tuple) else v
                for k, v in ARCHITECTURES.get(kind, ARCHITECTURES["icr"]).items()
            },
        },
        "input": "grayscale crop resized to input_size (INTER_AREA), float32 in [0, 1], NCHW",
        "metrics": {
            "validation": metrics,
            "history": history,
            "best_epoch": best["epoch"],
        },
        "dataset": {
            **(dataset_info or {}),
            "n_samples": int(len(labels)),
            "n_train": len(train_idx),
            "n_val": len(val_idx),
            "split": (
                "by source"
                if groups is not None and len({g for g in groups if g}) >= 5
                else "stratified"
            ),
            "class_counts": {
                label: int((targets == i).sum()) for i, label in enumerate(label_names)
            },
        },
        "training": {
            "epochs": epochs,
            "epochs_run": len(history),
            "batch_size": batch_size,
            "lr": lr,
            "balance": balance,
            "augment": augment,
            "seed": seed,
            "seconds": round(time.time() - started, 1),
            "torch_version": torch.__version__,
            "onnx_max_abs_diff": export_difference,
        },
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    sidecar = out_path.with_suffix(".json")
    sidecar.write_text(json.dumps(metadata, indent=2))
    try:
        metadata["inference_crops_per_sec"] = measure_inference_speed(
            out_path, input_size
        )
        sidecar.write_text(json.dumps(metadata, indent=2))
    except Exception as error:  # the model is usable even if the speed test fails
        if verbose:
            print(f"inference speed test failed: {error}")
    return metadata


def train_classifier(
    data_dir, out_path, kind=KIND_BUBBLE, input_size=(32, 32), **kwargs
):
    """Load a dataset folder (src/ml/dataset.py format) and train on it."""
    images, labels, rows = load_samples(data_dir, input_size, kind=kind)
    if len(labels) == 0:
        raise ValueError(f"no {kind} crops found in {data_dir}")
    groups = [row.get("source", "") for row in rows]
    return train_from_arrays(
        images,
        labels,
        out_path,
        kind=kind,
        groups=groups,
        input_size=input_size,
        dataset_info={"path": str(data_dir)},
        **kwargs,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m src.ml.train",
        description="Train a crop classifier and export ONNX.",
    )
    parser.add_argument(
        "--data", required=True, help="dataset folder (manifest.csv or class folders)"
    )
    parser.add_argument(
        "--out", required=True, help="output .onnx path (sidecar .json next to it)"
    )
    parser.add_argument("--kind", choices=[KIND_BUBBLE, "icr"], default=KIND_BUBBLE)
    parser.add_argument(
        "--input-size", type=int, nargs=2, default=[32, 32], metavar=("W", "H")
    )
    parser.add_argument("--epochs", type=int, default=30)
    parser.add_argument("--batch-size", type=int, default=256)
    parser.add_argument("--lr", type=float, default=3e-3)
    parser.add_argument("--val-fraction", type=float, default=0.15)
    parser.add_argument("--patience", type=int, default=5)
    parser.add_argument("--balance", choices=["weights", "none"], default="weights")
    parser.add_argument("--no-augment", action="store_true")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--threads", type=int, default=0)
    args = parser.parse_args(argv)
    metadata = train_classifier(
        args.data,
        args.out,
        kind=args.kind,
        input_size=tuple(args.input_size),
        epochs=args.epochs,
        batch_size=args.batch_size,
        lr=args.lr,
        val_fraction=args.val_fraction,
        patience=args.patience,
        balance=args.balance,
        augment=not args.no_augment,
        seed=args.seed,
        threads=args.threads,
    )
    validation = metadata["metrics"]["validation"]
    print(
        json.dumps(
            {
                "model": str(args.out),
                "labels": metadata["labels"],
                "val_accuracy": validation["accuracy"],
                "per_class": validation["per_class"],
                "calibration": validation["calibration"],
                "inference_crops_per_sec": metadata.get("inference_crops_per_sec"),
            },
            indent=2,
        )
    )
    return metadata


if __name__ == "__main__":
    main()
