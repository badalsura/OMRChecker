"""
Field label and value assignment from labelled sheets.

For every block and both reading directions, each bubble line (a field slot)
is compared with each labelled field: per value position the Pearson (phi)
correlation between "bubble marked" and "label contains token" across the
labelled sheets is computed, and the slot/label score is the mean over value
positions of the best-correlated token. Directions are chosen per block by
the optimal (Hungarian) slot-to-label matching score; the final matching is
then solved jointly over all blocks so that a label is used once. The block's
bubble values come from a second Hungarian matching of value positions to
tokens, completed with a standard sequence (A-Z, 0-9, 1-9...) when that
sequence agrees with every matched position.

Without labels, fields are numbered q1..qN in reading order, values are A..
by count (0-9 for ten-value blocks), and ten-value blocks become roll digits.
"""

import re
import string
from collections import OrderedDict

import numpy as np

from src.template_gen.assignment import linear_sum_assignment

LABEL_NUMBER = re.compile(r"^([^\d\.]+)(\d+)$")
STANDARD_SEQUENCES = [
    list(string.ascii_uppercase),
    list(string.ascii_lowercase),
    list("0123456789"),
    list("1234567890"),
    list("123456789"),
]


def tokens(value):
    """Marked values of a label: multi-marks are concatenated single characters."""
    if value is None:
        return None
    value = str(value).strip()
    return set(value) if value else set()


def prepare_labels(labels, n_sheets):
    """
    Normalise per-sheet label dicts. Composite columns (e.g. "Roll": "217232",
    every value the same length) are split into per-character sub-labels
    roll1..roll6. Returns (names, table, composites) where table[s][name] is a
    token set or None (unknown) and composites maps the original name to its
    ordered sub-labels.
    """
    labels = list(labels or [None] * n_sheets)
    if len(labels) != n_sheets:
        raise ValueError("labels must have one entry per image")
    names = []
    for entry in labels:
        for key in entry or {}:
            if key not in names:
                names.append(key)
    composites = OrderedDict()
    for name in names:
        values = [
            str(e[name]).strip("\r\n")
            for e in labels
            if e and e.get(name) is not None and str(e[name]).strip()
        ]
        if len(values) < 2:
            continue
        lengths = [len(v) for v in values]
        common = max(set(lengths), key=lengths.count)
        # Mostly multi-character values of one length: a concatenated column
        # (blank digits may shorten some values; those become "unknown")
        long_values = sum(n >= 2 for n in lengths)
        if (
            common >= 2
            and lengths.count(common) >= 0.5 * len(values)
            and long_values >= 0.8 * len(values)
        ):
            stem = name.lower()
            if re.search(r"[\d\.]", stem) or not stem:
                stem = "f"
            composites[name] = [f"{stem}{i}" for i in range(1, common + 1)]
    table = []
    for entry in labels:
        row = {}
        for name in names:
            raw = None if not entry else entry.get(name)
            if name in composites:
                subs = composites[name]
                text = None if raw is None else str(raw).strip("\r\n")
                if text is not None and not text.strip():
                    text = ""
                for k, sub in enumerate(subs):
                    if text is None or (text and len(text) != len(subs)):
                        row[sub] = None
                    elif text == "":
                        row[sub] = set()
                    else:
                        row[sub] = set() if text[k] in " _-" else {text[k]}
            else:
                row[name] = tokens(raw)
        table.append(row)
    flat = []
    for name in names:
        flat.extend(composites.get(name, [name]))
    return flat, table, composites


def _label_tensors(flat, table, vocab):
    sheets = len(table)
    lb = np.zeros((sheets, len(flat), len(vocab)), np.float64)
    known = np.zeros((sheets, len(flat)), np.float64)
    index = {t: k for k, t in enumerate(vocab)}
    for s, row in enumerate(table):
        for l, name in enumerate(flat):
            value = row.get(name)
            if value is None:
                continue
            known[s, l] = 1
            for token in value:
                if token in index:
                    lb[s, l, index[token]] = 1
    return lb, known


def correlation(marks, lb, known):
    """
    marks (S, I, J), lb (S, L, T), known (S, L) ->
    corr (I, L, J, T) and informative (I, L, J): the bubble's mark varies.
    """
    n = np.maximum(known.sum(axis=0), 1)  # (L,)
    weighted = known[:, :, None] * lb
    mean_m = np.einsum("sl,sij->lij", known, marks) / n[:, None, None]
    mean_l = weighted.sum(axis=0) / n[:, None]
    joint = np.einsum("sij,slt->iljt", marks, weighted) / n[None, :, None, None]
    cov = joint - mean_m.transpose(1, 0, 2)[:, :, :, None] * mean_l[None, :, None, :]
    var_m = (mean_m - mean_m**2).transpose(1, 0, 2)[:, :, :, None]
    var_l = (mean_l - mean_l**2)[None, :, None, :]
    denom = np.sqrt(var_m * var_l)
    with np.errstate(divide="ignore", invalid="ignore"):
        corr = np.where(denom > 1e-9, cov / denom, 0.0)
    return corr, var_m[..., 0] > 1e-9


def pair_scores(corr, informative):
    """
    Slot/label score: mean over value positions of the best token correlation,
    counting only positions whose bubble was marked on some sheets but not all
    (others carry no evidence). Slight shrinkage favours more evidence.
    """
    if corr.size == 0:
        return np.zeros(corr.shape[:2])
    best = np.where(informative, corr.max(axis=3), 0.0).sum(axis=2)
    return best / (informative.sum(axis=2) + 0.25)


def block_marks(fills, threshold, direction):
    """fills (S, rows, cols) -> binary marks (S, slots, values) for a direction."""
    marked = (fills > threshold).astype(np.float64)
    return marked if direction == "horizontal" else marked.transpose(0, 2, 1)


def infer_values(mapping, n_values):
    """mapping {position: token}. Returns (values, completed_from_standard)."""
    for sequence in STANDARD_SEQUENCES:
        for offset in range(0, max(1, len(sequence) - n_values + 1)):
            candidate = sequence[offset : offset + n_values]
            if len(candidate) < n_values:
                continue
            if mapping and all(candidate[j] == t for j, t in mapping.items()):
                if offset == 0 or all(j in mapping for j in range(n_values)):
                    return candidate, len(mapping) < n_values
    values = []
    used = set(mapping.values())
    for j in range(n_values):
        if j in mapping:
            values.append(mapping[j])
        else:
            filler = f"v{j + 1}"
            while filler in used:
                filler += "_"
            values.append(filler)
    return values, len(mapping) < n_values


def default_values(n_values):
    if n_values == 10:
        return list("0123456789")
    if n_values <= 26:
        return list(string.ascii_uppercase[:n_values])
    return [str(k) for k in range(1, n_values + 1)]


def default_direction(grid):
    """Values run along the shorter side; ten-bubble lines are digit columns."""
    if grid.rows == 1:
        return "horizontal"
    if grid.cols == 1:
        return "vertical"
    if grid.rows == 10 and grid.cols != 10:
        return "vertical"
    if grid.cols == 10 and grid.rows != 10:
        return "horizontal"
    if grid.rows == 10 and grid.cols == 10:
        return "vertical"
    return "horizontal" if grid.cols <= grid.rows else "vertical"


def assign_with_labels(grids, fills, threshold, labels, min_pair_score=0.35):
    """
    grids: list of Grid; fills: list of (S, rows, cols) arrays (all sheets);
    labels: list (len S) of dict or None.
    Returns per-block dicts: direction, field_labels (None for unmatched
    slots), values, value_completed, agreement, slot_scores; plus extras.
    """
    n_sheets = fills[0].shape[0] if fills else 0
    flat, table, composites = prepare_labels(labels, n_sheets)
    labelled = [s for s, e in enumerate(labels or []) if e]
    if not labelled or not flat:
        return None
    table_l = [table[s] for s in labelled]
    vocab = sorted({t for row in table_l for v in row.values() if v for t in v})
    lb, known = _label_tensors(flat, table_l, vocab)

    per_block = []
    for grid, fill in zip(grids, fills):
        options = {}
        for direction in ("horizontal", "vertical"):
            if direction == "vertical" and grid.rows == 1:
                continue
            if direction == "horizontal" and grid.cols == 1 and grid.rows > 1:
                continue
            marks = block_marks(fill[labelled], threshold, direction)
            corr, informative = correlation(marks, lb, known)  # (I, L, J, T)
            pair = pair_scores(corr, informative)
            rows, cols = linear_sum_assignment(pair, maximize=True)
            score = pair[rows, cols].sum() / max(pair.shape[0], 1)
            options[direction] = (score, marks, corr, pair)
        direction = max(options, key=lambda d: options[d][0])
        per_block.append((direction,) + options[direction][1:])

    # Joint assignment over every slot of every block
    stacked = np.concatenate([b[3] for b in per_block], axis=0)
    owners = [
        (b, i) for b, block in enumerate(per_block) for i in range(block[3].shape[0])
    ]
    rows, cols = linear_sum_assignment(stacked, maximize=True)
    slot_label = {}
    for r, c in zip(rows, cols):
        if stacked[r, c] >= min_pair_score:
            slot_label[owners[r]] = c

    results = []
    for b, (direction, marks, corr, pair) in enumerate(per_block):
        n_slots, n_values = marks.shape[1], marks.shape[2]
        assigned = [
            (i, slot_label[(b, i)]) for i in range(n_slots) if (b, i) in slot_label
        ]
        mapping = {}
        if assigned:
            value_corr = sum(corr[i, l] for i, l in assigned) / len(assigned)  # (J, T)
            vr, vc = linear_sum_assignment(value_corr, maximize=True)
            mapping = {
                int(j): vocab[t] for j, t in zip(vr, vc) if value_corr[j, t] > 0.2
            }
        values, completed = (
            infer_values(mapping, n_values)
            if mapping
            else (
                default_values(n_values),
                True,
            )
        )
        field_labels = [None] * n_slots
        for i, l in assigned:
            field_labels[i] = flat[l]
        # Agreement of the thresholded reads with the labels
        agree, total = 0, 0
        for k, s in enumerate(labelled):
            for i, l in assigned:
                truth = table_l[k].get(flat[l])
                if truth is None:
                    continue
                read = {values[j] for j in range(n_values) if marks[k, i, j] > 0}
                agree += int(read == truth)
                total += 1
        results.append(
            {
                "direction": direction,
                "field_labels": field_labels,
                "values": values,
                "value_completed": completed,
                "mapped_positions": len(mapping),
                "slot_scores": [
                    (
                        round(float(pair[i, slot_label[(b, i)]]), 3)
                        if (b, i) in slot_label
                        else 0.0
                    )
                    for i in range(n_slots)
                ],
                "agreement": round(agree / total, 4) if total else None,
                "checked": total,
            }
        )
    used = {flat[c] for c in slot_label.values()}
    unmatched = [name for name in flat if name not in used]
    return {
        "blocks": results,
        "composites": composites,
        "unmatched_labels": unmatched,
        "all_labels": flat,
        "table": table,
    }


def compress_labels(field_labels):
    """['q1', 'q2', 'q3', 'x'] -> ['q1..3', 'x'] (template range syntax)."""
    out, k = [], 0
    while k < len(field_labels):
        match = LABEL_NUMBER.match(field_labels[k])
        end = k
        if match:
            prefix, number = match.group(1), int(match.group(2))
            while end + 1 < len(field_labels):
                nxt = LABEL_NUMBER.match(field_labels[end + 1])
                if not nxt or nxt.group(1) != prefix:
                    break
                if int(nxt.group(2)) != number + (end + 1 - k):
                    break
                if nxt.group(2) != str(int(nxt.group(2))):
                    break
                end += 1
            if end > k and match.group(2) == str(number):
                out.append(f"{prefix}{number}..{number + end - k}")
                k = end + 1
                continue
        out.append(field_labels[k])
        k += 1
    return out
