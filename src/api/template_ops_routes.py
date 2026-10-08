"""
Template housekeeping and scoring helpers (plan items 6, 7, 12):

- POST /templates/{id}/duplicate      copy template, config, answer key and
                                       reference image into a new layout
- POST /templates/{id}/rename         change the display name (id kept)
- POST /templates/{id}/confirm-warnings  permanently clear generator warnings
- POST /templates/{id}/validate       check template/config/evaluation JSON
                                       without saving (JSON editor)
- POST /templates/{id}/read-sheet     read one sheet, nothing stored (answer
                                       key from a master sheet, scoring preview)
- POST /templates/{id}/score-preview  score responses with a draft evaluation
- POST /templates/answer-key/parse    rows of a CSV / Excel answer key file
"""

import csv
import io
import json
import shutil
import tempfile
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import Body, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from src.api.storage import read_json, safe_filename, write_json_atomic
from src.api.templates import META_FILE, schema_errors

DEFAULT_USER = "local"
NAME_MAX = 120
# Meta keys carried over to a copy (the rest is per layout)
COPIED_META = ("report", "report_confirmed", "generated", "reference", "samples")


class NameBody(BaseModel):
    name: str = Field(..., description="New display name (must be unique)")


class ValidateBody(BaseModel):
    template: Optional[Dict[str, Any]] = Field(
        None, description="template.json to check (default: the saved one)"
    )
    config: Optional[Dict[str, Any]] = Field(
        None, description="config.json to check (default: the saved one)"
    )
    evaluation: Optional[Dict[str, Any]] = Field(
        None, description="evaluation.json to check (default: the saved one)"
    )
    clear: list = Field(
        default_factory=list,
        description='Files to treat as absent, e.g. ["evaluation"] when it was emptied',
    )
    engine: bool = Field(
        True, description="Also build the engine (overlaps, groups, assets)"
    )


class PreviewBody(BaseModel):
    evaluation: Dict[str, Any] = Field(..., description="Draft evaluation.json")
    responses: Optional[Dict[str, Any]] = Field(
        None, description="Concatenated responses of one sheet"
    )
    scan_id: Optional[str] = Field(
        None, description="Use the stored responses of this scan instead"
    )


def clean_name(name):
    name = " ".join(str(name or "").split())
    if not name:
        raise HTTPException(422, "The name can't be empty")
    if len(name) > NAME_MAX:
        raise HTTPException(422, f"The name is longer than {NAME_MAX} characters")
    return name


def name_owner(store, name, except_id=None):
    """Id of the template already using this name (case-insensitive), or None."""
    key = name.casefold()
    for item in store.list():
        if item["id"] != except_id and str(item.get("name", "")).casefold() == key:
            return item["id"]
    return None


def json_errors(validator_name, data, defaults=None):
    from src.schemas import SCHEMA_VALIDATORS
    from src.utils.parsing import OVERRIDE_MERGER

    if not isinstance(data, dict):
        return [{"path": "$root", "message": "must be a JSON object"}]
    if defaults is not None:
        data = OVERRIDE_MERGER.merge(deepcopy(defaults), deepcopy(data))
    errors = []
    for error in sorted(
        SCHEMA_VALIDATORS[validator_name].iter_errors(data),
        key=lambda e: [str(p) for p in e.path],
    ):
        path = ".".join(str(p) for p in error.path) or "$root"
        errors.append({"path": path, "message": error.message})
    return errors


def copy_template_dir(source, target):
    """Copy a template folder (no backups, no meta) into an existing folder."""
    for path in source.rglob("*"):
        relative = path.relative_to(source)
        if path.name == META_FILE or path.name.startswith("."):
            continue
        destination = target / relative
        if path.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)


def parse_table(filename, content):
    """Rows (lists of strings) of the first sheet of a CSV / TSV / XLSX file."""
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        try:
            import openpyxl
        except ImportError:
            raise HTTPException(
                501, "Excel files need openpyxl on the server; save the key as CSV"
            ) from None
        try:
            book = openpyxl.load_workbook(
                io.BytesIO(content), read_only=True, data_only=True
            )
        except Exception as error:
            raise HTTPException(400, f"Not a readable Excel file: {error}") from None
        sheet = book.worksheets[0]
        rows = []
        for row in sheet.iter_rows(values_only=True):
            cells = ["" if v is None else _cell_text(v) for v in row]
            while cells and cells[-1] == "":
                cells.pop()
            rows.append(cells)
            if len(rows) > 5000:
                break
        book.close()
    elif name.endswith(".xls"):
        raise HTTPException(400, "Old .xls files are not supported; save as .xlsx or CSV")
    else:
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = content.decode("latin-1")
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        rows = [list(row) for row in csv.reader(io.StringIO(text), dialect)]
    while rows and not any(cell.strip() for cell in rows[-1]):
        rows.pop()
    return [[cell.strip() for cell in row] for row in rows[:5000]]


def _cell_text(value):
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def register(app, ctx, secured, template_detail):
    store = ctx.templates

    def require(template_id):
        if not store.exists(template_id):
            raise HTTPException(404, f"Template '{template_id}' not found")
        return store.path(template_id)

    def user_of(request):
        return request.headers.get("x-user") or DEFAULT_USER

    @app.post(
        "/templates/answer-key/parse", tags=["templates"], dependencies=secured
    )
    def parse_answer_key(file: UploadFile = File(...)):
        """Rows of a CSV or Excel (.xlsx) answer key; the GUI maps the columns."""
        content = file.file.read(20 * 1024 * 1024 + 1)
        if len(content) > 20 * 1024 * 1024:
            raise HTTPException(413, "The answer key file is too large")
        return {"rows": parse_table(file.filename, content)}

    @app.post(
        "/templates/{template_id}/duplicate",
        tags=["templates"],
        dependencies=secured,
        status_code=201,
    )
    def duplicate_template(
        template_id: str, request: Request, body: NameBody = Body(...)
    ):
        """
        Copy the template, config, answer key (evaluation.json) and reference
        image into a new layout. Scans and results stay with the original.
        """
        source = require(template_id)
        name = clean_name(body.name)
        owner = name_owner(store, name)
        if owner:
            raise HTTPException(409, f"The name '{name}' is already used by '{owner}'")
        new_template_id = store.allocate_id(name)
        target = store.path(new_template_id)
        try:
            copy_template_dir(source, target)
            old_meta = store.meta(template_id)
            now = time.time()
            meta = {
                key: old_meta[key] for key in COPIED_META if key in old_meta
            }
            meta.update(
                {
                    "name": name,
                    "status": old_meta.get("status", "ready"),
                    "created_at": now,
                    "updated_at": now,
                    "duplicated_from": template_id,
                    "duplicated_by": user_of(request),
                }
            )
            if old_meta.get("validation_errors"):
                meta["validation_errors"] = old_meta["validation_errors"]
            store.write_meta(new_template_id, meta)
        except Exception:
            shutil.rmtree(target, ignore_errors=True)
            raise
        return template_detail(new_template_id)

    @app.post(
        "/templates/{template_id}/rename", tags=["templates"], dependencies=secured
    )
    def rename_template(template_id: str, request: Request, body: NameBody = Body(...)):
        """Change the display name; the id stays, so jobs and results stay linked."""
        require(template_id)
        name = clean_name(body.name)
        owner = name_owner(store, name, except_id=template_id)
        if owner:
            raise HTTPException(409, f"The name '{name}' is already used by '{owner}'")
        meta = store.meta(template_id)
        previous = meta.get("name", template_id)
        if previous != name:
            meta["name"] = name
            meta["updated_at"] = time.time()
            history = list(meta.get("renames") or [])[-49:]
            history.append(
                {"from": previous, "to": name, "by": user_of(request), "at": time.time()}
            )
            meta["renames"] = history
            store.write_meta(template_id, meta)
        return template_detail(template_id)

    @app.post(
        "/templates/{template_id}/confirm-warnings",
        tags=["templates"],
        dependencies=secured,
    )
    def confirm_warnings(template_id: str, request: Request):
        """Mark the generator's warnings and verification list as checked, for good."""
        require(template_id)
        meta = store.meta(template_id)
        meta["report_confirmed"] = {"by": user_of(request), "at": time.time()}
        store.write_meta(template_id, meta)
        detail = template_detail(template_id)
        detail["report_confirmed"] = meta["report_confirmed"]
        return detail

    @app.post(
        "/templates/{template_id}/validate", tags=["templates"], dependencies=secured
    )
    def validate_json(template_id: str, body: ValidateBody = Body(...)):
        """
        Check template.json, config.json and evaluation.json with the engine's
        own rules, without saving. Returns {template, config, evaluation}: lists
        of {path, message}; engine errors go to the file they most likely belong to.
        """
        from src.defaults import CONFIG_DEFAULTS

        directory = require(template_id)
        files = {}
        for key in ("template", "config", "evaluation"):
            value = getattr(body, key)
            if key in body.clear:
                files[key] = None
            elif value is not None:
                files[key] = value
            else:
                files[key] = read_json(directory / f"{key}.json")
        result = {"template": [], "config": [], "evaluation": [], "ok": False}
        if files["template"] is None:
            result["template"].append(
                {"path": "$root", "message": "template.json is required"}
            )
        else:
            result["template"] = schema_errors(files["template"])
        if files["config"] is not None:
            result["config"] = json_errors(
                "config", files["config"], CONFIG_DEFAULTS.toDict()
            )
        if files["evaluation"] is not None:
            result["evaluation"] = json_errors("evaluation", files["evaluation"])
        schema_ok = not (
            result["template"] or result["config"] or result["evaluation"]
        )
        if schema_ok and body.engine:
            message = engine_error(directory, files)
            if message:
                target = (
                    "evaluation"
                    if files["evaluation"] is not None
                    and any(
                        word in message.lower()
                        for word in ("answer", "evaluation", "marking", "question")
                    )
                    else "template"
                )
                result[target].append({"path": "$root", "message": message})
        result["ok"] = not (
            result["template"] or result["config"] or result["evaluation"]
        )
        return result

    def engine_error(directory, files):
        from src.api.worker import build_engine

        with tempfile.TemporaryDirectory(prefix="omr_validate_") as scratch:
            scratch = Path(scratch)
            copy_template_dir(directory, scratch)
            for key, value in files.items():
                path = scratch / f"{key}.json"
                if value is None:
                    if path.exists():
                        path.unlink()
                else:
                    write_json_atomic(path, value)
            try:
                build_engine(scratch)
            except SystemExit as error:
                return f"Template rejected: {error}"
            except Exception as error:
                return str(error)
        return None

    @app.post(
        "/templates/{template_id}/read-sheet",
        tags=["templates"],
        dependencies=secured,
    )
    def read_sheet(template_id: str, file: UploadFile = File(...)):
        """Read one sheet with the saved template; nothing is stored."""
        require(template_id)
        limit = ctx.settings.max_upload_mb * 1024 * 1024
        content = file.file.read(limit + 1)
        if len(content) > limit:
            raise HTTPException(413, "The file is too large")
        suffix = Path(safe_filename(file.filename or "sheet.png")).suffix or ".png"
        with tempfile.TemporaryDirectory(prefix="omr_sheet_") as scratch:
            path = Path(scratch) / f"sheet{suffix.lower()}"
            path.write_bytes(content)
            with ctx.engines.engine(template_id) as engine:
                results = engine.scan_path(path, keep_images=False)
        result = results[0].to_dict()
        result["file_id"] = file.filename or result.get("file_id")
        return {
            "file_id": result["file_id"],
            "status": result["status"],
            "error": result.get("error"),
            "responses": result.get("responses") or {},
            "review": [
                {"name": item.get("name"), "flags": item.get("flags")}
                for item in result.get("review") or []
            ],
            "score": result.get("score"),
            "scoring": result.get("scoring"),
        }

    @app.post(
        "/templates/{template_id}/score-preview",
        tags=["templates"],
        dependencies=secured,
    )
    def score_preview(template_id: str, body: PreviewBody = Body(...)):
        """Score one sheet's responses with a draft evaluation.json (not saved)."""
        from src.evaluation import (
            EvaluationConfig,
            evaluate_concatenated_response_detailed,
        )

        directory = require(template_id)
        responses = body.responses
        if responses is None:
            if not body.scan_id or not body.scan_id.isalnum():
                raise HTTPException(422, "Give responses or a scan_id")
            stored = read_json(ctx.data.scan_dir(body.scan_id) / "result.json")
            if stored is None:
                raise HTTPException(404, f"Scan '{body.scan_id}' not found")
            responses = stored.get("responses") or {}
        errors = json_errors("evaluation", body.evaluation)
        if errors:
            return JSONResponse(
                status_code=422,
                content={"detail": "evaluation.json is invalid", "errors": errors},
            )
        with tempfile.TemporaryDirectory(prefix="omr_eval_") as scratch:
            path = Path(scratch) / "evaluation.json"
            path.write_text(json.dumps(body.evaluation))
            try:
                with ctx.engines.engine(template_id) as engine:
                    config = EvaluationConfig(
                        directory, path, engine.template, engine.tuning_config
                    )
                    config.should_explain_scoring = False
                    detailed = evaluate_concatenated_response_detailed(
                        {k: str(v) for k, v in responses.items()},
                        config,
                        Path("preview"),
                        None,
                    )
            except HTTPException:
                raise
            except Exception as error:
                return JSONResponse(
                    status_code=422,
                    content={
                        "detail": str(error),
                        "errors": [{"path": "$root", "message": str(error)}],
                    },
                )
        detailed["grade"] = config.grade_enabled
        return detailed



