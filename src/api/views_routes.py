"""
Other views of a graded sheet for the Results screen (plan item 15):

- "original": the scan as it came off the scanner (in colour, upright), with
  every bubble, field and zone outline mapped back from the aligned page
  through the page transform the engine recorded (result["geometry"]);
- "color": the aligned page in its original colours (no colour dropout, no
  grey), built by applying the same recorded transform to the colour scan, so
  the normal overlay sits exactly as on the aligned view.

Nothing is re-detected: both views replay the stored geometry. Sheets read
before geometry was recorded fall back to a plain resize and say so.
"""

import hashlib
import json

import cv2
from types import SimpleNamespace
from typing import List, Optional

import numpy as np
from fastapi import HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from src.api.results import RenderCache, ResultsError
from src.geometry import map_points, warp_to_aligned

VIEWS = ("original", "color")
NO_GEOMETRY = (
    "This sheet was read before the page transform was recorded: the original "
    "is only resized onto the page, so outlines are approximate."
)


def rotate(image, degrees):
    degrees = int(degrees or 0) % 360
    if degrees == 90:
        return cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE)
    if degrees == 180:
        return cv2.rotate(image, cv2.ROTATE_180)
    if degrees == 270:
        return cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE)
    return image


def source_page(service, result):
    """The sheet's page in colour, in the geometry's source pixels."""
    from src.utils.image import ImageUtils

    path, tried = service.source_of(result)
    if path is None:
        raise ResultsError(
            "The original file was not found (tried: "
            + ", ".join(tried or ["no path recorded"])
            + "). Set a path remap (Results > Path remap) if the folder moved.",
            404,
        )
    service.check_source(result, path)
    with service.engines.engine(result) as (engine, _):
        images = ImageUtils.load_omr_image(path, engine.tuning_config, color=True)
    page = int(result.get("page") or 0)
    if not images or page >= len(images):
        raise ResultsError(f"Could not read page {page + 1} of '{path}'", 422)
    image = images[page][1]
    if image.ndim == 2:
        image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    geometry = result.get("geometry") or {}
    image = rotate(image, geometry.get("rotation"))
    size = geometry.get("source_size")
    if size and (image.shape[1], image.shape[0]) != (int(size[0]), int(size[1])):
        image = cv2.resize(image, (int(size[0]), int(size[1])))
    return image, str(path)


def effective_geometry(service, scan_id, result, source_image):
    """(geometry, recorded?) used to map between the original and aligned page."""
    geometry = result.get("geometry")
    if geometry and geometry.get("aligned_size"):
        geometry = dict(geometry)
        geometry.setdefault(
            "source_size", [source_image.shape[1], source_image.shape[0]]
        )
        return geometry, True
    try:
        aligned = service.render(scan_id)["image"]
    except ResultsError:
        # A sheet that failed registration: the original is still worth showing
        aligned = source_image
    return (
        {
            "source_size": [source_image.shape[1], source_image.shape[0]],
            "aligned_size": [aligned.shape[1], aligned.shape[0]],
        },
        False,
    )


def _quad(box):
    x, y, w, h = [float(v) for v in box]
    return [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]


def map_overlay(result, geometry):
    """Outlines of every bubble, field, zone and block, in source pixels."""
    quads, keys = [], []

    def add(key, box):
        if box and len(box) == 4:
            keys.append(key)
            quads.extend(_quad(box))

    for name, field in (result.get("fields") or {}).items():
        bubbles = field.get("bubbles") or []
        for bubble in bubbles:
            add(
                ("bubble", name, bubble["value"]),
                [bubble["x"], bubble["y"], bubble["w"], bubble["h"]],
            )
        if bubbles:
            x0 = min(b["x"] for b in bubbles)
            y0 = min(b["y"] for b in bubbles)
            x1 = max(b["x"] + b["w"] for b in bubbles)
            y1 = max(b["y"] + b["h"] for b in bubbles)
            add(("field", name, None), [x0, y0, x1 - x0, y1 - y0])
    for name, zone in (result.get("zones") or {}).items():
        add(("zone", name, None), zone.get("box"))
    blocks = {}
    block_corners = []
    for name, block in ((result.get("geometry") or {}).get("blocks") or {}).items():
        corners = (block or {}).get("corners")
        if corners and len(corners) == 4:
            blocks[name] = {"status": block.get("status"), "corners": None}
            block_corners.append(name)
            quads.extend([[float(x), float(y)] for x, y in corners])
    mapped = map_points(geometry, quads, "aligned_to_source") if quads else []
    out = {"fields": {}, "zones": {}, "blocks": blocks}
    for index, (kind, name, value) in enumerate(keys):
        poly = [[round(x, 1), round(y, 1)] for x, y in mapped[index * 4 : index * 4 + 4]]
        if kind == "bubble":
            out["fields"].setdefault(name, {"bubbles": {}, "box": None})["bubbles"][
                value
            ] = poly
        elif kind == "field":
            out["fields"].setdefault(name, {"bubbles": {}, "box": None})["box"] = poly
        else:
            out["zones"][name] = poly
    offset = len(keys) * 4
    for index, name in enumerate(block_corners):
        start = offset + index * 4
        blocks[name]["corners"] = [
            [round(x, 1), round(y, 1)] for x, y in mapped[start : start + 4]
        ]
    # Registration view: where the aligned page sits on the original
    size = geometry.get("aligned_size")
    if size and result.get("geometry"):
        page = map_points(geometry, _quad([0, 0, size[0] - 1, size[1] - 1]), "aligned_to_source")
        out["page"] = [[round(x, 1), round(y, 1)] for x, y in page]
    return out


class DecodeBody(BaseModel):
    box: List[float] = Field(
        ..., min_length=4, max_length=4, description="[x, y, w, h] in the view's pixels"
    )
    view: str = Field(
        "aligned", description="Pixels of: aligned (and color) or original"
    )
    zone: Optional[str] = Field(
        None, description="The barcode/QR zone the box is for (picks QR or 1-D)"
    )


def decode_area(source, geometry, box, view, zone_type):
    """Barcode/QR symbols in a box drawn on the aligned page or the original."""
    from src.readers.barcode import read_barcode_zone

    quad = _quad(box)
    if view != "original":
        quad = map_points(geometry, quad, "aligned_to_source")
    quad = np.asarray(quad, dtype=np.float64)
    h, w = source.shape[:2]
    x0, y0 = quad.min(axis=0)
    x1, y1 = quad.max(axis=0)
    pad = 0.08 * max(x1 - x0, y1 - y0)
    x0, y0 = int(max(0, x0 - pad)), int(max(0, y0 - pad))
    x1, y1 = int(min(w, x1 + pad)), int(min(h, y1 + pad))
    if x1 - x0 < 8 or y1 - y0 < 8:
        raise ResultsError("Draw a bigger box around the code", 422)
    crop = source[y0:y1, x0:x1]
    # The full source is used: the original pixels decode best
    zone = SimpleNamespace(
        name="area",
        type=zone_type,
        options={},
        crop=lambda image, padding=0: image,
    )
    read = read_barcode_zone(zone, crop)
    symbols = (read.details or {}).get("symbols") or []
    return {
        "value": read.value,
        "format": read.format,
        "engine": read.engine,
        "symbols": symbols,
        "flags": read.flags,
        "source_box": [x0, y0, x1 - x0, y1 - y0],
    }


def register(app, ctx, secured):
    service = ctx.results
    cache = RenderCache(size=12)
    service.view_renders = cache

    def fail(error):
        raise HTTPException(error.status, str(error)) from None

    def build(scan_id, view):
        result = service.load(scan_id)
        # A regrade or a path remap changes what is shown; key on both
        stamp = hashlib.sha1(
            json.dumps(
                [result.get("geometry"), result.get("regrade"), service.source_of(result)[0]],
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest()[:12]
        key = f"{scan_id}:{view}:{stamp}"
        cached = cache.get(key)
        if cached is not None:
            return cached
        source, path = source_page(service, result)
        geometry, recorded = effective_geometry(service, scan_id, result, source)
        warnings = [] if recorded else [NO_GEOMETRY]
        meta = {
            "geometry_recorded": recorded,
            "resolved_path": path,
            "warnings": warnings,
        }
        if view == "original":
            image = source
            meta["map"] = map_overlay(result, geometry)
        else:
            image = warp_to_aligned(geometry, source)
        cache.put(key, image, meta)
        return cache.get(key)

    def encoded(item, fmt):
        if fmt not in item["encoded"]:
            if fmt == "png":
                ok, buffer = cv2.imencode(".png", item["image"])
            else:
                ok, buffer = cv2.imencode(
                    ".jpg", item["image"], [cv2.IMWRITE_JPEG_QUALITY, 90]
                )
            if not ok:
                raise HTTPException(500, "Could not encode image")
            item["encoded"][fmt] = buffer.tobytes()
        return item["encoded"][fmt]

    @app.post("/scans/{scan_id}/decode", tags=["results"], dependencies=secured)
    def scan_decode(scan_id: str, body: DecodeBody):
        """
        Decode a barcode or QR code inside a box the reviewer drew (box in the
        aligned page's pixels, or the original's with view=original). Reads the
        original scan at full resolution; nothing is stored.
        """
        try:
            result = service.load(scan_id)
            zone_type = "barcode"
            if body.zone:
                zone = (result.get("zones") or {}).get(body.zone)
                if not isinstance(zone, dict):
                    raise ResultsError(f"Unknown zone '{body.zone}'", 404)
                zone_type = zone.get("type") or zone_type
            item = build(scan_id, "original")
            source = item["image"]
            geometry, _ = effective_geometry(service, scan_id, result, source)
            view = "original" if body.view == "original" else "aligned"
            return decode_area(source, geometry, body.box, view, zone_type)
        except ResultsError as error:
            fail(error)

    @app.get("/scans/{scan_id}/views/{view}", tags=["results"], dependencies=secured)
    def scan_view(
        scan_id: str,
        view: str,
        format: str = Query("jpg", pattern="^(jpg|png)$"),
    ):
        """
        view=original: the colour scan with every outline mapped back through
        the recorded page transform (map: fields -> {box, bubbles: {value:
        quad}}, zones -> quad, blocks -> {corners, status}; quads in image
        pixels, TL TR BR BL). view=color: the aligned page in full colour
        (same coordinates as /render). Nothing is re-detected.
        """
        if view not in VIEWS:
            raise HTTPException(404, f"Unknown view '{view}' (original or color)")
        try:
            item = build(scan_id, view)
        except ResultsError as error:
            fail(error)
        image = item["image"]
        return {
            "scan_id": scan_id,
            "view": view,
            "width": int(image.shape[1]),
            "height": int(image.shape[0]),
            "image_url": f"/scans/{scan_id}/views/{view}/image?format={format}",
            **item["meta"],
        }

    @app.get(
        "/scans/{scan_id}/views/{view}/image", tags=["results"], dependencies=secured
    )
    def scan_view_image(
        scan_id: str, view: str, format: str = Query("jpg", pattern="^(jpg|png)$")
    ):
        if view not in VIEWS:
            raise HTTPException(404, f"Unknown view '{view}'")
        try:
            item = build(scan_id, view)
        except ResultsError as error:
            fail(error)
        return Response(
            content=encoded(item, format),
            media_type="image/png" if format == "png" else "image/jpeg",
            headers={"Cache-Control": "private, no-cache"},
        )


def drop_views(ctx, scan_id):
    cache = getattr(ctx.results, "view_renders", None)
    if cache is not None:
        cache.drop(scan_id)

