"""
Housekeeping and review-queue helpers (plan items 8, 28, 31, 32, 33):

    DELETE /results/{scan_id}     delete one scan (audited)
    DELETE /jobs/{job_id}         delete a job and all its scans (audited)
    POST   /review/accept-bulk    accept pending review items as read (who + when)
    GET    /review/counts         pending total and per-name counts (polling)
    POST   /review/states         pending / done (by whom) for queue items

Deleting never touches the user's own input files: only what the server
stored (results, stored images, uploaded copies) is removed.
"""

import json
import os
import shutil
import time
from typing import List, Optional

from fastapi import HTTPException, Query, Request
from pydantic import BaseModel, Field

from src.api import jobs as jobs_module
from src.api.results import DEFAULT_USER, ResultsError
from src.api.review import ReviewError, apply_review, current_value
from src.api.storage import read_json
from src.logger import logger

ACTIVE = {jobs_module.QUEUED, jobs_module.RUNNING}
BULK_SOURCE = "bulk_accept"


def workers_warning(workers, cpus=None):
    """A warning (never an error) when more workers than CPU cores are asked for."""
    cpus = cpus or os.cpu_count() or 1
    try:
        workers = int(workers or 0)
    except (TypeError, ValueError):
        return None
    if workers > cpus:
        return (
            f"{workers} worker processes on {cpus} CPU cores: more workers than "
            "cores only helps when reading from a slow disk or network share, "
            "and uses more memory."
        )
    return None


class ReviewFilters(BaseModel):
    template_id: Optional[str] = None
    job_id: Optional[str] = None
    scan_id: Optional[str] = None
    name: Optional[str] = None
    kind: Optional[str] = Field(
        None, pattern="^(field|zone|check|custom_label|sheet)$"
    )
    flag: Optional[str] = None


class AcceptBulkBody(ReviewFilters):
    limit: int = Field(
        2000,
        ge=1,
        le=20000,
        description="Items handled by this call (repeat while remaining > 0)",
    )
    expected: Optional[int] = Field(
        None,
        description="The pending count the user confirmed; refused (409) when "
        "more items are pending now, so nothing unseen is accepted",
    )
    before: Optional[float] = Field(
        None,
        description="Only items queued at or before this server time (the 'now' "
        "of the /review/counts the user confirmed), so later arrivals stay pending",
    )
    user: Optional[str] = Field(None, description="Defaults to the X-User header")


class ItemRef(BaseModel):
    scan_id: str
    name: str


class StatesBody(BaseModel):
    items: List[ItemRef] = Field(default_factory=list, max_length=500)


def register(app, ctx, secured):
    service = ctx.results

    def user_of(request, explicit=None):
        return request.headers.get("x-user") or explicit or DEFAULT_USER

    def drop_caches(scan_id):
        service.renders.drop(scan_id)
        cache = getattr(service, "view_renders", None)
        if cache is not None:
            cache.drop(scan_id)
        aligned = str(ctx.data.scan_dir(scan_id) / "aligned.png")
        with ctx.image_cache_guard:
            ctx.image_cache.pop(aligned, None)

    def refresh_job_counts(job_id):
        if not job_id or job_id in ctx.jobs.live:
            return
        counts = ctx.index.status_counts(job_id=job_id)
        ctx.jobs.update(job_id, {"counts": counts, "pages": sum(counts.values())})

    def remove_scan_files(scan_id):
        shutil.rmtree(ctx.data.scan_dir(scan_id), ignore_errors=True)
        drop_caches(scan_id)

    def summary(result):
        return json.dumps(
            {
                "file_name": result.get("file_name"),
                "file_id": result.get("file_id"),
                "page": result.get("page"),
                "status": result.get("status"),
                "responses": result.get("responses"),
                "source_path": result.get("source_path"),
            },
            default=str,
        )

    # ------------------------------------------------------------------
    # delete
    # ------------------------------------------------------------------
    @app.delete("/results/{scan_id}", tags=["results"], dependencies=secured)
    def delete_scan(scan_id: str, request: Request):
        """
        Delete one scan: its result, stored images and uploaded copy. The
        original file of a server-folder job is left alone. An audit record
        (kind "delete") keeps who deleted what and when.
        """
        user = user_of(request)
        with ctx.scan_lock(scan_id):
            try:
                result = service.load(scan_id)
            except ResultsError as error:
                raise HTTPException(error.status, str(error)) from None
            job_id = result.get("job_id")
            job = ctx.jobs.live.get(job_id) if job_id else None
            if job is not None and job.get("state") in ACTIVE:
                raise HTTPException(
                    409, "This sheet's job is still running; wait or cancel it first"
                )
            remove_scan_files(scan_id)
            ctx.index.delete_scans([scan_id])
            ctx.index.add_corrections(
                [
                    {
                        "scan_id": scan_id,
                        "name": "*scan*",
                        "kind": "delete",
                        "template_id": result.get("template_id"),
                        "job_id": job_id,
                        "old": summary(result),
                        "new": None,
                        "original": None,
                        "user": user,
                        "source": "delete",
                        "at": time.time(),
                    }
                ]
            )
        refresh_job_counts(job_id)
        logger.info(f"Scan {scan_id} ({result.get('file_id')}) deleted by {user}")
        return {"deleted": scan_id, "file_id": result.get("file_id"), "by": user}

    @app.delete("/jobs/{job_id}", tags=["jobs"], dependencies=secured)
    def delete_job(job_id: str, request: Request):
        """
        Delete a job and every scan it produced (results, stored images and
        uploaded files). Files of a server folder are never deleted. A running
        job must be cancelled first.
        """
        user = user_of(request)
        job = ctx.jobs.get(job_id) if job_id.isalnum() else None
        if job is None:
            raise HTTPException(404, f"Job '{job_id}' not found")
        if job.get("state") in ACTIVE:
            raise HTTPException(409, "The job is still running; cancel it first")
        scan_ids = [row["id"] for row in ctx.index.iter_job_scans(job_id)]
        for scan_id in scan_ids:
            with ctx.scan_lock(scan_id):
                remove_scan_files(scan_id)
        ctx.index.delete_scans(scan_ids)
        ctx.jobs.live.pop(job_id, None)
        for path in (ctx.data.job_file(job_id), ctx.jobs.files_path(job_id)):
            try:
                path.unlink()
            except OSError:
                pass
        shutil.rmtree(ctx.data.jobs / job_id, ignore_errors=True)
        ctx.index.add_corrections(
            [
                {
                    "scan_id": "",
                    "name": "*job*",
                    "kind": "delete_job",
                    "template_id": job.get("template_id"),
                    "job_id": job_id,
                    "old": json.dumps(
                        {
                            "name": job.get("name"),
                            "source": job.get("source"),
                            "folder": job.get("folder"),
                            "total_files": job.get("total_files"),
                            "scans": len(scan_ids),
                        }
                    ),
                    "new": None,
                    "original": None,
                    "user": user,
                    "source": "delete",
                    "at": time.time(),
                }
            ]
        )
        logger.info(f"Job {job_id} and {len(scan_ids)} scan(s) deleted by {user}")
        return {"deleted": job_id, "scans": len(scan_ids), "by": user}

    # ------------------------------------------------------------------
    # review queue
    # ------------------------------------------------------------------
    def filters_of(body):
        return {
            key: getattr(body, key)
            for key in ("template_id", "job_id", "scan_id", "name", "kind", "flag")
            if getattr(body, key)
        }

    @app.get("/review/counts", tags=["review"], dependencies=secured)
    def review_counts(
        template_id: Optional[str] = None,
        job_id: Optional[str] = None,
        scan_id: Optional[str] = None,
        name: Optional[str] = None,
        kind: Optional[str] = Query(
            None, pattern="^(field|zone|check|custom_label|sheet)$"
        ),
        since: Optional[float] = Query(
            None, description="Also count items queued after this server time"
        ),
        flag: Optional[str] = None,
    ):
        """
        Pending items under the queue filters, per field/zone name (for the
        name filter, which ignores the name filter itself), and how many were
        queued since a time. "now" is the server time to pass as since next.
        """
        now = time.time()
        filters = {
            "template_id": template_id,
            "job_id": job_id,
            "scan_id": scan_id,
            "kind": kind,
            "flag": flag,
        }
        _, total = ctx.index.pending_reviews(name=name, limit=1, **filters)
        where_names = ctx.index.pending_review_names(
            template_id=template_id, job_id=job_id
        )
        new = None
        if since is not None:
            _, new = ctx.index.pending_reviews(
                name=name, limit=1, created_after=since, **filters
            )
        return {
            "total": total,
            "by_name": where_names,
            "all_names": sum(row["n"] for row in where_names),
            "by_flag": ctx.index.pending_review_flags(template_id, job_id),
            "new": new,
            "now": now,
        }

    @app.post("/review/states", tags=["review"], dependencies=secured)
    def review_states(body: StatesBody):
        """
        For each queue item: "pending", "done" (with who decided it, when and
        the value) or "gone" (the scan was deleted or regraded). The queue
        screen uses it to skip items someone else already decided.
        """
        pairs = [(item.scan_id, item.name) for item in body.items]
        states = ctx.index.review_states(pairs)
        logs = {}
        out = []
        for scan_id, name in pairs:
            state = states.get((scan_id, name), "gone")
            entry = {"scan_id": scan_id, "name": name, "state": state}
            if state == "done" and scan_id.isalnum():
                if scan_id not in logs:
                    result = read_json(ctx.data.scan_dir(scan_id) / "result.json") or {}
                    logs[scan_id] = result.get("review_log") or {}
                log = logs[scan_id].get(name) or {}
                entry.update(
                    by=log.get("reviewer"),
                    at=log.get("at"),
                    action=log.get("action"),
                    value=log.get("label"),
                )
            out.append(entry)
        return {"items": out}

    @app.post("/review/accept-bulk", tags=["review"], dependencies=secured)
    def accept_bulk(body: AcceptBulkBody, request: Request):
        """
        Accept every pending item under the filters as read (their values stay
        as the engine read them). Nothing is deleted: each item is settled with
        who accepted it and when (review_log action "bulk_accepted", plus an
        audit record per item), so it can be traced and changed later. No
        training samples are written for these unchecked values.
        """
        user = user_of(request, body.user)
        filters = filters_of(body)
        if body.before is not None:
            filters["created_before"] = body.before
        _, pending = ctx.index.pending_reviews(limit=1, **filters)
        if body.expected is not None and pending > body.expected:
            raise HTTPException(
                409,
                f"{pending} items are pending now, more than the {body.expected} "
                "you confirmed; look again before accepting",
            )
        rows, _ = ctx.index.pending_reviews(limit=body.limit, **filters)
        by_scan = {}
        for row in rows:
            by_scan.setdefault(row["scan_id"], []).append(row["name"])
        accepted, errors = 0, []
        for scan_id, names in by_scan.items():
            try:
                accepted += accept_scan(scan_id, names, user)
            except (ResultsError, ReviewError) as error:
                errors.append({"scan_id": scan_id, "error": str(error)})
                if getattr(error, "status", None) == 404:
                    # The result is gone: its queue rows are stale
                    ctx.index.resolve_review_items(scan_id, names)
        _, remaining = ctx.index.pending_reviews(limit=1, **filters)
        return {
            "accepted": accepted,
            "scans": len(by_scan),
            "errors": errors,
            "remaining": remaining,
            "by": user,
        }

    def accept_scan(scan_id, names, user):
        with ctx.scan_lock(scan_id):
            result = service.load(scan_id)
            info = service.template_info(result)
            values = {}
            for name in names:
                values[name] = current(result, name)
            resolved, _ = apply_review(
                result, {}, names, user, None, info["custom_labels"]
            )
            now = time.time()
            rows = []
            for name in resolved:
                log = (result.get("review_log") or {}).get(name)
                if log is not None:
                    log["action"] = "bulk_accepted"
                    log["source"] = BULK_SOURCE
                row = {
                    "scan_id": scan_id,
                    "name": name,
                    "kind": "bulk_accept",
                    "template_id": result.get("template_id"),
                    "job_id": result.get("job_id"),
                    "old": values.get(name, ""),
                    "new": values.get(name, ""),
                    "original": values.get(name, ""),
                    "user": user,
                    "source": BULK_SOURCE,
                    "at": now,
                }
                rows.append(row)
                result.setdefault("audit", []).append(
                    {
                        k: row[k]
                        for k in (
                            "name",
                            "kind",
                            "old",
                            "new",
                            "original",
                            "user",
                            "source",
                            "at",
                        )
                    }
                )
            service.finish_edit(result, resolved)
            ctx.index.add_corrections(rows)
        return len(resolved)

    def current(result, name):
        value = current_value(result, name)
        return "" if value is None else str(value)
