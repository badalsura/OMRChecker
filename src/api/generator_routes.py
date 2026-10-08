"""
Template generator helpers for the editor's Alignment panel (plan items 10, 16).

All coordinates are template page pixels (the reference image is the blank
form at pageDimensions). The routes only look at the template's own reference
image and the sample sheets saved when the draft was generated.

- POST /templates/{id}/generator/find-track      {box}          -> track marks
- GET  /templates/{id}/generator/printed-boxes  -> printed rectangles
- POST /templates/{id}/generator/find-mark       {point | box}  -> index point
- POST /templates/{id}/generator/adopt-box       {box}          -> field block
- POST /templates/{id}/generator/test-alignment  {}             -> per-sample result
- POST /templates/{id}/generator/calibrate-index {}            -> measured points
- POST /templates/{id}/generator/acknowledge     {warnings, all} -> report
"""

import json
from pathlib import Path
from typing import List, Optional

import cv2
import numpy as np
from fastapi import Body, HTTPException
from pydantic import BaseModel, Field

SAMPLES_DIR = "_samples"
MAX_TEST_SAMPLES = 30


class BoxBody(BaseModel):
    box: List[float] = Field(..., min_length=4, max_length=4, description="[x, y, w, h]")


class MarkBody(BaseModel):
    point: Optional[List[float]] = Field(None, description="[x, y] of a click")
    box: Optional[List[float]] = Field(None, description="[x, y, w, h] of a drawn box")


class VerifyBody(BaseModel):
    items: List[str] = Field(default_factory=list, description="Item keys to mark verified")
    undo: bool = Field(False, description="Un-verify the given items (all when none given)")


class AckBody(BaseModel):
    warnings: List[str] = Field(default_factory=list, description="Warning texts confirmed")
    all: bool = Field(False, description="Confirm every current warning")
    undo: bool = Field(False, description="Bring the confirmed warnings back")


def fallback_draft(image, error, max_width=1700):
    """An empty draft (no blocks) on the first sample, used when generation
    raised: the editor still opens on the real sheet with the error shown."""
    from types import SimpleNamespace

    gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    if gray.shape[1] > max_width:
        scale = max_width / gray.shape[1]
        gray = cv2.resize(gray, (max_width, int(round(gray.shape[0] * scale))), interpolation=cv2.INTER_AREA)
    height, width = gray.shape[:2]
    template = {
        "pageDimensions": [int(width), int(height)],
        "bubbleDimensions": [20, 20],
        "preProcessors": [],
        "fieldBlocks": {},
    }
    report = {
        "warnings": [
            f"Template generation failed ({error}). This is an empty draft on the "
            "first sample sheet: add blocks, timing tracks or index points by hand."
        ],
        "generation_failed": str(error),
    }
    return SimpleNamespace(template=template, report=report, reference_image=gray, config=None)


def save_samples(directory, uploads):
    """Keep the generator's sample sheets next to the template (for testing)."""
    folder = Path(directory) / SAMPLES_DIR
    folder.mkdir(parents=True, exist_ok=True)
    saved = []
    for index, (name, content) in enumerate(uploads):
        safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in Path(name).name)
        safe = safe or f"sample{index}.png"
        target = folder / f"{index:03d}_{safe}"
        target.write_bytes(content)
        saved.append(target.name)
    return saved


def register(app, ctx, secured, decode_image):
    def template_dir(template_id):
        if not ctx.templates.exists(template_id):
            raise HTTPException(404, f"Unknown template '{template_id}'")
        return ctx.templates.path(template_id)

    def page_size(template_id):
        template = ctx.templates.template_json(template_id) or {}
        dims = template.get("pageDimensions") or [0, 0]
        return int(dims[0]), int(dims[1])

    def reference(template_id):
        path = ctx.templates.reference_path(template_id)
        if path is None:
            raise HTTPException(409, "This template has no reference image")
        gray = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if gray is None:
            raise HTTPException(409, "The reference image can't be read")
        size = page_size(template_id)
        if size[0] > 0 and (gray.shape[1], gray.shape[0]) != size:
            gray = cv2.resize(gray, size, interpolation=cv2.INTER_AREA)
        return gray, (gray.shape[1], gray.shape[0])

    @app.post(
        "/templates/{template_id}/generator/find-track",
        tags=["templates"],
        dependencies=secured,
    )
    def find_track(template_id: str, body: BoxBody):
        """Find the timing marks inside a box drawn over a strip of marks."""
        from src.template_gen import marks

        gray, size = reference(template_id)
        track = marks.find_marks_in_box(gray, body.box, size)
        if track is None:
            raise HTTPException(422, "No row of solid marks found inside the box")
        track.pop("boxes", None)
        return track

    @app.get(
        "/templates/{template_id}/generator/printed-boxes",
        tags=["templates"],
        dependencies=secured,
    )
    def printed_boxes(template_id: str):
        """Rectangles printed on the reference sheet (block border candidates)."""
        from src.template_gen import boxes

        gray, size = reference(template_id)
        found = boxes.detect_printed_boxes(gray, size, min_area=0.0005)
        return {"boxes": [[int(v) for v in box] for box in found], "page_size": list(size)}

    @app.post(
        "/templates/{template_id}/generator/find-mark",
        tags=["templates"],
        dependencies=secured,
    )
    def find_mark(template_id: str, body: MarkBody):
        """The printed mark under a click (or inside a box): an index point."""
        from src.template_gen import marks

        if body.point is None and body.box is None:
            raise HTTPException(400, "Give a point or a box")
        gray, size = reference(template_id)
        found = marks.find_mark_near(gray, body.point, size, box=body.box)
        if found is None:
            raise HTTPException(422, "No solid printed mark found there")
        return found

    @app.post(
        "/templates/{template_id}/generator/adopt-box",
        tags=["templates"],
        dependencies=secured,
    )
    def adopt_box(template_id: str, body: BoxBody):
        """Turn a printed box into a field block (bubble grid found inside it)."""
        from src.template_gen import boxes

        gray, size = reference(template_id)
        template = ctx.templates.template_json(template_id) or {}
        block, info = boxes.block_from_box(
            gray, body.box, size, template.get("bubbleDimensions")
        )
        if block is None:
            raise HTTPException(422, f"Can't adopt this box: {info}")
        return {"block": block, **info}

    @app.post(
        "/templates/{template_id}/generator/test-alignment",
        tags=["templates"],
        dependencies=secured,
    )
    def test_alignment(template_id: str):
        """
        Re-run the saved template's alignment on the sample sheets kept from
        generation: per sheet, the status, the marks matched and the index
        points found, read on the aligned page the engine produced.
        """
        from src.template_gen import marks

        directory = template_dir(template_id)
        folder = directory / SAMPLES_DIR
        files = sorted(folder.glob("*")) if folder.is_dir() else []
        if not files:
            raise HTTPException(
                409, "No sample sheets are stored with this template (generate it again)"
            )
        template = ctx.templates.template_json(template_id) or {}
        timing = next(
            (
                p.get("options") or {}
                for p in template.get("preProcessors", []) or []
                if p.get("name") == "TimingMarkAlignment"
            ),
            None,
        )
        tracks = {}
        if timing:
            for name, track in (timing.get("tracks") or {}).items():
                points = track.get("marks") or []
                gaps = np.diff(np.array(points, dtype=float), axis=0) if len(points) > 1 else []
                pitch = float(np.median(np.linalg.norm(gaps, axis=1))) if len(gaps) else 20.0
                tracks[name] = {"marks": points, "pitch": pitch}
        index_points = list((timing or {}).get("indexPoints") or [])
        size = page_size(template_id)
        out = []
        try:
            engine_context = ctx.engines.engine(template_id)
            engine = engine_context.__enter__()
        except Exception as error:
            raise HTTPException(422, f"Template failed to load: {error}") from None
        try:
            for path in files[:MAX_TEST_SAMPLES]:
                name = path.name.split("_", 1)[-1]
                entry = {"file": name}
                image = decode_image(path.read_bytes(), path.name, colour=True)
                if image is None:
                    out.append({**entry, "status": "error", "error": "unreadable"})
                    continue
                try:
                    result = engine.scan(image, name, keep_images=True)
                except Exception as error:
                    out.append({**entry, "status": "error", "error": str(error)})
                    continue
                entry["status"] = result.status
                if result.error:
                    entry["error"] = result.error
                for processor in engine.template.pre_processors:
                    record = getattr(processor, "last_registration", None)
                    if record:
                        entry["registration"] = record
                aligned = result.aligned_image
                if aligned is not None and (tracks or index_points):
                    if aligned.ndim == 3:
                        aligned = cv2.cvtColor(aligned, cv2.COLOR_BGR2GRAY)
                    if size[0] and (aligned.shape[1], aligned.shape[0]) != size:
                        aligned = cv2.resize(aligned, size, interpolation=cv2.INTER_AREA)
                    counts = marks.match_counts(aligned, tracks, index_points)
                    entry.update(counts)
                    entry["found"] = sum(v[0] for v in counts["tracks"].values())
                    entry["expected"] = sum(v[1] for v in counts["tracks"].values())
                out.append(entry)
        finally:
            engine_context.__exit__(None, None, None)
        return {"sheets": out, "tested": len(out), "stored": len(files)}

    @app.post(
        "/templates/{template_id}/generator/calibrate-index",
        tags=["templates"],
        dependencies=secured,
    )
    def calibrate_index(template_id: str):
        """
        Measure each index point on the stored sample sheets, aligned by the
        tracks alone: the median position and size, the spread, and the sheets
        it was missed on. The editor stores the medians.
        """
        from src.template_gen import marks

        directory = template_dir(template_id)
        folder = directory / SAMPLES_DIR
        files = sorted(folder.glob("*")) if folder.is_dir() else []
        if not files:
            raise HTTPException(
                409, "No sample sheets are stored with this template (generate it again)"
            )
        size = page_size(template_id)
        with ctx.engines.engine(template_id) as engine:
            aligner = next(
                (
                    p
                    for p in engine.template.pre_processors
                    if getattr(p, "index_points", None)
                ),
                None,
            )
            if aligner is None:
                raise HTTPException(409, "This template has no index points")
            points = [
                {
                    "name": p["name"],
                    "center": [float(v) for v in p["center"]],
                    "size": [float(v) for v in p["size"]],
                }
                for p in aligner.index_points
            ]
            blocks = []
            for block in engine.template.field_blocks:
                bubbles = [b for row in block.traverse_bubbles for b in row]
                if bubbles:
                    bw, bh = block.bubble_dimensions
                    blocks.append(
                        (
                            min(b.x for b in bubbles),
                            min(b.y for b in bubbles),
                            max(b.x for b in bubbles) + bw,
                            max(b.y for b in bubbles) + bh,
                        )
                    )
            # Tracks alone place the page; the points are then only measured
            joint = getattr(aligner, "joint_fit", True)
            aligner.joint_fit = False if len(aligner.expected) else joint
            seen = {p["name"]: [] for p in points}
            missed = {p["name"]: [] for p in points}
            tested = 0
            try:
                for path in files[:MAX_TEST_SAMPLES]:
                    image = decode_image(path.read_bytes(), path.name, colour=True)
                    if image is None:
                        continue
                    try:
                        result = engine.scan(image, path.name, keep_images=True)
                    except Exception:
                        continue
                    aligned = result.aligned_image
                    if aligned is None:
                        continue
                    tested += 1
                    if aligned.ndim == 3:
                        aligned = cv2.cvtColor(aligned, cv2.COLOR_BGR2GRAY)
                    if size[0] and (aligned.shape[1], aligned.shape[0]) != size:
                        aligned = cv2.resize(aligned, size, interpolation=cv2.INTER_AREA)
                    name = path.name.split("_", 1)[-1]
                    for point in points:
                        found = marks.find_mark_near(
                            aligned,
                            point["center"],
                            (aligned.shape[1], aligned.shape[0]),
                            radius=1.5 * max(point["size"]),
                        )
                        if found is not None and np.hypot(
                            found["center"][0] - point["center"][0],
                            found["center"][1] - point["center"][1],
                        ) > max(point["size"]):
                            found = None
                        if found is None:
                            missed[point["name"]].append(name)
                        else:
                            seen[point["name"]].append(
                                ([float(v) for v in found["center"]], [float(v) for v in found["size"]])
                            )
            finally:
                aligner.joint_fit = joint
        out = []
        for point in points:
            found = seen[point["name"]]
            entry = {"name": point["name"], "found": len(found), "missed": missed[point["name"]]}
            if found:
                centres = np.array([c for c, _ in found])
                sizes = np.array([s for _, s in found])
                centre = np.median(centres, axis=0)
                measured = np.median(sizes, axis=0)
                spread = float(np.median(np.linalg.norm(centres - centre, axis=1)))
                entry.update(
                    center=[round(float(v), 1) for v in centre],
                    size=[int(round(float(v))) for v in measured],
                    spread=round(spread, 2),
                    moved=round(float(np.hypot(*(centre - np.array(point["center"])))), 1),
                )
                problems = []
                if spread > 0.25 * float(max(measured)) or missed[point["name"]]:
                    problems.append("pick a more distinct mark")
                x, y = centre
                if any(x0 <= x <= x1 and y0 <= y <= y1 for x0, y0, x1, y1 in blocks):
                    problems.append("it lies on a bubble block")
                entry["problems"] = problems
            else:
                entry["problems"] = ["not found on any sample"]
            out.append(entry)
        return {"points": out, "tested": tested}

    @app.post(
        "/templates/{template_id}/generator/acknowledge",
        tags=["templates"],
        dependencies=secured,
    )
    def acknowledge(template_id: str, body: AckBody = Body(...)):
        """Confirm (clear) generator warnings; they stay listed as confirmed."""
        template_dir(template_id)
        meta = ctx.templates.meta(template_id)
        report = meta.get("report") or {}
        current = [w for w in report.get("warnings") or [] if isinstance(w, str)]
        confirmed = list(report.get("acknowledged_warnings") or [])
        if body.undo:
            current = current + confirmed
            confirmed = []
        else:
            chosen = current if body.all else [w for w in current if w in body.warnings]
            confirmed += chosen
            current = [w for w in current if w not in chosen]
        report["warnings"] = current
        report["acknowledged_warnings"] = confirmed
        meta["report"] = report
        ctx.templates.write_meta(template_id, meta)
        return {"warnings": current, "acknowledged_warnings": confirmed}

    @app.post(
        "/templates/{template_id}/generator/verify",
        tags=["templates"],
        dependencies=secured,
    )
    def verify(template_id: str, body: VerifyBody = Body(...)):
        """Mark "Needs verification" items as checked; saved with the template."""
        template_dir(template_id)
        meta = ctx.templates.meta(template_id)
        report = meta.get("report") or {}
        verified = [k for k in report.get("verified_items") or [] if isinstance(k, str)]
        if body.undo:
            verified = [k for k in verified if body.items and k not in body.items]
        else:
            verified += [k for k in body.items if k not in verified]
        report["verified_items"] = verified
        meta["report"] = report
        ctx.templates.write_meta(template_id, meta)
        return {"verified_items": verified}


def json_safe(value):
    return json.loads(json.dumps(value, default=str))
