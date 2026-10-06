"""
Training data for the crop classifiers.

Three sources feed the same on-disk format:

1. Labelled scans: a template, sheet images and a ground-truth table. Each sheet
   is read by the engine; every bubble crop (and every ICR character box) is cut
   from the aligned page and labelled from the truth.
2. Review corrections: a ScanResult (dict) plus the values an operator confirmed
   or corrected in the review queue (export_from_review).
3. Synthetic sheets rendered by src/synth for pretraining (generate_synthetic).

On disk a dataset is a folder:

    <out>/<label dir>/<name>.png      one grayscale crop per file
    <out>/manifest.csv                path,label,kind,source,field,value,predicted,confidence

Label directories are file-system safe encodings of the label ("<blank>" is
stored as "_blank_"); the manifest keeps the real label, and load_samples()
falls back to the folder names when there is no manifest.

The crops are cut exactly as the runtime cuts them (the template bubble box on
the aligned image, the cleaned ICR character box), so a model trained on them
sees the same input in production.
"""

import argparse
import csv
import json
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

import cv2
import numpy as np

MARKED = "marked"
EMPTY = "empty"
BLANK_LABEL = "<blank>"
KIND_BUBBLE = "bubble"
KIND_ICR = "icr"
MANIFEST_NAME = "manifest.csv"
MANIFEST_COLUMNS = [
    "path",
    "label",
    "kind",
    "source",
    "field",
    "value",
    "predicted",
    "confidence",
]
FILE_COLUMN_CANDIDATES = ("file_name", "file_id", "filename", "file", "image")
# Truth value meaning "two or more bubbles marked; which ones is not recorded"
MULTI_MARK = "*"
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".pdf"}


@dataclass
class Sample:
    """One labelled crop (image in memory) plus provenance for the manifest."""

    image: np.ndarray
    label: str
    kind: str
    source: str = ""
    field: str = ""
    value: str = ""
    predicted: str = ""
    confidence: Optional[float] = None


# --------------------------------------------------------------------------- labels


def label_to_dirname(label: str) -> str:
    if label == BLANK_LABEL:
        return "_blank_"
    if re.fullmatch(r"[A-Za-z0-9_-]+", label):
        # Case-insensitive file systems would merge "a" and "A"
        return label if not label.isalpha() or label.islower() else f"{label}_upper"
    return "u" + "_".join(f"{ord(c):04x}" for c in label)


def dirname_to_label(name: str) -> str:
    if name == "_blank_":
        return BLANK_LABEL
    if name.endswith("_upper"):
        return name[: -len("_upper")]
    match = re.fullmatch(r"u([0-9a-f]{4}(?:_[0-9a-f]{4})*)", name)
    if match:
        return "".join(chr(int(code, 16)) for code in match.group(1).split("_"))
    return name


def split_field_value(value, bubble_values: Sequence[str]):
    """Which bubbles produce `value`? Returns the set of marked indices, or None.

    The engine concatenates the values of the marked bubbles in template order, so
    a truth value is decomposed by finding an ordered subset of bubble values whose
    concatenation equals it ("" means no bubble marked).
    """
    value = "" if value is None else str(value).strip()
    bubble_values = [str(v) for v in bubble_values]
    memo = {}

    def solve(start, pos):
        if pos == len(value):
            return frozenset()
        if start >= len(bubble_values):
            return None
        key = (start, pos)
        if key in memo:
            return memo[key]
        result = None
        for index in range(start, len(bubble_values)):
            candidate = bubble_values[index]
            if candidate and value.startswith(candidate, pos):
                rest = solve(index + 1, pos + len(candidate))
                if rest is not None:
                    result = rest | {index}
                    break
        memo[key] = result
        return result

    return solve(0, 0)


# --------------------------------------------------------------------------- truth


def _normalize_cell(value):
    if value is None:
        return ""
    if isinstance(value, float) and np.isnan(value):
        return ""
    return str(value).strip()


def _file_key(record):
    """The record's file-name column: file_name, "File Name", Image, ..."""
    normalized = {
        "".join(ch for ch in str(key).lower() if ch.isalnum()): key for key in record
    }
    for candidate in FILE_COLUMN_CANDIDATES:
        key = normalized.get(candidate.replace("_", ""))
        if key is not None:
            return key
    return None


def _read_xlsx(path):
    try:
        import openpyxl
    except ImportError as error:  # pragma: no cover - depends on install
        raise ValueError(f"{path}: reading .xlsx needs openpyxl") from error
    sheet = openpyxl.load_workbook(path, read_only=True, data_only=True).active
    rows = sheet.iter_rows(values_only=True)
    header = [str(h).strip() if h is not None else "" for h in next(rows, [])]
    records = []
    for row in rows:
        if not row or row[0] is None or not str(row[0]).strip():
            continue
        records.append(
            {
                key: ("" if value is None else str(value))
                for key, value in zip(header, row)
                if key
            }
        )
    return records


def expand_answer_string(value, prefix="q", count=None):
    """One answer string ("CB A*D") -> {q1: "C", q2: "B", q3: "", q4: "A", q5: "*", ...}.

    A space is a blank question and "*" a multi-marked one; questions past the
    end of the string are blank when `count` is larger than the string.
    """
    value = "" if value is None else str(value)
    count = len(value) if count is None else count
    return {
        f"{prefix}{index + 1}": (value[index].strip() if index < len(value) else "")
        for index in range(count)
    }


def load_truth(path, answer_columns=None) -> Dict[str, Dict[str, str]]:
    """Ground truth as {file_name: {column: value}}.

    CSV / XLSX: a file-name column (file_name / file_id / filename / file / image,
    any case and spacing, e.g. "File Name") and one column per output column,
    field label or zone name. JSON: either {file_name: {column: value}}, a list of
    records with a file-name key, or the synthetic truth format
    {file_name: {"answers": {...}, "zones": {...}}}.
    `answer_columns` maps a column holding one character per question (e.g.
    {"ANS": "q"}, optionally {"ANS": ("q", 120)}) to per-question columns, see
    expand_answer_string. A value of "*" means multi-marked (MULTI_MARK).
    Keys are matched on the full name and on the bare file name (see lookup_truth).
    """
    path = Path(path)
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text())
        if isinstance(data, list):
            records = data
        else:
            records = []
            for name, values in data.items():
                record = dict(values)
                record.setdefault("file_name", name)
                records.append(record)
    elif path.suffix.lower() in (".xlsx", ".xlsm"):
        records = _read_xlsx(path)
    else:
        with open(path, newline="", encoding="utf-8-sig") as handle:
            records = list(csv.DictReader(handle))
    answer_columns = answer_columns or {}
    truth = {}
    for record in records:
        file_key = _file_key(record)
        if file_key is None:
            raise ValueError(
                f"{path}: every truth record needs one of the columns {FILE_COLUMN_CANDIDATES}"
            )
        values = {}
        if isinstance(record.get("answers"), dict) or isinstance(
            record.get("zones"), dict
        ):
            values.update(record.get("answers") or {})
            values.update(record.get("zones") or {})
        else:
            values.update({k: v for k, v in record.items() if k != file_key})
        for column, spec in answer_columns.items():
            if column not in values:
                continue
            prefix, count = spec if isinstance(spec, (tuple, list)) else (spec, None)
            raw = values.pop(column)
            raw = "" if raw is None else str(raw)
            values.update(expand_answer_string(raw, prefix, count))
        truth[str(record[file_key]).strip()] = {
            str(k): _normalize_cell(v) for k, v in values.items()
        }
    return truth


def lookup_truth(truth, file_id):
    """Find the truth row for a scanned file (exact, bare name, or stem match)."""
    if file_id in truth:
        return truth[file_id]
    name = Path(str(file_id)).name
    if name in truth:
        return truth[name]
    stem = Path(name).stem
    for key, values in truth.items():
        if Path(key).stem == stem:
            return values
    return None


# --------------------------------------------------------------------------- crops


def crop_box(image, x, y, w, h):
    """Same slicing as ImageInstanceOps.get_model_marked_probs."""
    return image[max(int(y), 0) : int(y) + int(h), max(int(x), 0) : int(x) + int(w)]


def _result_dict(result):
    return result.to_dict() if hasattr(result, "to_dict") else result


def bubble_samples_from_result(
    result, truth_values, aligned_image=None, source=None, fields=None
) -> List[Sample]:
    """Label every bubble of a scan from the true field values.

    `truth_values` maps field labels to the correct value (concatenated values for
    multi-marked fields, "" for blank). Fields missing from the truth, or whose
    value cannot be produced by the field's bubbles, are skipped.
    """
    if aligned_image is None:
        aligned_image = getattr(result, "aligned_image", None)
    if aligned_image is None:
        raise ValueError("bubble export needs the aligned page image")
    data = _result_dict(result)
    source = source or data.get("file_id", "")
    samples = []
    for label, details in (data.get("fields") or {}).items():
        if fields is not None and label not in fields:
            continue
        if label not in truth_values:
            continue
        if truth_values[label] == MULTI_MARK:
            # Which bubbles are marked is not recorded
            continue
        bubbles = details.get("bubbles") or []
        marked = split_field_value(truth_values[label], [b["value"] for b in bubbles])
        if marked is None:
            continue
        for index, bubble in enumerate(bubbles):
            crop = crop_box(
                aligned_image, bubble["x"], bubble["y"], bubble["w"], bubble["h"]
            )
            if crop.size == 0:
                continue
            probability = bubble.get("model_marked_prob")
            samples.append(
                Sample(
                    image=np.ascontiguousarray(crop),
                    label=MARKED if index in marked else EMPTY,
                    kind=KIND_BUBBLE,
                    source=source,
                    field=label,
                    value=str(bubble["value"]),
                    predicted=MARKED if bubble.get("marked") else EMPTY,
                    confidence=(
                        probability
                        if probability is not None
                        else bubble.get("confidence")
                    ),
                )
            )
    return samples


def icr_box_crops(zone_crop, count):
    """Split an ICR zone crop into cleaned character boxes (as the reader does).

    Returns [(cleaned_box, ink_ratio)].
    """
    from src.readers.icr import clean_box, split_character_boxes

    if zone_crop.ndim == 3:
        zone_crop = cv2.cvtColor(zone_crop, cv2.COLOR_BGR2GRAY)
    return [clean_box(box) for box in split_character_boxes(zone_crop, count)]


def icr_samples_from_zone(
    aligned_image,
    box,
    count,
    truth_value,
    source="",
    zone_name="",
    predicted_chars=None,
    confidences=None,
    include_blank=False,
) -> List[Sample]:
    """Label the character boxes of one ICR zone from its true value.

    The value is assigned left to right, one character per box; remaining boxes
    are blank. Boxes the reader would skip as blank (too little ink) are only
    exported when include_blank is set, as "<blank>".
    """
    from src.readers.icr import MIN_INK_RATIO

    x, y, w, h = box
    crop = crop_box(aligned_image, x, y, w, h)
    if crop.size == 0:
        return []
    truth_value = _normalize_cell(truth_value).replace(" ", "")
    if len(truth_value) > count:
        return []
    samples = []
    for index, (cleaned, ink_ratio) in enumerate(icr_box_crops(crop, count)):
        if cleaned.size == 0:
            continue
        char = truth_value[index] if index < len(truth_value) else ""
        has_ink = ink_ratio >= MIN_INK_RATIO
        if not char and not (include_blank and has_ink):
            continue
        if char and not has_ink:
            # The reader would never classify this box; nothing useful to learn
            continue
        predicted = ""
        if predicted_chars is not None and index < len(predicted_chars):
            predicted = predicted_chars[index] or BLANK_LABEL
        confidence = None
        if confidences is not None and index < len(confidences):
            confidence = confidences[index]
        samples.append(
            Sample(
                image=np.ascontiguousarray(cleaned),
                label=char or BLANK_LABEL,
                kind=KIND_ICR,
                source=source,
                field=zone_name,
                value=str(index),
                predicted=predicted,
                confidence=confidence,
            )
        )
    return samples


def icr_samples_from_result(
    result, truth_values, template_zones, aligned_image=None, source=None, **kwargs
):
    """ICR character crops for every boxed ICR zone that has a truth value."""
    if aligned_image is None:
        aligned_image = getattr(result, "aligned_image", None)
    data = _result_dict(result)
    source = source or data.get("file_id", "")
    samples = []
    for zone in template_zones:
        count = zone.options.get("characterBoxes")
        if zone.type != "icr" or not count or zone.name not in truth_values:
            continue
        details = ((data.get("zones") or {}).get(zone.name) or {}).get("details") or {}
        samples += icr_samples_from_zone(
            aligned_image,
            [*zone.origin, *zone.dimensions],
            count,
            truth_values[zone.name],
            source=source,
            zone_name=zone.name,
            predicted_chars=details.get("characters"),
            confidences=details.get("confidences"),
            **kwargs,
        )
    return samples


def export_from_review(
    result,
    corrections,
    aligned_image,
    out_dir=None,
    template_zones=None,
    include_blank=False,
) -> List[Sample]:
    """Training samples from a reviewed scan.

    `result` is a ScanResult or its to_dict(); `corrections` maps field labels and
    zone names to the value the operator confirmed (corrected or accepted). Only
    those entries are labelled, so unreviewed fields never leak unverified
    machine readings into the training set. For ICR zones the number of
    character boxes comes from `template_zones` (template.zones) or, failing
    that, from the per-character details the ICR model reader stores.
    Samples are appended to `out_dir` when it is given.
    """
    data = _result_dict(result)
    source = data.get("file_id", "")
    samples = bubble_samples_from_result(
        data, corrections, aligned_image=aligned_image, source=source
    )
    box_counts = {
        zone.name: (
            zone.options.get("characterBoxes"),
            [*zone.origin, *zone.dimensions],
        )
        for zone in template_zones or []
        if zone.type == "icr"
    }
    for name, zone in (data.get("zones") or {}).items():
        if zone.get("type") != "icr" or name not in corrections:
            continue
        details = zone.get("details") or {}
        characters = details.get("characters")
        count, box = box_counts.get(name, (None, None))
        count = count or (len(characters) if characters else None)
        box = zone.get("box") or box
        if not count or not box:
            continue
        samples += icr_samples_from_zone(
            aligned_image,
            box,
            count,
            corrections[name],
            source=source,
            zone_name=name,
            predicted_chars=characters,
            confidences=details.get("confidences"),
            include_blank=include_blank,
        )
    if out_dir is not None:
        DatasetWriter(out_dir).write_all(samples)
    return samples


# --------------------------------------------------------------------------- writing


def _safe_name(text):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(text)).strip("_") or "x"


class DatasetWriter:
    """Writes crops as PNG files and appends to manifest.csv (safe to reopen)."""

    def __init__(self, out_dir):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.out_dir / MANIFEST_NAME
        self._names = set()
        self.count = 0

    def _unique_path(self, sample):
        directory = self.out_dir / label_to_dirname(sample.label)
        directory.mkdir(exist_ok=True)
        base = _safe_name(f"{Path(sample.source).stem}__{sample.field}__{sample.value}")
        name, suffix = f"{base}.png", 1
        while name in self._names or (directory / name).exists():
            suffix += 1
            name = f"{base}__{suffix}.png"
        self._names.add(name)
        return directory / name

    def write_all(self, samples: Iterable[Sample]):
        new_file = not self.manifest_path.exists()
        with open(self.manifest_path, "a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=MANIFEST_COLUMNS)
            if new_file:
                writer.writeheader()
            for sample in samples:
                path = self._unique_path(sample)
                cv2.imwrite(str(path), sample.image)
                writer.writerow(
                    {
                        "path": path.relative_to(self.out_dir).as_posix(),
                        "label": sample.label,
                        "kind": sample.kind,
                        "source": sample.source,
                        "field": sample.field,
                        "value": sample.value,
                        "predicted": sample.predicted,
                        "confidence": (
                            ""
                            if sample.confidence is None
                            else round(float(sample.confidence), 4)
                        ),
                    }
                )
                self.count += 1
        return self.count


# --------------------------------------------------------------------------- reading


def load_manifest(data_dir, kind=None):
    """[(absolute path, label, row)] from manifest.csv or from class folders."""
    data_dir = Path(data_dir)
    manifest = data_dir / MANIFEST_NAME
    entries = []
    if manifest.exists():
        with open(manifest, newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if kind and row.get("kind") and row["kind"] != kind:
                    continue
                entries.append((data_dir / row["path"], row["label"], row))
        return entries
    for directory in sorted(p for p in data_dir.iterdir() if p.is_dir()):
        label = dirname_to_label(directory.name)
        for path in sorted(directory.iterdir()):
            if path.suffix.lower() in IMAGE_EXTENSIONS:
                entries.append((path, label, {"source": path.stem.split("__")[0]}))
    return entries


def load_samples(data_dir, input_size=(32, 32), kind=None):
    """Load a dataset into memory: (images uint8 (N, H, W), labels, rows)."""
    width, height = input_size
    entries = load_manifest(data_dir, kind)
    images = np.zeros((len(entries), height, width), dtype=np.uint8)
    labels, rows, keep = [], [], []
    for index, (path, label, row) in enumerate(entries):
        image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if image is None or image.size == 0:
            continue
        images[index] = resize_crop(image, input_size)
        labels.append(label)
        rows.append(row)
        keep.append(index)
    return images[keep], labels, rows


def resize_crop(crop, input_size):
    """The resize CropClassifier.prepare() applies at inference."""
    width, height = input_size
    if crop.ndim == 3:
        crop = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    return cv2.resize(crop, (width, height), interpolation=cv2.INTER_AREA)


# --------------------------------------------------------------------------- scans


def iter_images(images):
    """Accept a folder, a single file or a list of paths."""
    if isinstance(images, (str, Path)):
        images = Path(images)
        if images.is_dir():
            return sorted(
                p
                for p in images.rglob("*")
                if p.suffix.lower() in IMAGE_EXTENSIONS and p.is_file()
            )
        return [images]
    return [Path(p) for p in images]


def export_from_scans(
    template_path,
    images,
    truth,
    out_dir,
    kinds=(KIND_BUBBLE, KIND_ICR),
    bubble_model_path=None,
    icr_model_path=None,
    include_blank=False,
    skip_flagged_registration=True,
    config_overrides=None,
):
    """Read labelled sheets with the engine and export their crops.

    `truth` is a path (CSV/JSON, see load_truth) or an already loaded dict.
    Returns a summary dict with counts per label and the files that were skipped.
    """
    from src.pipeline import STATUS_ERROR, OMREngine

    if not isinstance(truth, dict):
        truth = load_truth(truth)
    engine = OMREngine(
        template_path,
        bubble_model_path=bubble_model_path,
        icr_model_path=icr_model_path,
        config_overrides=config_overrides,
    )
    writer = DatasetWriter(out_dir)
    summary = {"sheets": 0, "skipped": [], "labels": {}}
    for path in iter_images(images):
        values = lookup_truth(truth, path.name) or lookup_truth(truth, str(path))
        if values is None:
            summary["skipped"].append({"file": str(path), "reason": "no truth row"})
            continue
        for result in engine.scan_path(path, keep_images=True):
            if result.status == STATUS_ERROR or result.aligned_image is None:
                if skip_flagged_registration:
                    summary["skipped"].append(
                        {"file": str(path), "reason": result.error or "error"}
                    )
                    continue
            samples = []
            if KIND_BUBBLE in kinds:
                samples += bubble_samples_from_result(result, values, source=path.name)
            if KIND_ICR in kinds:
                samples += icr_samples_from_result(
                    result,
                    values,
                    engine.template.zones,
                    source=path.name,
                    include_blank=include_blank,
                )
            writer.write_all(samples)
            summary["sheets"] += 1
            for sample in samples:
                summary["labels"][sample.label] = (
                    summary["labels"].get(sample.label, 0) + 1
                )
    summary["samples"] = writer.count
    return summary


# --------------------------------------------------------------------------- synthetic

# Degradations that keep the page geometry (the crops are cut at template
# coordinates without registration); registration jitter is simulated by the
# training-time augmentation instead.
SYNTH_DEGRADATIONS = {
    "clean": None,
    "scan": dict(blur=1.0, noise=4.0, shadow=0.15, jpeg_quality=(70, 95)),
    "phone": dict(blur=1.5, noise=9.0, shadow=0.45, jpeg_quality=(45, 85)),
}
SYNTH_STYLES = ("pen", "pencil", "partial", "tick", "mixed")


def synthetic_sheets(
    n_sheets, seed=0, styles=SYNTH_STYLES, degradations=("clean", "scan", "phone")
):
    """Yield (spec, image, truth, style, degradation) for varied synthetic sheets."""
    from src.synth import augment, default_spec, random_answers, render_sheet

    rng = random.Random(seed)
    specs = {}
    for index in range(n_sheets):
        with_zones = index % 2 == 0
        spec = specs.setdefault(with_zones, default_spec(with_zones=with_zones))
        style = styles[index % len(styles)]
        answers = random_answers(spec, rng, blank_rate=0.08, multi_rate=0.04)
        image, truth = render_sheet(
            spec, answers, rng=rng, mark_style=style, erasures=rng.randint(0, 12)
        )
        degradation = degradations[index % len(degradations)]
        options = SYNTH_DEGRADATIONS.get(degradation)
        if options:
            image, _ = augment(
                image,
                rng,
                rotation=0.0,
                perspective=0.0,
                background=False,
                **options,
            )
        yield spec, image, truth, style, degradation


def synthetic_samples(
    n_sheets,
    seed=0,
    kinds=(KIND_BUBBLE,),
    include_blank=False,
    max_empty_per_sheet=None,
    **kwargs,
) -> Iterable[Sample]:
    """Labelled crops from synthetic sheets, labelled from the renderer's truth.

    Empty bubbles vastly outnumber marked ones; max_empty_per_sheet subsamples
    them (erased bubbles are always kept since they are the hard negatives).
    """
    rng = random.Random(seed + 1)
    for index, (spec, image, truth, style, degradation) in enumerate(
        synthetic_sheets(n_sheets, seed, **kwargs)
    ):
        source = f"synth_{seed}_{index:05d}_{style}_{degradation}.png"
        if KIND_BUBBLE in kinds:
            bw, bh = spec.bubble
            marks = truth["marks"]
            empties = []
            for block, label, value, x, y in spec.bubble_positions():
                is_marked = f"{label}:{value}" in marks
                sample = Sample(
                    image=np.ascontiguousarray(crop_box(image, x, y, bw, bh)),
                    label=MARKED if is_marked else EMPTY,
                    kind=KIND_BUBBLE,
                    source=source,
                    field=label,
                    value=value,
                )
                if is_marked:
                    yield sample
                else:
                    empties.append(sample)
            if max_empty_per_sheet is not None and len(empties) > max_empty_per_sheet:
                # Erased bubbles are darker than clean empty ones: keep the darkest
                empties.sort(key=lambda s: float(s.image.mean()))
                hard = empties[: max_empty_per_sheet // 2]
                rest = empties[max_empty_per_sheet // 2 :]
                empties = hard + rng.sample(rest, max_empty_per_sheet - len(hard))
            yield from empties
        if KIND_ICR in kinds:
            for zone in spec.zones:
                count = zone.options.get("characterBoxes")
                if zone.type != "icr" or not count:
                    continue
                yield from icr_samples_from_zone(
                    image,
                    [*zone.origin, *zone.dimensions],
                    count,
                    truth["zones"][zone.name],
                    source=source,
                    zone_name=zone.name,
                    include_blank=include_blank,
                )


def generate_synthetic(out_dir, n_sheets, seed=0, **kwargs):
    """Write a synthetic pretraining dataset; returns label counts."""
    writer = DatasetWriter(out_dir)
    counts = {}
    batch = []
    for sample in synthetic_samples(n_sheets, seed, **kwargs):
        counts[sample.label] = counts.get(sample.label, 0) + 1
        batch.append(sample)
        if len(batch) >= 2000:
            writer.write_all(batch)
            batch = []
    writer.write_all(batch)
    return counts


# --------------------------------------------------------------------------- CLI


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m src.ml.dataset",
        description="Build crop-classifier datasets (bubbles and ICR characters).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    scans = sub.add_parser("scans", help="export crops from labelled scans")
    scans.add_argument("--template", required=True)
    scans.add_argument("--images", required=True, help="folder or image file")
    scans.add_argument("--truth", required=True, help="truth CSV or JSON")
    scans.add_argument("--out", required=True)
    scans.add_argument(
        "--kind",
        choices=["bubble", "icr", "all"],
        default="all",
        help="crops to export",
    )
    scans.add_argument("--bubble-model", default=None)
    scans.add_argument("--include-blank", action="store_true")

    synth = sub.add_parser("synthetic", help="render synthetic training crops")
    synth.add_argument("--out", required=True)
    synth.add_argument("--sheets", type=int, default=200)
    synth.add_argument("--seed", type=int, default=0)
    synth.add_argument("--kind", choices=["bubble", "icr", "all"], default="bubble")
    synth.add_argument(
        "--max-empty-per-sheet",
        type=int,
        default=60,
        help="subsample empty bubbles per sheet (0 keeps all)",
    )
    synth.add_argument("--include-blank", action="store_true")

    args = parser.parse_args(argv)
    kinds = (KIND_BUBBLE, KIND_ICR) if args.kind == "all" else (args.kind,)
    if args.command == "scans":
        summary = export_from_scans(
            args.template,
            args.images,
            args.truth,
            args.out,
            kinds=kinds,
            bubble_model_path=args.bubble_model,
            include_blank=args.include_blank,
        )
    else:
        summary = generate_synthetic(
            args.out,
            args.sheets,
            args.seed,
            kinds=kinds,
            include_blank=args.include_blank,
            max_empty_per_sheet=args.max_empty_per_sheet or None,
        )
    print(json.dumps(summary, indent=2))
    return summary


if __name__ == "__main__":
    main()
