"""
Value "shape" validation for any output: a bubble field, a custom label (e.g. a
multi-column roll number), a zone or a rule output.

Template key::

    "validate": {
      "roll_no": {"length": 12, "allowGaps": false, "allowEmptyEnds": false,
                  "leadingZeros": "keep", "pattern": "\\d+", "onFail": "review"}
    }

Every key is optional. Values stay text: `range` compares numerically but never
changes the value. Gap and empty-end checks use the per-column values of a
custom label (one entry per bubble column) or the character boxes of an ICR
zone, not the concatenated string, so a blank middle column is caught even when
the template's emptyValue is "".
"""

import re

ON_FAIL_ACTIONS = ("review", "blank", "both", "flag")
LEADING_ZEROS = ("keep", "forbid")
VALIDATION_FLAG = "validation_failed"


class ValidationRule:
    def __init__(self, name, spec):
        self.name = name
        self.spec = dict(spec)
        self.length = spec.get("length")
        self.allow_gaps = spec.get("allowGaps", True)
        self.allow_empty_ends = spec.get("allowEmptyEnds", True)
        self.leading_zeros = spec.get("leadingZeros", "keep")
        self.required = spec.get("required", False)
        self.allowed = spec.get("allowed")
        self.range = spec.get("range")
        self.on_fail = spec.get("onFail", "review")
        pattern = spec.get("pattern")
        try:
            self.pattern = re.compile(pattern) if pattern else None
        except re.error as error:
            raise Exception(f"validate['{name}']: invalid pattern {pattern!r}: {error}")
        if self.on_fail not in ON_FAIL_ACTIONS:
            raise Exception(
                f"validate['{name}']: onFail must be one of {ON_FAIL_ACTIONS}"
            )
        if self.leading_zeros not in LEADING_ZEROS:
            raise Exception(
                f"validate['{name}']: leadingZeros must be one of {LEADING_ZEROS}"
            )
        if self.allowed is not None:
            self.allowed = [str(v) for v in self.allowed]

    @property
    def blanks(self):
        return self.on_fail in ("blank", "both")

    @property
    def reviews(self):
        return self.on_fail in ("review", "both")

    def failures(self, value, columns=None, is_empty=None):
        """
        Reasons the value fails, or [] when it passes.

        `columns`: per-position values (custom label columns, ICR boxes);
        `is_empty`: per-position emptiness, defaults to "blank string".
        Without columns, each character is a position and whitespace is empty.
        """
        value = "" if value is None else str(value)
        if columns is None:
            columns = list(value)
            is_empty = [not ch.strip() for ch in columns]
        elif is_empty is None:
            is_empty = [not str(c).strip() for c in columns]
        filled = [i for i, empty in enumerate(is_empty) if not empty]
        if not filled:
            return ["empty"] if self.required else []

        reasons = []
        text = value
        if self.length is not None:
            low, high = _bounds(self.length)
            if (low is not None and len(text) < low) or (
                high is not None and len(text) > high
            ):
                reasons.append(f"length {len(text)} not in {_show(self.length)}")
        first, last = filled[0], filled[-1]
        if not self.allow_gaps:
            gaps = [i + 1 for i in range(first, last + 1) if is_empty[i]]
            if gaps:
                reasons.append(f"gap at position {', '.join(map(str, gaps))}")
        if not self.allow_empty_ends:
            if first > 0:
                reasons.append(f"{first} empty leading position(s)")
            if last < len(is_empty) - 1:
                reasons.append(f"{len(is_empty) - 1 - last} empty trailing position(s)")
        if self.leading_zeros == "forbid" and len(text) > 1 and text.startswith("0"):
            reasons.append("leading zero")
        if self.pattern is not None and not self.pattern.fullmatch(text):
            reasons.append(f"does not match {self.pattern.pattern!r}")
        if self.allowed is not None and text not in self.allowed:
            reasons.append("not an allowed value")
        if self.range is not None:
            low, high = _bounds(self.range)
            try:
                number = float(text)
            except ValueError:
                reasons.append("not a number")
            else:
                if (low is not None and number < low) or (
                    high is not None and number > high
                ):
                    reasons.append(f"{text} outside {_show(self.range)}")
        return reasons


def _bounds(spec):
    if isinstance(spec, (list, tuple)):
        low = spec[0] if len(spec) > 0 else None
        high = spec[1] if len(spec) > 1 else None
        return low, high
    return spec, spec


def _show(spec):
    if isinstance(spec, (list, tuple)):
        low, high = _bounds(spec)
        return f"[{'' if low is None else low}, {'' if high is None else high}]"
    return str(spec)
