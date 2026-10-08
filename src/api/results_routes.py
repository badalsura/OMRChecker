"""HTTP endpoints of the Results screen (see src/api/results.py)."""

import base64
from typing import Any, Dict, List, Optional

from fastapi import Body, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from src.api.results import DEFAULT_USER, ResultsError, clean_rules

VIEW_PATTERN = (
    "^(all|flagged|unflagged|reviewed|not_reviewed|verified|corrected|errors"
    "|duplicates|duplicates_template)$"
)


class BubbleToggle(BaseModel):
    field: str = Field(..., description="Bubble field label, e.g. q7")
    value: str = Field(..., description="Bubble value to toggle, e.g. B")


class CorrectionBody(BaseModel):
    changes: Dict[str, Any] = Field(
        default_factory=dict,
        description="{name: new value} for fields, zones, custom labels (split "
        "over their columns) and cross-field checks (by check name or output "
        "column). Bubble field values are concatenated bubble values ('' = "
        "blank, 'AB' = multi-mark).",
    )
    accept: List[str] = Field(
        default_factory=list,
        description="Names whose current value is right (settles their review item)",
    )
    toggle: List[BubbleToggle] = Field(
        default_factory=list,
        description="Bubbles to flip; the field value is recomputed server-side",
    )
    user: Optional[str] = Field(None, description="Defaults to the X-User header")


class VerifyBody(BaseModel):
    user: Optional[str] = None


class RegradeBody(BaseModel):
    template_overrides: Dict[str, Any] = Field(
        default_factory=dict,
        description='Deep-merged into template.json, e.g. {"colorDropout": {...}}',
    )
    config_overrides: Dict[str, Any] = Field(
        default_factory=dict,
        description='Per-section config.json overrides, e.g. {"threshold_params": {...}}',
    )
    apply: bool = Field(
        False, description="false: preview only; true: replace the stored read"
    )
    keep_corrections: bool = Field(
        True, description="Re-apply manual corrections on top of the new read"
    )
    use_current_template: bool = Field(
        False,
        description="Use the template as it is now instead of the version the "
        "sheet was read with",
    )
    user: Optional[str] = None


class RemapBody(BaseModel):
    rules: List[Any] = Field(
        default_factory=list,
        description='[{"from": "D:\\\\old", "to": "E:\\\\new"}] or ["D:\\\\old=E:\\\\new"]',
    )


def register(app, ctx, secured):
    service = ctx.results

    def fail(error):
        raise HTTPException(error.status, str(error)) from None

    def user_of(request: Request, explicit=None):
        return request.headers.get("x-user") or explicit or DEFAULT_USER

    def overlay_response(result, info=None, **extra):
        payload = service.overlay(result, info)
        payload.update(extra)
        if result.get("key_fields"):
            # Other sheets of the job with the same primary key
            payload["duplicates"] = [
                {"scan_id": d["id"], "file_name": d["file_name"], "status": d["status"]}
                for d in ctx.index.duplicates_of(result["scan_id"])
            ]
        return payload

    @app.get("/results", tags=["results"], dependencies=secured)
    def list_results(
        template_id: Optional[str] = None,
        job_id: Optional[str] = None,
        status: Optional[str] = None,
        view: str = Query("all", pattern=VIEW_PATTERN),
        name: Optional[str] = Query(
            None, description="Only sheets where this field was flagged"
        ),
        flag: Optional[str] = Query(None, description="Only sheets with this flag"),
        file: Optional[str] = Query(None, description="File name contains"),
        order: Optional[str] = Query(None, pattern="^(auto|seq|asc|desc)$"),
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ):
        """Graded sheets from the index (never loads results into memory)."""
        items, total = ctx.index.list_results(
            template_id=template_id,
            job_id=job_id,
            status=status,
            view=view,
            name=name,
            flag=flag,
            file=file,
            order=order,
            limit=limit,
            offset=offset,
        )
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    @app.get("/results/facets", tags=["results"], dependencies=secured)
    def result_facets(template_id: Optional[str] = None, job_id: Optional[str] = None):
        """Flagged field names, flags and per-view counts for the filters."""
        return ctx.index.facets(template_id=template_id, job_id=job_id)

    @app.get("/results/neighbours", tags=["results"], dependencies=secured)
    def result_neighbours(
        scan_id: str,
        template_id: Optional[str] = None,
        job_id: Optional[str] = None,
        status: Optional[str] = None,
        view: str = Query("all", pattern=VIEW_PATTERN),
        name: Optional[str] = None,
        flag: Optional[str] = None,
        file: Optional[str] = None,
        order: Optional[str] = Query(None, pattern="^(auto|seq|asc|desc)$"),
    ):
        previous, following = ctx.index.neighbours(
            scan_id,
            order=order,
            template_id=template_id,
            job_id=job_id,
            status=status,
            view=view,
            name=name,
            flag=flag,
            file=file,
        )
        return {"previous": previous, "next": following}

    @app.get("/results/accuracy", tags=["results"], dependencies=secured)
    def result_accuracy(
        template_id: Optional[str] = None, job_id: Optional[str] = None
    ):
        """How often fields the engine did not flag were right, over verified sheets."""
        return service.accuracy(template_id=template_id, job_id=job_id)

    @app.get("/scans/{scan_id}/overlay", tags=["results"], dependencies=secured)
    def scan_overlay(scan_id: str):
        """Overlay geometry and values without re-reading the image."""
        try:
            return overlay_response(service.load(scan_id))
        except ResultsError as error:
            fail(error)

    @app.get("/scans/{scan_id}/render", tags=["results"], dependencies=secured)
    def scan_render(
        scan_id: str,
        inline: bool = Query(False, description="Embed the image as a data URL"),
        format: str = Query("jpg", pattern="^(jpg|png)$"),
        view: str = Query(
            "dropout",
            pattern="^(dropout|print)$",
            description="dropout: the image the bubbles were read on; print: the "
            "same page with the print kept (where block borders were searched)",
        ),
    ):
        """
        Re-read the original file with the template version (and regrade
        overrides) it was graded with and return the aligned image plus the
        overlay: every bubble's box/centre and state, every zone's box and value.
        Falls back to the stored aligned.png when the file is gone.
        """
        try:
            result = service.load(scan_id)
            rendered = service.render(scan_id, view=view)
            image = rendered["image"]
            suffix = "" if view == "dropout" else f"&view={view}"
            extra = {
                **rendered["meta"],
                "width": int(image.shape[1]),
                "height": int(image.shape[0]),
                "image_url": f"/scans/{scan_id}/render/image?format={format}{suffix}",
            }
            if inline:
                data = service.encoded(scan_id, format, view=view)
                mime = "image/png" if format == "png" else "image/jpeg"
                extra["image_data"] = (
                    f"data:{mime};base64," + base64.b64encode(data).decode()
                )
            return overlay_response(result, **extra)
        except ResultsError as error:
            fail(error)

    @app.get("/scans/{scan_id}/render/image", tags=["results"], dependencies=secured)
    def scan_render_image(
        scan_id: str,
        format: str = Query("jpg", pattern="^(jpg|png)$"),
        preview: bool = Query(False, description="The last regrade preview"),
        view: str = Query("dropout", pattern="^(dropout|print)$"),
    ):
        try:
            service.load(scan_id)
            data = service.encoded(scan_id, format, preview, view)
        except ResultsError as error:
            fail(error)
        return Response(
            content=data,
            media_type="image/png" if format == "png" else "image/jpeg",
            headers={"Cache-Control": "private, no-cache"},
        )

    @app.post("/scans/{scan_id}/corrections", tags=["results"], dependencies=secured)
    def scan_corrections(scan_id: str, body: CorrectionBody, request: Request):
        """
        Change values (typed, or by toggling bubbles). The server recomputes the
        field value (multi-marks in sheet order), custom-label outputs, score and
        status; every change is an audit record.
        """
        try:
            result, info = service.correct(
                scan_id,
                body.changes,
                [{"field": t.field, "value": t.value} for t in body.toggle],
                user_of(request, body.user),
                accept=body.accept,
            )
        except ResultsError as error:
            fail(error)
        return overlay_response(result, info)

    @app.post("/scans/{scan_id}/verify", tags=["results"], dependencies=secured)
    def scan_verify(scan_id: str, request: Request, body: Optional[VerifyBody] = None):
        """A person checked every value of this sheet (feeds the accuracy readout)."""
        try:
            result, records = service.verify(
                scan_id, user_of(request, body.user if body else None)
            )
        except ResultsError as error:
            fail(error)
        return overlay_response(result, training_records=len(records))

    @app.delete("/scans/{scan_id}/verify", tags=["results"], dependencies=secured)
    def scan_unverify(scan_id: str, request: Request):
        try:
            result, _ = service.verify(scan_id, user_of(request), verified=False)
        except ResultsError as error:
            fail(error)
        return overlay_response(result)

    @app.post("/scans/{scan_id}/regrade", tags=["results"], dependencies=secured)
    def scan_regrade(scan_id: str, body: RegradeBody, request: Request):
        """
        Re-read the original file with template/config overrides. apply=false
        returns a preview (overlay + changed values); apply=true stores the new
        read (the previous one is kept in result.history).
        """
        try:
            record, changes, applied = service.regrade(
                scan_id,
                body.template_overrides,
                body.config_overrides,
                apply=body.apply,
                keep_corrections=body.keep_corrections,
                use_current_template=body.use_current_template,
                user=user_of(request, body.user),
            )
            info = service.template_info(record)
        except ResultsError as error:
            fail(error)
        rendered = service.renders.get(scan_id if applied else f"{scan_id}:preview")
        extra = {"applied": applied, "changes": changes}
        if rendered is not None:
            image = rendered["image"]
            extra.update(
                width=int(image.shape[1]),
                height=int(image.shape[0]),
                image_url=f"/scans/{scan_id}/render/image"
                + ("" if applied else "?preview=true"),
            )
        return {**service.overlay(record, info), **extra}

    @app.get("/scans/{scan_id}/audit", tags=["results"], dependencies=secured)
    def scan_audit(scan_id: str, limit: int = Query(200, ge=1, le=1000)):
        items, total = ctx.index.list_corrections(scan_id=scan_id, limit=limit)
        return {"items": items, "total": total}

    @app.get("/audit", tags=["results"], dependencies=secured)
    def audit_log(
        template_id: Optional[str] = None,
        job_id: Optional[str] = None,
        user: Optional[str] = None,
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ):
        """Every manual change (who, when, old/new/original value), newest first."""
        items, total = ctx.index.list_corrections(
            template_id=template_id,
            job_id=job_id,
            user=user,
            limit=limit,
            offset=offset,
        )
        return {"items": items, "total": total}

    @app.get("/settings/path-remap", tags=["results"], dependencies=secured)
    def get_path_remap():
        """Folder prefix rewrites used to find moved input files."""
        return {
            "rules": service.global_remap(),
            "environment": ctx.settings.path_remap,
        }

    @app.put("/settings/path-remap", tags=["results"], dependencies=secured)
    def put_path_remap(body: RemapBody):
        try:
            rules = service.set_global_remap(body.rules)
        except ResultsError as error:
            fail(error)
        service.renders.items.clear()
        return {"rules": rules, "environment": ctx.settings.path_remap}

    @app.patch("/jobs/{job_id}", tags=["jobs"], dependencies=secured)
    def patch_job(job_id: str, body: Dict[str, Any] = Body(...)):
        """Edit a job's name or path_remap ([{"from": ..., "to": ...}])."""
        changes = {}
        if "name" in body:
            changes["name"] = str(body["name"] or "")
        if "path_remap" in body:
            try:
                changes["path_remap"] = clean_rules(body["path_remap"])
            except ResultsError as error:
                fail(error)
        unknown = set(body) - {"name", "path_remap"}
        if unknown:
            raise HTTPException(422, f"Cannot change: {', '.join(sorted(unknown))}")
        job = ctx.jobs.update(job_id, changes) if job_id.isalnum() else None
        if job is None:
            raise HTTPException(404, f"Job '{job_id}' not found")
        service.renders.items.clear()
        return job
