"""
Manual review: crops for the reviewer, applying corrections to a stored result,
and exporting corrected samples for model training.

Training export (under <data_dir>/training/):

    labels.jsonl                one JSON line per reviewed field/zone
    crops/<template>/<scan>_<name>.png         field or zone crop
    bubbles/<template>/<scan>_<name>_<i>.png   single-bubble crops (fields only)

Each line holds the crop path(s), the corrected label, the original prediction,
its confidence and flags, and whether the reviewer corrected or accepted it.
"""

import json
import threading
import time
from pathlib import Path

import cv2

from src.api.storage import safe_filename

_TRAINING_LOCK = threading.Lock()


class ReviewError(Exception):
    pass


def item_box(result, name):
    """(kind, [x, y, w, h]) of a field or zone in aligned-image coordinates."""
    field = (result.get("fields") or {}).get(name)
    if field is not None:
        bubbles = field.get("bubbles") or []
        if not bubbles:
            return "field", None
        x0 = min(b["x"] for b in bubbles)
        y0 = min(b["y"] for b in bubbles)
        x1 = max(b["x"] + b["w"] for b in bubbles)
        y1 = max(b["y"] + b["h"] for b in bubbles)
        return "field", [x0, y0, x1 - x0, y1 - y0]
    zone = (result.get("zones") or {}).get(name)
    if zone is not None:
        return "zone", zone.get("box")
    return None, None


def crop_box(image, box, pad=20):
    x, y, w, h = [int(v) for v in box]
    img_h, img_w = image.shape[:2]
    x0, y0 = max(x - pad, 0), max(y - pad, 0)
    x1, y1 = min(x + w + pad, img_w), min(y + h + pad, img_h)
    if x1 <= x0 or y1 <= y0:
        return None
    return image[y0:y1, x0:x1]


def split_value(value, bubble_values):
    """Split a field value into bubble values (multi-marks are concatenated)."""
    if value in ("", None):
        return []
    if value in bubble_values:
        return [value]
    ordered = sorted(set(bubble_values), key=len, reverse=True)
    picked, rest = [], value
    while rest:
        for candidate in ordered:
            if candidate and rest.startswith(candidate):
                picked.append(candidate)
                rest = rest[len(candidate) :]
                break
        else:
            return None
    return picked


def recompute(result, engine):
    """Recompute concatenated responses, score and status after edits."""
    from src.evaluation import evaluate_concatenated_response
    from src.utils.parsing import get_concatenated_response

    template = engine.template
    omr_response = {
        name: field.get("value", "")
        for name, field in (result.get("fields") or {}).items()
    }
    for name, zone in (result.get("zones") or {}).items():
        omr_response[name] = zone.get("value", "")
    try:
        result["responses"] = get_concatenated_response(omr_response, template)
    except KeyError:
        # Template changed since the scan; keep a flat response
        result["responses"] = {**(result.get("responses") or {}), **omr_response}
    if engine.evaluation_config is not None:
        try:
            result["score"] = evaluate_concatenated_response(
                result["responses"],
                engine.evaluation_config,
                Path(result.get("file_id", "scan")),
                None,
            )
        except Exception as error:
            result["score_error"] = str(error)
    result["status"] = "needs_review" if result.get("review") else "ok"
    if result.get("error") and not result.get("fields"):
        result["status"] = "error"


def normalize_label(label):
    if isinstance(label, list):
        label = "".join(str(v) for v in label)
    return "" if label is None else str(label)


def validate_field_value(name, field, label, empty_value=None):
    """Raise ReviewError unless label is blank/empty or a combination of bubble values."""
    if empty_value is not None and label == empty_value:
        return
    values = [b["value"] for b in field.get("bubbles") or []]
    if split_value(label, values) is None:
        raise ReviewError(
            f"'{label}' is not a combination of the bubble values {values} of '{name}'"
        )


def apply_review(result, corrections, accept, reviewer=None, empty_values=None):
    """
    Update the result in place. Returns the list of resolved item names and a
    list of training events: (kind, name, predicted, label, action, item).

    The first machine read of every item survives as "original_value" and the
    items flagged when the sheet was read as "read_review", so later accuracy
    statistics compare against what the engine actually produced.
    empty_values: {field: value written for an unmarked field} (template emptyValue).
    """
    corrections = {k: normalize_label(v) for k, v in dict(corrections or {}).items()}
    accept = list(accept or [])
    empty_values = empty_values or {}
    fields = result.get("fields") or {}
    zones = result.get("zones") or {}
    unknown = [n for n in [*corrections, *accept] if n not in fields and n not in zones]
    if unknown:
        raise ReviewError(f"Unknown field or zone: {', '.join(sorted(unknown))}")
    # Validate everything before changing anything
    for name, label in corrections.items():
        if name in fields:
            validate_field_value(name, fields[name], label, empty_values.get(name))

    if "read_review" not in result:
        result["read_review"] = [dict(item) for item in result.get("review") or []]
    events, resolved = [], []
    now = time.time()
    for name in [*corrections.keys(), *[a for a in accept if a not in corrections]]:
        corrected = name in corrections
        target = fields.get(name) if name in fields else zones.get(name)
        kind = "field" if name in fields else "zone"
        predicted = target.get("value", "")
        label = corrections[name] if corrected else predicted
        events.append(
            (
                kind,
                name,
                predicted,
                label,
                "corrected" if corrected and label != predicted else "accepted",
                dict(target),
            )
        )
        target["value"] = label
        target["needs_review"] = False
        target["reviewed"] = True
        if corrected and label != predicted and "original_value" not in target:
            target["original_value"] = predicted
        result.setdefault("review_log", {})[name] = {
            "predicted": predicted,
            "label": label,
            "action": events[-1][4],
            "at": now,
            "reviewer": reviewer,
        }
        resolved.append(name)
    result["review"] = [
        item for item in (result.get("review") or []) if item["name"] not in resolved
    ]
    result["reviewed"] = True
    return resolved, events


def write_training_records(training_root, result, events, aligned):
    """Append labelled crops for every reviewed item (needs the aligned image)."""
    if not events:
        return []
    training_root = Path(training_root)
    template_id = safe_filename(result.get("template_id") or "unknown")
    scan_id = result["scan_id"]
    records = []
    for kind, name, predicted, label, action, item in events:
        safe_name = safe_filename(name)
        record = {
            "scan_id": scan_id,
            "template_id": result.get("template_id"),
            "file_id": result.get("file_id"),
            "kind": kind,
            "name": name,
            "type": item.get("type", "bubbles") if kind == "zone" else "bubbles",
            "predicted": predicted,
            "label": label,
            "action": action,
            "confidence": item.get("confidence"),
            "flags": item.get("flags", []),
            "created_at": time.time(),
        }
        if aligned is not None:
            _, box = item_box(result, name)
            crop = crop_box(aligned, box, pad=4) if box else None
            if crop is not None:
                crop_path = (
                    training_root / "crops" / template_id / f"{scan_id}_{safe_name}.png"
                )
                crop_path.parent.mkdir(parents=True, exist_ok=True)
                cv2.imwrite(str(crop_path), crop)
                record["crop"] = str(crop_path.relative_to(training_root))
                record["box"] = box
            if kind == "field":
                bubbles = item.get("bubbles") or []
                marks = set(split_value(label, [b["value"] for b in bubbles]) or [])
                bubble_records = []
                for index, bubble in enumerate(bubbles):
                    bubble_crop = crop_box(
                        aligned,
                        [bubble["x"], bubble["y"], bubble["w"], bubble["h"]],
                        pad=2,
                    )
                    if bubble_crop is None:
                        continue
                    path = (
                        training_root
                        / "bubbles"
                        / template_id
                        / f"{scan_id}_{safe_name}_{index}.png"
                    )
                    path.parent.mkdir(parents=True, exist_ok=True)
                    cv2.imwrite(str(path), bubble_crop)
                    bubble_records.append(
                        {
                            "crop": str(path.relative_to(training_root)),
                            "value": bubble["value"],
                            "label": "marked" if bubble["value"] in marks else "empty",
                            "predicted": "marked" if bubble.get("marked") else "empty",
                            "fill_ratio": bubble.get("fill_ratio"),
                        }
                    )
                record["bubbles"] = bubble_records
        records.append(record)
    with _TRAINING_LOCK:
        with open(training_root / "labels.jsonl", "a") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")
    return records
