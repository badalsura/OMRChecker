"""
Template-authoring tools: sheet colour palette, colour-dropout suggestions and
previews (used by the editor's Colours panel).

    POST /tools/colors            multipart file (+ template_id, k) -> palette
    POST /tools/dropout-preview   multipart file + settings JSON    -> PNG
    POST /tools/dropout-suggest   JSON {target, keep}                -> settings
"""

import json
from typing import List, Optional

import cv2
import numpy as np
from fastapi import Body, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from src.color import (
    analyse_sheet,
    apply_dropout,
    dropout_to_json,
    normalize_dropout,
    suggest_dropout,
)

PREVIEW_MAX_WIDTH = 1600


class SuggestBody(BaseModel):
    target: str = Field(..., description="Colour to remove, '#RRGGBB'")
    keep: Optional[List[str]] = Field(
        None,
        description="Colours that must stay dark (default: black, pencil, blue pens)",
    )


def decode_color_upload(content, filename=None):
    """BGR image from an uploaded image or the first page of a PDF."""
    if (filename or "").lower().endswith(".pdf"):
        try:
            import fitz

            document = fitz.open(stream=content, filetype="pdf")
            pixmap = document[0].get_pixmap(dpi=100, colorspace=fitz.csRGB)
            rgb = np.frombuffer(pixmap.samples, np.uint8).reshape(
                pixmap.height, pixmap.width, pixmap.n
            )
            return cv2.cvtColor(rgb[:, :, :3], cv2.COLOR_RGB2BGR)
        except Exception:
            return None
    return cv2.imdecode(np.frombuffer(content, np.uint8), cv2.IMREAD_COLOR)


def register_tool_routes(app, secured, read_upload, ctx):
    def load(upload):
        image = decode_color_upload(read_upload(upload), upload.filename)
        if image is None:
            raise HTTPException(400, f"'{upload.filename}' is not a readable image")
        return image

    @app.post("/tools/colors", tags=["tools"], dependencies=secured)
    def sheet_colors(
        file: UploadFile = File(..., description="A sample sheet (image or PDF)"),
        template_id: Optional[str] = Form(None),
        k: int = Form(6, ge=2, le=12),
    ):
        """Main colours of a sheet with dropout suggestions for print colours."""
        image = load(file)
        palette = analyse_sheet(image, k=k)
        palette["width"], palette["height"] = image.shape[1], image.shape[0]
        palette["current"] = None
        if template_id:
            if not ctx.templates.exists(template_id):
                raise HTTPException(404, f"Template '{template_id}' not found")
            template = ctx.templates.template_json(template_id) or {}
            if template.get("colorDropout") is not None:
                palette["current"] = template["colorDropout"]
        return palette

    @app.post("/tools/dropout-suggest", tags=["tools"], dependencies=secured)
    def dropout_suggest(body: SuggestBody = Body(...)):
        """Dropout settings that remove `target` while keeping `keep` dark."""
        try:
            return suggest_dropout(body.target, body.keep)
        except ValueError as error:
            raise HTTPException(400, str(error)) from None

    @app.post("/tools/dropout-preview", tags=["tools"], dependencies=secured)
    def dropout_preview(
        file: UploadFile = File(...),
        settings: str = Form(
            "{}", description='colorDropout JSON, e.g. {"mode": "red"} or "grey"'
        ),
        max_width: int = Form(1000, ge=64, le=PREVIEW_MAX_WIDTH),
    ):
        """The grey image the reader would see with these dropout settings (PNG)."""
        text = (settings or "").strip()
        try:
            value = json.loads(text) if text else None
        except ValueError:
            value = text  # a bare mode name such as grey or red
        try:
            spec = normalize_dropout(value)
        except (ValueError, TypeError, AttributeError) as error:
            raise HTTPException(400, f"settings: {error}") from None
        image = load(file)
        height, width = image.shape[:2]
        if width > max_width:
            image = cv2.resize(
                image, (max_width, int(height * max_width / width)), cv2.INTER_AREA
            )
        grey = apply_dropout(image, spec)
        ok, buffer = cv2.imencode(".png", grey, [cv2.IMWRITE_PNG_COMPRESSION, 1])
        if not ok:
            raise HTTPException(500, "Could not encode image")
        return Response(
            content=buffer.tobytes(),
            media_type="image/png",
            headers={"X-Dropout": json.dumps(dropout_to_json(spec))},
        )
