"""
Compare tab: reconcile our results with another reader's export (a physical
OMR scanner such as Scantron OpScan 6, or any CSV / fixed-width file).

    POST   /compare/preview                         read an external file: columns, sample, suggested mapping
    GET    /compare/sessions                        comparisons (with progress)
    POST   /compare/sessions                        create one from a previewed file
    GET    /compare/sessions/{id}                   settings, summary, progress
    PATCH  /compare/sessions/{id}                   change mapping / key / rules (compared again)
    POST   /compare/sessions/{id}/rebuild           compare again (our values changed after review)
    DELETE /compare/sessions/{id}                   delete it and its decisions
    POST   /compare/sessions/{id}/claim             lock the next / previous / a given sheet
    POST   /compare/sessions/{id}/sheets/{scan}     save the choices for one sheet
    POST   /compare/sessions/{id}/sheets/{scan}/release   let go of a sheet (skip)
    GET    /compare/sessions/{id}/sheets/{scan}/page      the aligned page (JPEG)
    GET    /compare/sessions/{id}/export?kind=      reconciled | audit | unmatched (CSV)

Decisions are kept in the reconciled layer (src/api/compare.py); read results
are never changed.
"""

import csv
import io
import json
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Union

import cv2
from fastapi import File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from src.api import compare
from src.api.accounts import request_user
from src.api.results import DEFAULT_USER
from src.api.review import item_box, related_names
from src.api.storage import new_id, read_json, safe_filename, write_json_atomic

PAGE_WIDTH = 1600


class KeySpec(BaseModel):
    column: Optional[str] = None
    ours: str = Field(
        "file",
        description="file, order, or one of our names (an imprint zone, a roll number)",
    )
    normalize: str = Field("digits", pattern="^(digits|text|exact)$")


class SessionBody(BaseModel):
    upload: str
    template_id: str
    job_id: Optional[str] = None
    name: Optional[str] = None
    parse: Dict = Field(default_factory=dict)
    mapping: Dict[str, Optional[str]] = Field(default_factory=dict)
    key: KeySpec = Field(default_factory=KeySpec)
    rules: Dict = Field(default_factory=dict)


class SessionPatch(BaseModel):
    name: Optional[str] = None
    mapping: Optional[Dict[str, Optional[str]]] = None
    key: Optional[KeySpec] = None
    rules: Optional[Dict] = None


class ClaimBody(BaseModel):
    after: Optional[int] = Field(None, description="ord of the sheet you are on")
    direction: str = Field("next", pattern="^(next|prev)$")
    scan_id: Optional[str] = None


class DecideBody(BaseModel):
    decisions: Dict[str, Optional[Union[str, Dict]]] = Field(
        default_factory=dict,
        description='{field: "external" | "ours" | null | {"source": "manual", "value": "..."}}',
    )


class ReparseBody(BaseModel):
    template_id: str
    parse: Dict = Field(default_factory=dict)


class ProfileBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    template_id: Optional[str] = None
    parse: Dict = Field(default_factory=dict)
    mapping: Dict[str, Optional[str]] = Field(default_factory=dict)
    key: KeySpec = Field(default_factory=KeySpec)
    rules: Dict = Field(default_factory=dict)


def register(app, ctx, secured):
    root = ctx.data.root / "compare"
    uploads = root / "uploads"
    store = compare.CompareStore(ctx.data.root / "compare.sqlite3")
    ctx.compare = store

    def user_of(request):
        return request_user(request, None, DEFAULT_USER)

    def fail(error):
        raise HTTPException(error.status, str(error)) from None

    def template_or_404(template_id):
        template_json = (
            ctx.templates.template_json(template_id)
            if ctx.templates.exists(template_id)
            else None
        )
        if template_json is None:
            raise HTTPException(404, f"Template '{template_id}' not found")
        return template_json

    def load_result(scan_id):
        return read_json(ctx.data.scan_dir(scan_id) / "result.json")

    def our_sheets(template_id, job_id):
        return [
            row
            for row in ctx.index.iter_results(
                template_id=template_id, job_id=job_id or None
            )
        ]

    def empty_values(scans):
        for row in scans[:20]:
            result = load_result(row["id"])
            if result:
                return ctx.results.template_info(result)["empty_values"]
        return {}

    def run_build(session):
        template_json = template_or_404(session["template_id"])
        scans = our_sheets(session["template_id"], session.get("job_id"))
        try:
            return compare.build(
                store,
                session,
                compare.load_source(root / session["id"]),
                scans,
                load_result,
                template_json,
                empty_values(scans),
            )
        except compare.CompareError as error:
            fail(error)

    def session_or_404(session_id):
        try:
            return store.session(session_id)
        except compare.CompareError as error:
            fail(error)

    def with_progress(session):
        return {**session, "progress": store.progress(session["id"])}

    # ------------------------------------------------------------------
    @app.post("/compare/preview", tags=["compare"], dependencies=secured)
    def compare_preview(
        file: UploadFile = File(...),
        template_id: str = Form(...),
        parse: str = Form("{}"),
    ):
        template_json = template_or_404(template_id)
        try:
            options = json.loads(parse or "{}")
        except ValueError:
            raise HTTPException(422, "parse must be JSON") from None
        content = file.file.read(ctx.settings.max_upload_mb * 1024 * 1024 + 1)
        if len(content) > ctx.settings.max_upload_mb * 1024 * 1024:
            raise HTTPException(
                413, f"The file exceeds the {ctx.settings.max_upload_mb} MB limit"
            )
        token = new_id()
        described = describe(token, content, options, template_json, file.filename)
        folder = uploads / token
        folder.mkdir(parents=True, exist_ok=True)
        suffix = Path(file.filename or "").suffix.lower()[:8] or ".txt"
        (folder / f"source{suffix}").write_bytes(content)
        write_json_atomic(
            folder / "meta.json", {"name": file.filename, "at": time.time()}
        )
        return described

    @app.post("/compare/preview/{token}", tags=["compare"], dependencies=secured)
    def compare_reparse(token: str, body: ReparseBody):
        """Read the uploaded file again with other settings (header, names, split...)."""
        template_json = template_or_404(body.template_id)
        folder = uploads / token
        if not token.isalnum() or not folder.is_dir():
            raise HTTPException(404, "Read the file again (the preview expired)")
        meta = read_json(folder / "meta.json", {})
        try:
            return describe(
                token,
                compare.load_source(folder),
                body.parse,
                template_json,
                meta.get("name"),
            )
        except compare.CompareError as error:
            fail(error)

    def describe(token, content, options, template_json, file_name):
        try:
            columns, rows = compare.read_table(content, options)
        except compare.CompareError as error:
            fail(error)
        names, questions, zones, imprint = compare.template_names(template_json)
        return {
            "upload": token,
            "file_name": file_name,
            "columns": columns,
            "rows": rows[: compare.SAMPLE_ROWS],
            "count": len(rows),
            "names": names,
            "questions": questions,
            "zones": zones,
            "imprint_zones": imprint,
            "mapping": compare.suggest_mapping(columns, names, questions),
            "key": compare.suggest_key(columns, names, imprint),
            "rules": compare.DEFAULT_RULES,
        }

    @app.get("/compare/profiles", tags=["compare"], dependencies=secured)
    def compare_profiles():
        return {"profiles": store.profiles()}

    @app.post(
        "/compare/profiles", tags=["compare"], dependencies=secured, status_code=201
    )
    def compare_save_profile(body: ProfileBody, request: Request):
        settings = {
            "parse": body.parse,
            "mapping": {k: v for k, v in body.mapping.items() if v},
            "key": body.key.model_dump(),
            "rules": {**compare.DEFAULT_RULES, **body.rules},
        }
        store.save_profile(
            body.name.strip(), body.template_id, settings, user_of(request)
        )
        return {"name": body.name.strip(), "settings": settings}

    @app.delete("/compare/profiles/{name}", tags=["compare"], dependencies=secured)
    def compare_delete_profile(name: str):
        if not store.delete_profile(name):
            raise HTTPException(404, "Profile not found")
        return {"deleted": name}

    @app.get("/compare/sessions", tags=["compare"], dependencies=secured)
    def compare_sessions(template_id: Optional[str] = None):
        return {"sessions": store.sessions(template_id)}

    @app.post(
        "/compare/sessions", tags=["compare"], dependencies=secured, status_code=201
    )
    def compare_create(body: SessionBody, request: Request):
        template_json = template_or_404(body.template_id)
        staged = uploads / body.upload
        if not body.upload.isalnum() or not staged.is_dir():
            raise HTTPException(404, "Read the file again (the preview expired)")
        names, questions, _, _ = compare.template_names(template_json)
        unknown = sorted(
            {
                n
                for n in body.mapping.values()
                if n and n not in set(names) | set(questions)
            }
        )
        if unknown:
            raise HTTPException(
                422, f"Not a field of this template: {', '.join(unknown[:5])}"
            )
        meta = read_json(staged / "meta.json", {})
        session_id = compare.new_session_id()
        folder = root / session_id
        shutil.move(str(staged), str(folder))
        session = {
            "id": session_id,
            "name": body.name or meta.get("name") or "Comparison",
            "template_id": body.template_id,
            "job_id": body.job_id or None,
            "source_name": meta.get("name"),
            "settings": {
                "parse": body.parse,
                "mapping": {k: v for k, v in body.mapping.items() if v},
                "key": body.key.model_dump(),
                "rules": {**compare.DEFAULT_RULES, **body.rules},
            },
            "created_by": user_of(request),
            "created_at": time.time(),
        }
        store.add_session(session)
        try:
            session["summary"] = run_build(session)
        except HTTPException:
            store.delete_session(session_id)
            shutil.rmtree(folder, ignore_errors=True)
            raise
        return with_progress(store.session(session_id))

    @app.get("/compare/sessions/{session_id}", tags=["compare"], dependencies=secured)
    def compare_session(session_id: str):
        return with_progress(session_or_404(session_id))

    @app.patch("/compare/sessions/{session_id}", tags=["compare"], dependencies=secured)
    def compare_patch(session_id: str, body: SessionPatch):
        session = session_or_404(session_id)
        settings = dict(session["settings"])
        if body.mapping is not None:
            settings["mapping"] = {k: v for k, v in body.mapping.items() if v}
        if body.key is not None:
            settings["key"] = body.key.model_dump()
        if body.rules is not None:
            settings["rules"] = {**settings.get("rules", {}), **body.rules}
        previous = session["settings"]
        session["settings"] = settings
        store.update_session(
            session_id, settings=settings, **({"name": body.name} if body.name else {})
        )
        try:
            run_build(session)
        except HTTPException:
            store.update_session(session_id, settings=previous)
            raise
        return with_progress(store.session(session_id))

    @app.post(
        "/compare/sessions/{session_id}/rebuild", tags=["compare"], dependencies=secured
    )
    def compare_rebuild(session_id: str):
        run_build(session_or_404(session_id))
        return with_progress(store.session(session_id))

    @app.delete(
        "/compare/sessions/{session_id}", tags=["compare"], dependencies=secured
    )
    def compare_delete(session_id: str, request: Request):
        session = session_or_404(session_id)
        signed_in = getattr(request.state, "user", None)
        if (
            signed_in
            and signed_in.get("role") != "admin"
            and signed_in["name"] != session["created_by"]
        ):
            raise HTTPException(
                403,
                "Only an administrator or the person who made it can delete a comparison",
            )
        store.delete_session(session_id)
        shutil.rmtree(root / session_id, ignore_errors=True)
        return {"deleted": session_id}

    # ------------------------------------------------------------------
    # reviewing
    # ------------------------------------------------------------------
    def sheet_payload(session, sheet):
        result = load_result(sheet["scan_id"]) or {}
        info = ctx.results.template_info(result) if result else {"custom_labels": {}}
        labels = info.get("custom_labels") or {}
        size = (result.get("geometry") or {}).get("aligned_size")
        if not size:
            image = ctx.aligned_image(sheet["scan_id"], result)
            size = [image.shape[1], image.shape[0]] if image is not None else None
        items = []
        for item in store.items(session["id"], sheet["scan_id"]):
            _, box = item_box(result, item["name"], labels)
            sources = [item["name"]] + related_names(result, item["name"], labels)
            flags, confidences = [], []
            for name in sources:
                for group in ("fields", "zones", "checks"):
                    found = (result.get(group) or {}).get(name)
                    if isinstance(found, dict):
                        flags.extend(
                            f for f in found.get("flags") or [] if f not in flags
                        )
                        if found.get("confidence") is not None:
                            confidences.append(found["confidence"])
            items.append(
                {
                    "name": item["name"],
                    "external": item["external"],
                    "ours": item["ours"],
                    "source": item["source"],
                    "chosen": item["chosen"],
                    "decided_by": item["decided_by"],
                    "box": box,
                    "flags": flags,
                    "confidence": min(confidences) if confidences else None,
                }
            )
        position = store.query(
            "SELECT COUNT(*) AS n FROM sheets WHERE session_id=? AND ord <= ? AND state IN "
            "('pending','partial','done')",
            (session["id"], sheet["ord"]),
        )[0]["n"]
        return {
            "scan_id": sheet["scan_id"],
            "ord": sheet["ord"],
            "key": sheet["key"],
            "file_name": sheet["file_name"] or result.get("file_id"),
            "state": sheet["state"],
            "row_no": sheet["row_no"],
            "done_by": sheet["done_by"],
            "position": position,
            "page": f"/compare/sessions/{session['id']}/sheets/{sheet['scan_id']}/page",
            "page_size": size,
            "items": items,
            "progress": store.progress(session["id"]),
        }

    @app.post(
        "/compare/sessions/{session_id}/claim", tags=["compare"], dependencies=secured
    )
    def compare_claim(session_id: str, body: ClaimBody, request: Request):
        session = session_or_404(session_id)
        try:
            sheet = store.claim(
                session_id, user_of(request), body.after, body.direction, body.scan_id
            )
        except compare.CompareError as error:
            fail(error)
        if sheet is None:
            return {"sheet": None, "progress": store.progress(session_id)}
        return {"sheet": sheet_payload(session, sheet)}

    @app.post(
        "/compare/sessions/{session_id}/sheets/{scan_id}",
        tags=["compare"],
        dependencies=secured,
    )
    def compare_decide(
        session_id: str, scan_id: str, body: DecideBody, request: Request
    ):
        session_or_404(session_id)
        try:
            state = store.decide(session_id, scan_id, user_of(request), body.decisions)
        except compare.CompareError as error:
            fail(error)
        return {"state": state, "progress": store.progress(session_id)}

    @app.post(
        "/compare/sessions/{session_id}/sheets/{scan_id}/release",
        tags=["compare"],
        dependencies=secured,
    )
    def compare_release(session_id: str, scan_id: str, request: Request):
        store.release(session_id, scan_id, user_of(request))
        return {"released": scan_id}

    @app.get(
        "/compare/sessions/{session_id}/sheets/{scan_id}/page",
        tags=["compare"],
        dependencies=secured,
    )
    def compare_page(session_id: str, scan_id: str):
        try:
            store.sheet(session_id, scan_id)
        except compare.CompareError as error:
            fail(error)
        result = load_result(scan_id)
        image = ctx.aligned_image(scan_id, result) if result else None
        if image is None:
            raise HTTPException(
                404, "No image of this sheet (the original file moved or changed)"
            )
        if image.shape[1] > PAGE_WIDTH:
            scale = PAGE_WIDTH / image.shape[1]
            image = cv2.resize(
                image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA
            )
        ok, buffer = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 85])
        return Response(
            buffer.tobytes(),
            media_type="image/jpeg",
            headers={"Cache-Control": "private, max-age=3600"},
        )

    # ------------------------------------------------------------------
    # export
    # ------------------------------------------------------------------
    def csv_stream(header, rows):
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(header)
        for count, row in enumerate(rows, 1):
            writer.writerow(row)
            if count % 500 == 0:
                yield buffer.getvalue()
                buffer.seek(0)
                buffer.truncate()
        yield buffer.getvalue()

    def when(at):
        return datetime.fromtimestamp(at).isoformat(timespec="seconds") if at else ""

    @app.get(
        "/compare/sessions/{session_id}/export", tags=["compare"], dependencies=secured
    )
    def compare_export(
        session_id: str,
        kind: str = Query("reconciled", pattern="^(reconciled|audit|unmatched)$"),
    ):
        session = session_or_404(session_id)
        sheets = store.iter_sheets(session_id)
        files = {sheet["scan_id"]: sheet for sheet in sheets}
        if kind == "audit":
            header = [
                "at",
                "user",
                "file_name",
                "key",
                "field",
                "external",
                "ours",
                "chosen",
                "source",
                "action",
            ]
            rows = (
                [
                    when(row["at"]),
                    row["user"],
                    (files.get(row["scan_id"]) or {}).get("file_name", ""),
                    (files.get(row["scan_id"]) or {}).get("key", ""),
                    row["name"],
                    row["external"],
                    row["ours"],
                    row["chosen"] if row["chosen"] is not None else "",
                    row["source"] or "",
                    row["action"],
                ]
                for row in store.audit(session_id)
            )
        elif kind == "unmatched":
            header = ["side", "row_or_file", "key", "reason"]
            external = [
                ["external", row["row_no"], row["key"], row["reason"]]
                for row in store.unmatched(session_id)
            ]
            ours = [
                ["ours", sheet["file_name"], sheet["key"], sheet["state"]]
                for sheet in sheets
                if sheet["state"] in ("no_record", "duplicate", "not_read")
            ]
            rows = iter(external + ours)
        else:
            items = store.all_items(session_id)
            names = compare.output_names(template_or_404(session["template_id"]))

            def reconciled():
                for sheet in sheets:
                    result = load_result(sheet["scan_id"]) or {}
                    responses = dict(result.get("responses") or {})
                    unresolved, external = [], 0
                    for item in items.get(sheet["scan_id"], []):
                        if item["source"]:
                            responses[item["name"]] = item["chosen"]
                            external += item["source"] == "external"
                        else:
                            unresolved.append(item["name"])
                    yield [
                        sheet["file_name"] or result.get("file_id", ""),
                        sheet["scan_id"],
                        sheet["key"],
                        sheet["state"],
                        sheet["mismatches"],
                        external,
                        " ".join(unresolved),
                        sheet["done_by"] or "",
                    ] + [responses.get(name, "") for name in names]

            header = [
                "file_name",
                "scan_id",
                "key",
                "compare_status",
                "mismatches",
                "external_kept",
                "unresolved",
                "reconciled_by",
            ] + names
            rows = reconciled()
        filename = safe_filename(f"{session['name']}_{kind}.csv", f"compare_{kind}.csv")
        return StreamingResponse(
            csv_stream(header, rows),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
