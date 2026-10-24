"""
Compare: reconcile our results with another reader's, such as a physical OMR
scanner (Scantron OpScan 6 with ScanTools Plus) or any other CSV or
fixed-width export.

A comparison (a "session") holds:

- the external file, kept under <data_dir>/compare/<id>/ (read again when
  the rules change);
- how its columns map to our fields, how its records join our sheets (file
  name, an imprint or roll number, or scan order) and the compare rules;
- the reconciled layer in <data_dir>/compare.sqlite3: one row per sheet, one
  row per mismatch with the value a reviewer chose and its source (external
  or ours), and an audit row for every decision.

Read results are never changed: the reconciled export applies the decisions on
top of them. Several reviewers work at once: a sheet being worked on is locked
to its reviewer for LOCK_SECONDS after their last action.
"""

import csv
import io
import json
import re
import sqlite3
import threading
import time
from pathlib import Path

from src.api.storage import new_id
from src.constants.common import FIELD_TYPES
from src.utils.parsing import parse_field_string

LOCK_SECONDS = 600
SAMPLE_ROWS = 8
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".pdf", ".webp"}

# Canonical kinds of a compared value
BLANK, MULTI, VALUE = "blank", "multi", "value"

DEFAULT_RULES = {
    # "all" mapped columns, "questions" (bubble fields with letter options) or
    # "list" (names / ranges in "list")
    "fields": "all",
    "list": "",
    # One side blank, the other marked: "mismatch" or "ignore"
    "blank": "mismatch",
    # Multi-marks: "same" (any multi-mark equals any other), "exact" (the
    # marked options must agree when both sides list them) or "ignore"
    "multi": "same",
    "case": False,
    # Ignore spaces inside a value (a roll number with a blank digit)
    "spaces": True,
    # What the external file writes for a blank and for a multi-mark
    "blank_codes": [" ", "-"],
    "multi_codes": ["*"],
    # Compare each sheet's questions only within the band its level selects
    # (TERI: level 1 answers q1..q50, level 2 q51..q100...). field: our name
    # holding the level; source: "theirs", "ours" or "either" (both bands);
    # map: "1: q1..q50; 2: q51..q100". Empty field: every question.
    "band_field": "",
    "band_source": "theirs_then_ours",
    "band_map": "",
    # Several of our scans with one key (the same file read in several jobs):
    # "newest" compares the newest readable scan, "skip" compares none
    "duplicates": "newest",
    # Where a choice goes: "layer" (the reconciled layer only; our results
    # stay as read) or "results" (also a correction of our result, audited
    # and undoable like one made in Results)
    "save_to": "layer",
}

SESSION_STATES = ("pending", "partial", "done")
OPEN_STATES = ("pending", "partial")


class CompareError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# the external file
# ---------------------------------------------------------------------------
def decode_text(content):
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("latin-1")


def parse_fixed_spec(text):
    """
    Fixed-width columns, one per line: "NAME START LENGTH" (START counts from 1).
    A range names consecutive cells: "q1..q200 40 1" puts q1 at 40, q2 at 41...
    """
    columns = []
    for number, line in enumerate((text or "").splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = re.split(r"[\s,;]+", line)
        if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
            raise CompareError(
                f"Fixed-width line {number}: write NAME START LENGTH, e.g. 'q1..q200 40 1'"
            )
        start, length = int(parts[1]), int(parts[2])
        if start < 1 or length < 1:
            raise CompareError(
                f"Fixed-width line {number}: START and LENGTH start at 1"
            )
        try:
            names = expand_range(parts[0])
        except CompareError as error:
            raise CompareError(f"Fixed-width line {number}: {error}") from None
        for i, name in enumerate(names):
            columns.append((name, start - 1 + i * length, length))
    if not columns:
        raise CompareError(
            "Describe the fixed-width columns (NAME START LENGTH per line)"
        )
    return columns


def expand_range(text):
    """q1..q200, q1..200 or a plain name."""
    match = re.fullmatch(r"([^\d.]*)(\d+)\.{2,3}([^\d.]*)(\d+)", text)
    if not match:
        if "." in text:
            raise CompareError(f"'{text}' is not a range like q1..q50")
        return [text]
    prefix, start, other, end = match.groups()
    if other and other != prefix:
        raise CompareError(f"'{text}': both ends need the same prefix")
    start, end = int(start), int(end)
    if start > end:
        raise CompareError(f"'{text}': the range runs backwards")
    return [f"{prefix}{n}" for n in range(start, end + 1)]


def unique_names(names):
    seen, out = {}, []
    for name in names:
        name = name.strip() or f"C{len(out) + 1}"
        count = seen.get(name, 0)
        seen[name] = count + 1
        out.append(name if not count else f"{name}_{count + 1}")
    return out


def header_row(parse):
    """The line holding the column names (1 = the first; 0 = no header)."""
    if "header_row" in parse and parse["header_row"] is not None:
        try:
            return max(0, int(parse["header_row"]))
        except (TypeError, ValueError):
            raise CompareError("Header row must be a number (0 = no header)") from None
    return 1 if parse.get("header", True) else 0


def name_list(text):
    """Column names given by hand: "SEQ, ROLL, q1..q200" (ranges allowed)."""
    names = []
    for part in re.split(r"[,\n\t]+", text or ""):
        part = part.strip()
        if part:
            names.extend(expand_range(part) if ".." in part else [part])
    return names


def parse_split_spec(text):
    """
    Columns that hold several answers, one per line:
        ANSWERS q1..q200            one character per field
        A1 q1..q10 width=2          two characters per field
        RESP q1..q50 sep=,          separated by a comma (sep=space, sep=tab)
    """
    splits = []
    for number, line in enumerate((text or "").splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        options = {}
        while len(parts) > 2 and "=" in parts[-1]:
            key, _, value = parts.pop().partition("=")
            options[key.lower()] = value
        if len(parts) < 2:
            raise CompareError(
                f"Split line {number}: write COLUMN FIELDS, e.g. 'ANSWERS q1..q200'"
            )
        column, target = " ".join(parts[:-1]), parts[-1]
        try:
            names = expand_range(target)
        except CompareError as error:
            raise CompareError(f"Split line {number}: {error}") from None
        sep = options.get("sep")
        sep = {"space": " ", "tab": "\t", "comma": ",", "semicolon": ";"}.get(sep, sep)
        try:
            width = int(options.get("width", 1))
        except ValueError:
            raise CompareError(f"Split line {number}: width must be a number") from None
        if width < 1:
            raise CompareError(f"Split line {number}: width starts at 1")
        splits.append({"column": column, "names": names, "sep": sep, "width": width})
    return splits


def apply_splits(columns, rows, splits):
    """Replace each packed column by one column per field."""
    for split in splits:
        if split["column"] not in columns:
            raise CompareError(f"Split: the file has no column '{split['column']}'")
        at = columns.index(split["column"])
        names, sep, width = split["names"], split["sep"], split["width"]

        def cells(value):
            if sep:
                parts = value.split(sep)
            else:
                value = value.ljust(len(names) * width)
                parts = [value[i * width : (i + 1) * width] for i in range(len(names))]
            return (parts + [""] * len(names))[: len(names)]

        columns = columns[:at] + names + columns[at + 1 :]
        rows = [row[:at] + cells(row[at]) + row[at + 1 :] for row in rows]
    return unique_names(columns), rows


def read_table(content, parse=None):
    """(columns, rows) of an external file; cells are kept as written (a single
    space can be the scanner's blank). parse: format (auto, csv, fixed),
    delimiter, header_row (0 = none), names (column names given by hand),
    fixed (fixed-width layout) and split (packed answer columns)."""
    parse = parse or {}
    text = decode_text(content) if isinstance(content, bytes) else content
    # A DOS end-of-file mark (Ctrl-Z) on the last line is not a record
    lines = [line.rstrip("\r\n").rstrip("\x1a") for line in text.splitlines()]
    fmt = parse.get("format") or "auto"
    if fmt == "auto":
        fmt = "fixed" if (parse.get("fixed") or "").strip() else "csv"
    header = header_row(parse)
    if fmt == "fixed":
        spec = parse_fixed_spec(parse.get("fixed"))
        body = [line for line in lines[header:] if line.strip()]
        rows = [
            [line[start : start + length].ljust(length) for _, start, length in spec]
            for line in body
        ]
        columns = [name for name, _, _ in spec]
    elif fmt == "csv":
        delimiter = parse.get("delimiter") or None
        if delimiter == "tab":
            delimiter = "\t"
        if not delimiter:
            # The header line decides (packed answers may hold other separators)
            sample = lines[header - 1] if header and len(lines) >= header else ""
            sample = sample or "\n".join(lines[header : header + 20])
            counts = {d: sample.count(d) for d in (",", ";", "\t", "|")}
            delimiter = max(counts, key=counts.get) if any(counts.values()) else ","
        reader = csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter)
        all_rows = list(reader)
        if header > len(all_rows):
            raise CompareError(f"The file has fewer than {header} lines")
        head = all_rows[header - 1] if header else []
        rows = [row for row in all_rows[header:] if any(cell.strip() for cell in row)]
        if not rows:
            raise CompareError("The file has no rows")
        width = max([len(row) for row in rows] + [len(head)])
        columns = head + [f"C{i + 1}" for i in range(len(head), width)]
        if not header:
            columns = [f"C{i + 1}" for i in range(width)]
        rows = [row + [""] * (width - len(row)) for row in rows]
    else:
        raise CompareError("format must be auto, csv or fixed")
    given = name_list(parse.get("names"))
    if given:
        if len(given) > len(columns):
            raise CompareError(
                f"{len(given)} column names given; the file has {len(columns)} columns"
            )
        columns = given + columns[len(given) :]
    columns = unique_names(columns)
    return apply_splits(columns, rows, parse_split_spec(parse.get("split")))


# ---------------------------------------------------------------------------
# our side: names, questions, keys
# ---------------------------------------------------------------------------
def custom_members(template_json):
    """{custom label: [its fields]} of a template."""
    return {
        label: [name for spec in specs for name in parse_field_string(spec)]
        for label, specs in (template_json.get("customLabels") or {}).items()
    }


def output_names(template_json):
    """The output columns in order: custom labels stand for their fields."""
    names, _, _, _ = template_names(template_json)
    members = {
        name for fields in custom_members(template_json).values() for name in fields
    }
    return [name for name in names if name not in members]


def template_names(template_json):
    """(names that can be compared, question names, zone names, imprint zone
    names) of a template. A field inside a custom label stays comparable on
    its own (a scanner writes one column per question)."""
    blocks = template_json.get("fieldBlocks") or {}
    fields, questions = [], []
    for block in blocks.values():
        values = block.get("bubbleValues") or (
            FIELD_TYPES.get(block.get("fieldType") or "", {}).get("bubbleValues") or []
        )
        letters = values and not all(str(v).isdigit() for v in values)
        for spec in block.get("fieldLabels") or []:
            for label in parse_field_string(spec):
                fields.append(label)
                if letters:
                    questions.append(label)
    custom = template_json.get("customLabels") or {}
    # Zones are an object keyed by name (a list of named zones is accepted too)
    zone_map = template_json.get("zones") or {}
    if isinstance(zone_map, list):
        zone_map = {
            z.get("name"): z for z in zone_map if isinstance(z, dict) and z.get("name")
        }
    zones = list(zone_map)
    imprint = [
        name
        for name, zone in zone_map.items()
        if isinstance(zone, dict) and zone.get("type") == "imprint"
    ]
    checks = [
        check.get("output") or check.get("name")
        for check in template_json.get("checks") or []
        if isinstance(check, dict) and (check.get("output") or check.get("name"))
    ]
    names = list(custom) + fields + zones + checks
    seen = set()
    names = [n for n in names if not (n in seen or seen.add(n))]
    return names, questions, zones, imprint


def name_key(name):
    """Q001, q-1 and Q1 all become q1."""
    text = re.sub(r"[^0-9a-z]", "", str(name).lower())
    match = re.fullmatch(r"([a-z]*?)0*(\d+)", text)
    return f"{match.group(1)}{int(match.group(2))}" if match else text


def suggest_mapping(columns, names, questions):
    """External column -> our name, for the columns whose name matches."""
    ours = {}
    for name in names + questions:
        ours.setdefault(name_key(name), name)
    prefixes = {}
    for question in questions:
        match = re.fullmatch(r"(\D*)(\d+)", question)
        if match:
            prefixes[match.group(1)] = prefixes.get(match.group(1), 0) + 1
    prefix = max(prefixes, key=prefixes.get) if prefixes else None
    mapping = {}
    for column in columns:
        key = name_key(column)
        if key in ours:
            mapping[column] = ours[key]
        elif (
            prefix is not None and key.isdigit() and f"{prefix}{int(key)}" in questions
        ):
            mapping[column] = f"{prefix}{int(key)}"
    return mapping


def suggest_key(columns, names, imprint):
    """A likely join: the scanner's sequence number against our imprint zone,
    a file name column against our file names, or a roll number."""
    lowered = {column: re.sub(r"[^a-z]", "", column.lower()) for column in columns}
    for column, low in lowered.items():
        if low in ("file", "filename", "image", "imagename", "path", "imagefile"):
            return {"column": column, "ours": "file", "normalize": "text"}
    for column, low in lowered.items():
        if low.startswith(
            ("seq", "serial", "imprint", "sheetno", "sheetnumber", "batch")
        ):
            ours = imprint[0] if imprint else "file"
            return {"column": column, "ours": ours, "normalize": "digits"}
    roll = next((n for n in names if "roll" in n.lower()), None)
    for column, low in lowered.items():
        if "roll" in low and roll:
            return {"column": column, "ours": roll, "normalize": "digits"}
    return {
        "column": columns[0] if columns else None,
        "ours": "file",
        "normalize": "digits",
    }


def key_of(value, normalize, file_like=False):
    text = str(value or "").strip()
    if file_like or (
        normalize == "text" and Path(text).suffix.lower() in IMAGE_SUFFIXES
    ):
        from src.readers.base import file_name_key

        text = file_name_key(text.replace("\\", "/"))
    if normalize == "digits":
        digits = "".join(ch for ch in text if ch.isdigit())
        return digits.lstrip("0") or ("0" if digits else "")
    if normalize == "text":
        return text.casefold()
    return text


def expand_list(text):
    names = []
    for part in re.split(r"[,\s]+", text or ""):
        if part:
            try:
                names.extend(expand_range(part))
            except CompareError as error:
                raise CompareError(f"Fields to compare: {error}") from None
    return names


# ---------------------------------------------------------------------------
# comparing one value
# ---------------------------------------------------------------------------
def squeeze(text, rules):
    if rules.get("spaces", True):
        text = re.sub(r"\s+", "", text)
    else:
        text = text.strip()
    return text if rules.get("case") else text.upper()


def external_value(raw, rules, question=False):
    """(kind, text) of a cell of the external file; a question with more than
    one option written is a multi-mark that lists its options."""
    raw = "" if raw is None else str(raw)
    stripped = raw.strip()
    multi = {str(c) for c in rules.get("multi_codes") or []}
    blank = {str(c) for c in rules.get("blank_codes") or []}
    if raw in multi or (stripped and stripped in {c.strip() for c in multi}):
        return MULTI, ""
    if (
        not stripped
        or raw in blank
        or stripped in {c.strip() for c in blank if c.strip()}
    ):
        return BLANK, ""
    text = squeeze(raw, rules)
    if question and len(text) > 1:
        return MULTI, "".join(sorted(text))
    return VALUE, text


def our_value(value, empty, question, multi_flag, rules):
    """(kind, text) of our value: blank when empty or the field's empty value;
    a question with more than one option marked, or a value that is a
    multi-mark code (a group's "*"), is a multi-mark."""
    text = "" if value is None else str(value)
    if not text.strip() or (empty not in (None, "") and text == empty):
        return BLANK, ""
    if text.strip() in {
        str(c).strip() for c in rules.get("multi_codes") or [] if str(c).strip()
    }:
        return MULTI, ""
    text = squeeze(text, rules)
    if multi_flag or (question and len(text) > 1):
        return MULTI, "".join(sorted(text))
    return VALUE, text


def parse_bands(text):
    """ "1: q1..q50; 2: q51..q100" -> {"1": {"q1", ..., "q50"}, "2": {...}}"""
    bands = {}
    for number, entry in enumerate(re.split(r"[;\n]+", text or ""), 1):
        entry = entry.strip()
        if not entry:
            continue
        value, sep, names = entry.partition(":")
        if not sep:
            value, sep, names = entry.partition("=")
        if not sep or not value.strip():
            raise CompareError(
                f"Level bands, entry {number}: write LEVEL: FIELDS, e.g. '1: q1..q50'"
            )
        bands[value.strip().upper()] = set(expand_list(names))
    return bands


def sheet_band(rules, bands, theirs, ours):
    """The questions this sheet answers, or None for all of them."""
    if not bands:
        return None
    source = rules.get("band_source") or "theirs_then_ours"

    def band_of(value):
        key = "" if value is None else str(value).strip().upper()
        return bands.get(key) if key else None

    if source == "theirs_then_ours":
        # Their level, or ours where theirs is blank or a multi-mark
        chosen = [band_of(theirs) or band_of(ours)]
    else:
        values = {"theirs": [theirs], "ours": [ours], "either": [theirs, ours]}.get(
            source, [theirs]
        )
        chosen = [band_of(value) for value in values]
    chosen = [band for band in chosen if band]
    if not chosen:
        return None  # level blank or unknown on both sides: every question
    return set().union(*chosen)


def differs(external, ours, rules):
    """True for a mismatch, False when they agree, None when the rules skip it."""
    (ek, et), (ok, ot) = external, ours
    if BLANK in (ek, ok) and ek != ok and rules.get("blank") == "ignore":
        return None
    if MULTI in (ek, ok):
        if rules.get("multi") == "ignore":
            return None
        if ek == ok == MULTI:
            if rules.get("multi") == "exact" and et and ot:
                return et != ot
            return False
        return True
    return (ek, et) != (ok, ot)


def shown(raw, kind):
    """A value as the reviewer sees it and as the export writes it."""
    if kind == BLANK:
        return ""
    return str(raw).strip() if kind == VALUE else (str(raw).strip() or "*")


# ---------------------------------------------------------------------------
# the reconciled layer
# ---------------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    name TEXT,
    template_id TEXT,
    job_id TEXT,
    source_name TEXT,
    settings TEXT,
    summary TEXT,
    created_by TEXT,
    created_at REAL,
    updated_at REAL
);
-- Every sheet in scope: matched (with its external row) or not
CREATE TABLE IF NOT EXISTS sheets (
    session_id TEXT NOT NULL,
    scan_id TEXT NOT NULL,
    ord INTEGER,
    key TEXT,
    file_name TEXT,
    row_no INTEGER,
    mismatches INTEGER DEFAULT 0,
    -- clean, pending, partial, done; not compared: no_record, not_read,
    -- replaced (an older scan of a file read again), duplicate
    state TEXT,
    locked_by TEXT,
    locked_at REAL,
    done_by TEXT,
    done_at REAL,
    PRIMARY KEY (session_id, scan_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_cmp_sheets_state ON sheets(session_id, state, ord);
CREATE INDEX IF NOT EXISTS ix_cmp_sheets_ord ON sheets(session_id, ord);
-- External rows that matched no sheet
CREATE TABLE IF NOT EXISTS unmatched (
    session_id TEXT NOT NULL,
    row_no INTEGER NOT NULL,
    key TEXT,
    reason TEXT,
    PRIMARY KEY (session_id, row_no)
) WITHOUT ROWID;
-- One row per mismatch; chosen/source once a reviewer decided
CREATE TABLE IF NOT EXISTS items (
    session_id TEXT NOT NULL,
    scan_id TEXT NOT NULL,
    name TEXT NOT NULL,
    pos INTEGER,
    external TEXT,
    ours TEXT,
    chosen TEXT,
    source TEXT,
    decided_by TEXT,
    decided_at REAL,
    PRIMARY KEY (session_id, scan_id, name)
) WITHOUT ROWID;
-- Every decision (who, when, both values, which one was kept)
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    scan_id TEXT,
    name TEXT,
    external TEXT,
    ours TEXT,
    chosen TEXT,
    source TEXT,
    user TEXT,
    action TEXT,
    at REAL
);
CREATE INDEX IF NOT EXISTS ix_cmp_audit ON audit(session_id, id);
-- Our key values (an imprint or roll number) by result file version
CREATE TABLE IF NOT EXISTS key_cache (
    scan_id TEXT NOT NULL,
    name TEXT NOT NULL,
    mtime REAL,
    value TEXT,
    PRIMARY KEY (scan_id, name)
) WITHOUT ROWID;
-- Saved layouts (how a scanner's file is read and mapped), reused for the next file
CREATE TABLE IF NOT EXISTS profiles (
    name TEXT PRIMARY KEY,
    template_id TEXT,
    settings TEXT,
    saved_by TEXT,
    saved_at REAL
);
"""


def chunked(values, size=500):
    for start in range(0, len(values), size):
        yield values[start : start + size]


class CompareStore:
    """Thread-safe SQLite store of comparisons and their decisions."""

    def __init__(self, path):
        self.conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.executescript(_SCHEMA)
            self.conn.commit()

    def query(self, sql, params=()):
        with self.lock:
            return [dict(row) for row in self.conn.execute(sql, params)]

    def execute(self, sql, params=()):
        with self.lock:
            cursor = self.conn.execute(sql, params)
            self.conn.commit()
            return cursor.rowcount

    # ---- sessions ---------------------------------------------------------
    def session(self, session_id):
        rows = self.query("SELECT * FROM sessions WHERE id=?", (session_id,))
        if not rows:
            raise CompareError("Comparison not found", 404)
        row = rows[0]
        row["settings"] = json.loads(row["settings"] or "{}")
        row["summary"] = json.loads(row["summary"] or "{}")
        return row

    def sessions(self, template_id=None):
        sql, params = "SELECT * FROM sessions", ()
        if template_id:
            sql, params = sql + " WHERE template_id=?", (template_id,)
        rows = self.query(sql + " ORDER BY created_at DESC", params)
        for row in rows:
            row["settings"] = json.loads(row["settings"] or "{}")
            row["summary"] = json.loads(row["summary"] or "{}")
            row["progress"] = self.progress(row["id"])
        return rows

    def add_session(self, row):
        with self.lock:
            self.conn.execute(
                "INSERT INTO sessions (id, name, template_id, job_id, source_name, "
                "settings, summary, created_by, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    row["id"],
                    row["name"],
                    row["template_id"],
                    row.get("job_id"),
                    row.get("source_name"),
                    json.dumps(row["settings"]),
                    json.dumps(row.get("summary") or {}),
                    row.get("created_by"),
                    row["created_at"],
                    row["created_at"],
                ),
            )
            self.conn.commit()

    def update_session(self, session_id, **values):
        sets, params = [], []
        for column, value in values.items():
            sets.append(f"{column}=?")
            params.append(
                json.dumps(value) if column in ("settings", "summary") else value
            )
        sets.append("updated_at=?")
        params.append(time.time())
        self.execute(
            f"UPDATE sessions SET {', '.join(sets)} WHERE id=?", (*params, session_id)
        )

    def delete_session(self, session_id):
        with self.lock:
            for table in ("sheets", "unmatched", "items", "audit"):
                self.conn.execute(
                    f"DELETE FROM {table} WHERE session_id=?", (session_id,)
                )
            self.conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
            self.conn.commit()

    def progress(self, session_id):
        counts = {
            row["state"]: row["n"]
            for row in self.query(
                "SELECT state, COUNT(*) AS n FROM sheets WHERE session_id=? GROUP BY state",
                (session_id,),
            )
        }
        items = self.query(
            "SELECT COUNT(*) AS n, SUM(source IS NOT NULL) AS decided, "
            "SUM(source='external') AS external FROM items WHERE session_id=?",
            (session_id,),
        )[0]
        unmatched = self.query(
            "SELECT COUNT(*) AS n FROM unmatched WHERE session_id=?", (session_id,)
        )[0]["n"]
        return {
            "sheets": counts,
            "to_review": sum(counts.get(s, 0) for s in SESSION_STATES),
            "open": sum(counts.get(s, 0) for s in OPEN_STATES),
            "mismatches": items["n"] or 0,
            "decided": items["decided"] or 0,
            "chose_external": items["external"] or 0,
            "unmatched_external": unmatched,
        }

    # ---- profiles ---------------------------------------------------------
    def profiles(self):
        rows = self.query("SELECT * FROM profiles ORDER BY name COLLATE NOCASE")
        for row in rows:
            row["settings"] = json.loads(row["settings"] or "{}")
        return rows

    def save_profile(self, name, template_id, settings, user):
        self.execute(
            "INSERT OR REPLACE INTO profiles VALUES (?,?,?,?,?)",
            (name, template_id, json.dumps(settings), user, time.time()),
        )

    def delete_profile(self, name):
        return self.execute("DELETE FROM profiles WHERE name=?", (name,))

    # ---- building ---------------------------------------------------------
    def write_sheets(self, session_id, sheets, items):
        """
        Write (re)compared sheets and their mismatches, a part of the
        comparison at a time. A decision stays when both its values are
        unchanged (read when written, so a choice saved meanwhile counts), and
        so does who holds a sheet.
        """
        if not sheets:
            return
        ids = [sheet["scan_id"] for sheet in sheets]
        with self.lock:
            old_sheets, keep = {}, {}
            for part in chunked(ids):
                marks = ",".join("?" * len(part))
                for row in self.conn.execute(
                    "SELECT scan_id, locked_by, locked_at, done_by, done_at FROM sheets "
                    f"WHERE session_id=? AND scan_id IN ({marks})",
                    (session_id, *part),
                ):
                    old_sheets[row["scan_id"]] = dict(row)
                for row in self.conn.execute(
                    "SELECT * FROM items WHERE session_id=? AND source IS NOT NULL "
                    f"AND scan_id IN ({marks})",
                    (session_id, *part),
                ):
                    keep[(row["scan_id"], row["name"])] = dict(row)
            decided = {}
            item_rows = []
            for item in items:
                previous = keep.get((item["scan_id"], item["name"]))
                if previous and (previous["external"], previous["ours"]) == (
                    item["external"],
                    item["ours"],
                ):
                    item.update(
                        {
                            k: previous[k]
                            for k in ("chosen", "source", "decided_by", "decided_at")
                        }
                    )
                    decided[item["scan_id"]] = decided.get(item["scan_id"], 0) + 1
                item_rows.append(
                    (
                        session_id,
                        item["scan_id"],
                        item["name"],
                        item["pos"],
                        item["external"],
                        item["ours"],
                        item.get("chosen"),
                        item.get("source"),
                        item.get("decided_by"),
                        item.get("decided_at"),
                    )
                )
            sheet_rows = []
            for sheet in sheets:
                state = sheet["state"]
                old = old_sheets.get(sheet["scan_id"]) or {}
                done_by = done_at = None
                if state == "pending":
                    count = decided.get(sheet["scan_id"], 0)
                    if count and count >= sheet["mismatches"]:
                        state = "done"
                        done_by, done_at = old.get("done_by"), old.get("done_at")
                    elif count:
                        state = "partial"
                held = state in SESSION_STATES
                sheet_rows.append(
                    (
                        session_id,
                        sheet["scan_id"],
                        sheet["ord"],
                        sheet["key"],
                        sheet["file_name"],
                        sheet.get("row_no"),
                        sheet["mismatches"],
                        state,
                        old.get("locked_by") if held else None,
                        old.get("locked_at") if held else None,
                        done_by,
                        done_at,
                    )
                )
            for part in chunked(ids):
                marks = ",".join("?" * len(part))
                for table in ("sheets", "items"):
                    self.conn.execute(
                        f"DELETE FROM {table} WHERE session_id=? AND scan_id IN ({marks})",
                        (session_id, *part),
                    )
            self.conn.executemany(
                "INSERT INTO sheets (session_id, scan_id, ord, key, file_name, row_no, "
                "mismatches, state, locked_by, locked_at, done_by, done_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                sheet_rows,
            )
            self.conn.executemany(
                "INSERT INTO items VALUES (?,?,?,?,?,?,?,?,?,?)", item_rows
            )
            self.conn.commit()

    def finish_build(self, session_id, scan_ids, unmatched):
        """The last step of a comparison: sheets no longer in scope go, and
        their rows that matched no sheet are written."""
        wanted = set(scan_ids)
        with self.lock:
            gone = [
                row["scan_id"]
                for row in self.conn.execute(
                    "SELECT scan_id FROM sheets WHERE session_id=?", (session_id,)
                )
                if row["scan_id"] not in wanted
            ]
            for part in chunked(gone):
                marks = ",".join("?" * len(part))
                for table in ("sheets", "items"):
                    self.conn.execute(
                        f"DELETE FROM {table} WHERE session_id=? AND scan_id IN ({marks})",
                        (session_id, *part),
                    )
            self.conn.execute("DELETE FROM unmatched WHERE session_id=?", (session_id,))
            self.conn.executemany(
                "INSERT INTO unmatched VALUES (?,?,?,?)",
                [(session_id, row, key, reason) for row, key, reason in unmatched],
            )
            self.conn.commit()

    def replace_rows(self, session_id, sheets, items, unmatched):
        self.write_sheets(session_id, sheets, items)
        self.finish_build(session_id, [sheet["scan_id"] for sheet in sheets], unmatched)

    def decided_items(self, session_id):
        return {
            (row["scan_id"], row["name"]): row
            for row in self.query(
                "SELECT * FROM items WHERE session_id=? AND source IS NOT NULL",
                (session_id,),
            )
        }

    # ---- reviewing --------------------------------------------------------
    def sheet(self, session_id, scan_id):
        rows = self.query(
            "SELECT * FROM sheets WHERE session_id=? AND scan_id=?",
            (session_id, scan_id),
        )
        if not rows:
            raise CompareError("This sheet is not part of the comparison", 404)
        return rows[0]

    def items(self, session_id, scan_id):
        return self.query(
            "SELECT * FROM items WHERE session_id=? AND scan_id=? ORDER BY pos",
            (session_id, scan_id),
        )

    def claim(
        self, session_id, user, after=None, direction="next", scan_id=None, now=None
    ):
        """
        Lock the next sheet to review for user (and release their other locks).
        next: the next open sheet after ord `after` (wrapping round); prev: the
        sheet before it, decided or not; scan_id: that sheet. A sheet another
        reviewer holds is passed over (scan_id: refused). None when nothing is left.
        """
        now = now or time.time()
        stale = now - LOCK_SECONDS
        free = "(locked_by IS NULL OR locked_by=? OR locked_at < ?)"
        with self.lock:
            if scan_id:
                rows = self.conn.execute(
                    f"SELECT * FROM sheets WHERE session_id=? AND scan_id=? AND state IN "
                    f"('pending','partial','done')",
                    (session_id, scan_id),
                ).fetchall()
                if rows and not (
                    rows[0]["locked_by"] in (None, user)
                    or (rows[0]["locked_at"] or 0) < stale
                ):
                    raise CompareError(
                        f"{rows[0]['locked_by']} is working on this sheet", 409
                    )
            elif direction == "prev":
                rows = self.conn.execute(
                    f"SELECT * FROM sheets WHERE session_id=? AND state IN "
                    f"('pending','partial','done') AND ord < ? AND {free} "
                    "ORDER BY ord DESC LIMIT 1",
                    (session_id, after if after is not None else 1 << 62, user, stale),
                ).fetchall()
            else:
                query = (
                    f"SELECT * FROM sheets WHERE session_id=? AND state IN ('pending','partial') "
                    f"AND ord > ? AND {free} ORDER BY ord LIMIT 1"
                )
                start = after if after is not None else -1
                rows = self.conn.execute(
                    query, (session_id, start, user, stale)
                ).fetchall()
                if not rows and start >= 0:
                    rows = self.conn.execute(
                        query, (session_id, -1, user, stale)
                    ).fetchall()
            self.conn.execute(
                "UPDATE sheets SET locked_by=NULL, locked_at=NULL "
                "WHERE session_id=? AND locked_by=?",
                (session_id, user),
            )
            if not rows:
                self.conn.commit()
                return None
            row = dict(rows[0])
            self.conn.execute(
                "UPDATE sheets SET locked_by=?, locked_at=? WHERE session_id=? AND scan_id=?",
                (user, now, session_id, row["scan_id"]),
            )
            self.conn.commit()
        row.update(locked_by=user, locked_at=now)
        return row

    def release(self, session_id, scan_id, user):
        """Let go of a sheet this user holds; False when they do not hold it."""
        return bool(
            self.execute(
                "UPDATE sheets SET locked_by=NULL, locked_at=NULL "
                "WHERE session_id=? AND scan_id=? AND locked_by=?",
                (session_id, scan_id, user),
            )
        )

    def cached_keys(self, name):
        return {
            row["scan_id"]: (row["mtime"], row["value"])
            for row in self.query("SELECT * FROM key_cache WHERE name=?", (name,))
        }

    def cache_keys(self, name, rows):
        with self.lock:
            self.conn.executemany(
                "INSERT OR REPLACE INTO key_cache VALUES (?,?,?,?)",
                [(scan_id, name, mtime, value) for scan_id, mtime, value in rows],
            )
            self.conn.commit()

    def decide(self, session_id, scan_id, user, decisions, now=None):
        """
        Save a reviewer's choices for one sheet: decisions {name: "external" |
        "ours" | None (undo) | {"source": "manual", "value": "..."}}. Refused while another reviewer holds the sheet.
        Returns the sheet's new state.
        """
        now = now or time.time()
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM sheets WHERE session_id=? AND scan_id=?",
                (session_id, scan_id),
            ).fetchall()
            if not rows or rows[0]["state"] not in SESSION_STATES:
                raise CompareError("This sheet has nothing to reconcile", 404)
            sheet = rows[0]
            if (
                sheet["locked_by"] not in (None, user)
                and (sheet["locked_at"] or 0) >= now - LOCK_SECONDS
            ):
                raise CompareError(
                    f"{sheet['locked_by']} is working on this sheet", 409
                )
            items = {
                row["name"]: dict(row)
                for row in self.conn.execute(
                    "SELECT * FROM items WHERE session_id=? AND scan_id=?",
                    (session_id, scan_id),
                )
            }
            unknown = [name for name in decisions if name not in items]
            if unknown:
                raise CompareError(
                    f"Not a mismatch on this sheet: {', '.join(unknown)}", 422
                )
            audit = []
            for name, decision in decisions.items():
                item = items[name]
                # "external", "ours", None (undo) or {"source": "manual", "value": ...}
                if isinstance(decision, dict) and decision.get("source") == "manual":
                    source, chosen = "manual", str(decision.get("value") or "")
                    if item["source"] == source and item["chosen"] == chosen:
                        continue
                elif decision in ("external", "ours", None):
                    source = decision
                    if item["source"] == source:
                        continue
                    chosen = None if source is None else item[source]
                else:
                    raise CompareError("Pick 'external', 'ours' or a typed value", 422)
                self.conn.execute(
                    "UPDATE items SET chosen=?, source=?, decided_by=?, decided_at=? "
                    "WHERE session_id=? AND scan_id=? AND name=?",
                    (
                        chosen,
                        source,
                        user if source else None,
                        now if source else None,
                        session_id,
                        scan_id,
                        name,
                    ),
                )
                item["source"] = source
                audit.append(
                    (
                        session_id,
                        scan_id,
                        name,
                        item["external"],
                        item["ours"],
                        chosen,
                        source,
                        user,
                        "decide" if source else "undo",
                        now,
                    )
                )
            decided = sum(1 for item in items.values() if item["source"])
            state = (
                "done"
                if decided >= len(items)
                else ("partial" if decided else "pending")
            )
            self.conn.executemany(
                "INSERT INTO audit (session_id, scan_id, name, external, ours, chosen, "
                "source, user, action, at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                audit,
            )
            self.conn.execute(
                "UPDATE sheets SET state=?, done_by=?, done_at=?, locked_by=NULL, locked_at=NULL "
                "WHERE session_id=? AND scan_id=?",
                (
                    state,
                    user if state == "done" else None,
                    now if state == "done" else None,
                    session_id,
                    scan_id,
                ),
            )
            self.conn.commit()
        return state

    def iter_sheets(self, session_id):
        return self.query(
            "SELECT * FROM sheets WHERE session_id=? ORDER BY ord", (session_id,)
        )

    def all_items(self, session_id):
        out = {}
        for row in self.query("SELECT * FROM items WHERE session_id=?", (session_id,)):
            out.setdefault(row["scan_id"], []).append(row)
        return out

    def audit(self, session_id):
        return self.query(
            "SELECT * FROM audit WHERE session_id=? ORDER BY id", (session_id,)
        )

    def unmatched(self, session_id):
        return self.query(
            "SELECT * FROM unmatched WHERE session_id=? ORDER BY row_no", (session_id,)
        )


# ---------------------------------------------------------------------------
# building a comparison
# ---------------------------------------------------------------------------
def compared_names(mapping, rules, questions):
    """The (external column, our name) pairs the rules compare."""
    pairs = [(column, name) for column, name in mapping.items() if name]
    mode = rules.get("fields") or "all"
    if mode == "questions":
        keep = set(questions)
        pairs = [pair for pair in pairs if pair[1] in keep]
    elif mode == "list":
        keep = set(expand_list(rules.get("list")))
        pairs = [pair for pair in pairs if pair[1] in keep]
    return pairs


def our_key(row, result, ours, normalize):
    if ours == "file":
        return key_of(
            row["file_name"] or (result or {}).get("file_id"), normalize, True
        )
    from src.api.review import current_value

    return key_of(current_value(result or {}, ours), normalize)


def prepare(session, content, template_json):
    """
    Read their file with the session's settings and check the mapping and key:
    everything a comparison needs before it looks at our sheets. Raises
    CompareError for a setting that cannot work (quickly, before any sheet).
    """
    settings = session["settings"]
    rules = {**DEFAULT_RULES, **(settings.get("rules") or {})}
    key = settings.get("key") or {}
    mapping = settings.get("mapping") or {}
    columns, rows = read_table(content, settings.get("parse"))
    names, questions, _, _ = template_names(template_json)
    pairs = compared_names(mapping, rules, questions)
    # A custom label whose fields are compared one by one is not compared again
    mapped = {name for _, name in pairs}
    members = custom_members(template_json)
    pairs = [
        (column, name)
        for column, name in pairs
        if not mapped.intersection(members.get(name) or [])
    ]
    missing = [column for column, _ in pairs if column not in columns]
    if missing:
        raise CompareError(f"The file has no column {', '.join(missing[:5])}", 422)
    if not pairs:
        raise CompareError("Map at least one external column to one of our fields", 422)
    index = {column: i for i, column in enumerate(columns)}
    ours_source = key.get("ours") or "file"
    normalize = key.get("normalize") or "digits"
    by_order = ours_source == "order"
    if not by_order and key.get("column") not in index:
        raise CompareError("Pick the external column that identifies each sheet", 422)
    band_field = (rules.get("band_field") or "").strip()
    bands = parse_bands(rules.get("band_map")) if band_field else {}
    band_column = next(
        (column for column, name in mapping.items() if name == band_field), None
    )
    if band_field and band_column is not None and band_column not in index:
        band_column = None

    external, unmatched = {}, []
    if by_order:
        external = {str(i): (i, row) for i, row in enumerate(rows)}
    else:
        counts = {}
        for i, row in enumerate(rows):
            value = key_of(row[index[key["column"]]], normalize)
            counts[value] = counts.get(value, 0) + 1
            external[value] = (i, row)
        for value, count in counts.items():
            if not value or count > 1:
                external.pop(value, None)
        for i, row in enumerate(rows):
            value = key_of(row[index[key["column"]]], normalize)
            if not value:
                unmatched.append((i + 1, value, "no key"))
            elif counts[value] > 1:
                unmatched.append((i + 1, value, "duplicate key"))
    return {
        "rules": rules,
        "rows": len(rows),
        "pairs": pairs,
        "index": index,
        "ours": ours_source,
        "normalize": normalize,
        "by_order": by_order,
        "questions": set(questions),
        "band_field": band_field,
        "bands": bands,
        "band_column": band_column,
        "external": external,
        "unmatched": unmatched,
    }


def sheet_keys(plan, scans, load_result, key_values=None):
    """[(ord, scan row, our key)] in order. Several of our scans with one key
    (the same file read in several jobs): the newest readable one is compared
    and the others are 'replaced' (rules duplicates "newest"), or none of them
    is ('duplicate', rules duplicates "skip")."""
    out = []
    for ord_, row in enumerate(scans):
        if plan["by_order"]:
            value = str(ord_)
        elif plan["ours"] == "file":
            value = our_key(row, None, "file", plan["normalize"])
        elif key_values is not None and row["id"] in key_values:
            value = key_of(key_values[row["id"]], plan["normalize"])
        else:
            value = our_key(
                row, load_result(row["id"]), plan["ours"], plan["normalize"]
            )
        out.append((ord_, row, value))
    groups = {}
    for ord_, row, value in out:
        if value:
            groups.setdefault(value, []).append(row)
    chosen = {}
    newest = (plan["rules"].get("duplicates") or "newest") == "newest"
    for value, rows in groups.items():
        if len(rows) == 1:
            chosen[value] = rows[0]["id"]
        elif newest:
            best = max(
                rows,
                key=lambda r: (r.get("status") != "error", r.get("created_at") or 0),
            )
            chosen[value] = best["id"]
    plan["chosen"] = chosen
    return out


def compare_sheet(plan, ord_, row, value, load_result, empty_values):
    """One of our sheets against its row: (sheet, mismatch items)."""
    sheet = {
        "scan_id": row["id"],
        "ord": ord_,
        "key": value,
        "file_name": row.get("file_name"),
        "mismatches": 0,
    }
    if value and value in plan["chosen"] and plan["chosen"][value] != row["id"]:
        sheet["state"] = "replaced"
        return sheet, []
    if row.get("status") == "error":
        sheet["state"] = "not_read"
        return sheet, []
    if not value or value not in plan["external"]:
        sheet["state"] = "no_record"
        return sheet, []
    if value not in plan["chosen"]:
        sheet["state"] = "duplicate"
        return sheet, []
    rules, index, questions = plan["rules"], plan["index"], plan["questions"]
    row_no, ext_row = plan["external"][value]
    sheet["row_no"] = row_no + 1
    result = load_result(row["id"]) or {}
    band = None
    if plan["bands"]:
        from src.api.review import current_value

        column = plan["band_column"]
        theirs = ext_row[index[column]] if column is not None else None
        band = sheet_band(
            rules, plan["bands"], theirs, current_value(result, plan["band_field"])
        )
    items = []
    for column, name in plan["pairs"]:
        if band is not None and name in questions and name not in band:
            continue  # a question outside this sheet's level
        raw = ext_row[index[column]]
        mine, multi_flag, empty = our_side(result, name, empty_values)
        ext_norm = external_value(raw, rules, name in questions)
        our_norm = our_value(mine, empty, name in questions, multi_flag, rules)
        if differs(ext_norm, our_norm, rules):
            items.append(
                {
                    "scan_id": row["id"],
                    "name": name,
                    "pos": len(items),
                    "external": shown(raw, ext_norm[0]),
                    "ours": shown(mine, our_norm[0]),
                }
            )
    sheet["mismatches"] = len(items)
    sheet["state"] = "pending" if items else "clean"
    return sheet, items


def run(
    store,
    session,
    plan,
    scans,
    load_result,
    empty_values,
    key_values=None,
    progress=None,
    current=None,
    chunk=200,
):
    """
    Compare every sheet and write the reconciled layer a part at a time, so
    reviewers can start on the first sheets while the rest is compared.
    progress(done, total, summary): after each part; current(): False once a
    newer comparison of this session replaced this one (it stops). Returns the
    summary, or None when stopped.
    """
    keyed = sheet_keys(plan, scans, load_result, key_values)
    states, used, scan_ids = {}, set(), []
    sheets, items, mismatches = [], [], 0
    started = time.time()

    def summary(done):
        return {
            "external_rows": plan["rows"],
            "sheets": len(keyed),
            "compared": done,
            "matched": sum(states.get(s, 0) for s in ("clean", "pending")),
            "states": dict(states),
            "unmatched_external": None,
            "compared_fields": len(plan["pairs"]),
            "mismatches": mismatches,
        }

    def flush(done):
        if current is not None and not current():
            return False
        store.write_sheets(session["id"], sheets, items)
        sheets.clear()
        items.clear()
        if progress is not None:
            progress(done, len(keyed), summary(done))
        return True

    for done, (ord_, row, value) in enumerate(keyed, 1):
        sheet, found = compare_sheet(plan, ord_, row, value, load_result, empty_values)
        states[sheet["state"]] = states.get(sheet["state"], 0) + 1
        if sheet.get("row_no"):
            used.add(value)
        scan_ids.append(sheet["scan_id"])
        sheets.append(sheet)
        items.extend(found)
        mismatches += len(found)
        if len(sheets) >= chunk and not flush(done):
            return None
    if not flush(len(keyed)):
        return None
    unmatched = list(plan["unmatched"])
    for value, (row_no, _) in plan["external"].items():
        if value not in used:
            unmatched.append((row_no + 1, value, "no sheet"))
    unmatched.sort()
    store.finish_build(session["id"], scan_ids, unmatched)
    result = {
        **summary(len(keyed)),
        "unmatched_external": len(unmatched),
        "built_at": time.time(),
        "seconds": round(time.time() - started, 1),
    }
    store.update_session(session["id"], summary=result)
    return result


def build(
    store,
    session,
    content,
    scans,
    load_result,
    template_json,
    empty_values,
    key_values=None,
):
    """
    (Re)compute a comparison in one go: join the external rows to our sheets,
    compare the mapped fields and write the reconciled layer, keeping every
    decision whose two values are unchanged. scans: our sheets in order (rows
    with id, file_name, status, created_at); load_result(scan_id) -> result
    dict; key_values: {scan_id: our raw key value} when the key is one of our
    fields (read beforehand, cached). Returns the summary.
    """
    plan = prepare(session, content, template_json)
    return run(store, session, plan, scans, load_result, empty_values, key_values)


def our_side(result, name, empty_values):
    """(value, multi-marked?, empty value) of one of our names."""
    from src.api.review import current_value

    value = current_value(result, name)
    field = (result.get("fields") or {}).get(name) or {}
    multi = "multi_marked" in (field.get("flags") or [])
    return value, multi, (empty_values or {}).get(name, "")


def new_session_id():
    return new_id()[:12]


def load_source(directory):
    for path in sorted(Path(directory).glob("source.*")):
        return path.read_bytes()
    raise CompareError("The external file of this comparison is missing", 410)
