"""
On-disk storage for the API.

    <data_dir>/
        templates/<template_id>/template.json (+ assets, config.json, evaluation.json)
        scans/<id[:2]>/<scan_id>/result.json, aligned.png, marked.jpg, input.<ext>
        jobs/<job_id>.json, jobs/<job_id>.files, jobs/<job_id>/inputs/...
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
        for path in (
            self.templates,
            self.scans,
            self.jobs,
            self.training,
            self.uploads,
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
]


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
            self.conn.commit()

    def close(self):
        with self.lock:
            self.conn.close()

    # ---- writes ---------------------------------------------------------
    def add_scans(self, records):
        """records: iterable of result dicts as written to result.json."""
        rows, review_rows, scan_ids = [], [], []
        now = time.time()
        for record in records:
            scan_ids.append(record["scan_id"])
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
            self.conn.commit()

    def update_after_review(self, result, resolved_names):
        with self.lock:
            self.conn.execute(
                "UPDATE scans SET status=?, score=?, review_count=?, reviewed=1 WHERE id=?",
                (
                    result.get("status"),
                    result.get("score"),
                    len(result.get("review") or []),
                    result["scan_id"],
                ),
            )
            self.conn.executemany(
                "UPDATE review_items SET state='done' WHERE scan_id=? AND name=?",
                [(result["scan_id"], name) for name in resolved_names],
            )
            self.conn.commit()

    def delete_template_rows(self, template_id):
        with self.lock:
            self.conn.execute("DELETE FROM scans WHERE template_id=?", (template_id,))
            self.conn.execute(
                "DELETE FROM review_items WHERE template_id=?", (template_id,)
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
    ):
        where, params = self._filters(
            state="pending",
            template_id=template_id,
            job_id=job_id,
            scan_id=scan_id,
            name=name,
            kind=kind,
        )
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

    # ---- maintenance ----------------------------------------------------
    def rebuild(self, scans_root):
        """Re-create the index from the result.json files on disk."""
        with self.lock:
            self.conn.execute("DELETE FROM scans")
            self.conn.execute("DELETE FROM review_items")
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
