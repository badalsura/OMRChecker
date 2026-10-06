"""
Cross-field rules ("checks"): combine several reads of the same value.

    "checks": [{
      "name": "answer_book",
      "sources": ["barcode2", "sr_no_ocr"],
      "normalize": "digits",
      "onMissing": "fallback",
      "onConflict": "prefer",
      "priority": ["barcode2", "sr_no_ocr"],
      "reviewOnConflict": true,
      "reviewOnFallback": false,
      "output": "answer_book"
    }]

A source is any bubble field, custom label, zone or another check's output.
The check's value goes to the `output` column (default: its name), and
result.checks[name] records every source's value, the chosen source and flags:

* ``fallback_used``: the first source in `priority` was missing, so a later one
  supplied the value (``onMissing: "review"`` also sends it to review);
* ``cross_check_failed``: present sources disagree after normalisation;
  ``onConflict`` picks the value: "prefer" (priority order), "review" (same, and
  always review) or "error" (blank, and review);
* ``all_sources_missing``: nothing to report (review unless
  ``reviewOnAllMissing`` is false).

The output may be one of the check's own sources: the check's value then
replaces that column in the results (this is what a barcode zone's
``fallbackZone`` option creates).
"""

import re

NORMALIZERS = ("none", "strip", "digits", "upper", "alnum")
ON_MISSING = ("fallback", "review")
ON_CONFLICT = ("prefer", "review", "error")

FLAG_CONFLICT = "cross_check_failed"
FLAG_FALLBACK = "fallback_used"
FLAG_ALL_MISSING = "all_sources_missing"


class CheckRule:
    def __init__(self, spec):
        name = spec.get("name")
        if not name:
            raise Exception("Every entry in 'checks' needs a name")
        self.name = name
        self.spec = dict(spec)
        self.sources = list(spec.get("sources") or [])
        if not self.sources:
            raise Exception(f"check '{name}': 'sources' must list at least one name")
        if len(set(self.sources)) != len(self.sources):
            raise Exception(f"check '{name}': duplicate sources")
        self.priority = list(spec.get("priority") or self.sources)
        if sorted(self.priority) != sorted(self.sources):
            if set(self.priority) - set(self.sources):
                raise Exception(
                    f"check '{name}': priority names {sorted(set(self.priority) - set(self.sources))} are not sources"
                )
            # Sources missing from priority keep their listed order, after it
            self.priority += [s for s in self.sources if s not in self.priority]
        self.output = spec.get("output") or name
        self.on_missing = spec.get("onMissing", "fallback")
        self.on_conflict = spec.get("onConflict", "prefer")
        self.review_on_conflict = spec.get("reviewOnConflict", True)
        self.review_on_fallback = spec.get("reviewOnFallback", False)
        self.review_on_all_missing = spec.get("reviewOnAllMissing", True)
        self.skip_invalid = spec.get("skipInvalid", True)
        self.skip_flagged = spec.get("skipFlagged", False)
        self.absorb_review = spec.get("absorbSourceReview", True)
        if self.on_missing not in ON_MISSING:
            raise Exception(f"check '{name}': onMissing must be one of {ON_MISSING}")
        if self.on_conflict not in ON_CONFLICT:
            raise Exception(f"check '{name}': onConflict must be one of {ON_CONFLICT}")
        self.normalize = make_normalizer(spec.get("normalize", "none"), name)

    @property
    def shadows(self):
        """True when the output replaces one of the check's own sources."""
        return self.output in self.sources


def make_normalizer(spec, name="check"):
    if isinstance(spec, dict):
        if "regex" not in spec:
            raise Exception(f"check '{name}': normalize object needs 'regex'")
        try:
            pattern = re.compile(spec["regex"])
        except re.error as error:
            raise Exception(f"check '{name}': invalid normalize regex: {error}")
        group = spec.get("group", 0)
        if isinstance(group, int) and group > pattern.groups:
            raise Exception(f"check '{name}': regex has no group {group}")

        def by_regex(value):
            match = pattern.search(value)
            return (match.group(group) or "") if match else ""

        return by_regex
    if spec not in NORMALIZERS:
        raise Exception(f"check '{name}': normalize must be one of {NORMALIZERS}")
    if spec == "none":
        return lambda value: value
    if spec == "strip":
        return lambda value: value.strip()
    if spec == "digits":
        return lambda value: "".join(ch for ch in value if ch.isdigit())
    if spec == "upper":
        return lambda value: value.strip().upper()
    return lambda value: "".join(ch for ch in value if ch.isalnum())


def resolve(rule, readings):
    """
    Decide a check's value.

    `readings`: list of (source, raw, normalized, usable) in priority order;
    `usable` is False for a missing/invalid/unread source. Returns
    (value, chosen_source, flags, needs_review); value None means blank.
    """
    present = [(src, norm) for src, _raw, norm, usable in readings if usable]
    flags = []
    if not present:
        flags.append(FLAG_ALL_MISSING)
        return None, None, flags, bool(rule.review_on_all_missing)

    chosen, value = present[0]
    review = False
    if chosen != rule.priority[0]:
        flags.append(FLAG_FALLBACK)
        review = rule.on_missing == "review" or bool(rule.review_on_fallback)
    if len({norm for _src, norm in present}) > 1:
        flags.append(FLAG_CONFLICT)
        if rule.on_conflict == "error":
            return None, None, flags, True
        if rule.on_conflict == "review" or rule.review_on_conflict:
            review = True
    return value, chosen, flags, review
