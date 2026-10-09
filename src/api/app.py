"""
FastAPI application: REST endpoints for templates, scans, bulk jobs and the
manual review queue, plus the static web GUI.

    from src.api.app import create_app
    app = create_app("./omr_data")

OpenAPI docs are served at /docs.
"""

import csv
import io
import json
import mimetypes
import os
import secrets
import shutil
import threading
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import cv2
import numpy as np
from fastapi import (
    Body,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from src.api import align_routes
from src.api import exports as exports_module
from src.api import jobs as jobs_module
from src.api import editor_routes, fs_routes, generator_routes, manage_routes, ocr_routes, results_routes, views_routes
from src.api import template_ops_routes
from src.api.results import DEFAULT_USER, ResultsService
from src.api.review import (
    ReviewError,
    apply_review,
    crop_box,
    item_box,
    recompute,
    write_training_records,
)
from src.api.settings import Settings
from src.api.storage import (
    DataDir,
    ScanIndex,
    new_id,
    read_json,
    safe_filename,
    write_json_atomic,
)
from src.api.templates import EnginePool, TemplateError, TemplateStore, draw_layout
from src.api.tools import register_tool_routes
from src.api.worker import (
    SAVE_ALL,
    SAVE_NONE,
    SAVE_REVIEW,
    archive_template_version,
    scan_and_store,
)

STATIC_DIR = Path(__file__).parent / "static"
# Older Pythons (3.8, the Windows 7 build) do not know these types
mimetypes.add_type("application/wasm", ".wasm")
mimetypes.add_type("text/javascript", ".mjs")
BROWSER_DIR = Path(__file__).resolve().parents[2] / "web" / "omr-browser"
API_VERSION = "1.0"


class ReviewBody(BaseModel):
    corrections: Dict[str, Any] = Field(
        default_factory=dict,
        description="{field_or_zone_name: corrected_value}. Field values are "
        "concatenated bubble values ('' = blank, 'AB' = multi-mark).",
    )
    accept: List[str] = Field(
        default_factory=list, description="Names whose current value is correct"
    )
    reviewer: Optional[str] = None


class Context:
    """Everything the endpoints share."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.data = DataDir(settings.data_dir)
        self.index = ScanIndex(self.data.root / "index.sqlite3")
        self.templates = TemplateStore(self.data.templates)
        self.engines = EnginePool(self.templates)
        self.jobs = jobs_module.JobManager(
            self.data, self.index, self.templates, settings
        )
        self.scan_locks = {}
        self.scan_locks_guard = threading.Lock()
        self.image_cache = OrderedDict()
        self.image_cache_guard = threading.Lock()
        self.results = ResultsService(self)
        self.exports = exports_module.ExportManager(self)

    def scan_lock(self, scan_id):
        with self.scan_locks_guard:
            if len(self.scan_locks) > 10000:
                self.scan_locks.clear()
            return self.scan_locks.setdefault(scan_id, threading.Lock())

    def aligned_image(self, scan_id, result=None):
        """
        The aligned page: the stored aligned.png, or, when the job kept no
        images, the page rebuilt from the original file with the geometry
        recorded at scan time (nothing is detected again).
        """
        path = self.data.scan_dir(scan_id) / "aligned.png"
        key = str(path)
        with self.image_cache_guard:
            if key in self.image_cache:
                self.image_cache.move_to_end(key)
                return self.image_cache[key]
        if path.exists():
            image = cv2.imread(key, cv2.IMREAD_UNCHANGED)
        elif result is not None and result.get("geometry"):
            from src.api.results import ResultsError

            try:
                image = self.results.replay(result)[0]
            except ResultsError:
                return None
        else:
            return None
        with self.image_cache_guard:
            self.image_cache[key] = image
            while len(self.image_cache) > 32:
                self.image_cache.popitem(last=False)
        return image


def has_crops(result):
    """Crops come from the stored aligned image or from the original + geometry."""
    return bool(result.get("has_images") or result.get("geometry"))


def create_app(data_dir=None, settings: Optional[Settings] = None, **overrides):
    _preload_readers()
    settings = settings or Settings.from_env(data_dir, **overrides)
    ctx = Context(settings)

    @asynccontextmanager
    async def lifespan(app):
        ctx.jobs.start()
        try:
            yield
        finally:
            ctx.jobs.stop()
            ctx.exports.stop()

    app = FastAPI(
        title="OMR Engine API",
        version=API_VERSION,
        description="Read OMR sheets (bubbles, barcodes, QR codes, OCR, ICR), "
        "manage templates, run bulk jobs and review uncertain reads.",
        lifespan=lifespan,
    )
    app.state.ctx = ctx
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["Content-Disposition"],
        )

    def require_api_key(request: Request):
        expected = settings.api_key
        if not expected:
            return
        provided = request.headers.get("x-api-key") or request.query_params.get(
            "api_key"
        )
        if not provided or not secrets.compare_digest(provided, expected):
            raise HTTPException(401, "Missing or invalid API key (X-API-Key header)")

    secured = [Depends(require_api_key)]

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    max_bytes = settings.max_upload_mb * 1024 * 1024

    def save_upload(upload: UploadFile, directory: Path, name=None):
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / safe_filename(name or upload.filename, "upload")
        stem, suffix, counter = target.stem, target.suffix, 1
        while target.exists():
            target = directory / f"{stem}_{counter}{suffix}"
            counter += 1
        size = 0
        with open(target, "wb") as handle:
            while True:
                chunk = upload.file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    handle.close()
                    target.unlink(missing_ok=True)
                    raise HTTPException(
                        413,
                        f"'{upload.filename}' exceeds the {settings.max_upload_mb} MB limit",
                    )
                handle.write(chunk)
        return target

    def read_upload(upload: UploadFile):
        content = upload.file.read(max_bytes + 1)
        if len(content) > max_bytes:
            raise HTTPException(
                413,
                f"'{upload.filename}' exceeds the {settings.max_upload_mb} MB limit",
            )
        return content

    def require_template(template_id):
        if not ctx.templates.exists(template_id):
            raise HTTPException(404, f"Template '{template_id}' not found")
        return ctx.templates.path(template_id)

    def load_result(scan_id):
        if not scan_id.isalnum():
            raise HTTPException(404, "Scan not found")
        result = read_json(ctx.data.scan_dir(scan_id) / "result.json")
        if result is None:
            raise HTTPException(404, f"Scan '{scan_id}' not found")
        return result

    def scan_links(result):
        scan_id = result["scan_id"]
        has_images = result.get("has_images")
        return {
            "self": f"/scans/{scan_id}",
            "aligned": f"/scans/{scan_id}/image?kind=aligned" if has_images else None,
            "marked": f"/scans/{scan_id}/image?kind=marked" if has_images else None,
            "crop": f"/scans/{scan_id}/crop" if has_crops(result) else None,
        }

    def with_links(result):
        return {**result, "links": scan_links(result)}

    def resolve_folder(folder):
        path = Path(folder).expanduser().resolve()
        if settings.allowed_dirs and not any(
            path == root or root in path.parents for root in settings.allowed_dirs
        ):
            raise HTTPException(403, f"Folder '{folder}' is outside OMR_ALLOWED_DIRS")
        if not path.is_dir():
            raise HTTPException(400, f"Folder '{folder}' does not exist")
        return path

    # ------------------------------------------------------------------
    # meta
    # ------------------------------------------------------------------
    @app.get("/health", tags=["meta"])
    def health():
        from src.capabilities import cached_summary

        return {
            "status": "ok",
            "version": API_VERSION,
            "time": time.time(),
            "engines": cached_summary(),
        }

    @app.get("/capabilities", tags=["meta"], dependencies=secured)
    def capabilities():
        from src.constants.common import FIELD_TYPES
        from src.readers.barcode import available_engines, supported_formats
        from src.readers.ocr import tesseract_available
        from src.schemas.template_schema import ZONE_SCHEMA

        try:
            import onnxruntime  # noqa: F401

            onnx = True
        except ImportError:
            onnx = False
        return {
            "barcode_formats": supported_formats(),
            "barcode_engines": available_engines(),
            "tesseract": tesseract_available(),
            "onnxruntime": onnx,
            "models": {
                "bubble": "Per-template ONNX model via config.json ml_params.bubble_model_path; "
                "classical adaptive thresholding otherwise",
                "icr": "Per-template ONNX model via config.json ml_params.icr_model_path; "
                "ICR zones are flagged for review until a model is configured",
            },
            "field_types": FIELD_TYPES,
            "zone_types": ZONE_SCHEMA["properties"]["type"]["enum"],
            "workers": settings.effective_workers,
            "cpu_count": os.cpu_count() or 1,
            "max_upload_mb": settings.max_upload_mb,
            "sync_max_files": settings.sync_max_files,
            "auth_required": bool(settings.api_key),
            "template_generation": _template_gen_available(),
        }

    # ------------------------------------------------------------------
    # templates
    # ------------------------------------------------------------------
    @app.get("/templates", tags=["templates"], dependencies=secured)
    def list_templates():
        return {"templates": ctx.templates.list()}

    @app.post("/templates", tags=["templates"], dependencies=secured, status_code=201)
    def upload_template(
        files: List[UploadFile] = File(
            ..., description="template.json plus assets, or one .zip of the folder"
        ),
        name: Optional[str] = Form(None),
    ):
        staged = [(upload.filename or "file", read_upload(upload)) for upload in files]
        try:
            template_id, _ = ctx.templates.create_from_files(name, staged)
        except TemplateError as error:
            return JSONResponse(
                status_code=422, content={"detail": str(error), "errors": error.errors}
            )
        return template_detail(template_id)

    def template_detail(template_id):
        require_template(template_id)
        summary = ctx.templates.summary(template_id)
        meta = ctx.templates.meta(template_id)
        directory = ctx.templates.path(template_id)
        return {
            **summary,
            "template": ctx.templates.template_json(template_id),
            "config": read_json(directory / "config.json"),
            "evaluation": read_json(directory / "evaluation.json"),
            "files": ctx.templates.files(template_id),
            "report": meta.get("report"),
            "report_confirmed": meta.get("report_confirmed"),
            "validation_errors": meta.get("validation_errors", []),
            "reference_url": (
                f"/templates/{template_id}/reference.png"
                if summary["has_reference"]
                else None
            ),
            "layout_url": f"/templates/{template_id}/layout.png",
        }

    @app.get("/templates/{template_id}", tags=["templates"], dependencies=secured)
    def get_template(template_id: str):
        return template_detail(template_id)

    @app.get(
        "/templates/{template_id}/files/{file_path:path}",
        tags=["templates"],
        dependencies=secured,
    )
    def get_template_file(template_id: str, file_path: str):
        """A file stored with the template, such as a CropOnMarkers marker image."""
        directory = require_template(template_id).resolve()
        path = (directory / file_path).resolve()
        if directory not in path.parents or not path.is_file():
            raise HTTPException(404, f"File '{file_path}' not found")
        return FileResponse(path)

    @app.put("/templates/{template_id}", tags=["templates"], dependencies=secured)
    def put_template(template_id: str, body: Dict[str, Any] = Body(...)):
        """Body: the template.json object, or {"template": ..., "config": ..., "evaluation": ...}."""
        directory = require_template(template_id)
        if "template" in body and "fieldBlocks" not in body:
            template_json = body["template"]
            extras = {k: body.get(k) for k in ("config", "evaluation") if k in body}
        else:
            template_json, extras = body, {}
        backups = {}
        try:
            for key, value in extras.items():
                path = directory / f"{key}.json"
                backups[path] = path.read_bytes() if path.exists() else None
                if value is None:
                    path.unlink(missing_ok=True)
                else:
                    write_json_atomic(path, value)
            ctx.templates.update_template(template_id, template_json)
        except TemplateError as error:
            for path, previous in backups.items():
                if previous is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(previous)
            return JSONResponse(
                status_code=422, content={"detail": str(error), "errors": error.errors}
            )
        ctx.engines.invalidate(template_id)
        return template_detail(template_id)

    @app.delete("/templates/{template_id}", tags=["templates"], dependencies=secured)
    def delete_template(template_id: str, purge_scans: bool = False):
        require_template(template_id)
        ctx.templates.delete(template_id)
        ctx.engines.invalidate(template_id)
        if purge_scans:
            ctx.index.delete_template_rows(template_id)
        return {"deleted": template_id}

    @app.get(
        "/templates/{template_id}/reference.png",
        tags=["templates"],
        dependencies=secured,
    )
    def template_reference(template_id: str):
        require_template(template_id)
        path = ctx.templates.reference_path(template_id)
        if path is None:
            raise HTTPException(404, "This template has no reference image")
        image = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
        if image is None:
            raise HTTPException(404, "Reference image could not be read")
        return png_response(image)

    @app.post(
        "/templates/{template_id}/reference", tags=["templates"], dependencies=secured
    )
    def upload_reference(template_id: str, file: UploadFile = File(...)):
        """Store a background image (in template coordinates) for the editor."""
        directory = require_template(template_id)
        image = cv2.imdecode(
            np.frombuffer(read_upload(file), np.uint8), cv2.IMREAD_GRAYSCALE
        )
        if image is None:
            raise HTTPException(400, "Not an image")
        template = ctx.templates.template_json(template_id) or {}
        width, height = template.get("pageDimensions", image.shape[::-1])
        image = cv2.resize(image, (int(width), int(height)))
        cv2.imwrite(str(directory / "reference.png"), image)
        meta = ctx.templates.meta(template_id)
        meta["reference"] = "reference.png"
        ctx.templates.write_meta(template_id, meta)
        return template_detail(template_id)

    @app.get(
        "/templates/{template_id}/layout.png", tags=["templates"], dependencies=secured
    )
    def template_layout(
        template_id: str,
        image: Optional[str] = Query(
            None, description="'reference' (default), 'blank', or a scan id"
        ),
        highlight: Optional[str] = Query(None, description="Comma separated names"),
    ):
        require_template(template_id)
        template_json = ctx.templates.template_json(template_id)
        background = None
        if image and image not in ("reference", "blank"):
            background = ctx.aligned_image(safe_filename(image))
            if background is None:
                raise HTTPException(404, f"No aligned image for scan '{image}'")
        elif image != "blank":
            path = ctx.templates.reference_path(template_id)
            if path is not None:
                background = cv2.imread(str(path), cv2.IMREAD_COLOR)
        names = set((highlight or "").split(",")) - {""}
        return png_response(draw_layout(template_json, background, names))

    @app.post(
        "/templates/generate", tags=["templates"], dependencies=secured, status_code=201
    )
    def generate(
        files: List[UploadFile] = File(..., description="Sample sheet images (~20)"),
        labels: Optional[UploadFile] = File(
            None,
            description="CSV or .xlsx: a file-name column (any spelling, e.g. "
            "'File Name') plus one column per field; answer strings expand to q1..qN",
        ),
        name: Optional[str] = Form(None),
        options: Optional[str] = Form(None, description="JSON options"),
    ):
        try:
            from src.template_gen import generate_template
        except ImportError as error:
            raise HTTPException(
                501, f"Template generation is not available: {error}"
            ) from None
        images, names, uploads = [], [], []
        for upload in files:
            content = read_upload(upload)
            # Colour kept so the generator can suggest colour dropout
            image = decode_image(content, upload.filename, colour=True)
            if image is None:
                raise HTTPException(400, f"'{upload.filename}' is not a readable image")
            images.append(image)
            names.append(upload.filename or f"image{len(names)}")
            uploads.append((names[-1], content))
        label_list, label_info = None, {}
        if labels is not None:
            label_list = parse_labels_csv(
                read_upload(labels), names, labels.filename, label_info
            )
        try:
            parsed_options = json.loads(options) if options else None
        except json.JSONDecodeError:
            raise HTTPException(400, "options must be JSON") from None
        try:
            result = generate_template(images, label_list, parsed_options)
        except Exception as error:
            # Never dead-end: open an empty draft on the first real sheet
            result = generator_routes.fallback_draft(images[0], error)

        template = getattr(result, "template", None) or {}
        report = getattr(result, "report", None) or {}
        reference = getattr(result, "reference_image", None)
        report = json.loads(json.dumps(report, default=_np_default))
        if isinstance(report, dict):
            report["sheet_names"] = names
            if label_info:
                report["labels"] = json.loads(json.dumps(label_info, default=str))
        staged = [("template.json", json.dumps(template, default=_np_default).encode())]
        generated_config = getattr(result, "config", None)
        if isinstance(generated_config, dict) and generated_config:
            staged.append(("config.json", json.dumps(generated_config).encode()))
        if reference is not None:
            ok, buffer = cv2.imencode(".png", reference)
            if ok:
                staged.append(("reference.png", buffer.tobytes()))
                for processor in template.get("preProcessors", []) or []:
                    ref_name = (processor.get("options") or {}).get("reference")
                    if isinstance(ref_name, str) and ref_name != "reference.png":
                        staged.append((ref_name, buffer.tobytes()))
        try:
            template_id, errors = ctx.templates.create_from_files(
                name or "generated",
                staged,
                status="draft",
                extra={
                    "report": report,
                    "generated": True,
                    "reference": "reference.png" if reference is not None else None,
                    "samples": names,
                },
            )
        except TemplateError as error:
            return JSONResponse(
                status_code=422, content={"detail": str(error), "errors": error.errors}
            )
        try:
            generator_routes.save_samples(ctx.templates.path(template_id), uploads)
        except OSError:
            pass  # "Test on samples" then asks for a new generation
        detail = template_detail(template_id)
        detail["validation_errors"] = errors
        return detail

    # ------------------------------------------------------------------
    # scans
    # ------------------------------------------------------------------
    @app.post("/scans", tags=["scans"], dependencies=secured)
    def create_scans(
        template_id: str = Form(...),
        files: List[UploadFile] = File(..., description="Images or PDFs"),
        save_images: str = Form(SAVE_ALL),
        pdf_dpi: Optional[str] = Form(None, description="PDF render DPI or 'auto'"),
        pdf_page: Optional[str] = Form(
            None, description="PDF pages: '1', '2-4', '3-' or 'all'"
        ),
    ):
        """Read sheets synchronously. Use /jobs for large batches."""
        require_template(template_id)
        pdf_params = editor_routes.parse_pdf_params(pdf_dpi, pdf_page)
        if len(files) > settings.sync_max_files:
            raise HTTPException(
                413,
                f"At most {settings.sync_max_files} files per request; use POST /jobs",
            )
        if save_images not in (SAVE_ALL, SAVE_REVIEW, SAVE_NONE):
            raise HTTPException(400, "save_images must be all, review or none")
        upload_dir = ctx.data.uploads / new_id()
        stored = []
        version = archive_template_version(
            ctx.templates.path(template_id), ctx.data.template_versions, template_id
        )
        try:
            paths = [save_upload(upload, upload_dir) for upload in files]
            with ctx.engines.engine(template_id) as engine:
                for path, upload in zip(paths, files):
                    meta = {
                        "template_id": template_id,
                        "job_id": None,
                        "seq": 0,
                        "file_name": safe_filename(upload.filename, path.name),
                        "template_version": version,
                        "pdf_params": pdf_params,
                    }
                    stored.extend(
                        scan_and_store(
                            engine,
                            path,
                            meta,
                            ctx.data.scans,
                            save_images,
                            copy_input=True,
                        )
                    )
        finally:
            shutil.rmtree(upload_dir, ignore_errors=True)
        ctx.index.add_scans(stored)
        return {"scans": [with_links(result) for result in stored]}

    @app.get("/scans", tags=["scans"], dependencies=secured)
    def list_scans(
        status: Optional[str] = None,
        template_id: Optional[str] = None,
        job_id: Optional[str] = None,
        reviewed: Optional[bool] = None,
        limit: int = Query(50, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ):
        items, total = ctx.index.list_scans(
            status=status,
            template_id=template_id,
            job_id=job_id,
            reviewed=None if reviewed is None else int(reviewed),
            limit=limit,
            offset=offset,
        )
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    @app.get("/scans/{scan_id}", tags=["scans"], dependencies=secured)
    def get_scan(scan_id: str):
        return with_links(load_result(scan_id))

    @app.get("/scans/{scan_id}/image", tags=["scans"], dependencies=secured)
    def scan_image(
        scan_id: str, kind: str = Query("marked", pattern="^(aligned|marked)$")
    ):
        load_result(scan_id)
        name = "aligned.png" if kind == "aligned" else "marked.jpg"
        path = ctx.data.scan_dir(scan_id) / name
        if not path.exists():
            raise HTTPException(404, f"No {kind} image stored for this scan")
        return FileResponse(path, headers={"Cache-Control": "private, max-age=3600"})

    @app.get("/scans/{scan_id}/crop", tags=["scans"], dependencies=secured)
    def scan_crop(
        scan_id: str,
        field: Optional[str] = None,
        zone: Optional[str] = None,
        name: Optional[str] = None,
        pad: int = Query(20, ge=0, le=400),
        outline: bool = Query(False, description="Outline the item inside the crop"),
    ):
        result = load_result(scan_id)
        target = field or zone or name
        if not target:
            raise HTTPException(400, "Pass field=<label> or zone=<name>")
        kind, box = item_box(result, target)
        if kind is None or box is None:
            raise HTTPException(404, f"'{target}' is not a field or zone of this scan")
        image = ctx.aligned_image(scan_id, result)
        if image is None:
            raise HTTPException(
                404,
                "No aligned image stored for this scan, and the original file "
                "could not be read again (moved or changed)",
            )
        crop = crop_box(image, box, pad)
        if crop is None:
            raise HTTPException(404, "Crop is outside the image")
        if outline and pad > 0:
            crop = cv2.cvtColor(crop, cv2.COLOR_GRAY2BGR) if crop.ndim == 2 else crop
            x0, y0 = min(pad, int(box[0])), min(pad, int(box[1]))
            cv2.rectangle(
                crop,
                (max(x0 - 3, 0), max(y0 - 3, 0)),
                (x0 + int(box[2]) + 2, y0 + int(box[3]) + 2),
                (0, 140, 255),
                1,
            )
        return png_response(crop)

    @app.post("/scans/{scan_id}/review", tags=["review"], dependencies=secured)
    def review_scan(scan_id: str, body: ReviewBody, request: Request):
        reviewer = request.headers.get("x-user") or body.reviewer or DEFAULT_USER
        with ctx.scan_lock(scan_id):
            result = load_result(scan_id)
            info = ctx.results.template_info(result)
            try:
                resolved, events = apply_review(
                    result,
                    body.corrections,
                    body.accept,
                    reviewer,
                    info["empty_values"],
                    info["custom_labels"],
                )
            except ReviewError as error:
                raise HTTPException(422, str(error)) from None
            audit = ctx.results.audit_rows(result, events, reviewer, "queue")
            template_id = result.get("template_id")
            if template_id and ctx.templates.exists(template_id):
                # Responses, rule outputs (checks/validation), score and status
                with ctx.engines.engine(template_id) as engine:
                    recompute(result, engine)
            else:
                result["status"] = "needs_review" if result.get("review") else "ok"
            write_json_atomic(ctx.data.scan_dir(scan_id) / "result.json", result)
            ctx.index.update_after_review(result, resolved)
            ctx.index.sync_review_items(result)
            ctx.index.add_corrections(audit)
        records = write_training_records(
            ctx.data.training, result, events, ctx.aligned_image(scan_id, result)
        )
        response = with_links(result)
        response["training_records"] = len(records)
        return response

    # ------------------------------------------------------------------
    # review queue
    # ------------------------------------------------------------------
    @app.get("/review", tags=["review"], dependencies=secured)
    def review_queue(
        template_id: Optional[str] = None,
        job_id: Optional[str] = None,
        scan_id: Optional[str] = None,
        name: Optional[str] = Query(None, description="Only this field/zone name"),
        kind: Optional[str] = Query(
            None, pattern="^(field|zone|check|custom_label|sheet)$"
        ),
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
        created_after: Optional[float] = Query(
            None, description="Only items queued after this server time ('now')"
        ),
        order: str = Query(
            "oldest",
            pattern="^(oldest|risk)$",
            description="risk: whole-sheet and check items before single fields",
        ),
        flag: Optional[str] = Query(
            None, description="Only items read with this flag (e.g. weak_mark)"
        ),
    ):
        # A little before the query, so an item committed meanwhile is not missed
        now = time.time() - 2
        rows, total = ctx.index.pending_reviews(
            template_id=template_id,
            job_id=job_id,
            scan_id=scan_id,
            name=name,
            kind=kind,
            limit=limit,
            offset=offset,
            created_after=created_after,
            order=order,
            flag=flag,
        )
        results, items = {}, []
        for row in rows:
            if row["scan_id"] not in results:
                results[row["scan_id"]] = read_json(
                    ctx.data.scan_dir(row["scan_id"]) / "result.json"
                )
            result = results[row["scan_id"]]
            if not result:
                continue
            items.append(review_item(result, row["name"]))
        return {"items": [i for i in items if i], "total": total, "now": now}

    def review_item(result, name):
        scan_id = result["scan_id"]
        field = (result.get("fields") or {}).get(name)
        zone = (result.get("zones") or {}).get(name)
        target = field if field is not None else zone
        if target is None:
            return rule_review_item(result, name)
        has_images = result.get("has_images")
        item = {
            "scan_id": scan_id,
            "file_id": result.get("file_id"),
            "template_id": result.get("template_id"),
            "job_id": result.get("job_id"),
            "kind": "field" if field is not None else "zone",
            "name": name,
            "type": zone.get("type") if zone is not None else "bubbles",
            "value": target.get("value", ""),
            "confidence": target.get("confidence"),
            "flags": target.get("flags", []),
            "crop_url": (
                f"/scans/{scan_id}/crop?name={quote(name)}" if has_crops(result) else None
            ),
            "options": None,
        }
        if field is not None:
            item["options"] = [
                {
                    "value": b["value"],
                    "marked": b.get("marked"),
                    "fill_ratio": b.get("fill_ratio"),
                    "confidence": b.get("confidence"),
                }
                for b in field.get("bubbles") or []
            ]
        if zone is not None and zone.get("details"):
            item["details"] = zone.get("details")
        return item

    def sheet_reasons(entry):
        if not entry or entry.get("kind") != "sheet":
            return []
        if "marked_bubbles" in entry:
            return [
                f"{entry['marked_bubbles']} marked bubbles, fewer than "
                f"{entry.get('min_marked_bubbles')}: blank or misread sheet? "
                "Accept to dismiss"
            ]
        return ["Sheet-level check; accept to dismiss"]

    def rule_review_item(result, name):
        """A cross-field check or custom-label validation waiting for a person."""
        scan_id = result["scan_id"]
        entry = next(
            (i for i in result.get("review") or [] if i.get("name") == name), None
        )
        check = (result.get("checks") or {}).get(name)
        if entry is None and not isinstance(check, dict):
            return None
        kind = (entry or {}).get("kind") or "check"
        validation = (result.get("validation") or {}).get(
            (check or {}).get("output") or name
        ) or {}
        if isinstance(check, dict):
            value = check.get("value", "")
            candidates = [
                {"source": source, "value": raw}
                for source, raw in (check.get("sources") or {}).items()
                if raw not in (None, "")
            ]
            flags = check.get("flags") or []
        else:
            value = (result.get("responses") or {}).get(name, "")
            candidates = []
            flags = (entry or {}).get("flags") or []
        _, box = item_box(result, name)
        return {
            "scan_id": scan_id,
            "file_id": result.get("file_id"),
            "template_id": result.get("template_id"),
            "job_id": result.get("job_id"),
            "kind": kind,
            "name": name,
            "type": kind,
            "value": value,
            "confidence": None,
            "flags": flags,
            "reasons": (entry or {}).get("reasons")
            or validation.get("reasons")
            or sheet_reasons(entry),
            "fields": (entry or {}).get("fields"),
            # Column states of a grouped value (ok / empty / multi / issue)
            "group": (result.get("groups") or {}).get(name),
            "candidates": candidates,
            "crop_url": (
                f"/scans/{scan_id}/crop?name={quote(name)}"
                if has_crops(result) and box
                else None
            ),
            "options": None,
        }

    @app.get("/review/summary", tags=["review"], dependencies=secured)
    def review_summary(template_id: Optional[str] = None, job_id: Optional[str] = None):
        names = ctx.index.pending_review_names(template_id=template_id, job_id=job_id)
        return {
            "by_name": names,
            "by_flag": ctx.index.pending_review_flags(template_id, job_id),
            "by_job": review_jobs(template_id),
            "total": sum(row["n"] for row in names),
        }

    def review_jobs(template_id=None):
        """Jobs with pending items, newest first, with their name and date."""
        rows = ctx.index.pending_review_jobs(template_id)
        out = []
        for row in rows:
            job = ctx.jobs.get(row["job_id"]) if row["job_id"] else None
            out.append(
                {
                    "job_id": row["job_id"],
                    "n": row["n"],
                    "name": (job or {}).get("name") or "",
                    "created_at": (job or {}).get("created_at"),
                }
            )
        out.sort(key=lambda r: r["created_at"] or 0, reverse=True)
        return out

    # ------------------------------------------------------------------
    # jobs
    # ------------------------------------------------------------------
    @app.post("/jobs", tags=["jobs"], dependencies=secured, status_code=201)
    def create_job(
        template_id: str = Form(...),
        files: Optional[List[UploadFile]] = File(None),
        folder: Optional[str] = Form(None, description="Server-side folder to read"),
        recursive: bool = Form(True),
        save_images: str = Form(SAVE_REVIEW),
        workers: Optional[int] = Form(None),
        prefetch: Optional[int] = Form(
            None, ge=0, le=10000, description="Files read into memory ahead of the workers (0 = off)"
        ),
        name: Optional[str] = Form(None),
        start: bool = Form(True, description="false: add more files, then /start"),
        pdf_dpi: Optional[str] = Form(None, description="PDF render DPI or 'auto'"),
        pdf_page: Optional[str] = Form(
            None, description="PDF pages: '1', '2-4', '3-' or 'all'"
        ),
    ):
        require_template(template_id)
        pdf_params = editor_routes.parse_pdf_params(pdf_dpi, pdf_page)
        if save_images not in (SAVE_ALL, SAVE_REVIEW, SAVE_NONE):
            raise HTTPException(400, "save_images must be all, review or none")
        if not files and not folder and start:
            raise HTTPException(400, "Upload files or give a server-side folder")
        folder_files = []
        if folder:
            folder_files = jobs_module.collect_folder(resolve_folder(folder), recursive)
            if not folder_files and not files:
                raise HTTPException(400, "No images or PDFs found in that folder")
        job = ctx.jobs.create(
            template_id,
            [],
            source="folder" if folder else "upload",
            options={
                "save_images": save_images,
                "workers": workers,
                "prefetch": prefetch,
                "name": name,
                "pdf_params": pdf_params,
            },
            start=False,
        )
        uploaded = [
            save_upload(upload, ctx.jobs.inputs_dir(job["id"]))
            for upload in files or []
        ]
        if folder:
            job["folder"] = str(folder)
            job["recursive"] = bool(recursive)
            fs_routes.remember_folder(ctx, folder)
        warning = manage_routes.workers_warning(workers)
        if warning:
            job["warnings"] = [warning]
        ctx.jobs.add_files(job, folder_files + uploaded)
        if start:
            ctx.jobs.enqueue(job)
        return ctx.jobs.get(job["id"])

    def live_job(job_id):
        job = ctx.jobs.live.get(job_id)
        if job is None:
            stored = ctx.jobs.get(job_id)
            if stored is None:
                raise HTTPException(404, f"Job '{job_id}' not found")
            if stored.get("state") != jobs_module.UPLOADING:
                raise HTTPException(409, f"Job is {stored.get('state')}")
            ctx.jobs.live[job_id] = job = stored
        return job

    @app.post("/jobs/{job_id}/files", tags=["jobs"], dependencies=secured)
    def add_job_files(job_id: str, files: List[UploadFile] = File(...)):
        job = live_job(job_id)
        if job["state"] != jobs_module.UPLOADING:
            raise HTTPException(
                409, f"Job is {job['state']}; files can only be added before start"
            )
        paths = [save_upload(upload, ctx.jobs.inputs_dir(job_id)) for upload in files]
        ctx.jobs.add_files(job, paths)
        return ctx.jobs.get(job_id)

    @app.post("/jobs/{job_id}/start", tags=["jobs"], dependencies=secured)
    def start_job(job_id: str):
        job = live_job(job_id)
        if job["state"] != jobs_module.UPLOADING:
            raise HTTPException(409, f"Job is already {job['state']}")
        if not job.get("total_files"):
            raise HTTPException(400, "The job has no files")
        ctx.jobs.enqueue(job)
        return ctx.jobs.get(job_id)

    @app.post("/jobs/{job_id}/pause", tags=["jobs"], dependencies=secured)
    def pause_job(job_id: str):
        if ctx.jobs.pause(job_id) is None:
            job = ctx.jobs.get(job_id)
            if job is None:
                raise HTTPException(404, f"Job '{job_id}' not found")
            raise HTTPException(409, f"Job is {job['state']}; only a queued or running job can be paused")
        return ctx.jobs.get(job_id)

    @app.post("/jobs/{job_id}/resume", tags=["jobs"], dependencies=secured)
    def resume_job(job_id: str):
        if ctx.jobs.resume(job_id) is None:
            job = ctx.jobs.get(job_id)
            if job is None:
                raise HTTPException(404, f"Job '{job_id}' not found")
            raise HTTPException(409, f"Job is {job['state']}; only a paused or interrupted job can be resumed")
        return ctx.jobs.get(job_id)

    @app.post("/jobs/{job_id}/cancel", tags=["jobs"], dependencies=secured)
    def cancel_job(job_id: str):
        job = ctx.jobs.cancel(job_id)
        if job is None:
            raise HTTPException(404, f"Job '{job_id}' not found")
        return job

    @app.get("/jobs", tags=["jobs"], dependencies=secured)
    def list_jobs(limit: int = Query(50, ge=1, le=500)):
        return {"jobs": ctx.jobs.list(limit)}

    @app.get("/jobs/{job_id}", tags=["jobs"], dependencies=secured)
    def get_job(job_id: str):
        job = ctx.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, f"Job '{job_id}' not found")
        total = job.get("total_files") or 0
        job["progress"] = (
            round(job.get("processed_files", 0) / total, 4) if total else 0
        )
        job["pending_review"] = ctx.index.pending_reviews(job_id=job_id, limit=1)[1]
        return job

    @app.get("/jobs/{job_id}/results.csv", tags=["jobs"], dependencies=secured)
    def job_results_csv(job_id: str):
        job = ctx.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, f"Job '{job_id}' not found")
        columns = output_columns(job["template_id"], job_id)
        head = [
            "file_name",
            "page",
            "scan_id",
            "status",
            "score",
            "needs_review",
            "reviewed",
            "error",
        ]

        def rows():
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            writer.writerow(head + columns)
            for count, row in enumerate(ctx.index.iter_job_scans(job_id)):
                result = read_json(ctx.data.scan_dir(row["id"]) / "result.json") or {}
                responses = result.get("responses") or {}
                writer.writerow(
                    [
                        result.get("file_name", row["file_name"]),
                        (result.get("page") or 0) + 1,
                        row["id"],
                        result.get("status", row["status"]),
                        "" if result.get("score") is None else result.get("score"),
                        ";".join(item["name"] for item in result.get("review") or []),
                        int(bool(result.get("reviewed"))),
                        result.get("error") or "",
                    ]
                    + [responses.get(column, "") for column in columns]
                )
                if count % 500 == 0:
                    yield buffer.getvalue()
                    buffer.seek(0)
                    buffer.truncate()
            yield buffer.getvalue()

        filename = f"job_{job_id[:8]}_results.csv"
        return StreamingResponse(
            rows(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    def output_columns(template_id, job_id):
        if ctx.templates.exists(template_id):
            try:
                with ctx.engines.engine(template_id) as engine:
                    columns = list(engine.template.output_columns)
                zone_names = [z.name for z in engine.template.zones]
                return columns + [z for z in zone_names if z not in columns]
            except Exception:
                pass
        for row in ctx.index.iter_job_scans(job_id, batch=1):
            result = read_json(ctx.data.scan_dir(row["id"]) / "result.json") or {}
            if result.get("responses"):
                return sorted(result["responses"])
        return []

    register_tool_routes(app, secured, read_upload, ctx)
    # results screen: render, correct, verify, regrade, accuracy, audit
    results_routes.register(app, ctx, secured)
    # editor: block border preview and "Test on samples" (src/api/align_routes.py)
    align_routes.register(app, ctx, secured, read_upload)
    # original / full-colour views, folder picker, deletes and bulk review
    views_routes.register(app, ctx, secured)
    fs_routes.register(app, ctx, secured)
    manage_routes.register(app, ctx, secured)
    ocr_routes.register(app, ctx, secured)
    # duplicate / rename / validate JSON / scoring preview (items 6, 7, 12)
    template_ops_routes.register(app, ctx, secured, template_detail)
    generator_routes.register(app, ctx, secured, decode_image)
    # template editor helpers: installed OCR languages and models
    editor_routes.register(app, ctx, secured)
    # exports: CSV, XLSX, PDF, SQLite / SQL with export profiles
    exports_module.register(app, ctx, secured)

    # ------------------------------------------------------------------
    # GUI
    # ------------------------------------------------------------------
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    if BROWSER_DIR.exists():

        @app.get("/browser", include_in_schema=False)
        @app.get("/browser/", include_in_schema=False)
        def browser_demo():
            return RedirectResponse("/browser/demo.html")

        # In-browser engine: phones read sheets locally with a template from this API
        app.mount(
            "/browser",
            StaticFiles(directory=BROWSER_DIR, html=True),
            name="browser",
        )

    @app.get("/", include_in_schema=False)
    @app.get("/ui", include_in_schema=False)
    def gui():
        return FileResponse(
            STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"}
        )

    return app


def png_response(image):
    ok, buffer = cv2.imencode(".png", image, [cv2.IMWRITE_PNG_COMPRESSION, 1])
    if not ok:
        raise HTTPException(500, "Could not encode image")
    return Response(content=buffer.tobytes(), media_type="image/png")


def decode_image(content, filename=None, colour=False):
    if (filename or "").lower().endswith(".pdf"):
        try:
            import fitz

            document = fitz.open(stream=content, filetype="pdf")
            page = document[0]
            pixmap = page.get_pixmap(dpi=150, colorspace=fitz.csGRAY)
            return (
                np.frombuffer(pixmap.samples, np.uint8)
                .reshape(pixmap.height, pixmap.width)
                .copy()
            )
        except Exception:
            return None
    flag = cv2.IMREAD_COLOR if colour else cv2.IMREAD_GRAYSCALE
    return cv2.imdecode(np.frombuffer(content, np.uint8), flag)


FILENAME_COLUMNS = (
    "file",
    "filename",
    "file_name",
    "image",
    "image_name",
    "name",
    "file_id",
)


def parse_labels_csv(content, image_names, filename=None, info=None):
    """
    Map a labels file (CSV or .xlsx) onto the uploaded images, by file name
    (any spelling of "File Name") or else by row order. Answer strings
    ("CB A*D", space = blank, * = multi-marked) expand to q1..qN.
    See src/utils/label_files.py; `info` (a dict) receives what was recognised.
    """
    from src.utils.label_files import LabelFileError, parse_label_file

    try:
        labels, details = parse_label_file(content, image_names, filename)
    except LabelFileError as error:
        raise HTTPException(400, str(error)) from None
    if info is not None:
        info.update(details)
    return labels


def _np_default(value):
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _template_gen_available():
    try:
        import importlib.util

        return importlib.util.find_spec("src.template_gen") is not None
    except Exception:
        return False


def _preload_readers():
    """
    Import the reading stack on the calling (main) thread: some native OCR
    bindings install signal handlers at import time, which fails when the
    first import happens inside a request worker thread.
    """
    import src.pipeline  # noqa: F401
    import src.readers.ocr  # noqa: F401
