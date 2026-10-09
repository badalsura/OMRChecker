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


def _field_box(field):
    bubbles = field.get("bubbles") or []
    if not bubbles:
        return None
    x0 = min(b["x"] for b in bubbles)
    y0 = min(b["y"] for b in bubbles)
    x1 = max(b["x"] + b["w"] for b in bubbles)
    y1 = max(b["y"] + b["h"] for b in bubbles)
    return [x0, y0, x1 - x0, y1 - y0]


def related_names(result, name, custom_labels=None):
    """Fields/zones behind a check or custom label (for crops and the overlay)."""
    checks = result.get("checks") or {}
    check = checks.get(name)
    if not isinstance(check, dict):
        check = next(
            (
                c
                for c in checks.values()
                if isinstance(c, dict) and c.get("output") == name
            ),
            None,
        )
    if isinstance(check, dict):
        return list((check.get("sources") or {}).keys())
    if custom_labels and name in custom_labels:
        return list(custom_labels[name])
    for item in (result.get("review") or []) + (result.get("read_review") or []):
        if item.get("name") == name and item.get("fields"):
            return list(item["fields"])
    return []


def item_box(result, name, custom_labels=None, _depth=0):
    """(kind, [x, y, w, h]) of a field, zone, check or custom label in
    aligned-image coordinates (checks and custom labels: the union of their
    sources)."""
    field = (result.get("fields") or {}).get(name)
    if field is not None:
        return "field", _field_box(field)
    zone = (result.get("zones") or {}).get(name)
    if zone is not None:
        return "zone", zone.get("box")
    related = related_names(result, name, custom_labels) if _depth < 3 else []
    if not related:
        return None, None
    kind = "check" if name in (result.get("checks") or {}) else "custom_label"
    boxes = []
    for source in related:
        _, box = item_box(result, source, custom_labels, _depth + 1)
        if box:
            boxes.append(box)
    if not boxes:
        return kind, None
    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[0] + b[2] for b in boxes)
    y1 = max(b[1] + b[3] for b in boxes)
    return kind, [x0, y0, x1 - x0, y1 - y0]


def sheet_entry(result, name):
    """The whole-sheet review entry called name (index points, blank sheet...)."""
    for entry in result.get("review") or []:
        if entry.get("name") == name and entry.get("kind") == "sheet":
            return entry
    return None


def template_index_points(template_json):
    """Index points of a template's TimingMarkAlignment, in template px."""
    for processor in template_json.get("preProcessors") or []:
        points = (processor.get("options") or {}).get("indexPoints")
        if points:
            return [
                {
                    "name": point.get("name") or "point%d" % (i + 1),
                    "center": point["center"],
                    "size": point.get("size") or [20, 20],
                }
                for i, point in enumerate(points)
            ]
    return []


def sheet_overview(image, result, entry, index_points=(), max_width=1000):
    """The whole aligned sheet with every field drawn (marked bubbles filled,
    flagged fields boxed) and the index points: found in green, missing in red."""
    canvas = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR) if image.ndim == 2 else image.copy()
    fill = canvas.copy()
    flagged = {item.get("name") for item in result.get("review") or []}
    for name, field in (result.get("fields") or {}).items():
        bubbles = field.get("bubbles") or []
        for b in bubbles:
            corner = (int(b["x"]), int(b["y"]))
            far = (int(b["x"] + b["w"]), int(b["y"] + b["h"]))
            if b.get("marked"):
                cv2.rectangle(fill, corner, far, (223, 111, 47), -1)
            else:
                cv2.rectangle(canvas, corner, far, (150, 150, 150), 1)
        box = _field_box(field)
        if box and name in flagged:
            x, y, w, h = [int(v) for v in box]
            cv2.rectangle(canvas, (x - 4, y - 4), (x + w + 4, y + h + 4), (0, 140, 255), 2)
    cv2.addWeighted(fill, 0.45, canvas, 0.55, 0, canvas)
    missing = set((entry or {}).get("missing") or [])
    for point in index_points:
        cx, cy = int(point["center"][0]), int(point["center"][1])
        radius = int(max(point["size"]) * 0.5 + 12)
        colour = (40, 40, 220) if point["name"] in missing else (40, 160, 40)
        cv2.circle(canvas, (cx, cy), radius, colour, 4 if point["name"] in missing else 2)
        text = point["name"] + (" missing" if point["name"] in missing else "")
        width = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.9, 2)[0][0]
        # Labels go towards the middle of the page so they stay on it
        x = cx + radius + 4 if cx < canvas.shape[1] / 2 else cx - radius - 4 - width
        cv2.putText(
            canvas,
            text,
            (x, cy + 8),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.9,
            colour,
            2,
        )
    if canvas.shape[1] > max_width:
        scale = max_width / canvas.shape[1]
        canvas = cv2.resize(canvas, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    return canvas


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
    """
    Recompute concatenated responses, rules (template "validate"/"checks"),
    score and status after edits. Values a person set or accepted stay settled:
    a rule can't flag them again while they keep that value.
    """
    from src.evaluation import (
        evaluate_concatenated_response_detailed,
        scoring_summary,
    )
    from src.utils.parsing import get_concatenated_response

    template = engine.template
    omr_response = {
        name: field.get("value", "")
        for name, field in (result.get("fields") or {}).items()
    }
    for name, zone in (result.get("zones") or {}).items():
        omr_response[name] = zone.get("value", "")
    # Sheet-level items (e.g. too_few_marks) are not rule items; keep them
    sheet_items = [
        item for item in result.get("review") or [] if item.get("kind") == "sheet"
    ]
    try:
        if getattr(template, "rules", None) is not None:
            from src.rules import reapply_rules

            reapply_rules(result, template, dict(omr_response))
            listed = {item["name"] for item in result.get("review") or []}
            result["review"] = list(result.get("review") or []) + [
                item for item in sheet_items if item["name"] not in listed
            ]
        else:
            result["responses"] = get_concatenated_response(
                omr_response, template, result.get("fields") or {}
            )
    except KeyError:
        # Template changed since the scan; keep a flat response
        result["responses"] = {**(result.get("responses") or {}), **omr_response}
    apply_manual_values(result)
    honour_decisions(result)
    if engine.evaluation_config is not None:
        try:
            detailed = evaluate_concatenated_response_detailed(
                result["responses"],
                engine.evaluation_config,
                Path(result.get("file_id", "scan")),
                None,
            )
            result["score"] = detailed["score"]
            result["scoring"] = scoring_summary(detailed)
        except Exception as error:
            result["score_error"] = str(error)
    result["status"] = "needs_review" if result.get("review") else "ok"
    if result.get("error") and not result.get("fields"):
        result["status"] = "error"


def apply_manual_values(result):
    """Re-impose values a person typed for check outputs (rules re-run would
    otherwise recompute them)."""
    responses = result.setdefault("responses", {})
    checks = result.get("checks") or {}
    for name, value in (result.get("manual_values") or {}).items():
        check = checks.get(name)
        if isinstance(check, dict):
            if not check.get("manual"):
                check["original_value"] = check.get("value", "")
            check["value"] = value
            check["manual"] = True
            check["needs_review"] = False
            responses[check.get("output") or name] = value
        else:
            responses[name] = value


def current_value(result, name):
    """The value a field, zone, check or custom label has now."""
    for group in ("fields", "zones"):
        item = (result.get(group) or {}).get(name)
        if item is not None:
            return item.get("value", "")
    check = (result.get("checks") or {}).get(name)
    if isinstance(check, dict):
        return check.get("value", "")
    return (result.get("responses") or {}).get(name, "")


def honour_decisions(result):
    """Clear review flags of everything a person decided, while it keeps the
    value they decided on."""
    settled = set()
    for name, log in (result.get("review_log") or {}).items():
        if current_value(result, name) != log.get("label"):
            continue
        settled.add(name)
        for group in ("fields", "zones", "checks"):
            item = (result.get(group) or {}).get(name)
            if isinstance(item, dict):
                item["needs_review"] = False
    if settled:
        result["review"] = [
            item for item in result.get("review") or [] if item["name"] not in settled
        ]


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


def _distribute(label_name, value, columns, fields, empty_values):
    """Split a custom label's corrected value over its columns (one bubble
    value per column; a space or the column's empty value = blank)."""
    pieces, rest = [], value
    for column in columns:
        field = fields.get(column)
        if field is None:
            raise ReviewError(
                f"'{label_name}' can't be edited: column '{column}' is not a bubble field"
            )
        empty = empty_values.get(column, "")
        options = sorted(
            {b["value"] for b in field.get("bubbles") or []}, key=len, reverse=True
        )
        for option in options:
            if option and rest.startswith(option):
                pieces.append(option)
                rest = rest[len(option) :]
                break
        else:
            if rest[:1] == " ":
                pieces.append(empty)
                rest = rest[1:]
            elif empty and rest.startswith(empty):
                pieces.append(empty)
                rest = rest[len(empty) :]
            elif not rest:
                pieces.append(empty)
            else:
                raise ReviewError(
                    f"'{value}' does not fit the columns {columns} of '{label_name}'"
                )
    if rest:
        raise ReviewError(
            f"'{value}' is longer than the {len(columns)} columns of '{label_name}'"
        )
    return dict(zip(columns, pieces))


def apply_review(
    result,
    corrections,
    accept,
    reviewer=None,
    empty_values=None,
    custom_labels=None,
):
    """
    Update the result in place. Returns the list of resolved item names and a
    list of training events: (kind, name, predicted, label, action, item).

    Names may be fields, zones, cross-field checks (by check name or output
    column), custom labels and sheet-level items (kind "sheet", e.g.
    too_few_marks; these can only be accepted, which dismisses them). A corrected custom label is split over its
    bubble columns; a corrected check keeps the typed value ("manual_values")
    when rules re-run. Call recompute() afterwards to refresh responses, rule
    outputs and status.

    The first machine read of every item survives as "original_value" and the
    items flagged when the sheet was read as "read_review", so later accuracy
    statistics compare against what the engine actually produced.
    empty_values: {field: value written for an unmarked field} (template emptyValue).
    custom_labels: {label: [columns]} of the template.
    """
    corrections = {k: normalize_label(v) for k, v in dict(corrections or {}).items()}
    accept = list(accept or [])
    empty_values = empty_values or {}
    fields = result.get("fields") or {}
    zones = result.get("zones") or {}
    checks = {
        name: check
        for name, check in (result.get("checks") or {}).items()
        if isinstance(check, dict)
    }
    by_output = {check.get("output"): name for name, check in checks.items()}
    custom_labels = dict(custom_labels or {})
    sheet_items = {}
    for item in (result.get("review") or []) + (result.get("read_review") or []):
        if item.get("kind") == "custom_label" and item.get("fields"):
            custom_labels.setdefault(item["name"], list(item["fields"]))
        elif item.get("kind") == "sheet":
            sheet_items.setdefault(item["name"], item)

    def canonical(name):
        if name in fields or name in zones or name in checks:
            return name
        return by_output.get(name, name)

    corrections = {canonical(k): v for k, v in corrections.items()}
    accept = [canonical(a) for a in accept]
    known = set(fields) | set(zones) | set(checks) | set(custom_labels)
    known |= set(sheet_items)
    unknown = [n for n in [*corrections, *accept] if n not in known]
    if unknown:
        raise ReviewError(
            f"Unknown field, zone, check or custom label: {', '.join(sorted(unknown))}"
        )
    sheet_only = [n for n in corrections if n in sheet_items and n not in fields]
    if sheet_only:
        raise ReviewError(
            f"{', '.join(sheet_only)}: a sheet-level item has no value; accept it to dismiss"
        )

    # Validate everything before changing anything; a custom label becomes
    # corrections of its columns
    column_changes = {}
    for name, label in corrections.items():
        if name in fields:
            validate_field_value(name, fields[name], label, empty_values.get(name))
        elif name not in zones and name not in checks and name in custom_labels:
            column_changes[name] = _distribute(
                name, label, custom_labels[name], fields, empty_values
            )

    if "read_review" not in result:
        result["read_review"] = [dict(item) for item in result.get("review") or []]
    events, resolved = [], []
    now = time.time()
    responses = result.setdefault("responses", {})

    def settle(kind, name, target, predicted, label, corrected):
        action = "corrected" if corrected and label != predicted else "accepted"
        events.append((kind, name, predicted, label, action, dict(target)))
        result.setdefault("review_log", {})[name] = {
            "predicted": predicted,
            "label": label,
            "action": action,
            "at": now,
            "reviewer": reviewer,
        }
        resolved.append(name)

    def set_entity(target, label, empty=None):
        predicted = target.get("value", "")
        if empty is not None and label != predicted:
            # Rules read the "empty" flag of bubble columns; keep it truthful
            blank = label in ("", empty)
            for holder in (target, target.get("pre_rules")):
                if isinstance(holder, dict):
                    flags = set(holder.get("flags") or [])
                    if blank:
                        flags.add("empty")
                    else:
                        flags.discard("empty")
                    holder["flags"] = sorted(flags)
        target["value"] = label
        target["needs_review"] = False
        target["reviewed"] = True
        if isinstance(target.get("pre_rules"), dict):
            # rules re-run restore this state; the person's decision stands
            target["pre_rules"]["needs_review"] = False
        if label != predicted and "original_value" not in target:
            target["original_value"] = predicted

    for name in [*corrections.keys(), *[a for a in accept if a not in corrections]]:
        corrected = name in corrections
        if name in fields or name in zones:
            target = fields.get(name) if name in fields else zones.get(name)
            kind = "field" if name in fields else "zone"
            predicted = target.get("value", "")
            label = corrections[name] if corrected else predicted
            before = dict(target)
            set_entity(
                target, label, empty_values.get(name, "") if kind == "field" else None
            )
            settle(kind, name, before, predicted, label, corrected)
        elif name in checks:
            check = checks[name]
            predicted = check.get("value", "")
            label = corrections[name] if corrected else predicted
            before = dict(check)
            if label != predicted:
                result.setdefault("manual_values", {})[name] = label
                responses[check.get("output") or name] = label
            set_entity(check, label)
            settle("check", name, before, predicted, label, corrected)
        elif name in sheet_items and name not in custom_labels:
            # Dismissed; current_value() of a sheet item is "" so it stays settled
            settle("sheet", name, dict(sheet_items[name]), "", "", False)
        else:
            predicted = responses.get(name, "")
            label = corrections[name] if corrected else predicted
            for column, value in column_changes.get(name, {}).items():
                field = fields[column]
                before = dict(field)
                if value != field.get("value", ""):
                    set_entity(field, value, empty_values.get(column, ""))
                    settle(
                        "field", column, before, before.get("value", ""), value, True
                    )
            if corrected:
                responses[name] = label
            settle(
                "custom_label",
                name,
                {"fields": custom_labels[name]},
                predicted,
                label,
                corrected,
            )
    result["review"] = [
        item for item in (result.get("review") or []) if item["name"] not in resolved
    ]
    result["reviewed"] = True
    return resolved, events


def write_training_records(training_root, result, events, aligned):
    """Append labelled crops for every reviewed item (needs the aligned image)."""
    # Checks and custom labels are derived values; their columns train instead
    events = [e for e in events or [] if e[0] in ("field", "zone")]
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
