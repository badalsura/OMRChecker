"""
Template generator helpers for the editor's Alignment panel (plan items 10, 16).

All coordinates are template page pixels (the reference image is the blank
form at pageDimensions). The routes only look at the template's own reference
image and the sample sheets saved when the draft was generated.

- POST /templates/{id}/generator/find-track      {box}          -> track marks
- POST /templates/{id}/generator/find-mark       {point | box}  -> index point
- POST /templates/{id}/generator/adopt-box       {box}          -> field block
- POST /templates/{id}/generator/test-alignment  {}             -> per-sample result
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
