"""
Helpers for the template editor and the Scan / New Job screens:

* GET /templates/{id}/models: ONNX models the template can use
  (config.json ml_params model pickers);
* parse_pdf_params(): per-request PDF DPI / pages for POST /scans and /jobs.
"""

import json
import os
import re
from pathlib import Path

from fastapi import HTTPException

# Bundled or trained models: <repo>/models and OMR_MODELS_DIR
REPO_MODELS = Path(__file__).resolve().parents[2] / "models"
PAGE_SPEC = re.compile(r"^\d+(?:-\d*)?$")


def parse_pdf_params(pdf_dpi=None, pdf_page=None):
    """{"pdf_dpi", "pdf_page"} from form values, or None to keep config.json."""
    params = {}
    dpi = (pdf_dpi or "").strip().lower()
    if dpi:
        if dpi == "auto":
            params["pdf_dpi"] = "auto"
        else:
            try:
                value = int(dpi)
            except ValueError:
                raise HTTPException(400, "pdf_dpi must be a number or 'auto'")
            if not 72 <= value <= 600:
                raise HTTPException(400, "pdf_dpi must be between 72 and 600")
            params["pdf_dpi"] = value
    page = (pdf_page or "").replace(" ", "").lower()
    if page:
        if page == "all":
            params["pdf_page"] = "1-"
        else:
            parts = [p for p in page.split(",") if p]
            if not parts or not all(PAGE_SPEC.match(p) for p in parts):
                raise HTTPException(
                    400, "pdf_page must look like 1, 2-4, 3- or all (comma separated)"
                )
            values = [int(p) if p.isdigit() else p for p in parts]
            if any(v == 0 for v in values):
                raise HTTPException(400, "PDF pages start at 1")
            params["pdf_page"] = values[0] if len(values) == 1 else values
    return params or None


def _model_kind(path):
    sidecar = Path(path).with_suffix(".json")
    try:
        return (json.loads(sidecar.read_text()) or {}).get("kind")
    except Exception:
        return None


def list_models(template_dir):
    """[{"path", "name", "kind", "location"}] of .onnx files the template can use."""
    models = []
    seen = set()
    template_dir = Path(template_dir)
    if template_dir.is_dir():
        for path in sorted(template_dir.rglob("*.onnx")):
            relative = path.relative_to(template_dir).as_posix()
            seen.add(path.resolve())
            models.append(
                {
                    "path": relative,
                    "name": relative,
                    "kind": _model_kind(path),
                    "location": "template",
                }
            )
    roots = [REPO_MODELS]
    if os.environ.get("OMR_MODELS_DIR"):
        roots.append(Path(os.environ["OMR_MODELS_DIR"]))
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.onnx")):
            if path.resolve() in seen:
                continue
            seen.add(path.resolve())
            models.append(
                {
                    "path": str(path.resolve()),
                    "name": path.name,
                    "kind": _model_kind(path),
                    "location": "server",
                }
            )
    return models


def register(app, ctx, secured):
    @app.get("/templates/{template_id}/models", tags=["templates"], dependencies=secured)
    def template_models(template_id: str):
        """ONNX models in the template folder and the server's model folders."""
        try:
            directory = ctx.templates.path(template_id)
        except KeyError:
            raise HTTPException(404, f"Template '{template_id}' not found")
        if not directory.is_dir():
            raise HTTPException(404, f"Template '{template_id}' not found")
        return {"models": list_models(directory)}
