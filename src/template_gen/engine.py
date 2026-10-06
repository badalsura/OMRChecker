"""
Automatic template generation from a few sheets of the same printed form.

    result = generate_template(images, labels)
    result.save("out_dir")  # template.json, reference.png, generation_report.json

Pipeline (see each module for the method details):
  rectify.py  page quadrilateral -> canonical page, flat-field correction,
              ECC (ORB+RANSAC fallback) registration to the least-inked sheet
  engine.py   blank page = per-pixel 80th percentile of the registered sheets
  marks.py    timing tracks and corner markers on the blank page
  zones.py    barcode/QR (ZXing), character boxes (ICR), variable text (OCR)
  bubbles.py  bubble outlines on the blank page -> regular grids
  labels.py   correlation + Hungarian matching of bubble lines to labels
Then the template is validated against the JSON schema and re-read with
OMREngine on the registered sheets (and end-to-end on the raw images when
every preprocessor it uses is available) to measure agreement with labels.
The template's preProcessors are TimingMarkAlignment when timing tracks exist;
otherwise the simplest chain (CropPage, CropPage+EccAlignment, EccAlignment or
FeatureBasedAlignment) that reads a sample of the raw sheets correctly.
"""

import json
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import cv2
import numpy as np

from src.template_gen import bubbles
from src.template_gen import labels as label_ops
from src.template_gen import marks, rectify, zones
from src.template_gen.assignment import boxes_overlap, otsu_1d, point_in_box

DEFAULT_OPTIONS = {
    # Force the canonical page size [w, h]; default: median measured page size
    "page_size": None,
    # Larger pages are scaled down (keeps reading fast; ~200 DPI for A4)
    "max_page_width": 1700,
    # Smaller pages are upsampled so that bubbles are big enough to detect
    "min_page_width": 1000,
    # Index of the sheet to register against; default: the sheet with least ink
    "reference_index": None,
    # Explicit preProcessors for the template; default: chosen automatically
    "pre_processors": None,
    "self_check": True,
    "end_to_end_check": True,
    "detect_ocr": True,
    "max_symbol_sheets": 6,
    "min_pair_score": 0.5,
    "workers": 4,
    # Agreement below this flags a block or field for verification
    "verify_below": 0.99,
}
DIGIT_PREFIXES = ["roll", "id", "code", "num", "numb", "numc", "numd"]


@dataclass
class GenerationResult:
    template: dict
    reference_image: np.ndarray
    report: dict
    registered_images: Optional[List[np.ndarray]] = field(default=None, repr=False)

    def to_dict(self):
        return {"template": self.template, "report": self.report}

    def save(self, out_dir, overlay=True):
        from src.template_gen.corrections import render_overlay

        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        with open(out_dir / "template.json", "w") as f:
            json.dump(self.template, f, indent=2)
        with open(out_dir / "generation_report.json", "w") as f:
            json.dump(self.report, f, indent=2)
        cv2.imwrite(str(out_dir / "reference.png"), self.reference_image)
        if overlay:
            cv2.imwrite(
                str(out_dir / "overlay.png"),
                render_overlay(self.reference_image, self.template),
            )
        return out_dir


def _jsonable(value):
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, set):
        return sorted(_jsonable(v) for v in value)
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    return value


def _to_gray(image):
    if image is None:
        raise ValueError("An input image is None (unreadable file?)")
    image = np.asarray(image)
    if image.ndim == 3:
        image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    if image.dtype != np.uint8:
        image = cv2.normalize(image, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
    return image


def _blank_page(pages, workers=4, quantile=0.8):
    """
    Per-pixel high quantile of the registered pages: marks only ever darken
    the paper, so this recovers the blank printed form even for bubbles that
    most sheets fill (e.g. the right answer to an easy question).
    Computed in horizontal strips on a thread pool.
    """
    stack = np.stack(pages)
    k = int(round(quantile * (stack.shape[0] - 1)))
    height = stack.shape[1]
    bounds = np.linspace(0, height, max(1, workers) * 2 + 1).astype(int)
    out = np.empty(stack.shape[1:], np.uint8)

    def strip(index):
        a, b = bounds[index], bounds[index + 1]
        out[a:b] = np.partition(stack[:, a:b], k, axis=0)[k]

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        list(pool.map(strip, range(len(bounds) - 1)))
    return out


def _fill_threshold(values):
    values = np.asarray(values, dtype=np.float64).ravel()
    threshold = otsu_1d(np.clip(values, -50, 255))
    low, high = values[values <= threshold], values[values > threshold]
    if len(low) == 0 or len(high) == 0 or high.mean() - low.mean() < 40:
        return 60.0
    return float(np.clip(threshold, 25, 150))


def _reading_order(grids):
    order = sorted(range(len(grids)), key=lambda k: grids[k].bbox()[0])
    columns = []
    for k in order:
        x, _, w, _ = grids[k].bbox()
        for column in columns:
            if x < column["x1"] - 0.5 * min(w, column["w"]):
                column["items"].append(k)
                column["x1"] = max(column["x1"], x + w)
                break
        else:
            columns.append({"x1": x + w, "w": w, "items": [k]})
    return [k for c in columns for k in sorted(c["items"], key=lambda i: grids[i].y0)]


def _unique(name, taken):
    candidate, suffix = name, 0
    while candidate in taken:
        suffix += 1
        candidate = f"{name}_{chr(96 + suffix) if suffix < 27 else suffix}"
    taken.add(candidate)
    return candidate


def _default_naming(grids, indices, taken, q_start=1):
    """Field names, values and direction for blocks without labels."""
    out, q_next, digit_blocks = {}, q_start, 0
    for k in _reading_order(grids):
        if k not in indices:
            continue
        grid = grids[k]
        direction = label_ops.default_direction(grid)
        n_values = grid.cols if direction == "horizontal" else grid.rows
        n_fields = grid.rows if direction == "horizontal" else grid.cols
        values = label_ops.default_values(n_values)
        if n_values == 10 and n_fields >= 2:
            prefix = DIGIT_PREFIXES[min(digit_blocks, len(DIGIT_PREFIXES) - 1)]
            while any(f"{prefix}{i}" in taken for i in range(1, n_fields + 1)):
                prefix += "x"
            names = [f"{prefix}{i}" for i in range(1, n_fields + 1)]
            digit_blocks += 1
        else:
            names = []
            for _ in range(n_fields):
                while f"q{q_next}" in taken:
                    q_next += 1
                names.append(f"q{q_next}")
                q_next += 1
        taken.update(names)
        out[k] = {
            "direction": direction,
            "field_labels": names,
            "values": values,
            "agreement": None,
            "slot_scores": [],
            "value_completed": True,
            "default_named": True,
        }
    return out


def _clamp_zone(zone, page_size):
    """Keep a zone inside the page (the Template rejects overflowing zones)."""
    page_w, page_h = page_size
    x, y = (max(0, int(v)) for v in zone["origin"])
    x, y = min(x, page_w - 2), min(y, page_h - 2)
    w = max(1, min(int(zone["dimensions"][0]), page_w - 1 - x))
    h = max(1, min(int(zone["dimensions"][1]), page_h - 1 - y))
    zone["origin"], zone["dimensions"] = [x, y], [w, h]
    return zone


def _block_template(grid, assignment, bubble_dims):
    direction = assignment["direction"]
    w, h = grid.bubble
    if direction == "horizontal":
        bubbles_gap, labels_gap = grid.dx, grid.dy
    else:
        bubbles_gap, labels_gap = grid.dy, grid.dx
    block = {
        "origin": [int(round(grid.x0 - w / 2)), int(round(grid.y0 - h / 2))],
        "bubblesGap": round(float(bubbles_gap), 2),
        "labelsGap": round(float(labels_gap), 2),
        "fieldLabels": label_ops.compress_labels(assignment["field_labels"]),
        "bubbleValues": list(assignment["values"]),
        "direction": direction,
    }
    if (
        abs(w - bubble_dims[0]) > 0.15 * bubble_dims[0]
        or abs(h - bubble_dims[1]) > 0.15 * bubble_dims[1]
    ):
        block["bubbleDimensions"] = [int(round(w)), int(round(h))]
    return block


def _block_name(assignment, taken):
    first = assignment["field_labels"][0]
    match = label_ops.LABEL_NUMBER.match(first)
    if (
        assignment["values"] == list("0123456789")
        and len(assignment["field_labels"]) > 1
        and match
    ):
        base = match.group(1).rstrip("_").capitalize() or "Digits"
    else:
        base = f"Block_{first}"
    return _unique(base, taken)


def _custom_labels(blocks, composites, field_names, zone_names):
    custom, used_fields = {}, set()
    taken = set(field_names) | set(zone_names)
    for original, subs in composites.items():
        if all(s in field_names for s in subs):
            key = original if original not in taken else f"{original}_value"
            custom[key] = label_ops.compress_labels(subs)
            used_fields.update(subs)
            taken.add(key)
    for assignment in blocks:
        names = assignment["field_labels"]
        if assignment["values"] != list("0123456789") or len(names) < 2:
            continue
        if assignment["direction"] != "vertical" or used_fields & set(names):
            continue
        matches = [label_ops.LABEL_NUMBER.match(n) for n in names]
        if not all(matches) or len({m.group(1) for m in matches}) != 1:
            continue
        prefix = matches[0].group(1).rstrip("_")
        key = prefix.capitalize() if prefix else "Digits"
        while key in taken:
            key += "_value"
        ordered = sorted(
            names, key=lambda n: int(label_ops.LABEL_NUMBER.match(n).group(2))
        )
        custom[key] = label_ops.compress_labels(ordered)
        used_fields.update(names)
        taken.add(key)
    return custom


def candidate_pre_processors(tracks, page_infos):
    """Registration chains worth trying, most preferred first."""
    if tracks:
        dims = np.median([t["mark_dimensions"] for t in tracks.values()], axis=0)
        timing = {
            "name": "TimingMarkAlignment",
            "options": {
                "tracks": {
                    name: {"marks": track["marks"]} for name, track in tracks.items()
                },
                "markDimensions": [round(float(d), 1) for d in dims],
            },
        }
        return [[timing]]
    crop = {"name": "CropPage", "options": {"morphKernel": [10, 10]}}
    ecc = {
        "name": "EccAlignment",
        "options": {"reference": "reference.png", "motion": "affine"},
    }
    features = {
        "name": "FeatureBasedAlignment",
        "options": {"reference": "reference.png"},
    }
    if np.mean([info["page_found"] for info in page_infos]) >= 0.5:
        return [[crop], [crop, ecc]]
    return [[ecc], [features]]


def validate_template(template):
    """Return a list of JSON-schema error messages (empty when valid)."""
    from src.schemas import SCHEMA_VALIDATORS

    return [
        f"{'/'.join(str(p) for p in error.path)}: {error.message}"
        for error in SCHEMA_VALIDATORS["template"].iter_errors(template)
    ]


def _compare(value, truth):
    if truth is None:
        return None
    return set(str(value or "").replace(" ", "")) == truth


def self_check(template, images, table, page_size, reference=None, end_to_end=False):
    """
    Re-read sheets with OMREngine and compare to the label table.
    images are registered pages (end_to_end False) or raw inputs (True).
    Returns a report dict.
    """
    from src.pipeline import OMREngine
    from src.processors.manager import PROCESSOR_MANAGER

    started = time.perf_counter()
    template = json.loads(json.dumps(template))
    if end_to_end:
        missing = [
            p["name"]
            for p in template["preProcessors"]
            if p["name"] not in PROCESSOR_MANAGER.processors
        ]
        if missing:
            return {"skipped": f"preprocessor(s) not available: {missing}"}
    else:
        template["preProcessors"] = []
    # Zones are checked separately; OCR/ICR engines would dominate the runtime
    template.pop("zones", None)
    per_field, failures, statuses = {}, [], []
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp, "template.json")
        path.write_text(json.dumps(template))
        if reference is not None:
            cv2.imwrite(str(Path(tmp, "reference.png")), reference)
        overrides = {"outputs": {"show_image_level": 0, "save_image_level": 0}}
        if not end_to_end:
            overrides["dimensions"] = {
                "processing_width": page_size[0],
                "processing_height": page_size[1],
            }
        try:
            engine = OMREngine(path, config_overrides=overrides)
        except Exception as error:  # pragma: no cover - reported, not raised
            return {"error": f"template failed to load: {error}"}
        for s, image in enumerate(images):
            try:
                result = engine.scan(image, f"sheet_{s}", keep_images=False)
            except Exception as error:
                failures.append({"sheet": s, "error": str(error)})
                continue
            statuses.append(result.status)
            if result.status == "error":
                failures.append({"sheet": s, "error": result.error})
                continue
            row = table[s] if table else None
            for label, details in result.fields.items():
                if not row or label not in row:
                    continue
                ok = _compare(details["value"], row[label])
                if ok is None:
                    continue
                stats = per_field.setdefault(label, [0, 0])
                stats[0] += int(ok)
                stats[1] += 1
    agree = sum(v[0] for v in per_field.values())
    total = sum(v[1] for v in per_field.values())
    return {
        "ms": round((time.perf_counter() - started) * 1000, 1),
        "sheets": len(images),
        "failed_sheets": failures,
        "needs_review_sheets": statuses.count("needs_review"),
        "overall_agreement": round(agree / total, 4) if total else None,
        "per_field": {k: round(v[0] / v[1], 4) for k, v in per_field.items()},
    }


def generate_template(images, labels=None, options=None):
    """
    Build a template.json for the form in `images` (grayscale arrays).
    labels: optional list (one per image) of {field_label: value} or None.
    """
    opts = {**DEFAULT_OPTIONS, **(options or {})}
    timings, warnings, verify = {}, [], []
    started = step = time.perf_counter()

    def lap(name):
        nonlocal step
        now = time.perf_counter()
        timings[name] = round((now - step) * 1000, 1)
        step = now

    images = [_to_gray(image) for image in images]
    if not images:
        raise ValueError("At least one image is required")
    if labels is not None and len(labels) != len(images):
        raise ValueError("labels must have the same length as images")

    # 1. Rectify and register
    rectified, page_size, page_infos = rectify.rectify_pages(
        images,
        opts["page_size"],
        opts["max_page_width"],
        opts["workers"],
        opts["min_page_width"],
    )
    missing_page = [i for i, info in enumerate(page_infos) if not info["page_found"]]
    if missing_page:
        warnings.append(
            f"page edges not found on {len(missing_page)} sheet(s) {missing_page[:10]}; "
            "the whole image was used as the page"
        )
    ref_index = opts["reference_index"]
    if ref_index is None:
        ref_index = int(np.argmax([float(r.mean()) for r in rectified]))
    reference = rectified[ref_index]

    def register(index):
        if index == ref_index:
            return reference, {"method": "reference", "correlation": 1.0, "ok": True}
        refine = not (
            page_infos[index]["page_found"] and page_infos[ref_index]["page_found"]
        )
        return rectify.register_to_reference(reference, rectified[index], refine=refine)

    with ThreadPoolExecutor(max_workers=max(1, int(opts["workers"]))) as pool:
        registered = list(pool.map(register, range(len(rectified))))
    aligned = [a for a, _ in registered]
    for index, (_, info) in enumerate(registered):
        page_infos[index]["registration"] = info
        if not info["ok"]:
            warnings.append(
                f"sheet {index}: registration is unreliable "
                f"(correlation {info['correlation']}); excluded from analysis"
            )
    good = [i for i, (_, info) in enumerate(registered) if info["ok"]]
    flipped = [i for i in good if registered[i][1].get("rotated_180")]
    if len(flipped) > len(good) / 2:
        # The reference sheet was the upside-down one: follow the majority
        aligned = [cv2.rotate(a, cv2.ROTATE_180) for a in aligned]
        for _, info in registered:
            info["rotated_180"] = not info.get("rotated_180", False)
        warnings.append("reference sheet was upside down; page rotated 180 degrees")
    lap("registration")

    if len(good) >= 2:
        blank = _blank_page([aligned[i] for i in good], opts["workers"])
    else:
        blank = aligned[good[0] if good else ref_index]
    lap("blank_page")

    # 2. Timing tracks, corner markers
    tracks, solid = marks.detect_timing_tracks(blank, page_size)
    corners = marks.detect_corner_markers(solid, page_size, tracks)
    lap("marks")

    # 3. Barcodes / QR codes / character boxes
    symbol_zones = zones.detect_symbol_zones(
        [aligned[i] for i in good],
        page_size,
        opts["max_symbol_sheets"],
        sheet_ids=good,
        workers=opts["workers"],
    )
    mark_boxes = [b for t in tracks.values() for b in t["boxes"]] + [
        [
            c["centre"][0] - c["dimensions"][0] / 2,
            c["centre"][1] - c["dimensions"][1] / 2,
            c["dimensions"][0],
            c["dimensions"][1],
        ]
        for c in corners.values()
    ]
    icr_zones = zones.detect_character_boxes(
        blank, page_size, [z["box"] for z in symbol_zones]
    )
    lap("zones")

    # 4. Bubbles -> grids
    candidates = bubbles.detect_bubble_candidates(blank, page_size)
    exclusions = mark_boxes + [z["box"] for z in symbol_zones + icr_zones]
    keep = [
        k
        for k, (cx, cy, _, _, _) in enumerate(candidates)
        if not any(point_in_box(cx, cy, box, 2) for box in exclusions)
    ]
    candidates = candidates[keep] if len(candidates) else candidates
    grids, rejected = bubbles.group_into_grids(candidates, blank)
    for item in rejected:
        if item["reason"] == "touching_cells" and item["grid"].rows == 1:
            box = [int(round(v)) for v in item["grid"].bbox()]
            if not any(boxes_overlap(box, z["box"]) for z in icr_zones):
                icr_zones.append(
                    {"type": "icr", "box": box, "character_boxes": item["grid"].cols}
                )
    if not grids:
        warnings.append("no bubble grids found")
    lap("bubbles")

    # 5. Fill levels and label assignment
    fills = bubbles.sample_fill(aligned, grids, blank)
    threshold = (
        _fill_threshold(np.concatenate([f[good].ravel() for f in fills]))
        if fills
        else 60.0
    )
    labelled = labels is not None and any(labels)
    assigned = None
    if labelled and grids:
        masked = [lab if i in good else None for i, lab in enumerate(labels)]
        assigned = label_ops.assign_with_labels(
            grids, fills, threshold, masked, opts["min_pair_score"]
        )
    taken, block_assignments, unlabelled = set(), {}, []
    if assigned:
        taken.update(assigned["all_labels"])
        for k, result in enumerate(assigned["blocks"]):
            matched = [n for n in result["field_labels"] if n is not None]
            poor = result["agreement"] is not None and result["agreement"] < 0.5
            if len(matched) < 0.5 * len(result["field_labels"]) or poor:
                # Too little evidence: fall back to guessed names, keep labels free
                unlabelled.append(k)
                assigned["unmatched_labels"].extend(matched)
                continue
            names = []
            for i, name in enumerate(result["field_labels"]):
                if name is None:
                    name = _unique(f"unlabelled_{k + 1}_{i + 1}", taken)
                    verify.append(
                        {
                            "kind": "field",
                            "name": name,
                            "reason": "no label matched this bubble line",
                        }
                    )
                names.append(name)
            block_assignments[k] = {**result, "field_labels": names}
    else:
        unlabelled = list(range(len(grids)))
    block_assignments.update(_default_naming(grids, set(unlabelled), taken))
    lap("labels")

    # 6. Build the template
    bubble_dims = (
        [int(round(v)) for v in np.median([g.bubble for g in grids], axis=0)]
        if grids
        else [20, 20]
    )
    field_blocks, block_reports, block_names = {}, [], set()
    order = [k for k in _reading_order(grids) if k in block_assignments]
    for k in order:
        grid, assignment = grids[k], block_assignments[k]
        name = _block_name(assignment, block_names)
        field_blocks[name] = _block_template(grid, assignment, bubble_dims)
        reasons = []
        if assignment.get("default_named"):
            reasons.append("labels and values guessed (no matching labels)")
        if assignment.get("value_completed") and not assignment.get("default_named"):
            reasons.append("some bubble values inferred, not observed in labels")
        if grid.confidence < 0.9:
            reasons.append(f"irregular grid (confidence {grid.confidence})")
        block_reports.append(
            {
                "name": name,
                "rows": grid.rows,
                "cols": grid.cols,
                "direction": assignment["direction"],
                "fields": len(assignment["field_labels"]),
                "field_labels": list(assignment["field_labels"]),
                "values": assignment["values"],
                "bbox": [round(v, 1) for v in grid.bbox()],
                "detection_confidence": grid.confidence,
                "pitch_residual_px": grid.residual,
                # agreement of a plain global-threshold read with the labels
                "threshold_agreement": assignment.get("agreement"),
                "label_agreement": assignment.get("agreement"),
                "slot_scores": assignment.get("slot_scores", []),
                "reasons": reasons,
            }
        )

    field_names = {n for a in block_assignments.values() for n in a["field_labels"]}
    template_zones, zone_reports = {}, []
    zone_taken = set(field_names)
    label_columns = {}
    if labelled:
        for s, entry in enumerate(labels):
            for key, value in (entry or {}).items():
                label_columns.setdefault(key, {})[s] = str(value).strip()
    used_columns = set()
    if assigned:
        used_columns = {
            key
            for key in label_columns
            if key in field_names
            or all(
                sub in field_names for sub in assigned["composites"].get(key, [None])
            )
        }
    for zone in symbol_zones:
        name = None
        for key, values in label_columns.items():
            if key in used_columns:
                continue
            hits = [values.get(s) == text for s, text in zone["texts"]]
            if hits and np.mean(hits) >= 0.6:
                name = key
                break
        name = _unique(name or zone["type"], zone_taken)
        used_columns.add(name)
        x, y, w, h = zone["box"]
        template_zones[name] = {
            "type": zone["type"],
            "origin": [int(x), int(y)],
            "dimensions": [int(w), int(h)],
            "options": {"formats": zone["formats"]},
        }
        zone_reports.append({"name": name, **zone})
        verify.append(
            {"kind": "zone", "name": name, "reason": f"detected {zone['formats']}"}
        )
    for zone in icr_zones:
        count = zone["character_boxes"]
        name, whitelist = None, None
        columns = []
        for key, values in label_columns.items():
            if key in used_columns:
                continue
            filled = [v for v in values.values() if v]
            if filled and np.mean([len(v) == count for v in filled]) >= 0.8:
                columns.append(key)
        if len(columns) == 1:
            # The only unused label column with one character per box
            name = columns[0]
            if all(v.isdigit() for v in label_columns[name].values() if v):
                whitelist = "0123456789"
        name = _unique(name or "icr", zone_taken)
        used_columns.add(name)
        x, y, w, h = zone["box"]
        options = {"characterBoxes": int(count)}
        if whitelist:
            options["whitelist"] = whitelist
        template_zones[name] = {
            "type": "icr",
            "origin": [max(int(x), 0), max(int(y), 0)],
            "dimensions": [int(w), int(h)],
            "options": options,
        }
        zone_reports.append({"name": name, **zone})
        verify.append(
            {
                "kind": "zone",
                "name": name,
                "reason": f"{count} character boxes; check name and whitelist",
            }
        )
    if opts["detect_ocr"]:
        exclude = (
            exclusions
            + [z["box"] for z in icr_zones]
            + [
                [b[0] - 10, b[1] - 10, b[2] + 20, b[3] + 20]
                for b in (g.bbox() for g in grids)
            ]
        )
        for zone in zones.detect_variable_text(
            [aligned[i] for i in good], blank, page_size, exclude
        ):
            name = _unique("ocr", zone_taken)
            x, y, w, h = zone["box"]
            template_zones[name] = {
                "type": "ocr",
                "origin": [int(x), int(y)],
                "dimensions": [int(w), int(h)],
                "options": {},
            }
            zone_reports.append({"name": name, **zone})
            verify.append(
                {
                    "kind": "zone",
                    "name": name,
                    "reason": "text that changes between sheets; OCR or ICR?",
                }
            )
    lap("zones_naming")

    for zone in template_zones.values():
        _clamp_zone(zone, page_size)
    composites = assigned["composites"] if assigned else {}
    custom_labels = _custom_labels(
        [block_assignments[k] for k in order], composites, field_names, template_zones
    )
    pre_processors = opts["pre_processors"]
    candidates = [pre_processors]
    if pre_processors is None:
        candidates = candidate_pre_processors(tracks, page_infos)
        pre_processors = candidates[0]
    template = {
        "pageDimensions": [int(page_size[0]), int(page_size[1])],
        "bubbleDimensions": bubble_dims,
        "preProcessors": pre_processors,
        "fieldBlocks": field_blocks,
    }
    if custom_labels:
        template["customLabels"] = custom_labels
    if template_zones:
        template["zones"] = template_zones
    template = _jsonable(template)
    schema_errors = validate_template(template)
    if schema_errors:
        warnings.extend(f"schema: {e}" for e in schema_errors)
    lap("template")

    # 7. Self-check
    checks = {}
    table = None
    if assigned:
        table = assigned["table"]
    if len(candidates) > 1 and opts["self_check"] and not schema_errors:
        # Pick the registration chain that reads a few raw sheets best
        trials, sample, best = [], good[:6], None
        for chain in candidates:
            trial = {**template, "preProcessors": chain}
            result = self_check(
                trial,
                [images[i] for i in sample],
                [table[i] for i in sample] if table else None,
                page_size,
                blank,
                end_to_end=True,
            )
            ok = len(sample) - len(result.get("failed_sheets", sample))
            score = (ok, result.get("overall_agreement") or 0.0)
            trials.append(
                {
                    "preProcessors": [p["name"] for p in chain],
                    "read": ok,
                    "of": len(sample),
                    "agreement": result.get("overall_agreement"),
                    "ms": result.get("ms"),
                }
            )
            if best is None or score > best[0]:
                best = (score, chain)
            agreement = result.get("overall_agreement")
            if ok == len(sample) and (
                agreement is None or agreement >= opts["verify_below"]
            ):
                break  # good enough: keep the preferred (simplest) chain
        template["preProcessors"] = best[1]
        checks["pre_processor_trials"] = trials
    if opts["self_check"] and field_blocks and not schema_errors:
        checks["registered"] = self_check(
            template,
            [aligned[i] for i in good],
            [table[i] for i in good] if table else None,
            page_size,
        )
        if opts["end_to_end_check"]:
            checks["end_to_end"] = self_check(
                template, images, table, page_size, blank, end_to_end=True
            )
        per_field = checks["registered"].get("per_field", {})
        for block in block_reports:
            scores = [per_field[n] for n in block["field_labels"] if n in per_field]
            if scores:
                block["label_agreement"] = round(float(np.mean(scores)), 4)
        for label, score in sorted(per_field.items()):
            if score < opts["verify_below"]:
                verify.append(
                    {
                        "kind": "field",
                        "name": label,
                        "reason": f"self-check agreement {score:.1%}",
                    }
                )
    lap("self_check")

    block_verify = []
    for block in block_reports:
        reasons = block.pop("reasons")
        agreement = block["label_agreement"]
        if agreement is not None and agreement < opts["verify_below"]:
            reasons.insert(0, f"label agreement {agreement:.1%}")
        block["needs_verification"] = bool(reasons)
        if reasons:
            block_verify.append(
                {"kind": "block", "name": block["name"], "reason": "; ".join(reasons)}
            )
    verify[:0] = block_verify

    if assigned and assigned["unmatched_labels"]:
        leftover = [
            n
            for n in assigned["unmatched_labels"]
            if n not in used_columns
            and not any(
                n in subs and orig in used_columns for orig, subs in composites.items()
            )
        ]
        if leftover:
            warnings.append(f"labels not matched to any bubbles: {leftover}")
    if not labelled:
        warnings.append("no labels given: field names and values are guesses")
    if corners and len(corners) < 4:
        warnings.append(f"only {len(corners)} corner markers found")

    agreements = [
        b["label_agreement"] for b in block_reports if b["label_agreement"] is not None
    ]
    timings["total"] = round((time.perf_counter() - started) * 1000, 1)
    report = {
        "page": {
            "dimensions": template["pageDimensions"],
            "reference_sheet": ref_index,
        },
        "sheets": [
            {
                "index": i,
                "page_found": info["page_found"],
                "registration": info.get("registration"),
                "used": i in good,
            }
            for i, info in enumerate(page_infos)
        ],
        "fill_threshold": round(threshold, 1),
        "blocks": block_reports,
        "label_agreement": round(float(np.mean(agreements)), 4) if agreements else None,
        "timing_tracks": {
            name: {k: v for k, v in track.items() if k != "boxes"}
            for name, track in tracks.items()
        },
        "corner_markers": corners,
        "zones": zone_reports,
        "self_check": checks,
        "schema_errors": schema_errors,
        "warnings": warnings,
        "needs_verification": verify,
        "timings_ms": timings,
    }
    return GenerationResult(
        template=template,
        reference_image=blank,
        report=_jsonable(report),
        registered_images=aligned,
    )
