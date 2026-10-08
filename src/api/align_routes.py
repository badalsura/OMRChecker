"""
Block border preview for the template editor (plan item 14).

    GET    /templates/{id}/align/samples            sample sheets kept for previews
    POST   /templates/{id}/align/samples            upload sample sheets (multipart)
    DELETE /templates/{id}/align/samples/{name}
    POST   /templates/{id}/align/preview            read one sheet (reference or a
                                                    sample) and return its aligned page,
                                                    detected block borders and the
                                                    fitted bubble positions
    POST   /templates/{id}/align/test               "Test on samples": every sample,
                                                    listing blocks whose border failed

Preview and test take the editor's unsaved template (and alignment overrides)
in the body, so the result shows straight away without saving. Everything
drawn comes from the read's own geometry record (src/geometry.py), exactly
like the Results screen.

Samples live in the template folder under _samples/ (not archived with
template versions and not part of the template hash).
"""

import base64
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import cv2
import numpy as np
from fastapi import Body, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from src.api.results import geometry_overlay
from src.api.storage import safe_filename

SAMPLES_DIR = "_samples"
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".pdf")
MAX_SAMPLES = 60
PREVIEW_MAX_SIDE = 2000


class AlignBody(BaseModel):
    sample: Optional[str] = Field(
        None, description="Sample name, or 'reference' (default) for the reference image"
    )
    template: Optional[Dict[str, Any]] = Field(
        None, description="Unsaved template.json from the editor (default: the saved one)"
    )
    alignment: Optional[Dict[str, Any]] = Field(
        None,
        description='Template "alignment" overrides, e.g. {"rectify_on_border": true}',
    )
    view: str = Field("print", description="'print' (borders) or 'dropout' image")
    image: bool = Field(True, description="Include the aligned page as a data URL")


class TestBody(BaseModel):
    template: Optional[Dict[str, Any]] = None
    alignment: Optional[Dict[str, Any]] = None
    samples: Optional[List[str]] = Field(None, description="Default: all samples")


def _encode(image):
    scale = min(1.0, PREVIEW_MAX_SIDE / float(max(image.shape[:2])))
    if scale < 1.0:
        image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    ok, buffer = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 88])
    if not ok:
        return None
    return "data:image/jpeg;base64," + base64.b64encode(buffer.tobytes()).decode()


def _bubbles(fields):
    out = {}
    for label, field in (fields or {}).items():
        out[label] = [
            {
                "value": b["value"],
                "x": b["x"],
                "y": b["y"],
                "w": b["w"],
                "h": b["h"],
                "marked": bool(b.get("marked")),
            }
            for b in field.get("bubbles") or []
        ]
    return out


def register(app, ctx, secured, read_upload):
    def template_dir(template_id):
        if not ctx.templates.exists(template_id):
            raise HTTPException(404, f"Template '{template_id}' not found")
        return ctx.templates.path(template_id)

    def samples_dir(template_id):
        return template_dir(template_id) / SAMPLES_DIR

    def sample_names(template_id):
        directory = samples_dir(template_id)
        if not directory.exists():
            return []
        return sorted(
            p.name
            for p in directory.iterdir()
            if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
        )

    def sample_path(template_id, name):
        if not name or name == "reference":
            path = ctx.templates.reference_path(template_id)
            if path is None:
                raise HTTPException(404, "This template has no reference image")
            return Path(path)
        safe = safe_filename(name, "")
        path = samples_dir(template_id) / safe
        if not safe or safe != name or not path.exists():
            raise HTTPException(404, f"Sample '{name}' not found")
        return path

    def build(template_id, template=None, alignment=None):
        from src.pipeline import OMREngine

        directory = template_dir(template_id)
        path = directory / "template.json"
        overrides = None
        if template is not None or alignment:
            saved = ctx.templates.template_json(template_id) or {}
            wanted = dict(template if template is not None else saved)
            if alignment:
                merged = dict(wanted.get("alignment") or {})
                merged.update(alignment)
                wanted["alignment"] = merged
            # OMREngine replaces whole top-level keys; None drops removed ones
            overrides = {key: None for key in saved if key not in wanted}
            overrides.update(wanted)
        try:
            return OMREngine(path, template_overrides=overrides)
        except SystemExit as error:
            raise HTTPException(422, f"Template rejected: {error}") from None
        except Exception as error:
            raise HTTPException(422, f"Could not build the engine: {error}") from None

    def read_sheet(engine, path):
        from src.utils.image import ImageUtils

        images = ImageUtils.load_omr_image(
            path, engine.tuning_config, color=engine.needs_color
        )
        if not images:
            raise HTTPException(422, f"Could not read '{path.name}'")
        name, image = images[0]
        return engine.scan(image, name, keep_images=True)

    def summary(result, sample):
        geometry = result.geometry or {}
        overlay = geometry_overlay(geometry) or {"blocks": []}
        return {
            "sample": sample,
            "status": result.status,
            "error": result.error,
            "blocks": overlay["blocks"],
            "failed_blocks": [
                {"name": b["name"], "reason": b["reason"], "status": b["status"]}
                for b in overlay["blocks"]
                if not b["used"]
            ],
            "residual": geometry.get("residual"),
            "margin_trim": geometry.get("margin_trim"),
            "sheet_review": [r for r in result.review if r.get("kind") == "sheet"],
        }

    @app.get("/templates/{template_id}/align/samples", tags=["templates"], dependencies=secured)
    def list_samples(template_id: str):
        template_dir(template_id)
        return {
            "samples": sample_names(template_id),
            "has_reference": ctx.templates.reference_path(template_id) is not None,
        }

    @app.post("/templates/{template_id}/align/samples", tags=["templates"], dependencies=secured)
    def add_samples(template_id: str, files: List[UploadFile] = File(...)):
        directory = samples_dir(template_id)
        directory.mkdir(parents=True, exist_ok=True)
        if len(sample_names(template_id)) + len(files) > MAX_SAMPLES:
            raise HTTPException(400, f"At most {MAX_SAMPLES} samples per template")
        added = []
        for upload in files:
            name = safe_filename(upload.filename, "sample.png")
            if Path(name).suffix.lower() not in IMAGE_SUFFIXES:
                raise HTTPException(400, f"'{upload.filename}' is not an image or PDF")
            (directory / name).write_bytes(read_upload(upload))
            added.append(name)
        return {"added": added, "samples": sample_names(template_id)}

    @app.delete(
        "/templates/{template_id}/align/samples/{name}",
        tags=["templates"],
        dependencies=secured,
    )
    def delete_sample(template_id: str, name: str):
        path = sample_path(template_id, name)
        if path.parent.name != SAMPLES_DIR:
            raise HTTPException(400, "Only uploaded samples can be deleted")
        path.unlink()
        return {"samples": sample_names(template_id)}

    @app.post("/templates/{template_id}/align/preview", tags=["templates"], dependencies=secured)
    def align_preview(template_id: str, body: AlignBody = Body(...)):
        """Read one sheet with the (unsaved) template; return its geometry to draw."""
        path = sample_path(template_id, body.sample)
        engine = build(template_id, body.template, body.alignment)
        result = read_sheet(engine, path)
        out = summary(result, body.sample or "reference")
        out["fields"] = _bubbles(result.fields)
        out["geometry"] = result.geometry
        image = result.aligned_image
        out["view"] = "dropout"
        if body.view == "print" and result.print_image is not None:
            image = result.print_image
            out["view"] = "print"
        if image is not None:
            out["width"], out["height"] = int(image.shape[1]), int(image.shape[0])
            if body.image:
                out["image_data"] = _encode(image)
        return json.loads(json.dumps(out, default=_np_default))

    @app.post("/templates/{template_id}/align/test", tags=["templates"], dependencies=secured)
    def align_test(template_id: str, body: TestBody = Body(TestBody())):
        """Run on every sample; list the blocks whose border was not found."""
        names = body.samples if body.samples is not None else sample_names(template_id)
        if not names:
            raise HTTPException(400, "No samples uploaded for this template")
        engine = build(template_id, body.template, body.alignment)
        rows = []
        for name in names:
            path = sample_path(template_id, name)
            try:
                rows.append(summary(read_sheet(engine, path), name))
            except HTTPException as error:
                rows.append({"sample": name, "status": "error", "error": error.detail,
                             "blocks": [], "failed_blocks": []})
        failed = sum(1 for row in rows if row["failed_blocks"] or row.get("error"))
        payload = {"samples": rows, "failed": failed, "total": len(rows)}
        return json.loads(json.dumps(payload, default=_np_default))


def _np_default(value):
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    raise TypeError(f"{type(value).__name__} is not JSON serialisable")
