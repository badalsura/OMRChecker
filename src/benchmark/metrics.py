"""
Accuracy and throughput metrics for scanned sheets against ground truth.

Input: a list of records {"file_id", "result": ScanResult.to_dict(), "truth":
{column: value} or None}. Every truth column is compared with what the engine
produced for it:

* a bubble field label (result["fields"]): the field value, its needs_review
  flag and per-bubble marks (truth decomposed with split_field_value);
* a zone name (result["zones"]): the zone value and its needs_review flag;
* any other response column (e.g. a customLabel concatenating several fields):
  the response value, flagged when any of its fields is flagged.

The number that matters for unattended processing is the accuracy of
auto-accepted values (not flagged for review): every error there is a silent
error that reaches the results. A good configuration keeps it at ~100% while the
review rate stays low.

A truth value of "*" means multi-marked without saying which bubbles: it counts
as correct when the value is sent to review. "Flagged" covers field/zone
needs_review and anything else on the sheet's review list (checks, validation).

Sheets that fail before reading (registration errors) are counted in the sheet
metrics but excluded from field metrics, since all of their values go to manual
handling anyway.
"""

from typing import Dict, List, Optional

import numpy as np

from src.ml.dataset import MULTI_MARK, split_field_value

KIND_FIELD = "field"
KIND_CUSTOM = "custom"


def normalize(value):
    if value is None:
        return ""
    if isinstance(value, float) and np.isnan(value):
        return ""
    return str(value).strip()


class _Counter:
    """Correct / flagged bookkeeping for one group of values."""

    def __init__(self):
        self.total = self.correct = self.flagged = self.flagged_wrong = 0

    def add(self, correct, flagged):
        self.total += 1
        self.correct += int(correct)
        self.flagged += int(flagged)
        self.flagged_wrong += int(flagged and not correct)

    def to_dict(self):
        wrong = self.total - self.correct
        auto = self.total - self.flagged
        silent = wrong - self.flagged_wrong
        return {
            "total": self.total,
            "correct": self.correct,
            "accuracy": _ratio(self.correct, self.total),
            "flagged": self.flagged,
            "review_rate": _ratio(self.flagged, self.total),
            "auto_accepted": auto,
            "auto_accepted_correct": auto - silent,
            # Key number: errors that are NOT sent to review end up in the results
            "auto_accepted_accuracy": _ratio(auto - silent, auto),
            "silent_errors": silent,
            # Of the values sent to review, how many were actually wrong
            "flagged_error_rate": _ratio(self.flagged_wrong, self.flagged),
            # Of all wrong values, how many review caught
            "errors_caught_rate": _ratio(self.flagged_wrong, wrong),
        }


def _ratio(numerator, denominator):
    return round(numerator / denominator, 6) if denominator else None


def percentiles(values):
    if not values:
        return None
    array = np.asarray(values, dtype=float)
    return {
        "mean": round(float(array.mean()), 2),
        "p50": round(float(np.percentile(array, 50)), 2),
        "p95": round(float(np.percentile(array, 95)), 2),
        "max": round(float(array.max()), 2),
    }


def _bubble_counts(field, truth_value, counts):
    bubbles = field.get("bubbles") or []
    marked = split_field_value(truth_value, [b["value"] for b in bubbles])
    if marked is None:
        counts["undecomposable_fields"] += 1
        return
    for index, bubble in enumerate(bubbles):
        expected, got = index in marked, bool(bubble.get("marked"))
        key = ("tp" if got else "fn") if expected else ("fp" if got else "tn")
        counts[key] += 1


def _bubble_summary(counts):
    tp, fp, fn, tn = counts["tp"], counts["fp"], counts["fn"], counts["tn"]
    precision = _ratio(tp, tp + fp)
    recall = _ratio(tp, tp + fn)
    f1 = (
        round(2 * precision * recall / (precision + recall), 6)
        if precision and recall
        else None
    )
    return {
        "total": tp + fp + fn + tn,
        "true_positive": tp,
        "false_positive": fp,
        "false_negative": fn,
        "true_negative": tn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "accuracy": _ratio(tp + tn, tp + fp + fn + tn),
        "undecomposable_fields": counts["undecomposable_fields"],
    }


def evaluate_record(result, truth, column_fields=None):
    """Per-column comparisons for one scanned sheet: list of dicts."""
    column_fields = column_fields or {}
    fields = result.get("fields") or {}
    zones = result.get("zones") or {}
    responses = result.get("responses") or {}
    # Everything the sheet's review list sends to a person, including checks and
    # validation failures that don't set needs_review on a field or zone
    reviewed = set()
    for review_item in result.get("review") or []:
        reviewed.add(review_item.get("name"))
        reviewed.update(review_item.get("fields") or [])
    items = []
    for column, expected in truth.items():
        expected = normalize(expected)
        if column in fields:
            field = fields[column]
            got = normalize(responses.get(column, field.get("value")))
            item = {
                "kind": KIND_FIELD,
                "flagged": bool(field.get("needs_review")) or column in reviewed,
                "confidence": field.get("confidence"),
                "flags": list(field.get("flags") or []),
            }
        elif column in zones:
            zone = zones[column]
            got = normalize(zone.get("value"))
            item = {
                "kind": f"zone:{zone.get('type', 'unknown')}",
                "flagged": bool(zone.get("needs_review")) or column in reviewed,
                "confidence": zone.get("confidence"),
                "flags": list(zone.get("flags") or []),
            }
        elif column in responses:
            got = normalize(responses[column])
            parts = [fields[f] for f in column_fields.get(column, []) if f in fields]
            item = {
                "kind": KIND_CUSTOM,
                "flagged": any(p.get("needs_review") for p in parts)
                or column in reviewed
                or bool(reviewed & set(column_fields.get(column, []))),
                "confidence": min(
                    (
                        p.get("confidence")
                        for p in parts
                        if p.get("confidence") is not None
                    ),
                    default=None,
                ),
                "flags": sorted({flag for p in parts for flag in p.get("flags") or []}),
            }
        else:
            items.append({"column": column, "kind": "missing", "expected": expected})
            continue
        item.update(
            {
                "column": column,
                "expected": expected,
                "got": got,
                # A multi-marked truth (which bubbles is not recorded) is right
                # when the value goes to review
                "correct": item["flagged"]
                if expected == MULTI_MARK
                else got == expected,
            }
        )
        items.append(item)
    return items


def compute_metrics(
    records: List[dict],
    column_fields: Optional[Dict[str, List[str]]] = None,
    wall_seconds: Optional[float] = None,
    workers: Optional[int] = None,
    worst: int = 50,
):
    overall, bubble_fields = _Counter(), _Counter()
    by_kind: Dict[str, _Counter] = {}
    bubble_counts = {"tp": 0, "fp": 0, "fn": 0, "tn": 0, "undecomposable_fields": 0}
    sheets = {
        "total": 0,
        "missing_truth": 0,
        "error": 0,
        "registration_failures": 0,
        "scanned": 0,
        "ok": 0,
        "needs_review": 0,
        "exact_match": 0,
        "auto_accepted_exact": 0,
    }
    errors, failed_sheets = [], []
    # Why values went to review, and how often each reason was a real error
    review_reasons: Dict[str, Dict[str, int]] = {}
    missing_columns = set()
    timings: Dict[str, List[float]] = {}

    for record in records:
        result = record["result"]
        truth = record.get("truth")
        file_id = record.get("file_id") or result.get("file_id")
        sheets["total"] += 1
        for stage, ms in (result.get("timings_ms") or {}).items():
            timings.setdefault(stage, []).append(float(ms))
        if result.get("status") == "error":
            sheets["error"] += 1
            message = str(result.get("error") or "")
            if "registration" in message.lower():
                sheets["registration_failures"] += 1
            failed_sheets.append({"file": file_id, "error": message})
            continue
        if truth is None:
            sheets["missing_truth"] += 1
            continue
        sheets["scanned"] += 1
        status_ok = result.get("status") == "ok"
        sheets["ok" if status_ok else "needs_review"] += 1

        items = evaluate_record(result, truth, column_fields)
        sheet_correct = True
        for item in items:
            if item["kind"] == "missing":
                missing_columns.add(item["column"])
                continue
            overall.add(item["correct"], item["flagged"])
            by_kind.setdefault(item["kind"], _Counter()).add(
                item["correct"], item["flagged"]
            )
            if item["flagged"]:
                for flag in item["flags"] or ["(none)"]:
                    reason = review_reasons.setdefault(flag, {"count": 0, "wrong": 0})
                    reason["count"] += 1
                    reason["wrong"] += int(not item["correct"])
            if item["kind"] in (KIND_FIELD, KIND_CUSTOM):
                bubble_fields.add(item["correct"], item["flagged"])
            if item["kind"] == KIND_FIELD and item["expected"] != MULTI_MARK:
                _bubble_counts(
                    result["fields"][item["column"]], item["expected"], bubble_counts
                )
            if not item["correct"]:
                sheet_correct = False
                errors.append(
                    {
                        "file": file_id,
                        "column": item["column"],
                        "kind": item["kind"],
                        "expected": item["expected"],
                        "got": item["got"],
                        "confidence": item["confidence"],
                        "flags": item["flags"],
                        "flagged": item["flagged"],
                    }
                )
        if sheet_correct:
            sheets["exact_match"] += 1
            if status_ok:
                sheets["auto_accepted_exact"] += 1

    scanned = sheets["scanned"]
    sheet_summary = {
        **sheets,
        "exact_match_rate": _ratio(sheets["exact_match"], scanned),
        "review_rate": _ratio(sheets["needs_review"], scanned),
        # Sheets released without review that were entirely right
        "auto_accepted_accuracy": _ratio(sheets["auto_accepted_exact"], sheets["ok"]),
        "registration_failure_rate": _ratio(
            sheets["registration_failures"], sheets["total"]
        ),
        "error_rate": _ratio(sheets["error"], sheets["total"]),
        # Fraction of all sheets that are fully automatic and fully right
        "straight_through_rate": _ratio(sheets["auto_accepted_exact"], sheets["total"]),
    }

    # Silent errors first (they reach the results), then most confident first
    errors.sort(
        key=lambda e: (
            e["flagged"],
            -(e["confidence"] if e["confidence"] is not None else 0),
        )
    )
    throughput = {
        "latency_ms": {stage: percentiles(values) for stage, values in timings.items()}
    }
    if wall_seconds:
        throughput.update(
            {
                "wall_seconds": round(wall_seconds, 3),
                "sheets_per_sec": round(sheets["total"] / wall_seconds, 2),
                "workers": workers,
            }
        )

    return {
        "sheets": sheet_summary,
        "values": overall.to_dict(),
        "fields": bubble_fields.to_dict(),
        "by_kind": {
            kind: counter.to_dict() for kind, counter in sorted(by_kind.items())
        },
        "bubbles": _bubble_summary(bubble_counts),
        "throughput": throughput,
        "review_reasons": dict(
            sorted(review_reasons.items(), key=lambda kv: -kv[1]["count"])
        ),
        "missing_columns": sorted(missing_columns),
        "worst_errors": errors[:worst],
        "n_errors": len(errors),
        "failed_sheets": failed_sheets[:worst],
    }


def _pct(value):
    return "  n/a " if value is None else f"{100 * value:6.2f}%"


def format_summary(metrics, title="OMR benchmark"):
    """Readable console summary of compute_metrics() output."""
    s, v, f, b = (
        metrics["sheets"],
        metrics["values"],
        metrics["fields"],
        metrics["bubbles"],
    )
    lines = [f"== {title} ==", ""]
    lines.append(
        f"Sheets: {s['total']}  scanned {s['scanned']}  ok {s['ok']}  "
        f"needs_review {s['needs_review']}  error {s['error']}"
        + (f"  missing_truth {s['missing_truth']}" if s["missing_truth"] else "")
    )
    lines.append(
        f"  exact match {_pct(s['exact_match_rate'])}   review rate {_pct(s['review_rate'])}   "
        f"auto-accepted exact {_pct(s['auto_accepted_accuracy'])}   "
        f"registration failures {_pct(s['registration_failure_rate'])}"
    )
    lines.append(
        f"  straight-through (auto and right) {_pct(s['straight_through_rate'])}"
    )
    lines.append("")
    header = f"  {'group':<18}{'n':>8}{'accuracy':>10}{'review':>10}{'auto acc':>10}{'silent':>8}{'flag err':>10}"
    lines.append(header)

    def row(name, c):
        return (
            f"  {name:<18}{c['total']:>8}{_pct(c['accuracy']):>10}{_pct(c['review_rate']):>10}"
            f"{_pct(c['auto_accepted_accuracy']):>10}{c['silent_errors']:>8}"
            f"{_pct(c['flagged_error_rate']):>10}"
        )

    lines.append(row("all values", v))
    lines.append(row("bubble fields", f))
    for kind, counter in metrics["by_kind"].items():
        if kind.startswith("zone:"):
            lines.append(row(kind, counter))
    lines.append("")
    lines.append(
        f"Bubbles: n {b['total']}  precision {_pct(b['precision'])}  recall {_pct(b['recall'])}  "
        f"F1 {_pct(b['f1'])}  (FP {b['false_positive']}, FN {b['false_negative']})"
    )
    if metrics.get("review_reasons"):
        reasons = ", ".join(
            f"{flag} {r['count']} ({r['wrong']} wrong)"
            for flag, r in metrics["review_reasons"].items()
        )
        lines.append(f"Review reasons: {reasons}")
    t = metrics["throughput"]
    if "sheets_per_sec" in t:
        lines.append(
            f"Throughput: {t['sheets_per_sec']} sheets/s  ({t['wall_seconds']} s, "
            f"workers {t.get('workers')})"
        )
    for stage, stats in t["latency_ms"].items():
        if stats:
            lines.append(
                f"  {stage:<14} p50 {stats['p50']:>8.1f} ms   p95 {stats['p95']:>8.1f} ms   "
                f"max {stats['max']:>8.1f} ms"
            )
    if metrics["missing_columns"]:
        lines.append(
            f"Truth columns not produced by the engine: {metrics['missing_columns']}"
        )
    if metrics["worst_errors"]:
        lines.append("")
        lines.append(
            f"Worst errors ({metrics['n_errors']} total; silent errors first):"
        )
        for e in metrics["worst_errors"][:15]:
            conf = "-" if e["confidence"] is None else f"{e['confidence']:.3f}"
            lines.append(
                f"  {'REVIEW' if e['flagged'] else 'SILENT'}  {e['file']}  {e['column']}: "
                f"expected {e['expected']!r} got {e['got']!r}  conf {conf}  {','.join(e['flags'])}"
            )
    if metrics["failed_sheets"]:
        lines.append("")
        lines.append("Failed sheets:")
        for failure in metrics["failed_sheets"][:10]:
            lines.append(f"  {failure['file']}: {failure['error']}")
    return "\n".join(lines)
