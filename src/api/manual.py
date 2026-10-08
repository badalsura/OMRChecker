"""
Completing sheets whose page, track or index-point detection failed.

Two ways out, both on the original image:
- align by hand: the user clicks the 4 page corners (or the template's index
  points); that homography becomes the first step of the scan's geometry and
  the sheet is read again with the template's own registration skipped;
- type the values: every output is typed; the sheet counts as reviewed.
"""

import json
import time

import cv2
import numpy as np

from src.api.results import ResultsError, summarize
from src.api.storage import is_corrected


def align_targets(service, result):
    """Page size and the template's index points, for the click prompts."""
    with service.engines.engine(result) as (engine, _):
        template = engine.template
        width, height = (int(v) for v in template.page_dimensions)
        points = []
        for step in template.pre_processors:
            for point in getattr(step, "index_points", None) or []:
                points.append(
                    {"name": point["name"], "center": [float(v) for v in point["center"]]}
                )
    return {
        "page_size": [width, height],
        "index_points": points,
        "output_columns": service.template_info(result)["output_columns"],
    }


def _homography(clicks, targets):
    src = np.float32(clicks)
    dst = np.float32(targets)
    if len(src) != len(dst) or len(src) < 4:
        raise ResultsError("Click at least 4 points", 422)
    if len(src) == 4:
        matrix = cv2.getPerspectiveTransform(src, dst)
    else:
        matrix, _ = cv2.findHomography(src, dst, 0)
    if matrix is None or not np.all(np.isfinite(matrix)) or np.linalg.cond(matrix) > 1e8:
        raise ResultsError("Those points do not make a usable page outline", 422)
    return matrix.astype(np.float64)


def manual_align(service, scan_id, clicks, kind="corners", names=None, user=None):
    """Read the sheet again through a homography from clicked points."""
    from src.utils.image import ImageUtils

    with service.ctx.scan_lock(scan_id):
        result = service.load(scan_id)
        path, tried = service.source_of(result)
        if path is None:
            raise ResultsError(
                "The original file was not found (tried: "
                + ", ".join(tried or ["no path recorded"])
                + ")",
                404,
            )
        service.check_source(result, path)
        targets = align_targets(service, result)
        width, height = targets["page_size"]
        if kind == "corners":
            goal = [[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]]
        else:
            known = {p["name"]: p["center"] for p in targets["index_points"]}
            missing = [n for n in names or [] if n not in known]
            if missing or not names:
                raise ResultsError(f"Unknown index point(s): {missing or names}", 422)
            goal = [known[n] for n in names]
        matrix = _homography(clicks, goal)
        with service.engines.engine(result) as (engine, info):
            template = engine.template
            images = ImageUtils.load_omr_image(
                path, engine.tuning_config, color=engine.needs_color
            )
            page = int(result.get("page") or 0)
            if not images or page >= len(images):
                raise ResultsError(f"Could not read page {page + 1} of '{path}'", 422)
            image = images[page][1]
            white = (255, 255, 255) if image.ndim == 3 else 255
            warped = cv2.warpPerspective(
                image, matrix, (width, height), flags=cv2.INTER_LINEAR, borderValue=white
            )
            saved = template.pre_processors, template.alignment
            template.pre_processors = []
            template.alignment = {**saved[1], "page_outline": False}
            try:
                scanned = engine.scan(warped, result.get("file_id") or "sheet")
            finally:
                template.pre_processors, template.alignment = saved
        if scanned.status == "error":
            raise ResultsError(scanned.error or "The sheet could not be read", 422)
        new = scanned.to_dict()
        record = json.loads(json.dumps(result))
        for key, value in new.items():
            if key != "file_id":
                record[key] = value
        for key in ("read_review", "review_log", "verified", "corrected", "manual_values"):
            record.pop(key, None)
        record["error"] = None
        record["reviewed"] = False
        record["template_version"] = info["template_version"]
        geometry = record.get("geometry")
        if geometry:
            # The clicked homography is the first step from the original pixels
            step = {
                "op": "warp",
                "matrix": matrix.tolist(),
                "size": [width, height],
                "inverse": False,
                "affine": False,
                "border": 255,
            }
            page_h = np.asarray(geometry["page_homography"], np.float64) @ matrix
            geometry["page_homography"] = (page_h / page_h[2, 2]).tolist()
            geometry["steps"] = [step] + list(geometry.get("steps") or [])
            geometry["source_size"] = [int(image.shape[1]), int(image.shape[0])]
            geometry["alignment_method"] = f"manual_{kind}"
        record["manual_alignment"] = {
            "kind": kind,
            "points": [[float(x), float(y)] for x, y in clicks],
            "names": names or None,
            "at": time.time(),
            "by": user,
        }
        record["corrected"] = is_corrected(record)
        service.save(record)
        service.ctx.index.add_scans([{**summarize(record), "read_review": None}])
        service.renders.drop(scan_id)
    return record


def manual_values(service, scan_id, values, user=None):
    """Store typed values for a sheet the engine could not read."""
    with service.ctx.scan_lock(scan_id):
        result = service.load(scan_id)
        info = service.template_info(result)
        columns = info["output_columns"]
        unknown = [name for name in values if name not in columns]
        if unknown:
            raise ResultsError(f"Not outputs of this template: {unknown}", 422)
        typed = {name: str(value) for name, value in values.items()}
        responses = {name: "" for name in columns}
        responses.update(result.get("responses") or {})
        responses.update(typed)
        result["responses"] = responses
        result["manual_values"] = {**(result.get("manual_values") or {}), **typed}
        if result.get("status") == "error":
            result["read_error"] = result.get("error")
            result["error"] = None
        result["manual_entry"] = {"at": time.time(), "by": user}
        result["review"] = []
        result["reviewed"] = True
        result["status"] = "ok"
        result["corrected"] = True
        service.save(result)
        service.ctx.index.add_scans([{**summarize(result), "read_review": None}])
    return result
