"""
Exact matching of bubble grids to label columns, sheet by sheet.

Correlation (labels.py) needs a column that varies between sheets; numbers
such as an exam code are often the same on every sample sheet. Here each
grid (or a run of side-by-side grids, e.g. the 0/1 "hundreds" column next to
a tens/units grid) is decoded on every sheet under the usual value orders
(0..9, 1..9,0, A..) and the decoded string is compared with every label
column. A column matches a grid when the strings are equal on (nearly) every
labelled sheet, which works for constant columns too.

The result names the fields after the column (pcode -> pcode1..5), joins them
with a customLabel named after the column, records the value order chosen and
gives the length to validate.
"""

import re

import numpy as np

DIGIT_ORDERS = {"0..9": list("0123456789"), "1..9,0": list("1234567890")}
LETTERS = [chr(c) for c in range(ord("A"), ord("Z") + 1)]
MIN_SCORE = 0.8
BLANK, MULTI = " ", "*"


def _adjacent(a, b):
    """b continues a to the right on the same rows (same row pitch, one column on)."""
    h = a.bubble[1]
    if abs(a.y0 - b.y0) > 0.3 * h:
        return False
    pitch_a = a.dx if a.cols > 1 and a.dx > 0 else None
    pitch_b = b.dx if b.cols > 1 and b.dx > 0 else None
    pitch = pitch_a or pitch_b or a.dy
    if a.dy > 0 and b.dy > 0 and abs(a.dy - b.dy) > 0.08 * max(a.dy, b.dy):
        return False
    gap = b.x0 - (a.x0 + (a.cols - 1) * (a.dx if a.cols > 1 else 0))
    return 0.7 * pitch <= gap <= 1.3 * pitch


def candidate_units(grids, max_chain=3):
    """Single grids plus left-to-right chains of adjacent vertical grids."""
    units = [[k] for k in range(len(grids))]
    order = sorted(range(len(grids)), key=lambda k: grids[k].x0)
    for start in order:
        chain = [start]
        while len(chain) < max_chain:
            nxt = [
                k
                for k in order
                if k not in chain and _adjacent(grids[chain[-1]], grids[k])
            ]
            if not nxt:
                break
            chain = chain + [nxt[0]]
            units.append(list(chain))
    return units


def _value_orders(unit, grids):
    """[(order name, [values per grid])] worth trying for a vertical unit."""
    rows = [grids[k].rows for k in unit]
    orders = []
    if max(rows) <= 10:
        for name, digits in DIGIT_ORDERS.items():
            orders.append((name, [digits[:r] for r in rows]))
    if len(unit) == 1 and rows[0] <= len(LETTERS):
        orders.append(("A..", [LETTERS[: rows[0]]]))
        if rows[0] < 10:
            orders.append(("1..", [[str(i) for i in range(1, rows[0] + 1)]]))
    return orders


def decode(fill_sheet, threshold, values, direction):
    """One sheet's fills (rows, cols) -> one character per field."""
    marked = fill_sheet > threshold
    if direction == "horizontal":
        lines = marked
    else:
        lines = marked.T
    out = []
    for line in lines:
        hits = [values[j] for j in np.nonzero(line)[0] if j < len(values)]
        out.append(hits[0] if len(hits) == 1 else (BLANK if not hits else MULTI))
    return "".join(out)


def same_value(decoded, label):
    """Decoded field string vs a label value (leading blanks/zeros tolerated)."""
    if decoded == label:
        return True
    d, l = decoded.strip(), label.strip()
    if d == l:
        return True
    if MULTI in d:
        return False
    d_compact = d.replace(BLANK, "")
    if d_compact == l:
        return True
    if d_compact.isdigit() and l.isdigit():
        return d_compact.lstrip("0") == l.lstrip("0") and len(d_compact) <= len(l) + 1
    return False


def _column_values(labels, sheets):
    columns = {}
    for s in sheets:
        for key, value in (labels[s] or {}).items():
            if value is None:
                continue
            text = str(value)
            if text.strip() == MULTI or MULTI in text:
                continue  # multi-marked truth: tells nothing about the order
            columns.setdefault(key, {})[s] = text.strip()
    return columns


def match_grids(grids, fills, threshold, labels, sheets=None, skip_columns=()):
    """
    grids: list of Grid; fills: list of (S, rows, cols); labels: list (S) of
    {column: value} or None. Returns a list of matches, best first:
    {column, grids, direction, values (per grid), order, score, checked,
     decoded (per sheet), lengths [min, max], digits (bool)}.
    """
    if not grids or not labels:
        return []
    if sheets is None:
        sheets = [s for s, lab in enumerate(labels) if lab]
    sheets = [s for s in sheets if labels[s]]
    columns = _column_values(labels, sheets)
    for key in list(columns):
        values = list(columns[key].values())
        filled = [v for v in values if v]
        if key in skip_columns or len(filled) < 2:
            columns.pop(key)
    if not columns:
        return []
    found = []
    for unit in candidate_units(grids):
        directions = ["vertical"]
        if len(unit) == 1:
            grid = grids[unit[0]]
            directions = []
            if grid.rows > 1:
                directions.append("vertical")
            if grid.cols > 1 or grid.rows == 1:
                directions.append("horizontal")
        elif any(grids[k].rows < 2 for k in unit):
            continue
        for direction in directions:
            if direction == "horizontal":
                grid = grids[unit[0]]
                n_fields, n_values = grid.rows, grid.cols
                orders = [
                    (name, [values[:n_values]])
                    for name, values in (
                        [(n, d) for n, d in DIGIT_ORDERS.items() if n_values <= 10]
                        + [("A..", LETTERS)]
                    )
                ]
            else:
                n_fields = sum(grids[k].cols for k in unit)
                orders = _value_orders(unit, grids)
            for order_name, per_grid in orders:
                decoded = {}
                for s in sheets:
                    parts = [
                        decode(fills[k][s], threshold, values, direction)
                        for k, values in zip(unit, per_grid)
                    ]
                    decoded[s] = "".join(parts)
                for column, truth in columns.items():
                    filled = [v for v in truth.values() if v]
                    typical = int(np.median([len(v) for v in filled]))
                    if n_fields == 1 and typical != 1:
                        continue
                    if n_fields > 1 and (typical < 2 or typical > n_fields):
                        continue
                    known = [s for s in truth if s in decoded]
                    hits = [s for s in known if same_value(decoded[s], truth[s])]
                    evidence = [s for s in hits if truth[s]]
                    if len(known) < 2 or len(evidence) < 2:
                        continue
                    score = len(hits) / float(len(known))
                    if score < MIN_SCORE:
                        continue
                    lengths = [
                        len(decoded[s].replace(BLANK, "")) for s in hits if truth[s]
                    ]
                    found.append(
                        {
                            "column": column,
                            "grids": list(unit),
                            "direction": direction,
                            "values": [list(v) for v in per_grid],
                            "order": order_name,
                            "score": round(score, 4),
                            "checked": len(known),
                            "fields": n_fields,
                            "decoded": {int(s): decoded[s] for s in known},
                            "lengths": [min(lengths), max(lengths)],
                            "digits": all(v.isdigit() for v in filled),
                            "_rank": (
                                score,
                                len(evidence),
                                -abs(n_fields - typical),
                                len(unit) == 1,
                                order_name == "0..9",
                            ),
                        }
                    )
    found.sort(key=lambda m: m["_rank"], reverse=True)
    chosen, used_grids, used_columns = [], set(), set()
    for match in found:
        if match["column"] in used_columns or used_grids & set(match["grids"]):
            continue
        chosen.append(match)
        used_grids.update(match["grids"])
        used_columns.add(match["column"])
    for match in chosen:
        match.pop("_rank", None)
    return chosen


def field_prefix(column, taken):
    """Label column -> field-name stem ("Paper Code 1" -> "papercode")."""
    base = re.sub(r"[^A-Za-z_]", "", str(column)).lower().strip("_") or "f"
    candidate, k = base, 0
    while candidate in taken:
        k += 1
        candidate = f"{base}_{chr(ord('a') + k)}" if k < 26 else f"{base}_{k}x"
    taken.add(candidate)
    return candidate
