"""
On-disk storage for the API.

    <data_dir>/
        templates/<template_id>/template.json (+ assets, config.json, evaluation.json)
        template_versions/<template_id>/<hash>/  every template version scans were read with
        scans/<id[:2]>/<scan_id>/result.json, aligned.png, marked.jpg, input.<ext>
        jobs/<job_id>.json, jobs/<job_id>.files, jobs/<job_id>/inputs/...
        exports/<export_id>.json + the exported file
        settings.json                      runtime settings edited from the GUI
        training/labels.jsonl, training/crops/..., training/bubbles/...
        index.sqlite3

result.json files are the source of truth. The SQLite index only exists so that
listing, filtering and the review queue stay fast with millions of scans; it can
be rebuilt from disk at any time (ScanIndex.rebuild).
"""

import json
import os
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path

SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def new_id():
    return uuid.uuid4().hex


def safe_filename(name, default="file"):
    """Strip directories and unsafe characters from an uploaded file name."""
    name = Path(str(name or "").replace("\\", "/")).name
    name = SAFE_NAME_RE.sub("_", name).strip("._")
    return name[:150] or default


def slugify(name, default="template"):
    slug = SAFE_NAME_RE.sub("-", str(name or "").strip()).strip("-._").lower()
    return slug[:60] or default


def write_json_atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, indent=1, default=_json_default))
    os.replace(tmp, path)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _json_default(value):
    try:
        import numpy as np

        if isinstance(value, np.generic):
            return value.item()
        if isinstance(value, np.ndarray):
            return value.tolist()
    except ImportError:  # pragma: no cover
        pass
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Not JSON serializable: {type(value)}")


class DataDir:
    def __init__(self, root):
        self.root = Path(root)
        self.templates = self.root / "templates"
        self.scans = self.root / "scans"
        self.jobs = self.root / "jobs"
        self.training = self.root / "training"
        self.uploads = self.root / "uploads"
        self.template_versions = self.root / "template_versions"
        self.exports = self.root / "exports"
        self.settings_file = self.root / "settings.json"
        for path in (
            self.templates,
            self.scans,
            self.jobs,
            self.training,
            self.uploads,
            self.template_versions,
            self.exports,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def scan_dir(self, scan_id):
        return scan_dir_for(self.scans, scan_id)

    def template_dir(self, template_id):
        return self.templates / template_id

    def job_file(self, job_id):
        return self.jobs / f"{job_id}.json"


def scan_dir_for(scans_root, scan_id):
    # Shard by the first two hex characters so no directory holds millions of entries
    return Path(scans_root) / scan_id[:2] / scan_id


_SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    id TEXT PRIMARY KEY,
    template_id TEXT,
    job_id TEXT,
    seq INTEGER,
    file_name TEXT,
    status TEXT,
    score REAL,
    review_count INTEGER DEFAULT 0,
    reviewed INTEGER DEFAULT 0,
    has_images INTEGER DEFAULT 0,
    error TEXT,
    created_at REAL
);
CREATE INDEX IF NOT EXISTS ix_scans_status ON scans(status, template_id);
CREATE INDEX IF NOT EXISTS ix_scans_job ON scans(job_id, seq);
CREATE INDEX IF NOT EXISTS ix_scans_template ON scans(template_id, created_at);
CREATE TABLE IF NOT EXISTS review_items (
    scan_id TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT,
    template_id TEXT,
    job_id TEXT,
    state TEXT DEFAULT 'pending',
    created_at REAL,
    PRIMARY KEY (scan_id, name)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_review_pending ON review_items(state, template_id, job_id);
CREATE INDEX IF NOT EXISTS ix_review_name ON review_items(state, name);
-- One row per flag of a field/zone/check as originally read (flagged items only)
CREATE TABLE IF NOT EXISTS scan_flags (
    scan_id TEXT NOT NULL,
    name TEXT NOT NULL,
    flag TEXT NOT NULL,
    template_id TEXT,
    job_id TEXT,
    PRIMARY KEY (scan_id, name, flag)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_flags_flag ON scan_flags(flag, template_id, job_id);
CREATE INDEX IF NOT EXISTS ix_flags_name ON scan_flags(name, template_id, job_id);
-- Every manual change of a value (audit trail; result.json keeps a copy)
CREATE TABLE IF NOT EXISTS corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT,
    template_id TEXT,
    job_id TEXT,
    old_value TEXT,
    new_value TEXT,
    original_value TEXT,
    user TEXT,
    source TEXT,
    at REAL
);
CREATE INDEX IF NOT EXISTS ix_corrections_scan ON corrections(scan_id, id);
CREATE INDEX IF NOT EXISTS ix_corrections_at ON corrections(at);
-- Per field outcome of fully verified sheets: drives the accuracy readout
CREATE TABLE IF NOT EXISTS field_outcomes (
    scan_id TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT,
    template_id TEXT,
    job_id TEXT,
    flagged INTEGER,
    corrected INTEGER,
    PRIMARY KEY (scan_id, name)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_outcomes_template ON field_outcomes(template_id, job_id, name);
"""

# Columns added after the first release; created on open when missing
_MIGRATIONS = {
    "flag_count": "INTEGER DEFAULT 0",
    "verified": "INTEGER DEFAULT 0",
    "corrected": "INTEGER DEFAULT 0",
    "template_version": "TEXT",
    "primary_key": "TEXT",
}
_LATE_INDEXES = """
CREATE INDEX IF NOT EXISTS ix_scans_key ON scans(job_id, primary_key);
CREATE INDEX IF NOT EXISTS ix_scans_template_key ON scans(template_id, primary_key);
CREATE INDEX IF NOT EXISTS ix_scans_flagged ON scans(template_id, job_id, flag_count);
CREATE INDEX IF NOT EXISTS ix_scans_verified ON scans(template_id, job_id, verified);
"""

SCAN_COLUMNS = [
    "id",
    "template_id",
    "job_id",
    "seq",
    "file_name",
    "status",
    "score",
    "review_count",
    "reviewed",
    "has_images",
    "error",
    "created_at",
    "flag_count",
    "verified",
    "corrected",
    "template_version",
    "primary_key",
]


def flag_rows(record):
    """(name, flag) pairs of everything flagged when the sheet was read."""
    pairs = set()
    review = record.get("read_review")
    if review is None:
        review = record.get("review") or []
    for item in review:
        for flag in item.get("flags") or ["needs_review"]:
            pairs.add((item["name"], flag))
    for name, flags in (record.get("check_flags") or {}).items():
        for flag in flags or []:
            pairs.add((name, flag))
    for name, check in (record.get("checks") or {}).items():
        if isinstance(check, dict):
            for flag in check.get("flags") or []:
                pairs.add((name, flag))
    return sorted(pairs)


def primary_key_of(record):
    """The sheet's joined primary key, or None when it is unset, blank or
    still flagged (an unreadable key is already in review; counting it would
    make every blank roll number a duplicate)."""
    spec = record.get("key_fields") or {}
    columns = spec.get("columns") or []
    if not columns or record.get("status") == "error":
        return None
    responses = record.get("responses") or {}
    values = [str(responses.get(column) or "").strip() for column in columns]
    if not all(values):
        return None
    flagged = {item.get("name") for item in record.get("review") or []}
    flagged |= {
        name
        for name, check in (record.get("checks") or {}).items()
        if isinstance(check, dict) and check.get("flags")
    }
    if flagged & set(spec.get("labels") or columns):
        return None
    return "\x1f".join(values)


def is_corrected(record):
    for group in ("fields", "zones"):
        for item in (record.get(group) or {}).values():
            if "original_value" in item and item["original_value"] != item.get("value"):
                return True
    return bool(record.get("manual_values"))


class ScanIndex:
    """Thread-safe SQLite index over scan results."""

    def __init__(self, path):
        self.path = str(path)
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.executescript(_SCHEMA)
            existing = {row[1] for row in self.conn.execute("PRAGMA table_info(scans)")}
            for column, kind in _MIGRATIONS.items():
                if column not in existing:
                    self.conn.execute(f"ALTER TABLE scans ADD COLUMN {column} {kind}")
            self.conn.executescript(_LATE_INDEXES)
            self.conn.commit()

    def close(self):
        with self.lock:
            self.conn.close()

    # ---- writes ---------------------------------------------------------
    def add_scans(self, records):
        """records: iterable of result dicts as written to result.json."""
        rows, review_rows, scan_ids, flags = [], [], [], []
        now = time.time()
        for record in records:
            scan_ids.append(record["scan_id"])
            record_flags = flag_rows(record)
            flags.extend(
                (
                    record["scan_id"],
                    name,
                    flag,
                    record.get("template_id"),
                    record.get("job_id"),
                )
                for name, flag in record_flags
            )
            rows.append(
                (
                    record["scan_id"],
                    record.get("template_id"),
                    record.get("job_id"),
                    record.get("seq"),
                    record.get("file_id"),
                    record.get("status"),
                    record.get("score"),
                    len(record.get("review") or []),
                    1 if record.get("reviewed") else 0,
                    1 if record.get("has_images") else 0,
                    record.get("error"),
                    record.get("created_at", now),
                    len({name for name, _ in record_flags}),
                    1 if record.get("verified") else 0,
                    1 if record.get("corrected") or is_corrected(record) else 0,
                    record.get("template_version"),
                    record.get("primary_key"),
                )
            )
            for item in record.get("review") or []:
                review_rows.append(
                    (
                        record["scan_id"],
                        item["name"],
                        item.get("kind"),
                        record.get("template_id"),
                        record.get("job_id"),
                        "pending",
                        now,
                    )
                )
        if not rows:
            return
        with self.lock:
            self.conn.executemany(
                f"INSERT OR REPLACE INTO scans ({','.join(SCAN_COLUMNS)}) "
                f"VALUES ({','.join('?' * len(SCAN_COLUMNS))})",
                rows,
            )
            self.conn.executemany(
                "DELETE FROM review_items WHERE scan_id = ?",
                [(scan_id,) for scan_id in scan_ids],
            )
            self.conn.executemany(
                "INSERT OR REPLACE INTO review_items VALUES (?,?,?,?,?,?,?)",
                review_rows,
            )
            self.conn.executemany(
                "DELETE FROM scan_flags WHERE scan_id = ?",
                [(scan_id,) for scan_id in scan_ids],
            )
            self.conn.executemany(
                "INSERT OR REPLACE INTO scan_flags VALUES (?,?,?,?,?)", flags
            )
            self.conn.commit()

    def update_after_review(self, result, resolved_names):
        with self.lock:
            self.conn.execute(
                "UPDATE scans SET status=?, score=?, review_count=?, reviewed=1, "
                "verified=?, corrected=? WHERE id=?",
                (
                    result.get("status"),
                    result.get("score"),
                    len(result.get("review") or []),
                    1 if result.get("verified") else 0,
                    1 if is_corrected(result) else 0,
                    result["scan_id"],
                ),
            )
            self.conn.executemany(
                "UPDATE review_items SET state='done' WHERE scan_id=? AND name=?",
                [(result["scan_id"], name) for name in resolved_names],
            )
            self.conn.commit()

    # ---- reads ----------------------------------------------------------
    def _query(self, sql, params=()):
        with self.lock:
            return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def get(self, scan_id):
        rows = self._query("SELECT * FROM scans WHERE id=?", (scan_id,))
        return rows[0] if rows else None

    @staticmethod
    def _filters(**filters):
        clauses, params = [], []
        for key, value in filters.items():
            if value is None or value == "":
                continue
            clauses.append(f"{key} = ?")
            params.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return where, params

    def list_scans(
        self,
        status=None,
        template_id=None,
        job_id=None,
        reviewed=None,
        limit=50,
        offset=0,
        order="desc",
    ):
        where, params = self._filters(
            status=status, template_id=template_id, job_id=job_id, reviewed=reviewed
        )
        if job_id and order == "seq":
            order_by = "seq ASC, rowid ASC"
        else:
            order_by = "rowid DESC" if order == "desc" else "rowid ASC"
        items = self._query(
            f"SELECT * FROM scans {where} ORDER BY {order_by} LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        total = self._query(f"SELECT COUNT(*) AS n FROM scans {where}", params)[0]["n"]
        return items, total

    def iter_job_scans(self, job_id, batch=2000):
        last_seq, last_rowid = -1, -1
        while True:
            rows = self._query(
                "SELECT rowid AS rid, * FROM scans WHERE job_id=? AND "
                "(seq > ? OR (seq = ? AND rowid > ?)) ORDER BY seq, rowid LIMIT ?",
                (job_id, last_seq, last_seq, last_rowid, batch),
            )
            if not rows:
                return
            yield from rows
            last_seq, last_rowid = rows[-1]["seq"], rows[-1]["rid"]

    def job_seqs(self, job_id):
        rows = self._query("SELECT DISTINCT seq FROM scans WHERE job_id=?", (job_id,))
        return {row["seq"] for row in rows}

    def status_counts(self, job_id=None, template_id=None):
        where, params = self._filters(job_id=job_id, template_id=template_id)
        rows = self._query(
            f"SELECT status, COUNT(*) AS n FROM scans {where} GROUP BY status", params
        )
        return {row["status"]: row["n"] for row in rows}

    def pending_reviews(
        self,
        template_id=None,
        job_id=None,
        scan_id=None,
        name=None,
        kind=None,
        limit=50,
        offset=0,
        created_after=None,
        created_before=None,
    ):
        where, params = self._filters(
            state="pending",
            template_id=template_id,
            job_id=job_id,
            scan_id=scan_id,
            name=name,
            kind=kind,
        )
        if created_after is not None:
            # Items queued since a client last looked (new sheets of a running job)
            where += " AND created_at > ?"
            params.append(created_after)
        if created_before is not None:
            where += " AND created_at <= ?"
            params.append(created_before)
        items = self._query(
            f"SELECT * FROM review_items {where} ORDER BY created_at, scan_id, name "
            "LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        total = self._query(f"SELECT COUNT(*) AS n FROM review_items {where}", params)[
            0
        ]["n"]
        return items, total

    def pending_review_names(self, template_id=None, job_id=None):
        where, params = self._filters(
            state="pending", template_id=template_id, job_id=job_id
        )
        return self._query(
            f"SELECT name, kind, COUNT(*) AS n FROM review_items {where} "
            "GROUP BY name, kind ORDER BY n DESC",
            params,
        )

    # ---- results screen -------------------------------------------------
    RESULT_VIEWS = {
        "all": "",
        "flagged": "flag_count > 0",
        "unflagged": "flag_count = 0",
        "reviewed": "(verified = 1 OR (flag_count > 0 AND review_count = 0))",
        "not_reviewed": "(verified = 0 AND NOT (flag_count > 0 AND review_count = 0))",
        "verified": "verified = 1",
        "corrected": "corrected = 1",
        "errors": "status = 'error'",
        # Another sheet of the same job shares this sheet's primary key
        "duplicates": "primary_key IS NOT NULL AND EXISTS (SELECT 1 FROM scans d "
        "WHERE d.primary_key = scans.primary_key AND d.job_id IS scans.job_id "
        "AND d.id != scans.id)",
        # ... or of any job of the same template
        "duplicates_template": "primary_key IS NOT NULL AND EXISTS (SELECT 1 FROM "
        "scans d WHERE d.primary_key = scans.primary_key AND d.template_id = "
        "scans.template_id AND d.id != scans.id)",
    }

    def _result_where(
        self,
        template_id=None,
        job_id=None,
        status=None,
        view="all",
        name=None,
        flag=None,
        file=None,
        scan_ids=None,
    ):
        if view and view not in self.RESULT_VIEWS:
            raise ValueError(f"Unknown view '{view}'")
        where, params = self._filters(
            template_id=template_id, job_id=job_id, status=status
        )
        clauses = [where[len("WHERE ") :]] if where else []
        if view and self.RESULT_VIEWS[view]:
            clauses.append(self.RESULT_VIEWS[view])
        if name or flag:
            sub, sub_params = self._filters(name=name, flag=flag)
            clauses.append(f"id IN (SELECT scan_id FROM scan_flags {sub})")
            params.extend(sub_params)
        if file:
            escaped = file.replace("\\", "\\\\").replace("%", "\\%")
            clauses.append("file_name LIKE ? ESCAPE '\\'")
            params.append("%" + escaped.replace("_", "\\_") + "%")
        if scan_ids:
            clauses.append(f"id IN ({','.join('?' * len(scan_ids))})")
            params.extend(scan_ids)
        return (f"WHERE {' AND '.join(clauses)}" if clauses else ""), params

    @staticmethod
    def _by_seq(job_id, order):
        return order == "seq" or bool(job_id and order in (None, "", "auto"))

    def list_results(self, limit=50, offset=0, order=None, **filters):
        where, params = self._result_where(**filters)
        if self._by_seq(filters.get("job_id"), order):
            order_by = "seq ASC, rowid ASC"
        else:
            order_by = "rowid ASC" if order == "asc" else "rowid DESC"
        items = self._query(
            f"SELECT * FROM scans {where} ORDER BY {order_by} LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        total = self._query(f"SELECT COUNT(*) AS n FROM scans {where}", params)[0]["n"]
        return items, total

    def iter_results(self, batch=2000, **filters):
        """Every matching scan row in a stable order, fetched in keyset batches."""
        where, params = self._result_where(**filters)
        prefix = f"{where} AND" if where else "WHERE"
        if filters.get("job_id"):
            last = (-1, -1)
            while True:
                rows = self._query(
                    f"SELECT rowid AS rid, * FROM scans {prefix} "
                    "(seq > ? OR (seq = ? AND rowid > ?)) ORDER BY seq, rowid LIMIT ?",
                    (*params, last[0], last[0], last[1], batch),
                )
                if not rows:
                    return
                yield from rows
                last = (rows[-1]["seq"], rows[-1]["rid"])
        else:
            last = -1
            while True:
                rows = self._query(
                    f"SELECT rowid AS rid, * FROM scans {prefix} rowid > ? "
                    "ORDER BY rowid LIMIT ?",
                    (*params, last, batch),
                )
                if not rows:
                    return
                yield from rows
                last = rows[-1]["rid"]

    def distinct_templates(self, **filters):
        where, params = self._result_where(**filters)
        rows = self._query(
            f"SELECT DISTINCT template_id FROM scans {where} LIMIT 100", params
        )
        return [row["template_id"] for row in rows if row["template_id"]]

    def count_results(self, **filters):
        where, params = self._result_where(**filters)
        return self._query(f"SELECT COUNT(*) AS n FROM scans {where}", params)[0]["n"]

    def neighbours(self, scan_id, order=None, **filters):
        """(previous, next) scan ids around scan_id within a filtered listing."""
        row = self._query("SELECT rowid AS rid, seq FROM scans WHERE id=?", (scan_id,))
        if not row:
            return None, None
        rid, seq = row[0]["rid"], row[0]["seq"] or 0
        where, params = self._result_where(**filters)
        prefix = f"{where} AND" if where else "WHERE"
        if self._by_seq(filters.get("job_id"), order):
            after = ("(seq > ? OR (seq = ? AND rowid > ?))", (seq, seq, rid))
            before = ("(seq < ? OR (seq = ? AND rowid < ?))", (seq, seq, rid))
            asc, desc = "seq ASC, rowid ASC", "seq DESC, rowid DESC"
        else:
            after, before = ("rowid > ?", (rid,)), ("rowid < ?", (rid,))
            asc, desc = "rowid ASC", "rowid DESC"
            if order != "asc":
                # Newest first: "next" is the older sheet
                after, before = before, after
                asc, desc = desc, asc

        def pick(condition, order_by):
            rows = self._query(
                f"SELECT id FROM scans {prefix} {condition[0]} "
                f"ORDER BY {order_by} LIMIT 1",
                (*params, *condition[1]),
            )
            return rows[0]["id"] if rows else None

        return pick(before, desc), pick(after, asc)

    def facets(self, template_id=None, job_id=None):
        where, params = self._filters(template_id=template_id, job_id=job_id)
        names = self._query(
            f"SELECT name, COUNT(DISTINCT scan_id) AS n FROM scan_flags {where} "
            "GROUP BY name ORDER BY n DESC LIMIT 500",
            params,
        )
        flags = self._query(
            f"SELECT flag, COUNT(DISTINCT scan_id) AS n FROM scan_flags {where} "
            "GROUP BY flag ORDER BY n DESC LIMIT 200",
            params,
        )
        views = {
            view: self.count_results(template_id=template_id, job_id=job_id, view=view)
            for view in (
                "all",
                "flagged",
                "reviewed",
                "not_reviewed",
                "corrected",
                "duplicates",
            )
        }
        return {"names": names, "flags": flags, "views": views}

    def duplicates_of(self, scan_id, across_jobs=False):
        """Other scans with this scan's primary key (same job, or same template)."""
        row = self.get(scan_id)
        if not row or not row.get("primary_key"):
            return []
        scope = "template_id = ?" if across_jobs else "job_id IS ?"
        return self._query(
            f"SELECT id, job_id, file_name, status FROM scans WHERE primary_key = ? "
            f"AND {scope} AND id != ? ORDER BY rowid LIMIT 50",
            (
                row["primary_key"],
                row["template_id"] if across_jobs else row["job_id"],
                scan_id,
            ),
        )

    def set_scan_state(self, result):
        with self.lock:
            self.conn.execute(
                "UPDATE scans SET status=?, score=?, review_count=?, verified=?, "
                "corrected=?, reviewed=?, primary_key=? WHERE id=?",
                (
                    result.get("status"),
                    result.get("score"),
                    len(result.get("review") or []),
                    1 if result.get("verified") else 0,
                    1 if is_corrected(result) else 0,
                    1 if result.get("reviewed") else 0,
                    primary_key_of(result),
                    result["scan_id"],
                ),
            )
            self.conn.commit()

    def sync_review_items(self, result):
        """Make the scan's queue rows match result["review"]: listed items are
        pending (added if new), other pending rows are done."""
        scan_id = result["scan_id"]
        pending = {
            item["name"]: item.get("kind") for item in result.get("review") or []
        }
        now = time.time()
        with self.lock:
            existing = {
                row[0]: row[1]
                for row in self.conn.execute(
                    "SELECT name, state FROM review_items WHERE scan_id=?", (scan_id,)
                )
            }
            self.conn.executemany(
                "INSERT OR REPLACE INTO review_items VALUES (?,?,?,?,?,?,?)",
                [
                    (
                        scan_id,
                        name,
                        kind,
                        result.get("template_id"),
                        result.get("job_id"),
                        "pending",
                        now,
                    )
                    for name, kind in pending.items()
                    if existing.get(name) != "pending"
                ],
            )
            self.conn.executemany(
                "UPDATE review_items SET state='done' WHERE scan_id=? AND name=?",
                [
                    (scan_id, name)
                    for name, state in existing.items()
                    if state == "pending" and name not in pending
                ],
            )
            self.conn.commit()

    def resolve_review_items(self, scan_id, names):
        with self.lock:
            self.conn.executemany(
                "UPDATE review_items SET state='done' WHERE scan_id=? AND name=?",
                [(scan_id, name) for name in names],
            )
            self.conn.commit()

    def add_corrections(self, rows):
        """rows: dicts with scan_id, name, kind, template_id, job_id, old, new, original, user, source, at."""
        if not rows:
            return
        with self.lock:
            self.conn.executemany(
                "INSERT INTO corrections (scan_id, name, kind, template_id, job_id, "
                "old_value, new_value, original_value, user, source, at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        r["scan_id"],
                        r["name"],
                        r.get("kind"),
                        r.get("template_id"),
                        r.get("job_id"),
                        r.get("old"),
                        r.get("new"),
                        r.get("original"),
                        r.get("user"),
                        r.get("source"),
                        r.get("at"),
                    )
                    for r in rows
                ],
            )
            self.conn.commit()

    def list_corrections(
        self,
        scan_id=None,
        template_id=None,
        job_id=None,
        user=None,
        limit=100,
        offset=0,
    ):
        where, params = self._filters(
            scan_id=scan_id, template_id=template_id, job_id=job_id, user=user
        )
        items = self._query(
            "SELECT id, scan_id, name, kind, template_id, job_id, old_value AS old, "
            "new_value AS new, original_value AS original, user, source, at "
            f"FROM corrections {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        total = self._query(f"SELECT COUNT(*) AS n FROM corrections {where}", params)[
            0
        ]["n"]
        return items, total

    def set_outcomes(self, scan_id, rows):
        """rows: (name, kind, template_id, job_id, flagged, corrected) of a verified sheet."""
        with self.lock:
            self.conn.execute("DELETE FROM field_outcomes WHERE scan_id=?", (scan_id,))
            self.conn.executemany(
                "INSERT OR REPLACE INTO field_outcomes VALUES (?,?,?,?,?,?,?)",
                [(scan_id, *row) for row in rows],
            )
            self.conn.commit()

    def accuracy(self, template_id=None, job_id=None):
        where, params = self._filters(template_id=template_id, job_id=job_id)
        per_field = self._query(
            "SELECT name, kind, "
            "SUM(flagged = 0) AS auto, "
            "SUM(flagged = 0 AND corrected = 0) AS auto_ok, "
            "SUM(flagged = 1) AS flagged, "
            "SUM(flagged = 1 AND corrected = 1) AS flagged_corrected, "
            "COUNT(*) AS total, SUM(corrected) AS corrected "
            f"FROM field_outcomes {where} GROUP BY name, kind ORDER BY name",
            params,
        )
        sheets = self._query(
            f"SELECT COUNT(DISTINCT scan_id) AS n FROM field_outcomes {where}", params
        )[0]["n"]
        return sheets, per_field

    def review_states(self, pairs):
        """{(scan_id, name): state} of review items ("pending" / "done")."""
        states = {}
        with self.lock:
            for scan_id, name in pairs:
                row = self.conn.execute(
                    "SELECT state FROM review_items WHERE scan_id=? AND name=?",
                    (scan_id, name),
                ).fetchone()
                if row is not None:
                    states[(scan_id, name)] = row[0]
        return states

    def delete_scans(self, scan_ids):
        """Drop scans from the index (the corrections audit trail is kept)."""
        scan_ids = list(scan_ids)
        if not scan_ids:
            return
        with self.lock:
            for table, column in (
                ("scans", "id"),
                ("review_items", "scan_id"),
                ("scan_flags", "scan_id"),
                ("field_outcomes", "scan_id"),
            ):
                self.conn.executemany(
                    f"DELETE FROM {table} WHERE {column}=?",
                    [(scan_id,) for scan_id in scan_ids],
                )
            self.conn.commit()

    def delete_template_rows(self, template_id):
        with self.lock:
            for table in ("scans", "review_items", "scan_flags", "field_outcomes"):
                self.conn.execute(
                    f"DELETE FROM {table} WHERE template_id=?", (template_id,)
                )
            self.conn.commit()

    # ---- maintenance ----------------------------------------------------
    def rebuild(self, scans_root):
        """Re-create the index from the result.json files on disk."""
        with self.lock:
            for table in ("scans", "review_items", "scan_flags", "field_outcomes"):
                self.conn.execute(f"DELETE FROM {table}")
            self.conn.commit()
        batch, count = [], 0
        for result_path in Path(scans_root).glob("*/*/result.json"):
            record = read_json(result_path)
            if not record or "scan_id" not in record:
                continue
            batch.append(record)
            if len(batch) >= 1000:
                self._add_with_done_reviews(batch)
                count += len(batch)
                batch = []
        if batch:
            self._add_with_done_reviews(batch)
            count += len(batch)
        return count

    def _add_with_done_reviews(self, records):
        self.add_scans(records)
        resolved = [
            (record["scan_id"], name)
            for record in records
            for name in (record.get("review_log") or {}).keys()
        ]
        if resolved:
            with self.lock:
                self.conn.executemany(
                    "UPDATE review_items SET state='done' WHERE scan_id=? AND name=?",
                    resolved,
                )
                self.conn.commit()
        for record in records:
            if record.get("verified"):
                self.set_outcomes(record["scan_id"], outcome_rows(record))


def original_value(item):
    return item.get("original_value", item.get("value"))


def outcome_rows(record):
    """(name, kind, template, job, flagged when read, corrected since) per field/zone."""
    flagged = {name for name, _ in flag_rows(record)}
    rows = []
    for kind, group in (("field", "fields"), ("zone", "zones")):
        for name, item in (record.get(group) or {}).items():
            rows.append(
                (
                    name,
                    kind,
                    record.get("template_id"),
                    record.get("job_id"),
                    1 if name in flagged else 0,
                    1 if original_value(item) != item.get("value") else 0,
                )
            )
    return rows
