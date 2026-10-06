"""
Exports through the API: run src.export over a job or any Results filter in a
background thread, keep the file under <data_dir>/exports/ for download, and
store reusable export profiles under <data_dir>/export_profiles/.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from fastapi import Body, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from src.api.results import DEFAULT_USER, ResultsError
from src.api.storage import new_id, read_json, slugify, write_json_atomic
from src.export import (
    EXTENSIONS,
    FORMATS,
    MEDIA_TYPES,
    ExportError,
    ExportProfile,
    export_results,
    plan_columns,
)
from src.export.rows import RowBuilder
from src.logger import logger

FILTER_KEYS = ("template_id", "job_id", "status", "view", "name", "flag", "file")


class ExportFilters(BaseModel):
    template_id: Optional[str] = None
    job_id: Optional[str] = None
    status: Optional[str] = None
    view: Optional[str] = Field(
        None, description="all, flagged, reviewed, not_reviewed, corrected, errors"
    )
    name: Optional[str] = None
    flag: Optional[str] = None
    file: Optional[str] = None
    scan_ids: Optional[List[str]] = Field(None, description="Only these scans")


class ExportRequest(BaseModel):
    format: str = Field(..., description="csv, xlsx, pdf, sqlite or sql")
    filters: ExportFilters = Field(default_factory=ExportFilters)
    profile: Optional[Dict[str, Any]] = Field(
        None, description="Export profile (see docs/engine-guide.md)"
    )
    profile_name: Optional[str] = Field(None, description="A saved profile instead")
    sql_url: Optional[str] = Field(
        None, description="format=sql: SQLAlchemy URL, e.g. postgresql+psycopg://..."
    )
    wait: bool = Field(False, description="Run in the request and return when done")


class ExportManager:
    def __init__(self, ctx):
        self.ctx = ctx
        self.root = ctx.data.exports
        self.profiles = ctx.data.root / "export_profiles"
        self.profiles.mkdir(parents=True, exist_ok=True)
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="omr-export")
        self.lock = threading.Lock()

    # ---- profiles --------------------------------------------------------
    def profile_path(self, name):
        return self.profiles / f"{slugify(name, 'profile')}.json"

    def list_profiles(self):
        items = []
        for path in sorted(self.profiles.glob("*.json")):
            data = read_json(path)
            if data:
                items.append({"name": data.get("name") or path.stem, "profile": data})
        return items

    def save_profile(self, name, profile):
        ExportProfile.from_dict(profile)
        profile = {**profile, "name": name}
        write_json_atomic(self.profile_path(name), profile)
        return profile

    # ---- exports ----------------------------------------------------------
    def record_path(self, export_id):
        if not export_id.isalnum():
            raise KeyError(export_id)
        return self.root / f"{export_id}.json"

    def get(self, export_id):
        try:
            return read_json(self.record_path(export_id))
        except KeyError:
            return None

    def list(self, limit=50):
        records = []
        for path in sorted(
            self.root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
        )[:limit]:
            record = read_json(path)
            if record:
                records.append(record)
        return records

    def filters_of(self, filters):
        values = {k: getattr(filters, k) for k in FILTER_KEYS if getattr(filters, k)}
        if filters.scan_ids:
            values["scan_ids"] = [s for s in filters.scan_ids if s.isalnum()][:5000]
        values.setdefault("view", "all")
        return values

    def results_factory(self, filters):
        index, scans = self.ctx.index, self.ctx.data

        def factory():
            for row in index.iter_results(**filters):
                result = read_json(scans.scan_dir(row["id"]) / "result.json")
                if result:
                    yield result

        return factory

    def template_infos(self, filters):
        infos = []
        for template_id in self.ctx.index.distinct_templates(**filters):
            infos.append(self.ctx.results.template_info({"template_id": template_id}))
        return infos

    def image_provider(self, result):
        try:
            return self.ctx.results.render(result["scan_id"])["image"]
        except (ResultsError, KeyError) as error:
            logger.warning(f"No image for {result.get('file_id')}: {error}")
            return None

    def resolve_profile(self, body):
        data = body.profile
        if data is None and body.profile_name:
            data = read_json(self.profile_path(body.profile_name))
            if data is None:
                raise ExportError(f"No saved profile '{body.profile_name}'")
        return ExportProfile.from_dict(data or {})

    def preview(self, body, limit=5):
        profile = self.resolve_profile(body)
        filters = self.filters_of(body.filters)
        sample = []
        for result in self.results_factory(filters)():
            sample.append(result)
            if len(sample) >= limit:
                break
        columns, warnings, custom_labels = plan_columns(
            profile, self.template_infos(filters), sample
        )
        builder = RowBuilder(columns, profile, custom_labels)
        rows, errors = [], []
        for result in sample:
            try:
                values, _ = builder.build(result)
                rows.append([_jsonable(v) for v in values])
            except ExportError as error:
                errors.append(str(error))
        return {
            "columns": [
                {"key": c.key, "field": c.name, "source": c.source, "type": c.type}
                for c in columns
            ],
            "rows": rows,
            "warnings": warnings + builder.failure_examples,
            "errors": errors,
            "total": self.ctx.index.count_results(**filters),
        }

    def create(self, body, user):
        if body.format not in FORMATS:
            raise ExportError(f"format must be one of {list(FORMATS)}")
        profile = self.resolve_profile(body)
        filters = self.filters_of(body.filters)
        if body.format == "sql" and not self.ctx.settings.export_sql:
            raise ExportError(
                "Database exports are disabled on this server (OMR_EXPORT_SQL=0)"
            )
        if body.format == "pdf":
            # Server ceiling for per-sheet PDF pages (OMR_PDF_SHEET_LIMIT)
            limit = self.ctx.settings.pdf_sheet_limit
            asked = int(profile.pdf.get("maxSheets") or 500)
            profile.pdf["maxSheets"] = min(asked, limit) if limit > 0 else asked
        export_id = new_id()
        record = {
            "id": export_id,
            "format": body.format,
            "filters": filters,
            "profile": profile.name or None,
            "state": "queued",
            "user": user,
            "created_at": time.time(),
            "finished_at": None,
            "rows": 0,
            "total": self.ctx.index.count_results(**filters),
            "warnings": [],
            "error": None,
            "file_name": self.file_name(body.format, filters),
            "download_url": None,
        }
        write_json_atomic(self.record_path(export_id), record)
        job = (record, profile, body.sql_url)
        if body.wait:
            self.run(*job)
            return self.get(export_id)
        self.pool.submit(self.run, *job)
        return record

    def file_name(self, fmt, filters):
        stem = "results"
        if filters.get("job_id"):
            job = self.ctx.jobs.get(filters["job_id"]) or {}
            stem = slugify(job.get("name") or f"job-{filters['job_id'][:8]}", "results")
        elif filters.get("template_id"):
            stem = slugify(filters["template_id"], "results")
        return f"{stem}.{EXTENSIONS[fmt]}"

    def run(self, record, profile, sql_url):
        export_id = record["id"]
        path = self.root / f"{export_id}.{EXTENSIONS[record['format']]}"
        record["state"] = "running"
        write_json_atomic(self.record_path(export_id), record)

        def progress(done):
            record["rows"] = done
            write_json_atomic(self.record_path(export_id), record)

        try:
            report = export_results(
                self.results_factory(record["filters"]),
                record["format"],
                path,
                profile,
                self.template_infos(record["filters"]),
                image_provider=self.image_provider,
                total=record["total"],
                title=record["file_name"],
                sql_url=sql_url,
                progress=progress,
            )
            record.update(
                state="completed",
                rows=report["rows"],
                columns=report["columns"],
                warnings=report["warnings"],
                download_url=f"/exports/{export_id}/download",
            )
        except ExportError as error:
            # Refused (e.g. a lossy cast): no partial file is left behind
            path.unlink(missing_ok=True)
            record.update(state="failed", error=str(error))
        except Exception as error:  # keep the record readable
            logger.error(f"Export {export_id} failed: {error}")
            if path.exists():
                path.unlink()
            record.update(state="failed", error=f"{type(error).__name__}: {error}")
        record["finished_at"] = time.time()
        write_json_atomic(self.record_path(export_id), record)
        return record

    def file_of(self, record):
        return self.root / f"{record['id']}.{EXTENSIONS[record['format']]}"

    def delete(self, export_id):
        record = self.get(export_id)
        if record is None:
            return None
        self.file_of(record).unlink(missing_ok=True)
        self.record_path(export_id).unlink(missing_ok=True)
        return record

    def stop(self):
        self.pool.shutdown(wait=False)


def _jsonable(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def register(app, ctx, secured):
    manager = ctx.exports

    def fail(error, status=422):
        raise HTTPException(status, str(error)) from None

    @app.post("/exports", tags=["exports"], dependencies=secured, status_code=201)
    def create_export(body: ExportRequest, request: Request):
        """
        Export a job's (filters.job_id) or any Results filter's sheets as CSV,
        XLSX, PDF, a SQLite file or into a database (format=sql + sql_url).
        Runs in the background unless wait=true; poll GET /exports/{id}.
        """
        user = request.headers.get("x-user") or DEFAULT_USER
        try:
            return manager.create(body, user)
        except ExportError as error:
            fail(error)

    @app.post("/exports/preview", tags=["exports"], dependencies=secured)
    def preview_export(body: ExportRequest):
        """Columns, warnings and the first rows a profile produces."""
        try:
            return manager.preview(body)
        except ExportError as error:
            fail(error)

    @app.get("/exports", tags=["exports"], dependencies=secured)
    def list_exports(limit: int = Query(50, ge=1, le=500)):
        return {"exports": manager.list(limit)}

    @app.get("/exports/{export_id}", tags=["exports"], dependencies=secured)
    def get_export(export_id: str):
        record = manager.get(export_id)
        if record is None:
            raise HTTPException(404, f"Export '{export_id}' not found")
        return record

    @app.get("/exports/{export_id}/download", tags=["exports"], dependencies=secured)
    def download_export(export_id: str):
        record = manager.get(export_id)
        if record is None:
            raise HTTPException(404, f"Export '{export_id}' not found")
        path = manager.file_of(record)
        if record.get("state") != "completed" or not path.exists():
            raise HTTPException(409, f"Export is {record.get('state')}")
        return FileResponse(
            path, media_type=MEDIA_TYPES[record["format"]], filename=record["file_name"]
        )

    @app.delete("/exports/{export_id}", tags=["exports"], dependencies=secured)
    def delete_export(export_id: str):
        if manager.delete(export_id) is None:
            raise HTTPException(404, f"Export '{export_id}' not found")
        return {"deleted": export_id}

    @app.get("/export-profiles", tags=["exports"], dependencies=secured)
    def list_profiles():
        return {"profiles": manager.list_profiles()}

    @app.put("/export-profiles/{name}", tags=["exports"], dependencies=secured)
    def put_profile(name: str, profile: Dict[str, Any] = Body(...)):
        try:
            return manager.save_profile(name, profile)
        except ExportError as error:
            fail(error)

    @app.delete("/export-profiles/{name}", tags=["exports"], dependencies=secured)
    def delete_profile(name: str):
        path = manager.profile_path(name)
        if not path.exists():
            raise HTTPException(404, f"No profile '{name}'")
        path.unlink()
        return {"deleted": name}

    return manager
