"""
Helpers for a verification GUI: apply a person's corrections to a generated
template and draw the template over the reference page.

Corrections are a list of small operations, applied in order:
    {"op": "move_block", "name": "Block_q1", "dx": 3, "dy": -2}
    {"op": "update_block", "name": "Block_q1", "set": {"bubblesGap": 51}}
    {"op": "rename_block", "name": "Block_q1", "to": "MCQ_1"}
    {"op": "delete_block", "name": "Block_q1"}
    {"op": "add_block", "name": "New", "block": {...fieldBlock...}}
    {"op": "move_zone" | "update_zone" | "rename_zone" | "delete_zone" | "add_zone", ...}
    {"op": "resize_zone", "name": "icr", "dimensions": [w, h]}
    {"op": "rename_label", "from": "q1", "to": "Q1"}
    {"op": "set", "key": "bubbleDimensions", "value": [30, 30]}
"""

import copy

import cv2
import numpy as np

from src.utils.parsing import parse_fields

_SECTION = {"block": "fieldBlocks", "zone": "zones"}


def _rename_key(mapping, old, new):
    if new in mapping:
        raise ValueError(f"'{new}' already exists")
    return {(new if k == old else k): v for k, v in mapping.items()}


def apply_corrections(template, corrections, validate=True):
    """Return a corrected copy of template (raises ValueError on bad input)."""
    template = copy.deepcopy(template)
    for correction in corrections:
        op = correction["op"]
        if op == "set":
            template[correction["key"]] = correction["value"]
            continue
        if op == "rename_label":
            old, new = correction["from"], correction["to"]
            for block in template.get("fieldBlocks", {}).values():
                labels = parse_fields("fieldLabels", block["fieldLabels"])
                if old in labels:
                    block["fieldLabels"] = [new if x == old else x for x in labels]
            for key, labels in template.get("customLabels", {}).items():
                labels = parse_fields(key, labels)
                template["customLabels"][key] = [new if x == old else x for x in labels]
            continue
        action, _, kind = op.partition("_")
        if kind not in _SECTION:
            raise ValueError(f"Unknown correction op '{op}'")
        section = template.setdefault(_SECTION[kind], {})
        name = correction["name"]
        if action == "add":
            if name in section:
                raise ValueError(f"'{name}' already exists")
            section[name] = copy.deepcopy(correction[kind])
            continue
        if name not in section:
            raise ValueError(f"No {kind} named '{name}'")
        item = section[name]
        if action == "move":
            item["origin"] = [
                int(round(item["origin"][0] + correction.get("dx", 0))),
                int(round(item["origin"][1] + correction.get("dy", 0))),
            ]
        elif action == "resize":
            item["dimensions"] = [int(v) for v in correction["dimensions"]]
        elif action == "update":
            item.update(copy.deepcopy(correction["set"]))
            for key in correction.get("unset", []):
                item.pop(key, None)
        elif action == "rename":
            template[_SECTION[kind]] = _rename_key(section, name, correction["to"])
        elif action == "delete":
            del section[name]
        else:
            raise ValueError(f"Unknown correction op '{op}'")
    if validate:
        from src.template_gen.engine import validate_template

        errors = validate_template(template)
        if errors:
            raise ValueError(f"Corrected template is invalid: {errors}")
    return template


def bubble_boxes(template):
    """Yield (block_name, field_label, value, x, y, w, h) for every bubble."""
    default_dims = template["bubbleDimensions"]
    for name, block in template.get("fieldBlocks", {}).items():
        if "bubbleValues" not in block:
            continue  # fieldType blocks: expanded by the Template class
        w, h = block.get("bubbleDimensions", default_dims)
        labels = parse_fields(name, block["fieldLabels"])
        horizontal = block.get("direction", "vertical") == "horizontal"
        for f, label in enumerate(labels):
            for v, value in enumerate(block["bubbleValues"]):
                along, across = v * block["bubblesGap"], f * block["labelsGap"]
                dx, dy = (along, across) if horizontal else (across, along)
                yield (
                    name,
                    label,
                    value,
                    block["origin"][0] + dx,
                    block["origin"][1] + dy,
                    w,
                    h,
                )


def render_overlay(reference, template):
    """Colour image of the template drawn over the reference page."""
    page_w, page_h = template["pageDimensions"]
    base = reference
    if base.shape[1] != page_w or base.shape[0] != page_h:
        base = cv2.resize(base, (page_w, page_h))
    image = cv2.cvtColor(base, cv2.COLOR_GRAY2BGR) if base.ndim == 2 else base.copy()
    for _, _, value, x, y, w, h in bubble_boxes(template):
        p0 = (int(round(x)), int(round(y)))
        p1 = (int(round(x + w)), int(round(y + h)))
        cv2.rectangle(image, p0, p1, (0, 160, 0), 1)
    for name, block in template.get("fieldBlocks", {}).items():
        x, y = block["origin"]
        cv2.putText(
            image,
            name,
            (x, max(y - 6, 10)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.45,
            (0, 120, 255),
            1,
            cv2.LINE_AA,
        )
    colours = {"barcode": (255, 0, 0), "qrcode": (255, 0, 255), "icr": (0, 0, 255)}
    for name, zone in template.get("zones", {}).items():
        (x, y), (w, h) = zone["origin"], zone["dimensions"]
        colour = colours.get(zone["type"], (0, 140, 140))
        cv2.rectangle(image, (x, y), (x + w, y + h), colour, 2)
        cv2.putText(
            image,
            f"{zone['type']}:{name}",
            (x, max(y - 5, 10)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.45,
            colour,
            1,
            cv2.LINE_AA,
        )
    for processor in template.get("preProcessors", []):
        if processor["name"] != "TimingMarkAlignment":
            continue
        mw, mh = processor["options"]["markDimensions"]
        for track in processor["options"]["tracks"].values():
            for cx, cy in track["marks"]:
                cv2.circle(
                    image,
                    (int(cx), int(cy)),
                    int(max(mw, mh) / 2) + 3,
                    (0, 200, 255),
                    2,
                )
    return image


def overlay_bubble_centres(template):
    """(N, 2) array of bubble centres, handy for interactive hit-testing."""
    centres = [[x + w / 2, y + h / 2] for _, _, _, x, y, w, h in bubble_boxes(template)]
    return np.array(centres) if centres else np.zeros((0, 2))
