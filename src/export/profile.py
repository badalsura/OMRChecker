"""
Export profiles: which columns to write, under which names, in which order and
with which types.

    {
      "fields": [
        {"field": "RollNo", "header": "Roll number"},          # text (default)
        {"field": "q1", "type": "text"},
        {"field": "count", "type": "int"},
        {"field": "price", "type": "decimal", "decimalComma": false},
        {"field": "dob", "type": "date", "format": "%d%m%Y", "outputFormat": "%Y-%m-%d"},
        {"field": "consent", "type": "bool", "true": ["A", "Y"], "false": ["B", "N"]},
        {"field": "notes", "include": false}
      ],
      "includeOtherFields": true,     # template fields not listed above, as text
      "meta": ["file_name", "page", "scan_id", "status", "score"],
      "includeConfidence": false,     # <header>_confidence per field
      "includeFlags": false,          # <header>_flags per field
      "includeReviewStatus": true,    # review_status column
      "includeCorrected": true,       # corrected column (+ per field with includeFieldCorrected)
      "allowLossyCast": false,        # int/decimal cast of "0123" is refused unless true
      "strictCast": false,            # uncastable values (e.g. "AB" as int) fail instead of NULL
      "csv": {"leadingZeros": "none" | "formula" | "apostrophe", "delimiter": ",", "bom": true},
      "xlsx": {"maxRowsPerSheet": 1048575, "highlight": true, "sheetName": "Results"},
      "pdf": {"mode": "table" | "sheets" | "both", "maxSheets": 500, "maxRows": 100000},
      "sql": {"table": "omr_results", "url": null, "batchSize": 1000}
    }
"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

FIELD_TYPES = ("text", "int", "decimal", "date", "bool")
META_COLUMNS = (
    "file_name",
    "file_id",
    "page",
    "scan_id",
    "job_id",
    "template_id",
    "status",
    "score",
    "source_path",
    "verified_by",
    "error",
    "created_at",
)
DEFAULT_META = ("file_name", "page", "scan_id", "status", "score")
META_TYPES = {"page": "int", "score": "decimal", "created_at": "decimal"}
DEFAULT_TRUE = ("1", "Y", "YES", "TRUE", "T", "X")
DEFAULT_FALSE = ("0", "N", "NO", "FALSE", "F", "")
INT_RE = re.compile(r"[+-]?\d+")
DECIMAL_RE = re.compile(r"[+-]?(\d+\.?\d*|\.\d+)")


class ExportError(Exception):
    """Invalid profile or export that cannot be produced."""


class LossyCastError(ExportError):
    pass


class CastFailure(Exception):
    pass


@dataclass
class Column:
    key: str  # unique output name (header)
    source: str  # "meta" | "field"
    name: str  # meta key or field/zone/custom-label name
    part: str = "value"  # value | confidence | flags | corrected
    type: str = "text"
    options: Dict[str, Any] = field(default_factory=dict)

    @property
    def header(self):
        return self.key


@dataclass
class ExportProfile:
    fields: List[Dict[str, Any]] = field(default_factory=list)
    include_other_fields: Optional[bool] = None
    meta: List[str] = field(default_factory=lambda: list(DEFAULT_META))
    include_confidence: bool = False
    include_flags: bool = False
    include_review_status: bool = True
    include_corrected: bool = True
    include_field_corrected: bool = False
    allow_lossy_cast: bool = False
    strict_cast: bool = False
    csv: Dict[str, Any] = field(default_factory=dict)
    xlsx: Dict[str, Any] = field(default_factory=dict)
    pdf: Dict[str, Any] = field(default_factory=dict)
    sql: Dict[str, Any] = field(default_factory=dict)
    name: str = ""

    @classmethod
    def from_dict(cls, data):
        data = dict(data or {})
        if not isinstance(data, dict):
            raise ExportError("The export profile must be a JSON object")
        profile = cls(
            fields=list(data.get("fields") or []),
            include_other_fields=data.get("includeOtherFields"),
            meta=list(data["meta"])
            if data.get("meta") is not None
            else list(DEFAULT_META),
            include_confidence=bool(data.get("includeConfidence", False)),
            include_flags=bool(data.get("includeFlags", False)),
            include_review_status=bool(data.get("includeReviewStatus", True)),
            include_corrected=bool(data.get("includeCorrected", True)),
            include_field_corrected=bool(data.get("includeFieldCorrected", False)),
            allow_lossy_cast=bool(data.get("allowLossyCast", False)),
            strict_cast=bool(data.get("strictCast", False)),
            csv=dict(data.get("csv") or {}),
            xlsx=dict(data.get("xlsx") or {}),
            pdf=dict(data.get("pdf") or {}),
            sql=dict(data.get("sql") or {}),
            name=str(data.get("name") or ""),
        )
        profile.validate()
        return profile

    def validate(self):
        unknown_meta = [m for m in self.meta if m not in META_COLUMNS]
        if unknown_meta:
            raise ExportError(
                f"Unknown meta column(s) {unknown_meta}; choose from {list(META_COLUMNS)}"
            )
        seen = set()
        for spec in self.fields:
            if not isinstance(spec, dict) or not spec.get("field"):
                raise ExportError(
                    f"Each entry of 'fields' needs a 'field' name: {spec}"
                )
            kind = spec.get("type", "text")
            if kind not in FIELD_TYPES:
                raise ExportError(
                    f"Field '{spec['field']}': type must be one of {list(FIELD_TYPES)}"
                )
            if kind == "date" and not spec.get("format"):
                raise ExportError(
                    f"Field '{spec['field']}': a date needs 'format' (e.g. %d%m%Y)"
                )
            if spec["field"] in seen:
                raise ExportError(f"Field '{spec['field']}' is listed twice")
            seen.add(spec["field"])
        leading = self.csv.get("leadingZeros", "none")
        if leading not in ("none", "formula", "apostrophe"):
            raise ExportError("csv.leadingZeros must be none, formula or apostrophe")
        mode = self.pdf.get("mode", "table")
        if mode not in ("table", "sheets", "both"):
            raise ExportError("pdf.mode must be table, sheets or both")

    def columns(self, field_names, custom_labels=None):
        """
        Resolve output columns against the template's output field names (in
        template order). Returns (columns, warnings).
        """
        warnings = []
        custom_labels = custom_labels or {}
        specs = {spec["field"]: spec for spec in self.fields}
        listed = [spec["field"] for spec in self.fields if spec.get("include", True)]
        include_others = self.include_other_fields
        if include_others is None:
            include_others = not self.fields
        ordered = list(listed)
        if include_others:
            ordered += [n for n in field_names if n not in specs]
        missing = [n for n in listed if n not in field_names]
        if missing and field_names:
            warnings.append(
                f"Not in the template (exported empty): {', '.join(missing)}"
            )
        columns, used = [], set()

        def add(column):
            key, n = column.key, 2
            while column.key in used:
                column.key = f"{key}_{n}"
                n += 1
            used.add(column.key)
            columns.append(column)

        for meta in self.meta:
            add(Column(meta, "meta", meta, type=META_TYPES.get(meta, "text")))
        if self.include_review_status:
            add(Column("review_status", "meta", "review_status"))
        if self.include_corrected:
            add(Column("corrected", "meta", "corrected", type="bool"))
        for name in ordered:
            spec = specs.get(name, {})
            header = str(spec.get("header") or spec.get("rename") or name)
            kind = spec.get("type", "text")
            options = {
                k: v
                for k, v in spec.items()
                if k not in ("field", "header", "rename", "type", "include")
            }
            if kind in ("int", "decimal"):
                parts = custom_labels.get(name)
                if parts and len(parts) > 1:
                    warnings.append(
                        f"'{name}' joins {len(parts)} fields; values starting with 0 "
                        f"cannot be stored as {kind} (allowLossyCast: "
                        f"{str(self.allow_lossy_cast).lower()})"
                    )
            add(Column(header, "field", name, "value", kind, options))
            if self.include_confidence:
                add(
                    Column(
                        f"{header}_confidence", "field", name, "confidence", "decimal"
                    )
                )
            if self.include_flags:
                add(Column(f"{header}_flags", "field", name, "flags"))
            if self.include_field_corrected:
                add(Column(f"{header}_corrected", "field", name, "corrected", "bool"))
        return columns, warnings


# ---------------------------------------------------------------------------
# casting
# ---------------------------------------------------------------------------
def has_leading_zero(text):
    digits = text.lstrip("+-")
    integer = digits.split(".")[0]
    return len(integer) > 1 and integer.startswith("0")


def cast_value(value, column, allow_lossy=False):
    """Typed value for a column; raises LossyCastError or CastFailure."""
    kind = column.type
    if value is None:
        return None
    if kind == "text":
        return value if isinstance(value, str) else str(value)
    if isinstance(value, bool) and kind == "bool":
        return value
    if isinstance(value, (int, float)) and kind in ("int", "decimal"):
        return int(value) if kind == "int" else Decimal(str(value))
    text = str(value).strip()
    if kind == "bool":
        options = column.options
        truthy = {str(v).upper() for v in options.get("true", DEFAULT_TRUE)}
        falsy = {str(v).upper() for v in options.get("false", DEFAULT_FALSE)}
        if text.upper() in truthy:
            return True
        if text.upper() in falsy:
            return False
        raise CastFailure(f"'{text}' is not a yes/no value")
    if text == "":
        return None
    if kind == "int":
        if not INT_RE.fullmatch(text):
            raise CastFailure(f"'{text}' is not a whole number")
        if has_leading_zero(text) and not allow_lossy:
            raise LossyCastError(
                f"Column '{column.key}': '{text}' has a leading zero that int would "
                'drop. Export it as text, or set "allowLossyCast": true.'
            )
        return int(text)
    if kind == "decimal":
        if column.options.get("decimalComma"):
            text = text.replace(".", "").replace(",", ".")
        if not DECIMAL_RE.fullmatch(text):
            raise CastFailure(f"'{text}' is not a number")
        if has_leading_zero(text) and not allow_lossy:
            raise LossyCastError(
                f"Column '{column.key}': '{text}' has a leading zero that decimal "
                'would drop. Export it as text, or set "allowLossyCast": true.'
            )
        try:
            return Decimal(text)
        except InvalidOperation:
            raise CastFailure(f"'{text}' is not a number") from None
    if kind == "date":
        try:
            return datetime.strptime(text, column.options["format"]).date()
        except ValueError:
            raise CastFailure(
                f"'{text}' does not match the date format {column.options['format']}"
            ) from None
    raise ExportError(f"Unknown type '{kind}'")


def text_of(value, column=None):
    """Plain text of a typed value (CSV, PDF)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if hasattr(value, "strftime"):
        fmt = (column.options.get("outputFormat") if column else None) or "%Y-%m-%d"
        return value.strftime(fmt)
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)
