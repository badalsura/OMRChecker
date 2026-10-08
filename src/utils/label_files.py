"""
Label files for the template generator: CSV or Excel (.xlsx), one row per sheet.

- The file-name column may be spelled any way: "file", "File Name",
  "file_name", "Image", "FILE-ID", ... (case, spaces and punctuation ignored).
  Without one, rows are matched to the images in order (same count needed).
- Excel number cells lose their leading zeros ("01051" stored as 1051); a
  digits-only column is padded back to its usual length.
- Answer-string columns ("ANS": "CB A*D...", one character per question) are
  expanded to q1..qN the same way the benchmark does
  (src.ml.dataset.expand_answer_string): a space is a blank question and "*"
  a multi-marked one. A column is an answer string when it is named like one
  (ANS, Answers, Responses, ...) or when its values are long runs of option
  letters, spaces and "*".

Only the standard library and openpyxl (for .xlsx, optional) are used, so this
works on the Windows 7 / Python 3.8 build; a missing openpyxl gives a clear
error instead of a crash.
"""

import csv
import io
import re
from pathlib import Path

from src.ml.dataset import MULTI_MARK, expand_answer_string

# Normalised (lower case, letters and digits only) names of the file column
FILE_COLUMNS = (
    "file",
    "filename",
    "fileid",
    "filepath",
    "image",
    "imagename",
    "imagefile",
    "imageid",
    "name",
    "sheet",
    "sheetname",
    "scan",
    "scanfile",
)
AMBIGUOUS_FILE_COLUMNS = {"name", "sheet", "scan", "image"}
ANSWER_COLUMN_NAMES = {
    "ans",
    "answer",
    "answers",
    "answerstring",
    "answerkey",
    "response",
    "responses",
    "resp",
    "omrstring",
    "marked",
}
ANSWER_PREFIX = "q"
_ANSWER_CHARS = re.compile(r"^[A-Za-z \*\-_\.]*$")


class LabelFileError(ValueError):
    """The label file can't be used; the message says why, in plain words."""


def normalize_header(name):
    return "".join(ch for ch in str(name or "").lower() if ch.isalnum())


def is_file_column(name):
    return normalize_header(name) in FILE_COLUMNS


def find_file_column(header):
    """The file-name column, most specific spelling first ("File Name" before "Name")."""
    normalized = {}
    for column in header:
        normalized.setdefault(normalize_header(column), column)
    for candidate in FILE_COLUMNS:
        if candidate in normalized:
            return normalized[candidate]
    return None


def _cell_text(value):
    """Excel / CSV cell -> text. Whole floats lose the '.0' Excel adds."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        if value != value:  # NaN
            return ""
        if value.is_integer():
            return str(int(value))
    return str(value)


def _read_xlsx(content):
    try:
        import openpyxl
    except ImportError:  # pragma: no cover - depends on the install
        raise LabelFileError(
            "Reading .xlsx label files needs the openpyxl package, which is not "
            "installed here; save the sheet as CSV instead"
        ) from None
    try:
        book = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as error:
        raise LabelFileError(f"Could not open the Excel file: {error}") from None
    sheet = book.active
    rows = sheet.iter_rows(values_only=True)
    header = None
    for row in rows:
        if row and any(v is not None and str(v).strip() for v in row):
            header = [_cell_text(v).strip() for v in row]
            break
    if not header:
        return [], []
    records, numeric = [], {}
    for row in rows:
        if not row or not any(v is not None and str(v).strip() for v in row):
            continue
        record = {}
        for key, value in zip(header, row):
            if not key:
                continue
            record[key] = _cell_text(value)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                numeric[key] = True
        records.append(record)
    try:
        book.close()
    except Exception:
        pass
    return [h for h in header if h], records


def _read_csv(content):
    text = content.decode("utf-8-sig", errors="replace") if isinstance(content, bytes) else content
    sample = text[:4096]
    delimiter = ","
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t").delimiter
    except csv.Error:
        pass
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    records = []
    for row in reader:
        if not any((v or "").strip() for k, v in row.items() if k):
            continue
        records.append({k.strip(): (v or "") for k, v in row.items() if k and k.strip()})
    header = [h.strip() for h in (reader.fieldnames or []) if h and h.strip()]
    return header, records


def read_table(content, filename=None):
    """(header, records, format) from CSV or .xlsx bytes."""
    suffix = Path(filename or "").suffix.lower()
    if suffix in (".xlsx", ".xlsm") or (
        suffix not in (".csv", ".txt", ".tsv") and content[:2] == b"PK"
    ):
        header, records = _read_xlsx(content)
        return header, records, "xlsx"
    if suffix == ".xls":
        raise LabelFileError(
            "Old Excel .xls files can't be read: save the sheet as .xlsx or CSV"
        )
    header, records = _read_csv(content)
    return header, records, "csv"


def looks_like_answer_column(name, values):
    """A column holding one character per question (e.g. "CB A*D...")."""
    filled = [v for v in values if v.strip()]
    if not filled:
        return False
    if normalize_header(name) in ANSWER_COLUMN_NAMES:
        return all(_ANSWER_CHARS.match(v) for v in filled)
    lengths = sorted(len(v) for v in filled)
    if lengths[len(lengths) // 2] < 15:
        return False
    if not all(_ANSWER_CHARS.match(v) for v in filled):
        return False
    letters = {ch.upper() for v in filled for ch in v if ch.isalpha()}
    return 0 < len(letters) <= 8


def _pad_digit_columns(header, records):
    """Restore leading zeros Excel dropped: pad short digit-only values."""
    for key in header:
        values = [r.get(key, "") for r in records]
        digits = [v.strip() for v in values if v.strip()]
        if len(digits) < 2 or not all(v.isdigit() for v in digits):
            continue
        lengths = [len(v) for v in digits]
        common = max(set(lengths), key=lengths.count)
        longest = max(lengths)
        # A value typed with a leading zero ("01051", kept as text) shows the
        # real width; otherwise pad only when one width clearly dominates
        zero_led = [len(v) for v in digits if len(v) >= 2 and v.startswith("0")]
        if zero_led:
            longest = max(zero_led)
        elif common != longest or common < 2:
            continue
        for record in records:
            value = record.get(key, "").strip()
            if value.isdigit() and len(value) < longest:
                record[key] = value.zfill(longest)


def parse_label_file(content, image_names, filename=None, answer_columns=None):
    """
    Map a label file onto the uploaded images.

    Returns (labels, info): labels is a list with one {column: value} dict
    (or None when the sheet has no row) per image; info describes what was
    recognised (file column, expanded answer columns, unmatched images).
    answer_columns: optional {column: prefix} to force answer-string expansion.
    """
    header, records, kind = read_table(content, filename)
    info = {
        "format": kind,
        "columns": list(header),
        "file_column": None,
        "answer_columns": {},
        "rows": len(records),
        "matched_images": 0,
        "unmatched_images": [],
    }
    if not records:
        return None, info
    key = find_file_column(header)
    if key is not None and normalize_header(key) in AMBIGUOUS_FILE_COLUMNS:
        # "Name" may be the student's name: use it only when it names the images
        stems = {Path(str(n).replace("\\", "/")).stem.lower() for n in image_names}
        values = [Path(str(r.get(key) or "").strip()).stem.lower() for r in records]
        if sum(v in stems for v in values if v) < 0.5 * max(1, min(len(values), len(stems))):
            key = None
    info["file_column"] = key
    data_columns = [h for h in header if h != key]
    _pad_digit_columns(data_columns, records)

    forced = {normalize_header(k): v for k, v in (answer_columns or {}).items()}
    expand = {}
    for column in data_columns:
        values = [r.get(column, "") for r in records]
        prefix = forced.get(normalize_header(column))
        if prefix or looks_like_answer_column(column, values):
            # Trailing blanks may be trimmed by the spreadsheet: use the longest
            expand[column] = (prefix or ANSWER_PREFIX, max(len(v.rstrip("\r\n")) for v in values))
    taken = set(data_columns)
    if len(expand) > 1:
        # Several answer columns: give each its own prefix (ans_q1.., set2_q1..)
        for column in list(expand):
            prefix, count = expand[column]
            if not forced.get(normalize_header(column)):
                expand[column] = (f"{normalize_header(column)}_q", count)
    for column, (prefix, count) in expand.items():
        info["answer_columns"][column] = {"prefix": prefix, "questions": count}
        clash = [f"{prefix}{i}" for i in range(1, count + 1) if f"{prefix}{i}" in taken]
        if clash:
            raise LabelFileError(
                f"Answer column '{column}' would create {clash[0]}, which is already a column"
            )

    def clean(record):
        row = {}
        for column in data_columns:
            value = record.get(column, "")
            if column in expand:
                prefix, count = expand[column]
                for name, char in expand_answer_string(value, prefix, count).items():
                    row[name] = MULTI_MARK if char == MULTI_MARK else char
            else:
                row[column] = value.strip()
        return row

    if key is None:
        if len(records) != len(image_names):
            raise LabelFileError(
                "The label file needs a file-name column (e.g. 'File Name'), or "
                f"exactly one row per image in the same order ({len(records)} rows "
                f"for {len(image_names)} images)"
            )
        labels = [clean(r) for r in records]
        info["matched_images"] = len(labels)
        return labels, info
    by_name, by_stem = {}, {}
    for record in records:
        raw = str(record.get(key) or "").strip().replace("\\", "/")
        if not raw:
            continue
        name = Path(raw).name
        by_name.setdefault(name.lower(), record)
        by_stem.setdefault(Path(name).stem.lower(), record)
    labels = []
    for image_name in image_names:
        name = Path(str(image_name).replace("\\", "/")).name
        record = by_name.get(name.lower()) or by_stem.get(Path(name).stem.lower())
        if record is None:
            labels.append(None)
            info["unmatched_images"].append(name)
        else:
            labels.append(clean(record))
    info["matched_images"] = len(labels) - len(info["unmatched_images"])
    return labels, info
