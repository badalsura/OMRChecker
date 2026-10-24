"""
Compare tab: reconcile our results with another reader's export (a physical
OMR scanner such as Scantron OpScan 6, or any CSV / fixed-width file).

    POST   /compare/preview                         read an external file: columns, sample, suggested mapping
    GET    /compare/sessions                        comparisons (with progress)
    POST   /compare/sessions                        create one from a previewed file
    GET    /compare/sessions/{id}                   settings, summary, progress
    PATCH  /compare/sessions/{id}                   change mapping / key / rules (compared again)
    POST   /compare/sessions/{id}/rebuild           compare again (our values changed after review)
    POST   /compare/sessions/{id}/sheets/{scan}/recompare compare one sheet again (after a regrade)
    GET    /compare/sessions/{id}/sheets/{scan}/fields   every compared field side by side
    DELETE /compare/sessions/{id}                   delete it and its decisions
    POST   /compare/sessions/{id}/claim             lock the next / previous / a given sheet
    POST   /compare/sessions/{id}/sheets/{scan}     save the choices for one sheet
    POST   /compare/sessions/{id}/sheets/{scan}/release   let go of a sheet (skip)
    GET    /compare/sessions/{id}/sheets/{scan}/page      the aligned page (JPEG)
    GET    /compare/sessions/{id}/export?kind=      reconciled | audit | unmatched (CSV)

A comparison runs in the background, a part of the sheets at a time: the
first sheets can be reviewed while the rest is compared, and the session
carries a live "building" counter. Decisions are kept in the reconciled layer
(src/api/compare.py); with rules save_to "results" a choice is also written to
our result as a correction (audited and undoable, as one made in Results).
"""

import csv
import io
import json
import re
import logging
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
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
from src.api.results import DEFAULT_USER, ResultsError
from src.api.review import item_box, related_names
from src.api.storage import new_id, read_json, safe_filename, write_json_atomic

PAGE_WIDTH = 1600
# A request that starts a comparison waits this long for it to finish (a small
# one answers with its result); a longer one carries on in the background
BUILD_WAIT_SECONDS = 3.0

logger = logging.getLogger(__name__)


def natural(text):
    """Sort key: 'sheet10' after 'sheet9'."""
    return [
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r"(\d+)", str(text))
    ]


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
        """Our sheets in order: a job's scan order, else by file name (numbers
        in the name counted as numbers)."""
        rows = list(
            ctx.index.iter_results(template_id=template_id, job_id=job_id or None)
        )
        if not job_id:
            rows.sort(key=lambda row: natural(row.get("file_name") or ""))
        return rows

    def key_values(scans, name):
        """{scan_id: our value of `name`} for the join, from a cache kept per
        result file version (reading 12,000 results takes half a minute)."""
        from src.api.review import current_value

        cached = store.cached_keys(name)
        out, fresh = {}, []

        def read(row):
            path = ctx.data.scan_dir(row["id"]) / "result.json"
            try:
                mtime = path.stat().st_mtime
            except OSError:
                return row["id"], None, ""
            hit = cached.get(row["id"])
            if hit and hit[0] == mtime:
                return row["id"], None, hit[1]
            value = str(current_value(read_json(path) or {}, name) or "")
            return row["id"], mtime, value

        with ThreadPoolExecutor(max_workers=8) as pool:
            for scan_id, mtime, value in pool.map(read, scans):
                out[scan_id] = value
                if mtime is not None:
                    fresh.append((scan_id, mtime, value))
        if fresh:
            store.cache_keys(name, fresh)
        return out

    def empty_values(scans):
        for row in scans[:20]:
            result = load_result(row["id"])
            if result:
                return ctx.results.template_info(result)["empty_values"]
        return {}

    # ---- comparing, in the background ------------------------------------
    builds = {}  # session_id -> live state of its running comparison
    builds_lock = threading.Lock()
    plans = {}  # session_id -> (settings, plan) for comparing one sheet again

    def plan_of(session):
        cached = plans.get(session["id"])
        if cached and cached[0] == session["settings"]:
            return cached[1]
        template_json = template_or_404(session["template_id"])
        try:
            plan = compare.prepare(
                session, compare.load_source(root / session["id"]), template_json
            )
        except compare.CompareError as error:
            fail(error)
        plans[session["id"]] = (session["settings"], plan)
        return plan

    def start_build(session, wait=None):
        """Check the settings (an error answers the request), then compare in
        a background thread; wait up to BUILD_WAIT_SECONDS for it."""
        plan = plan_of(session)
        session_id = session["id"]
        state = {"done": 0, "total": None, "summary": None, "started_at": time.time()}
        with builds_lock:
            builds[session_id] = state  # an older run of this session stops
        previous = session.get("summary") or {}
        store.update_session(session_id, summary={**previous, "building": True})
        thread = threading.Thread(
            target=build_worker,
            args=(dict(session), plan, state),
            name=f"compare-{session_id}",
            daemon=True,
        )
        thread.start()
        thread.join(BUILD_WAIT_SECONDS if wait is None else wait)

    def build_worker(session, plan, state):
        session_id = session["id"]

        def current():
            return builds.get(session_id) is state

        def progress(done, total, summary):
            state.update(done=done, total=total, summary=summary)

        try:
            scans = our_sheets(session["template_id"], session.get("job_id"))
            state["total"] = len(scans)
            ours = (session["settings"].get("key") or {}).get("ours") or "file"
            keys = None if ours in ("file", "order") else key_values(scans, ours)
            compare.run(
                store,
                session,
                plan,
                scans,
                load_result,
                empty_values(scans),
                keys,
                progress,
                current,
            )
        except Exception as error:  # the session shows what went wrong
            logger.exception(f"Comparison {session_id} failed")
            if current():
                store.update_session(
                    session_id,
                    summary={**(state["summary"] or {}), "error": str(error)},
                )
        finally:
            with builds_lock:
                if builds.get(session_id) is state:
                    builds.pop(session_id)

    def resume_builds():
        """Comparisons cut short by a restart carry on."""
        for session in store.sessions():
            if (session.get("summary") or {}).get("building"):
                try:
                    start_build(session, wait=0)
                except HTTPException as error:
                    store.update_session(
                        session["id"],
                        summary={
                            **session["summary"],
                            "building": False,
                            "error": error.detail,
                        },
                    )

    threading.Thread(target=resume_builds, name="compare-resume", daemon=True).start()

    def session_or_404(session_id):
        try:
            return store.session(session_id)
        except compare.CompareError as error:
            fail(error)

    def with_progress(session):
        out = {**session, "progress": store.progress(session["id"])}
        state = builds.get(session["id"])
        if state is not None:
            out["building"] = {
                "done": state["done"],
                "total": state["total"],
                "started_at": state["started_at"],
            }
            if state["summary"]:
                out["summary"] = {**state["summary"], "building": True}
        elif (session.get("summary") or {}).get("building"):
            out["building"] = {"done": 0, "total": None, "started_at": None}
        return out

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
        return {"sessions": [with_progress(s) for s in store.sessions(template_id)]}

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
            start_build(session)
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
            start_build(session)
        except HTTPException:
            store.update_session(session_id, settings=previous)
            raise
        return with_progress(store.session(session_id))

    @app.post(
        "/compare/sessions/{session_id}/rebuild", tags=["compare"], dependencies=secured
    )
    def compare_rebuild(session_id: str):
        start_build(session_or_404(session_id))
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
        with builds_lock:
            builds.pop(session_id, None)  # a running comparison stops
        plans.pop(session_id, None)
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
            "save_to": (session["settings"].get("rules") or {}).get("save_to")
            or "layer",
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
        session = session_or_404(session_id)
        user = user_of(request)
        try:
            state = store.decide(session_id, scan_id, user, body.decisions)
        except compare.CompareError as error:
            fail(error)
        out = {"state": state, "progress": store.progress(session_id)}
        rules = session["settings"].get("rules") or {}
        if rules.get("save_to") == "results" and body.decisions:
            out.update(
                write_to_results(session_id, scan_id, list(body.decisions), user)
            )
        return out

    def write_to_results(session_id, scan_id, names, user):
        """save_to "results": our result takes each chosen value (back to the
        value as read when a choice is undone or ours is kept), as a correction
        in Results would. A value our field cannot take (their bare "*" for a
        multi-mark) is left for the reviewer to type."""
        from src.api.review import current_value

        items = {item["name"]: item for item in store.items(session_id, scan_id)}
        result = load_result(scan_id) or {}
        changes = {}
        for name in names:
            item = items.get(name)
            if item is None:
                continue
            if item["source"] in ("external", "manual"):
                target = item["chosen"] or ""
            else:
                target = item["ours"] or ""
            now = current_value(result, name)
            if str("" if now is None else now) != target:
                changes[name] = target
        if not changes:
            return {"written": []}
        try:
            ctx.results.correct(scan_id, changes, None, user, source="compare")
            return {"written": sorted(changes)}
        except ResultsError:
            pass
        written, refused = [], {}
        for name, value in changes.items():
            try:
                ctx.results.correct(
                    scan_id, {name: value}, None, user, source="compare"
                )
                written.append(name)
            except ResultsError as error:
                refused[name] = str(error)
        return {"written": written, "not_written": refused}

    @app.get(
        "/compare/sessions/{session_id}/sheets/{scan_id}/fields",
        tags=["compare"],
        dependencies=secured,
    )
    def compare_fields(session_id: str, scan_id: str):
        """Every compared field of the sheet side by side (their value, ours,
        mismatch / match / skipped / outside), with its box on the page."""
        session = session_or_404(session_id)
        try:
            sheet = store.sheet(session_id, scan_id)
        except compare.CompareError as error:
            fail(error)
        plan = plan_of(session)
        found = plan["external"].get(sheet["key"])
        if found is None:
            return {"fields": []}
        result = load_result(scan_id) or {}
        info = ctx.results.template_info(result) if result else {}
        labels = info.get("custom_labels") or {}
        fields = compare.sheet_fields(
            plan, found[1], result, info.get("empty_values") or {}
        )
        for field in fields:
            field["box"] = item_box(result, field["name"], labels)[1]
        return {"fields": fields}

    @app.post(
        "/compare/sessions/{session_id}/sheets/{scan_id}/recompare",
        tags=["compare"],
        dependencies=secured,
    )
    def compare_recompare(session_id: str, scan_id: str, request: Request):
        """Compare one sheet again with our current values (after a regrade or
        a correction in Results), keeping the choices whose values held. The
        sheet stays with whoever holds it; null when nothing differs now."""
        session = session_or_404(session_id)
        try:
            sheet = store.sheet(session_id, scan_id)
        except compare.CompareError as error:
            fail(error)
        plan = plan_of(session)
        result = load_result(scan_id) or {}
        row = {
            "id": scan_id,
            "file_name": sheet["file_name"],
            "status": result.get("status"),
        }
        one = {**plan, "chosen": {sheet["key"]: scan_id}}
        fresh, items = compare.compare_sheet(
            one,
            sheet["ord"],
            row,
            sheet["key"],
            load_result,
            ctx.results.template_info(result)["empty_values"] if result else {},
        )
        store.write_sheets(session_id, [fresh], items)
        sheet = store.sheet(session_id, scan_id)
        if sheet["state"] not in compare.SESSION_STATES:
            return {
                "sheet": None,
                "state": sheet["state"],
                "progress": store.progress(session_id),
            }
        return {"sheet": sheet_payload(session, sheet)}

    @app.post(
        "/compare/sessions/{session_id}/sheets/{scan_id}/release",
        tags=["compare"],
        dependencies=secured,
    )
    def compare_release(session_id: str, scan_id: str, request: Request):
        if not store.release(session_id, scan_id, user_of(request)):
            raise HTTPException(409, "You do not hold this sheet")
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
        matched: bool = Query(
            False,
            description="reconciled: only the sheets that found a row in their file",
        ),
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
                    if matched and sheet["row_no"] is None:
                        continue
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
