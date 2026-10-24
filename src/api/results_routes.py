"""HTTP endpoints of the Results screen (see src/api/results.py)."""

import base64
from typing import Any, Dict, List, Optional

from fastapi import Body, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from src.api.accounts import request_user
from src.api.results import DEFAULT_USER, ResultsError, clean_rules
from src.api.storage import UNASSIGNED

VIEW_PATTERN = (
    "^(all|flagged|unflagged|reviewed|not_reviewed|verified|approved|corrected|errors"
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


class ManualAlignBody(BaseModel):
    points: List[List[float]] = Field(..., description="Clicked [x, y] in original pixels")
    kind: str = Field("corners", pattern="^(corners|index)$")
    names: Optional[List[str]] = Field(None, description="Index point per click (kind=index)")
    turn: int = Field(
        0, description="The further clockwise turn of the original view clicked on (0, 90, 180, 270)"
    )
    user: Optional[str] = None


class ManualValuesBody(BaseModel):
    values: Dict[str, Any] = Field(..., description="Output column -> typed value")
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


class ReturnBody(BaseModel):
    scan_ids: List[str] = Field(..., min_length=1, max_length=500)
    assign_to: Optional[str] = Field(None, description="Hand the sheets to this person")
    note: Optional[str] = Field(None, max_length=500)


class SheetRange(BaseModel):
    user: str = Field(..., min_length=1)
    first_sheet: Optional[int] = Field(
        None, ge=1, description="1 = the first of the job's sheets the view picks"
    )
    last_sheet: Optional[int] = Field(None, ge=1, description="Included; empty = the last")


class AssignBody(BaseModel):
    users: Optional[List[str]] = Field(None, max_length=100)
    ranges: Optional[List[SheetRange]] = Field(
        None,
        max_length=100,
        description="Instead of users: each person's sheet numbers (one job)",
    )
    scan_ids: Optional[List[str]] = Field(None, max_length=100000)
    template_id: Optional[str] = None
    job_id: Optional[str] = None
    view: str = Field("flagged", pattern=VIEW_PATTERN)
    flag: Optional[str] = None
    name: Optional[str] = None
    first_sheet: Optional[int] = Field(None, ge=1)
    last_sheet: Optional[int] = Field(None, ge=1)
    only_unassigned: bool = True
    limit: Optional[int] = Field(None, ge=1)


class UnassignBody(BaseModel):
    scan_ids: Optional[List[str]] = Field(None, max_length=100000)
    user: Optional[str] = None
    template_id: Optional[str] = None
    job_id: Optional[str] = None
    first_sheet: Optional[int] = Field(None, ge=1)
    last_sheet: Optional[int] = Field(None, ge=1)
    only_open: bool = Field(False, description="Only sheets the person has not finished")


class MoveBody(BaseModel):
    user: str = Field(..., min_length=1, description="Who has the sheets now")
    to: str = Field(..., min_length=1)
    template_id: Optional[str] = None
    job_id: Optional[str] = None
    first_sheet: Optional[int] = Field(None, ge=1)
    last_sheet: Optional[int] = Field(None, ge=1)
    only_open: bool = Field(False, description="Only sheets the person has not finished")


class BulkVerifyBody(BaseModel):
    scan_ids: List[str] = Field(..., min_length=1, max_length=500)


# Activity that changes a sheet after scanning (what an administrator checks)
CHANGE_ACTIONS = ("corrected", "reviewed", "regraded", "aligned", "typed")
# ...and what else puts a sheet in front of the administrator for approval
APPROVAL_ACTIONS = CHANGE_ACTIONS + ("verified", "unverified", "undone", "redone")


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
        return request_user(request, explicit, DEFAULT_USER)

    def overlay_response(result, info=None, **extra):
        payload = service.overlay(result, info)
        payload["assigned_to"] = ctx.index.assignee(result["scan_id"])
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
        assigned: Optional[str] = Query(
            None, description="Only sheets assigned to this user (__none__: nobody)"
        ),
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
            assigned=assigned,
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
        assigned: Optional[str] = None,
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
            assigned=assigned,
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
        """A person checked every value of this sheet (feeds the accuracy readout).
        With sign-in on, administrators do the final verification."""
        require_admin(request, "verify sheets")
        try:
            result, records = service.verify(
                scan_id, user_of(request, body.user if body else None)
            )
        except ResultsError as error:
            fail(error)
        return overlay_response(result, training_records=len(records))

    @app.delete("/scans/{scan_id}/verify", tags=["results"], dependencies=secured)
    def scan_unverify(scan_id: str, request: Request):
        require_admin(request, "take back a verification")
        try:
            result, _ = service.verify(scan_id, user_of(request), verified=False)
        except ResultsError as error:
            fail(error)
        return overlay_response(result)

    @app.post("/scans/{scan_id}/undo", tags=["results"], dependencies=secured)
    def scan_undo(scan_id: str, request: Request):
        """Undo the sheet's last change (correction, accept, verification,
        regrade, hand alignment, typed values), whoever made it."""
        try:
            result, step = service.undo(scan_id, user_of(request))
        except ResultsError as error:
            fail(error)
        return overlay_response(result, undone=step)

    @app.post("/scans/{scan_id}/redo", tags=["results"], dependencies=secured)
    def scan_redo(scan_id: str, request: Request):
        """Redo the change the last undo took back."""
        try:
            result, step = service.undo(scan_id, user_of(request), "redo")
        except ResultsError as error:
            fail(error)
        return overlay_response(result, redone=step)

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

    @app.get("/scans/{scan_id}/manual-align", tags=["results"], dependencies=secured)
    def scan_align_targets(scan_id: str):
        """Page size and index points a failed sheet can be aligned by."""
        from src.api.manual import align_targets

        try:
            return align_targets(service, service.load(scan_id))
        except ResultsError as error:
            fail(error)

    @app.post("/scans/{scan_id}/manual-align", tags=["results"], dependencies=secured)
    def scan_manual_align(scan_id: str, body: ManualAlignBody, request: Request):
        """Align a sheet by clicked page corners or index points and read it again."""
        from src.api.manual import manual_align

        try:
            record = manual_align(
                service,
                scan_id,
                body.points,
                body.kind,
                body.names,
                user_of(request, body.user),
                body.turn % 360 if body.turn % 90 == 0 else 0,
            )
            return overlay_response(record)
        except ResultsError as error:
            fail(error)

    @app.post("/scans/{scan_id}/manual-values", tags=["results"], dependencies=secured)
    def scan_manual_values(scan_id: str, body: ManualValuesBody, request: Request):
        """Type every output of a sheet the engine could not read."""
        from src.api.manual import manual_values

        try:
            record = manual_values(
                service, scan_id, body.values, user_of(request, body.user)
            )
            return overlay_response(record)
        except ResultsError as error:
            fail(error)

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

    def require_admin(request, what):
        """With sign-in on, only administrators may do this."""
        signed_in = getattr(request.state, "user", None)
        if signed_in and signed_in.get("role") != "admin":
            raise HTTPException(403, f"Only an administrator can {what}")

    def scanned_by(record):
        """Who scanned the sheet and when (a job's starter, or the upload's)."""
        rows, _ = ctx.index.list_activity(
            scan_id=record.get("scan_id"), action="scanned", limit=1
        )
        if rows:
            return {"by": rows[0]["user"], "at": rows[0]["at"]}
        job = ctx.jobs.get(record["job_id"]) if record.get("job_id") else None
        if job:
            return {"by": job.get("created_by"), "at": job.get("created_at")}
        return {"by": None, "at": record.get("created_at")}

    def sheet_changes(record):
        """The settings and values people changed on a sheet."""
        regrade = record.get("regrade") or {}
        return {
            "scanned": scanned_by(record),
            "regrade": {
                "by": regrade.get("by"),
                "at": regrade.get("at"),
                "template_overrides": regrade.get("template_overrides") or {},
                "config_overrides": regrade.get("config_overrides") or {},
            }
            if regrade
            else None,
            "manual_alignment": record.get("manual_alignment"),
            "manual_entry": record.get("manual_entry"),
            "verified": record.get("verified") or None,
            "values": (record.get("audit") or [])[-50:],
        }

    @app.get("/scans/{scan_id}/activity", tags=["results"], dependencies=secured)
    def scan_activity(scan_id: str, limit: int = Query(200, ge=1, le=1000)):
        """Who scanned, reviewed, corrected, regraded, aligned, typed and
        verified this sheet, newest first, with its changed settings and values."""
        try:
            record = service.load(scan_id)
        except ResultsError as error:
            fail(error)
        items, total = ctx.index.list_activity(scan_id=scan_id, limit=limit)
        return {"items": items, "total": total, **sheet_changes(record)}

    @app.get("/activity", tags=["results"], dependencies=secured)
    def activity_log(
        template_id: Optional[str] = None,
        job_id: Optional[str] = None,
        user: Optional[str] = None,
        action: Optional[str] = None,
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ):
        """Every action people took on sheets and jobs, newest first."""
        items, total = ctx.index.list_activity(
            template_id=template_id,
            job_id=job_id,
            user=user,
            action=action,
            limit=limit,
            offset=offset,
        )
        return {"items": items, "total": total}

    @app.get("/activity/sheets", tags=["results"], dependencies=secured)
    def changed_sheets(
        template_id: Optional[str] = None,
        job_id: Optional[str] = None,
        user: Optional[str] = None,
        include_checked: bool = Query(
            False, description="Also sheets approved after their last change"
        ),
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ):
        """Sheets people changed after scanning (corrected, reviewed, regraded,
        aligned or typed by hand) or verified, each with its changed settings
        and values, for an administrator to check and approve. Listed until
        approved; a later change lists the sheet again."""
        rows, total = ctx.index.changed_sheets(
            APPROVAL_ACTIONS,
            template_id=template_id,
            job_id=job_id,
            user=user,
            include_checked=include_checked,
            limit=limit,
            offset=offset,
        )
        items = []
        for row in rows:
            row["users"] = sorted(set((row["users"] or "").split(",")) - {""})
            row["actions"] = sorted(set((row["actions"] or "").split(",")) - {""})
            try:
                record = service.load(row["scan_id"])
            except ResultsError:
                continue
            row.update(sheet_changes(record))
            row["responses"] = record.get("responses") or {}
            row["approved"] = record.get("approved")
            row.pop("approved_id", None)
            row.pop("last_change", None)
            row["assigned_to"] = ctx.index.assignee(row["scan_id"])
            items.append(row)
        return {"items": items, "total": total}

    @app.post("/activity/verify", tags=["results"], dependencies=secured)
    def verify_sheets(body: BulkVerifyBody, request: Request):
        """Verify many changed sheets at once (administrators, when sign-in is on)."""
        require_admin(request, "verify sheets")
        user = user_of(request)
        done, failed = [], []
        for scan_id in dict.fromkeys(body.scan_ids):
            try:
                record = service.load(scan_id)
                if record.get("status") == "error" and not record.get("manual_values"):
                    raise ResultsError("The sheet has no values to verify", 422)
                service.verify(scan_id, user)
                done.append(scan_id)
            except ResultsError as error:
                failed.append({"scan_id": scan_id, "error": str(error)})
        return {"verified": done, "failed": failed}

    @app.post("/activity/approve", tags=["results"], dependencies=secured)
    def approve_sheets(body: BulkVerifyBody, request: Request):
        """The administrator's second check: approve sheets reviewers verified
        or changed (a sheet nobody verified is verified too). Administrators
        only, when sign-in is on."""
        require_admin(request, "approve sheets")
        user = user_of(request)
        done, failed = [], []
        for scan_id in dict.fromkeys(body.scan_ids):
            try:
                record = service.load(scan_id)
                if record.get("status") == "error" and not record.get("manual_values"):
                    raise ResultsError("The sheet has no values to approve", 422)
                service.approve(scan_id, user)
                done.append(scan_id)
            except ResultsError as error:
                failed.append({"scan_id": scan_id, "error": str(error)})
        return {"approved": done, "failed": failed}

    @app.post("/activity/return", tags=["results"], dependencies=secured)
    def return_sheets(body: ReturnBody, request: Request):
        """Send sheets back to review (administrators): the verification is
        taken back and the items flagged when the sheet was read wait in the
        review queue again; optionally hand them to someone."""
        require_admin(request, "send sheets back to review")
        user = user_of(request)
        done, failed = [], []
        for scan_id in dict.fromkeys(body.scan_ids):
            try:
                service.return_to_review(scan_id, user, body.note)
                done.append(scan_id)
            except ResultsError as error:
                failed.append({"scan_id": scan_id, "error": str(error)})
        if body.assign_to:
            check_people([body.assign_to])
        if body.assign_to and done:
            assign_sheets({body.assign_to: done}, user)
        return {"returned": done, "failed": failed}

    def check_people(names):
        """With sign-in on, work goes to active accounts only."""
        if not ctx.accounts.enabled():
            return
        unknown = []
        for name in names:
            row = ctx.accounts.get(name)
            if row is None or not row["active"]:
                unknown.append(name)
        if unknown:
            raise HTTPException(
                422, f"No active account named {', '.join(sorted(set(unknown)))}"
            )

    def sheet_range(first, last, job_id):
        if (first or last) and not job_id:
            raise HTTPException(422, "Pick one job to give sheet numbers")
        if first and last and last < first:
            raise HTTPException(422, f"Sheet {last} comes before sheet {first}")
        return (first, last) if first or last else None

    def assign_sheets(by_user, by):
        ctx.index.assign(by_user, by)
        rows = []
        for to, scan_ids in by_user.items():
            for row in ctx.index.list_scans_by_id(scan_ids):
                rows.append(
                    {
                        "scan_id": row["id"],
                        "job_id": row["job_id"],
                        "template_id": row["template_id"],
                        "action": "assigned",
                        "user": by,
                        "detail": {"to": to},
                    }
                )
        ctx.index.add_activity(rows)

    @app.post("/assignments", tags=["results"], dependencies=secured)
    def assign(body: AssignBody, request: Request):
        """Hand sheets to people for review (administrators). Either the given
        scan_ids, or every sheet matching the filters (by default flagged and
        not verified, not yet assigned), split into equal consecutive runs
        between the users in the order given; or, with ranges, each person's
        own sheet numbers of one job (the filters apply inside each range)."""
        require_admin(request, "assign sheets")

        def matching(sheets, limit=None):
            """Scan ids the view picks, in sheet order. sheets: (first, last),
            1-based places among the sheets the view picks (with Flagged,
            1 to 500 is the first 500 flagged sheets), already assigned
            sheets included in the count so the numbers stay put."""
            first, last = sheets or (None, None)
            taken = ctx.index.assigned_scan_ids(body.job_id) if body.only_unassigned else ()
            try:
                rows = ctx.index.iter_results(
                    template_id=body.template_id,
                    job_id=body.job_id,
                    view=body.view,
                    flag=body.flag,
                    name=body.name,
                    assigned=UNASSIGNED if body.only_unassigned and not sheets else None,
                )
                ids = []
                for place, row in enumerate(rows, 1):
                    if first and place < first:
                        continue
                    if last and place > last:
                        break
                    if sheets and row["id"] in taken:
                        continue
                    ids.append(row["id"])
                    if limit and len(ids) >= limit:
                        break
                return ids
            except ValueError as error:
                raise HTTPException(422, str(error)) from None

        if body.ranges:
            names = [r.user.strip() for r in body.ranges]
            if not all(names):
                raise HTTPException(422, "Name a person for every range")
            check_people(names)
            spans = [sheet_range(r.first_sheet, r.last_sheet, body.job_id) for r in body.ranges]
            ranked = sorted((s[0] or 1, s[1] or 10**9, n) for s, n in zip(spans, names))
            for (_, end, one), (start, _, two) in zip(ranked, ranked[1:]):
                if start <= end:
                    raise HTTPException(422, f"The sheets of {one} and {two} overlap")
            by_user = {}
            for name, sheets in zip(names, spans):
                ids = matching(sheets, body.limit)
                if ids:
                    by_user.setdefault(name, []).extend(ids)
            assign_sheets(by_user, user_of(request))
            return {
                "assigned": {name: len(ids) for name, ids in by_user.items()},
                "total": sum(len(ids) for ids in by_user.values()),
            }
        users = [u.strip() for u in body.users or [] if u and u.strip()]
        if not users:
            raise HTTPException(422, "Name at least one person")
        check_people(users)
        if body.scan_ids:
            scan_ids = list(dict.fromkeys(body.scan_ids))
        else:
            scan_ids = matching(
                sheet_range(body.first_sheet, body.last_sheet, body.job_id), body.limit
            )
        share, extra = divmod(len(scan_ids), len(users))
        by_user, start = {}, 0
        for index, name in enumerate(users):
            size = share + (1 if index < extra else 0)
            if size:
                by_user[name] = scan_ids[start : start + size]
            start += size
        assign_sheets(by_user, user_of(request))
        return {"assigned": {name: len(ids) for name, ids in by_user.items()}, "total": len(scan_ids)}

    @app.post("/assignments/clear", tags=["results"], dependencies=secured)
    def unassign(body: UnassignBody, request: Request):
        """Take sheets back from whoever they were assigned to (administrators):
        the given scan_ids, or every sheet of a user (within a template / job,
        optionally only some sheet numbers or only those not finished)."""
        require_admin(request, "change assignments")
        if body.scan_ids:
            ctx.index.unassign(body.scan_ids)
            return {"unassigned": len(body.scan_ids)}
        sheets = sheet_range(body.first_sheet, body.last_sheet, body.job_id)
        if body.user and (sheets or body.only_open):
            ids = ctx.index.assigned_ids(
                body.user, body.template_id, body.job_id, sheets, body.only_open
            )
            ctx.index.unassign(ids)
            return {"unassigned": len(ids)}
        count = ctx.index.unassign_where(
            template_id=body.template_id, job_id=body.job_id, user=body.user
        )
        return {"unassigned": count}

    @app.post("/assignments/move", tags=["results"], dependencies=secured)
    def move_assignments(body: MoveBody, request: Request):
        """Give one person's sheets to someone else (administrators): all of
        them, some sheet numbers, or only those not finished yet."""
        require_admin(request, "change assignments")
        to = body.to.strip()
        if to == body.user:
            raise HTTPException(422, "Pick someone else to move the sheets to")
        check_people([to])
        sheets = sheet_range(body.first_sheet, body.last_sheet, body.job_id)
        ids = ctx.index.assigned_ids(
            body.user, body.template_id, body.job_id, sheets, body.only_open
        )
        if ids:
            assign_sheets({to: ids}, user_of(request))
        return {"moved": len(ids), "to": to}

    @app.get("/assignments/summary", tags=["results"], dependencies=secured)
    def assignment_summary(
        template_id: Optional[str] = None, job_id: Optional[str] = None
    ):
        """Per person: sheets assigned, still to review, reviewed, verified,
        approved, sent back, the sheet numbers they hold and when they last
        worked; and flagged sheets nobody has yet."""
        return {
            "users": ctx.index.assignment_summary(template_id=template_id, job_id=job_id),
            "unassigned_flagged": ctx.index.count_results(
                template_id=template_id, job_id=job_id, view="flagged", assigned=UNASSIGNED
            ),
        }

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
        """Edit a job's name, label (how its source is shown) or path_remap
        ([{"from": ..., "to": ...}])."""
        changes = {}
        if "name" in body:
            changes["name"] = str(body["name"] or "")
        if "label" in body:
            changes["label"] = " ".join(str(body["label"] or "").split())[:60] or None
        if "path_remap" in body:
            try:
                changes["path_remap"] = clean_rules(body["path_remap"])
            except ResultsError as error:
                fail(error)
        unknown = set(body) - {"name", "label", "path_remap"}
        if unknown:
            raise HTTPException(422, f"Cannot change: {', '.join(sorted(unknown))}")
        job = ctx.jobs.update(job_id, changes) if job_id.isalnum() else None
        if job is None:
            raise HTTPException(404, f"Job '{job_id}' not found")
        service.renders.items.clear()
        return job
