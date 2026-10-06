"""Turn stored scan results into typed export rows (one per sheet)."""

from src.export.profile import CastFailure, LossyCastError, cast_value

FLAGGED, CORRECTED = "flagged", "corrected"


def split_value(value, bubble_values):
    """Bubble values making up a field value ('AC' -> ['A', 'C']); None if impossible."""
    if value in ("", None):
        return []
    if value in bubble_values:
        return [value]
    ordered = sorted(set(bubble_values), key=len, reverse=True)
    picked, rest = [], value
    while rest:
        match = next((v for v in ordered if v and rest.startswith(v)), None)
        if match is None:
            return None
        picked.append(match)
        rest = rest[len(match) :]
    return picked


def original_value(item):
    return item.get("original_value", item.get("value"))


def review_status(result):
    if result.get("verified"):
        return "verified"
    if result.get("status") == "error":
        return "error"
    if result.get("review"):
        return "needs_review"
    if result.get("reviewed"):
        return "reviewed"
    return "auto"


def is_corrected(result):
    for group in ("fields", "zones"):
        for item in (result.get(group) or {}).values():
            if original_value(item) != item.get("value"):
                return True
    return bool(result.get("manual_values"))


def field_names(template_infos, results_sample=()):
    """Output names in template order (union over templates), then zones and checks."""
    names = []
    for info in template_infos:
        for name in list(info.get("output_columns") or []) + list(
            info.get("zone_names") or []
        ):
            if name not in names:
                names.append(name)
    for result in results_sample:
        for name in list((result.get("responses") or {}).keys()) + list(
            (result.get("checks") or {}).keys()
        ):
            if name not in names:
                names.append(name)
    return names


class RowBuilder:
    def __init__(self, columns, profile, custom_labels=None):
        self.columns = columns
        self.profile = profile
        self.custom_labels = custom_labels or {}
        self.cast_failures = 0
        self.failure_examples = []
        self.rows = 0

    def item(self, result, name):
        fields = result.get("fields") or {}
        zones = result.get("zones") or {}
        responses = result.get("responses") or {}
        checks = result.get("checks") or {}
        parts = self.custom_labels.get(name) or [name]
        members = [fields.get(p) or zones.get(p) for p in parts]
        members = [m for m in members if m is not None]
        if name in responses:
            value = responses[name]
        elif name in zones:
            value = zones[name].get("value", "")
        elif name in fields:
            value = fields[name].get("value", "")
        elif isinstance(checks.get(name), dict):
            value = checks[name].get("value")
        else:
            value = None
        pending = {item["name"] for item in result.get("review") or []}
        flags = sorted({f for m in members for f in m.get("flags") or []})
        check, check_name = checks.get(name), name
        if not isinstance(check, dict):
            # a check whose output column has another name
            check_name, check = next(
                (
                    (key, c)
                    for key, c in checks.items()
                    if isinstance(c, dict) and c.get("output") == name
                ),
                (name, None),
            )
        if isinstance(check, dict) and check.get("flags"):
            flags = sorted(set(flags) | set(check["flags"]))
        confidences = [
            m["confidence"] for m in members if m.get("confidence") is not None
        ]
        return {
            "value": value,
            "confidence": min(confidences) if confidences else None,
            "flags": flags,
            "pending": any(p in pending for p in parts)
            or name in pending
            or check_name in pending,
            "corrected": any(original_value(m) != m.get("value") for m in members)
            or bool(isinstance(check, dict) and check.get("manual")),
        }

    def meta(self, result, key):
        if key == "review_status":
            return review_status(result)
        if key == "corrected":
            return is_corrected(result)
        if key == "page":
            return (result.get("page") or 0) + 1
        if key == "scan_id":
            return result.get("scan_id")
        if key == "verified_by":
            return (result.get("verified") or {}).get("by")
        if key == "source_path":
            return result.get("source_path") or result.get("input_path")
        return result.get(key)

    def build(self, result):
        """(values, states): typed cell values and None/'flagged'/'corrected' per cell."""
        values, states, cache = [], [], {}
        for column in self.columns:
            state = None
            if column.source == "meta":
                raw = self.meta(result, column.name)
            else:
                if column.name not in cache:
                    cache[column.name] = self.item(result, column.name)
                item = cache[column.name]
                if column.part == "confidence":
                    raw = item["confidence"]
                elif column.part == "flags":
                    raw = ";".join(item["flags"])
                elif column.part == "corrected":
                    raw = item["corrected"]
                else:
                    raw = item["value"]
                    state = (
                        FLAGGED
                        if item["pending"]
                        else CORRECTED
                        if item["corrected"]
                        else None
                    )
            try:
                value = cast_value(raw, column, self.profile.allow_lossy_cast)
            except LossyCastError as error:
                raise LossyCastError(
                    f"{error} (sheet {result.get('file_id') or result.get('scan_id')})"
                ) from None
            except CastFailure as error:
                if self.profile.strict_cast:
                    raise LossyCastError(
                        f"Column '{column.key}': {error} "
                        f"(sheet {result.get('file_id') or result.get('scan_id')})"
                    ) from None
                self.cast_failures += 1
                if len(self.failure_examples) < 10:
                    self.failure_examples.append(
                        f"{result.get('file_id') or result.get('scan_id')}: "
                        f"{column.key}: {error}"
                    )
                value = None
                state = FLAGGED
            values.append(value)
            states.append(state)
        self.rows += 1
        return values, states
