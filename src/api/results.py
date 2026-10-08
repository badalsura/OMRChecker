"""
Results review: look at any graded sheet with the exact overlay it was graded
with, correct it, regrade it with different settings, and measure accuracy.

Nothing per sheet is stored for this beyond result.json: the aligned image is
re-created on demand by re-reading the original file (its absolute path and the
template version are recorded with every scan) with an engine cached per
template version. A stored aligned.png is the fallback when the file is gone.

Corrections go through the same code as the review queue (review.apply_review)
so validation, training exports and recomputed responses/scores stay in one
place; every change is also an audit record (result.json "audit" + SQLite).
"""

import json
import ntpath
import re
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path

import cv2

from src.api.review import (
    ReviewError,
    apply_review,
    recompute,
    split_value,
    write_training_records,
)
from src.api.storage import (
    flag_rows,
    is_corrected,
    original_value,
    outcome_rows,
    read_json,
    write_json_atomic,
)
from src.api.worker import (
    PNG_FAST,
    archive_template_version,
    build_engine,
    summarize,
    template_hash,
    template_version,
)

DEFAULT_USER = "local"


class ResultsError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# path remapping
# ---------------------------------------------------------------------------
def _looks_windows(path):
    return bool(ntpath.splitdrive(path)[0]) or path.startswith("\\\\")


def _norm(path):
    path = str(path).replace("\\", "/")
    return path.rstrip("/") if len(path) > 1 else path


def remap_path(path, rule):
    """Apply one {"from", "to"} prefix rule; None when it does not match."""
    old, new = _norm(rule.get("from", "")), str(rule.get("to", ""))
    if not old:
        return None
    candidate = _norm(path)
    windows = _looks_windows(old) or _looks_windows(str(path))
    head = candidate[: len(old)]
    matches = head.lower() == old.lower() if windows else head == old
    rest = candidate[len(old) :]
    if not matches or (rest and not rest.startswith("/")):
        return None
    rest = rest.lstrip("/")
    if not rest:
        return new
    if "\\" in new and "/" not in new:
        return new.rstrip("\\") + "\\" + rest.replace("/", "\\")
    return new.rstrip("/\\") + "/" + rest


def source_candidates(path, rules):
    """Remapped locations first (in rule order), then the recorded path."""
    candidates = []
    for rule in rules or []:
        mapped = remap_path(path, rule)
        if mapped and mapped not in candidates:
            candidates.append(mapped)
    if path and str(path) not in candidates:
        candidates.append(str(path))
    return candidates


def resolve_source(path, rules):
    tried = source_candidates(path, rules)
    for candidate in tried:
        if Path(candidate).is_file():
            return Path(candidate), tried
    return None, tried


# ---------------------------------------------------------------------------
# engines per template version (and per regrade overrides)
# ---------------------------------------------------------------------------
class VersionedEngines:
    """
    Engines for re-reading stored scans. The current template version uses the
    app's EnginePool; older versions (archived under template_versions/) and
    engines with regrade overrides get a small LRU pool of their own.
    """

    def __init__(self, ctx, max_keys=16, max_idle=2):
        self.ctx = ctx
        self.max_keys = max_keys
        self.max_idle = max_idle
        self.idle = OrderedDict()
        self.lock = threading.Lock()

    def locate(self, result, use_current=False):
        """(template directory, version hash, exact) for the template of a result."""
        template_id = result.get("template_id")
        version = result.get("template_version")
        current_dir = None
        if template_id and self.ctx.templates.exists(template_id):
            current_dir = self.ctx.templates.path(template_id)
        if current_dir is not None:
            current = template_hash(current_dir)
            if use_current or version is None or version == current:
                return current_dir, current, version in (None, current)
        if template_id and version:
            archived = self.ctx.data.template_versions / template_id / version
            if (archived / "template.json").exists():
                return archived, version, True
        if current_dir is not None:
            return current_dir, template_hash(current_dir), False
        raise ResultsError(
            f"Template '{template_id}' no longer exists and the version this sheet "
            "was read with was not archived",
            409,
        )

    @contextmanager
    def engine(
        self, result, template_overrides=None, config_overrides=None, use_current=False
    ):
        directory, version, exact = self.locate(result, use_current)
        template_id = result.get("template_id")
        is_current = (
            template_id
            and self.ctx.templates.exists(template_id)
            and directory == self.ctx.templates.path(template_id)
        )
        info = {"directory": directory, "template_version": version, "exact": exact}
        if is_current and not template_overrides and not config_overrides:
            with self.ctx.engines.engine(template_id) as engine:
                yield engine, info
            return
        key = (
            str(directory),
            template_version(directory),
            json.dumps(template_overrides or {}, sort_keys=True),
            json.dumps(config_overrides or {}, sort_keys=True),
        )
        with self.lock:
            stack = self.idle.get(key)
            engine = stack.pop() if stack else None
        if engine is None:
            try:
                engine = build_engine(directory, template_overrides, config_overrides)
            except SystemExit as error:
                raise ResultsError(f"Template rejected: {error}", 422) from None
            except Exception as error:
                raise ResultsError(
                    f"Could not build the engine: {error}", 422
                ) from None
        try:
            yield engine, info
        finally:
            with self.lock:
                stack = self.idle.setdefault(key, [])
                self.idle.move_to_end(key)
                if len(stack) < self.max_idle:
                    stack.append(engine)
                while len(self.idle) > self.max_keys:
                    self.idle.popitem(last=False)


class RenderCache:
    """Last re-rendered aligned images (raw + encoded), so the image request after
    the JSON request, training crops and PDF pages don't re-read the sheet."""

    def __init__(self, size=24):
        self.size = size
        self.items = OrderedDict()
        self.lock = threading.Lock()

    def get(self, key):
        with self.lock:
            item = self.items.get(key)
            if item is not None:
                self.items.move_to_end(key)
            return item

    def put(self, key, image, meta=None):
        with self.lock:
            self.items[key] = {"image": image, "meta": meta or {}, "encoded": {}}
            self.items.move_to_end(key)
            while len(self.items) > self.size:
                self.items.popitem(last=False)

    def drop(self, scan_id):
        with self.lock:
            for key in [k for k in self.items if k.split(":")[0] == scan_id]:
                del self.items[key]


# ---------------------------------------------------------------------------
# the service object used by the endpoints, the exporter and tests
# ---------------------------------------------------------------------------
class ResultsService:
    def __init__(self, ctx):
        self.ctx = ctx
        self.engines = VersionedEngines(ctx)
        self.renders = RenderCache()

    # ---- helpers ---------------------------------------------------------
    def result_path(self, scan_id):
        if not scan_id or not scan_id.isalnum():
            raise ResultsError("Scan not found", 404)
        return self.ctx.data.scan_dir(scan_id) / "result.json"

    def load(self, scan_id):
        result = read_json(self.result_path(scan_id))
        if result is None:
            raise ResultsError(f"Scan '{scan_id}' not found", 404)
        return result

    def save(self, result):
        write_json_atomic(self.result_path(result["scan_id"]), result)

    def global_remap(self):
        stored = read_json(self.ctx.data.settings_file, {}) or {}
        return list(stored.get("path_remap") or [])

    def set_global_remap(self, rules):
        rules = clean_rules(rules)
        stored = read_json(self.ctx.data.settings_file, {}) or {}
        stored["path_remap"] = rules
        write_json_atomic(self.ctx.data.settings_file, stored)
        return rules

    def remap_rules(self, result):
        rules = []
        job_id = result.get("job_id")
        if job_id:
            job = self.ctx.jobs.get(job_id) or {}
            rules.extend(job.get("path_remap") or [])
        rules.extend(self.global_remap())
        rules.extend(self.ctx.settings.path_remap or [])
        return rules

    def source_of(self, result):
        recorded = result.get("source_path") or result.get("input_path")
        if not recorded:
            return None, []
        return resolve_source(recorded, self.remap_rules(result))

    def template_info(self, result):
        """Output columns, custom labels and empty values for a result's template."""
        try:
            with self.engines.engine(result) as (engine, info):
                template = engine.template
                empty_values = {}
                for block in template.field_blocks:
                    for bubbles in block.traverse_bubbles:
                        if bubbles:
                            empty_values[bubbles[0].field_label] = block.empty_val
                return {
                    "output_columns": list(template.output_columns),
                    "zone_names": [zone.name for zone in template.zones],
                    "custom_labels": {
                        k: list(v) for k, v in template.custom_labels.items()
                    },
                    "empty_values": empty_values,
                    "template_version": info["template_version"],
                    "exact": info["exact"],
                }
        except ResultsError:
            return {
                "output_columns": sorted((result.get("responses") or {}).keys()),
                "zone_names": sorted((result.get("zones") or {}).keys()),
                "custom_labels": {},
                "empty_values": {},
                "template_version": result.get("template_version"),
                "exact": False,
            }

    # ---- rendering ---------------------------------------------------------
    def reread(
        self, result, template_overrides=None, config_overrides=None, use_current=False
    ):
        """Re-run the engine on the original file; returns (ScanResult, path, info)."""
        path, tried = self.source_of(result)
        if path is None:
            raise ResultsError(
                "The original file was not found (tried: "
                + ", ".join(tried or ["no path recorded"])
                + "). Set a path remap (GUI: Results > Path remap, OMR_PATH_REMAP, "
                "or the job's path_remap) if the input folder was moved.",
                404,
            )
        from src.utils.image import ImageUtils

        with self.engines.engine(
            result, template_overrides, config_overrides, use_current
        ) as (engine, info):
            images = ImageUtils.load_omr_image(
                path, engine.tuning_config, color=engine.needs_color
            )
            page = int(result.get("page") or 0)
            if not images or page >= len(images):
                raise ResultsError(f"Could not read page {page + 1} of '{path}'", 422)
            name, image = images[page]
            scanned = engine.scan(
                image, result.get("file_id") or name, keep_images=True
            )
        return scanned, path, info

    def render(self, scan_id, preview=False):
        """Aligned image + metadata for a scan, re-read from its source when possible."""
        key = f"{scan_id}:preview" if preview else scan_id
        cached = self.renders.get(key)
        if cached is not None:
            return cached
        if preview:
            raise ResultsError("No regrade preview for this scan; run it again", 404)
        result = self.load(scan_id)
        regrade = result.get("regrade") or {}
        meta = {"image_source": None, "drift": [], "warnings": []}
        image = None
        source_error = None
        try:
            scanned, path, info = self.reread(
                result,
                regrade.get("template_overrides"),
                regrade.get("config_overrides"),
            )
            if scanned.aligned_image is None:
                raise ResultsError(scanned.error or "Sheet registration failed", 422)
            image = scanned.aligned_image
            meta.update(
                {
                    "image_source": "source",
                    "resolved_path": str(path),
                    "template_version_used": info["template_version"],
                    "exact_template": info["exact"],
                    "drift": drift(result, scanned),
                }
            )
            if not info["exact"]:
                meta["warnings"].append(
                    "The template changed since this sheet was read and the old "
                    "version was not archived; the current template was used."
                )
        except ResultsError as error:
            source_error = str(error)
        if image is None:
            stored = self.ctx.aligned_image(scan_id)
            if stored is None:
                raise ResultsError(
                    f"{source_error} No aligned image is stored for this scan either.",
                    404,
                )
            image = stored
            meta["image_source"] = "stored"
            meta["warnings"].append(f"Showing the stored aligned image. {source_error}")
        self.renders.put(key, image, meta)
        return self.renders.get(key)

    def encoded(self, scan_id, fmt="jpg", preview=False):
        item = self.render(scan_id, preview)
        if fmt not in item["encoded"]:
            image = item["image"]
            if fmt == "png":
                ok, buffer = cv2.imencode(".png", image, PNG_FAST)
            else:
                ok, buffer = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 90])
            if not ok:
                raise ResultsError("Could not encode image", 500)
            item["encoded"][fmt] = buffer.tobytes()
        return item["encoded"][fmt]

    def aligned_for_training(self, scan_id):
        item = self.renders.get(scan_id)
        if item is not None:
            return item["image"]
        return self.ctx.aligned_image(scan_id)

    # ---- overlay -------------------------------------------------------------
    def overlay(self, result, template_info=None):
        info = template_info or self.template_info(result)
        return overlay_payload(result, info)

    # ---- corrections ---------------------------------------------------------
    def correct(
        self,
        scan_id,
        changes=None,
        toggles=None,
        user=None,
        source="results",
        accept=None,
    ):
        user = user or DEFAULT_USER
        with self.ctx.scan_lock(scan_id):
            result = self.load(scan_id)
            info = self.template_info(result)
            changes = dict(changes or {})
            fields = result.get("fields") or {}
            for toggle in toggles or []:
                name, value = toggle.get("field"), toggle.get("value")
                if name not in fields:
                    raise ResultsError(f"'{name}' is not a bubble field", 422)
                current = changes.get(name, fields[name].get("value", ""))
                changes[name] = toggled_value(
                    fields[name], current, value, info["empty_values"].get(name, "")
                )
            accept = [name for name in accept or [] if name not in changes]
            if not changes and not accept:
                return result, info
            try:
                resolved, events = apply_review(
                    result,
                    changes,
                    accept,
                    user,
                    info["empty_values"],
                    info["custom_labels"],
                )
            except ReviewError as error:
                raise ResultsError(str(error), 422) from None
            audit = self.audit_rows(result, events, user, source)
            self.finish_edit(result, resolved)
            self.ctx.index.add_corrections(audit)
            training = [e for e in events if e[4] == "corrected"]
        if result.get("verified") and training:
            write_training_records(
                self.ctx.data.training,
                result,
                training,
                self.aligned_for_training(scan_id),
            )
        return result, info

    def audit_rows(self, result, events, user, source):
        now = time.time()
        rows = []
        for kind, name, predicted, label, _, item in events:
            if label == predicted:
                continue
            row = {
                "scan_id": result["scan_id"],
                "name": name,
                "kind": kind,
                "template_id": result.get("template_id"),
                "job_id": result.get("job_id"),
                "old": predicted,
                "new": label,
                "original": item.get("original_value", predicted),
                "user": user or DEFAULT_USER,
                "source": source,
                "at": now,
            }
            rows.append(row)
            result.setdefault("audit", []).append(
                {
                    k: row[k]
                    for k in (
                        "name",
                        "kind",
                        "old",
                        "new",
                        "original",
                        "user",
                        "source",
                        "at",
                    )
                }
            )
        return rows

    def finish_edit(self, result, resolved):
        """Recompute responses/score/status and persist file + index."""
        template_id = result.get("template_id")
        try:
            with self.engines.engine(result) as (engine, _):
                recompute(result, engine)
        except ResultsError:
            result["status"] = "needs_review" if result.get("review") else "ok"
        result["corrected"] = is_corrected(result)
        self.save(result)
        self.ctx.index.set_scan_state(result)
        # Rules may raise or settle items after an edit; mirror result["review"]
        self.ctx.index.sync_review_items(result)
        if result.get("verified"):
            self.ctx.index.set_outcomes(result["scan_id"], outcome_rows(result))
        return template_id

    def verify(self, scan_id, user=None, verified=True):
        """Mark every field of a sheet as checked by a person (or undo that)."""
        user = user or DEFAULT_USER
        with self.ctx.scan_lock(scan_id):
            result = self.load(scan_id)
            if not verified:
                result.pop("verified", None)
                self.save(result)
                self.ctx.index.set_scan_state(result)
                self.ctx.index.set_outcomes(scan_id, [])
                return result, []
            first_time = not result.get("verified")
            info = self.template_info(result)
            pending = [item["name"] for item in result.get("review") or []]
            try:
                resolved, events = apply_review(
                    result, {}, pending, user, None, info["custom_labels"]
                )
            except ReviewError as error:
                raise ResultsError(str(error), 422) from None
            result["verified"] = {"by": user, "at": time.time()}
            self.finish_edit(result, resolved)
        if first_time:
            # Corrections made before verification become training samples now
            done = {e[1] for e in events}
            for kind, group in (("field", "fields"), ("zone", "zones")):
                for name, item in (result.get(group) or {}).items():
                    if name not in done and original_value(item) != item.get("value"):
                        events.append(
                            (
                                kind,
                                name,
                                original_value(item),
                                item.get("value"),
                                "corrected",
                                dict(item),
                            )
                        )
        records = write_training_records(
            self.ctx.data.training, result, events, self.aligned_for_training(scan_id)
        )
        return result, records

    # ---- regrade -------------------------------------------------------------
    def regrade(
        self,
        scan_id,
        template_overrides=None,
        config_overrides=None,
        apply=False,
        keep_corrections=True,
        use_current_template=False,
        user=None,
    ):
        user = user or DEFAULT_USER
        result = self.load(scan_id)
        scanned, path, info = self.reread(
            result, template_overrides, config_overrides, use_current_template
        )
        new = scanned.to_dict()
        record = json.loads(json.dumps(result))
        for key, value in new.items():
            if key != "file_id":
                record[key] = value
        for key in (
            "read_review",
            "review_log",
            "verified",
            "corrected",
            "manual_values",
        ):
            record.pop(key, None)
        record["reviewed"] = False
        record["template_version"] = info["template_version"]
        kept = []
        if keep_corrections:
            kept = reapply_corrections(result, record)
        record["regrade"] = {
            "at": time.time(),
            "by": user,
            "template_overrides": template_overrides or {},
            "config_overrides": config_overrides or {},
            "template_version": info["template_version"],
            "kept_corrections": kept,
        }
        if use_current_template and result.get("template_id"):
            archive_template_version(
                info["directory"],
                self.ctx.data.template_versions,
                result["template_id"],
            )
        try:
            with self.engines.engine(record) as (engine, _):
                recompute(record, engine)
        except ResultsError:
            pass
        record["corrected"] = is_corrected(record)
        changes = diff_reads(result, record)
        if scanned.aligned_image is not None:
            self.renders.put(
                f"{scan_id}:preview",
                scanned.aligned_image,
                {
                    "image_source": "source",
                    "resolved_path": str(path),
                    "drift": [],
                    "warnings": [],
                },
            )
        if not apply:
            return record, changes, False
        with self.ctx.scan_lock(scan_id):
            history = record.setdefault("history", [])
            history.append(
                {
                    "at": time.time(),
                    "by": user,
                    "status": result.get("status"),
                    "responses": result.get("responses"),
                    "template_version": result.get("template_version"),
                    "regrade": result.get("regrade"),
                }
            )
            del history[:-20]
            scan_dir = self.ctx.data.scan_dir(scan_id)
            if result.get("has_images") and scanned.aligned_image is not None:
                cv2.imwrite(
                    str(scan_dir / "aligned.png"), scanned.aligned_image, PNG_FAST
                )
                if scanned.marked_image is not None:
                    cv2.imwrite(str(scan_dir / "marked.jpg"), scanned.marked_image)
                with self.ctx.image_cache_guard:
                    self.ctx.image_cache.pop(str(scan_dir / "aligned.png"), None)
            self.save(record)
            self.ctx.index.add_scans([{**summarize(record), "read_review": None}])
            self.ctx.index.set_outcomes(scan_id, [])
            self.ctx.index.add_corrections(
                [
                    {
                        "scan_id": scan_id,
                        "name": "*regrade*",
                        "kind": "regrade",
                        "template_id": record.get("template_id"),
                        "job_id": record.get("job_id"),
                        "old": json.dumps(result.get("regrade") or {}),
                        "new": json.dumps(
                            {
                                "template_overrides": template_overrides or {},
                                "config_overrides": config_overrides or {},
                            }
                        ),
                        "original": None,
                        "user": user,
                        "source": "regrade",
                        "at": time.time(),
                    }
                ]
            )
        self.renders.drop(scan_id)
        if scanned.aligned_image is not None:
            self.renders.put(
                scan_id,
                scanned.aligned_image,
                {
                    "image_source": "source",
                    "resolved_path": str(path),
                    "drift": [],
                    "warnings": [],
                },
            )
        return record, changes, True

    # ---- accuracy ------------------------------------------------------------
    def accuracy(self, template_id=None, job_id=None):
        sheets, rows = self.ctx.index.accuracy(template_id=template_id, job_id=job_id)
        totals = {"auto": 0, "auto_ok": 0, "flagged": 0, "flagged_corrected": 0}
        fields = []
        for row in rows:
            for key in totals:
                totals[key] += row[key] or 0
            fields.append(
                {
                    "name": row["name"],
                    "kind": row["kind"],
                    "auto_accepted": row["auto"] or 0,
                    "auto_correct": row["auto_ok"] or 0,
                    "auto_accuracy": _pct(row["auto_ok"], row["auto"]),
                    "flagged": row["flagged"] or 0,
                    "flagged_corrected": row["flagged_corrected"] or 0,
                    "flag_precision": _pct(row["flagged_corrected"], row["flagged"]),
                    "corrected": row["corrected"] or 0,
                    "total": row["total"] or 0,
                }
            )
        # Worst fields first, then in natural name order (q2 before q10)
        fields.sort(
            key=lambda f: (
                f["auto_accuracy"] is None,
                f["auto_accuracy"] or 0,
                [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", f["name"])],
            )
        )
        return {
            "verified_sheets": sheets,
            "auto_accepted_fields": totals["auto"],
            "auto_correct_fields": totals["auto_ok"],
            "auto_accuracy": _pct(totals["auto_ok"], totals["auto"]),
            "flagged_fields": totals["flagged"],
            "flagged_needed_correction": totals["flagged_corrected"],
            "flag_precision": _pct(totals["flagged_corrected"], totals["flagged"]),
            "fields": fields,
            "definition": "Across sheets verified on the Results screen: auto_accuracy "
            "is the % of fields the engine did not flag that needed no correction; "
            "flag_precision is the % of flagged fields that actually needed one.",
        }


def _pct(part, whole):
    if not whole:
        return None
    return round(100.0 * (part or 0) / whole, 2)


def clean_rules(rules):
    cleaned = []
    for rule in rules or []:
        if isinstance(rule, str):
            if "=" not in rule:
                raise ResultsError(f"Remap rule '{rule}' must look like old=new", 422)
            old, new = rule.split("=", 1)
            rule = {"from": old, "to": new}
        old, new = str(rule.get("from", "")).strip(), str(rule.get("to", "")).strip()
        if not old or not new:
            raise ResultsError("Each remap rule needs 'from' and 'to'", 422)
        cleaned.append({"from": old, "to": new})
    return cleaned


def toggled_value(field, current, bubble_value, empty_value=""):
    """Field value after toggling one bubble: marks stay in sheet order, so a
    multi-mark reads exactly as the engine would have concatenated it."""
    values = [b["value"] for b in field.get("bubbles") or []]
    if bubble_value not in values:
        raise ResultsError(f"'{bubble_value}' is not a bubble of this field", 422)
    if current == empty_value and empty_value not in values:
        marks = []
    else:
        marks = split_value(current, values) or []
    marks = set(marks)
    if bubble_value in marks:
        marks.discard(bubble_value)
    else:
        marks.add(bubble_value)
    ordered, seen = [], set()
    for value in values:
        if value in marks and value not in seen:
            ordered.append(value)
            seen.add(value)
    return "".join(ordered) if ordered else empty_value


def drift(result, scanned):
    """Names whose fresh read differs from the value originally read."""
    changed = []
    fresh = {**(scanned.fields or {}), **(scanned.zones or {})}
    stored = {**(result.get("fields") or {}), **(result.get("zones") or {})}
    for name, item in stored.items():
        if name in fresh and fresh[name].get("value") != original_value(item):
            changed.append(name)
    return changed


def reapply_corrections(old, new):
    kept = []
    for group in ("fields", "zones"):
        for name, item in (old.get(group) or {}).items():
            target = (new.get(group) or {}).get(name)
            if target is None or original_value(item) == item.get("value"):
                continue
            if target.get("value") != item.get("value"):
                target["original_value"] = target.get("value")
                target["value"] = item.get("value")
            target["needs_review"] = False
            target["reviewed"] = True
            kept.append(name)
    # Values typed for cross-field checks; decisions on kept items
    checks = new.get("checks") or {}
    for name, value in (old.get("manual_values") or {}).items():
        if name in checks:
            new.setdefault("manual_values", {})[name] = value
            kept.append(name)
    log = old.get("review_log") or {}
    for name in kept:
        if name in log:
            new.setdefault("review_log", {})[name] = log[name]
    if kept:
        new["review"] = [i for i in new.get("review") or [] if i["name"] not in kept]
    return kept


def diff_reads(old, new):
    changes = []
    for group in ("fields", "zones", "checks"):
        before = old.get(group) or {}
        for name, item in (new.get(group) or {}).items():
            previous = before.get(name) or {}
            if previous.get("value") != item.get("value") or sorted(
                previous.get("flags") or []
            ) != sorted(item.get("flags") or []):
                changes.append(
                    {
                        "name": name,
                        "before": previous.get("value"),
                        "after": item.get("value"),
                        "flags_before": previous.get("flags") or [],
                        "flags_after": item.get("flags") or [],
                    }
                )
    return changes


def group_highlights(result):
    """
    {column: [reason, ...]} for bubble columns of grouped values that need a
    look: columns a group reports as multi-marked or unclear (result "groups",
    {group: {"columns": [{"name", "state"}]}}), and the columns listed by a
    pending review item ("fields", or "field_flags": {column: [flags]}).
    Columns a person already decided are left out.
    """
    fields = result.get("fields") or {}
    marks = {}

    def add(column, reason):
        if fields.get(column, {}).get("reviewed"):
            return
        reasons = marks.setdefault(column, [])
        if reason not in reasons:
            reasons.append(reason)

    for group, details in (result.get("groups") or {}).items():
        if not isinstance(details, dict):
            continue
        for column in details.get("columns") or []:
            if isinstance(column, dict) and column.get("state") in ("multi", "issue"):
                add(column.get("name"), f"{group}: {column['state']}")
    for item in result.get("review") or []:
        for column, flags in (item.get("field_flags") or {}).items():
            for flag in flags or ["needs_review"]:
                add(column, f"{item.get('name')}: {flag}")
        if not item.get("field_flags"):
            for column in item.get("fields") or []:
                add(column, f"{item.get('name')}: needs review")
    return marks


def overlay_payload(result, info):
    """Everything the browser needs to draw and edit the overlay."""
    highlights = group_highlights(result)
    flagged = {}
    for name, flag in flag_rows(result):
        flagged.setdefault(name, []).append(flag)
    pending = {item["name"] for item in result.get("review") or []}
    fields = []
    for name, field in (result.get("fields") or {}).items():
        bubbles = field.get("bubbles") or []
        values = [b["value"] for b in bubbles]
        value = field.get("value", "")
        marks = set(split_value(value, values) or [])
        box = None
        if bubbles:
            x0 = min(b["x"] for b in bubbles)
            y0 = min(b["y"] for b in bubbles)
            x1 = max(b["x"] + b["w"] for b in bubbles)
            y1 = max(b["y"] + b["h"] for b in bubbles)
            box = [x0, y0, x1 - x0, y1 - y0]
        fields.append(
            {
                "name": name,
                "kind": "field",
                "value": value,
                "original_value": original_value(field),
                "corrected": original_value(field) != value,
                "confidence": field.get("confidence"),
                "flags": field.get("flags") or [],
                "flagged": name in flagged,
                "pending": name in pending,
                "group_flags": highlights.get(name, []),
                "box": box,
                "bubbles": [
                    {
                        "value": b["value"],
                        "x": b["x"],
                        "y": b["y"],
                        "w": b["w"],
                        "h": b["h"],
                        "cx": b["x"] + b["w"] / 2,
                        "cy": b["y"] + b["h"] / 2,
                        "marked": b["value"] in marks,
                        "read_marked": bool(b.get("marked")),
                        "fill_ratio": b.get("fill_ratio"),
                        "confidence": b.get("confidence"),
                    }
                    for b in bubbles
                ],
            }
        )
    zones = []
    for name, zone in (result.get("zones") or {}).items():
        zones.append(
            {
                "name": name,
                "kind": "zone",
                "type": zone.get("type"),
                "value": zone.get("value", ""),
                "original_value": original_value(zone),
                "corrected": original_value(zone) != zone.get("value", ""),
                "confidence": zone.get("confidence"),
                "flags": zone.get("flags") or [],
                "flagged": name in flagged,
                "pending": name in pending,
                "box": zone.get("box") or None,
                "format": zone.get("format"),
            }
        )
    items = {item["name"]: item for item in fields + zones}
    responses = result.get("responses") or {}
    checks = {
        name: check
        for name, check in (result.get("checks") or {}).items()
        if isinstance(check, dict)
    }
    check_of = {check.get("output") or name: name for name, check in checks.items()}
    validation = result.get("validation") or {}
    outputs, seen = [], set()
    columns = list(info.get("output_columns") or []) + [z["name"] for z in zones]
    for name in columns + sorted(responses):
        if name in seen:
            continue
        seen.add(name)
        parts = info.get("custom_labels", {}).get(name)
        members = [items[p] for p in parts or [name] if p in items]
        if not members and name not in responses:
            continue
        confidences = [m["confidence"] for m in members if m["confidence"] is not None]
        check_name = check_of.get(name)
        check = checks.get(check_name) or {}
        if check_name:
            kind = "check"
        elif parts:
            kind = "custom_label"
        else:
            kind = members[0]["kind"] if members else "output"
        value = responses.get(name, members[0]["value"] if members else "")
        flags = {f for m in members for f in m["flags"]} | set(check.get("flags") or [])
        reasons = (validation.get(name) or {}).get("reasons") or check.get(
            "validation_reasons"
        )
        if kind == "check":
            original = check.get("original_value", value)
        elif parts:
            original = (
                "".join(str(m["original_value"]) for m in members)
                if len(members) == len(parts)
                else value
            )
        else:
            original = members[0]["original_value"] if members else value
        outputs.append(
            {
                "name": name,
                "kind": kind,
                "check": check_name,
                "value": value,
                "original_value": original,
                "parts": parts,
                "flagged": any(m["flagged"] for m in members)
                or name in flagged
                or check_name in flagged
                or bool(check.get("flags")),
                "pending": any(m["pending"] for m in members)
                or name in pending
                or check_name in pending,
                "corrected": any(m["corrected"] for m in members)
                or bool(check.get("manual")),
                "editable": kind in ("check", "custom_label") or bool(members),
                "confidence": min(confidences) if confidences else None,
                "flags": sorted(flags),
                "reasons": reasons or [],
            }
        )
    return {
        "scan_id": result["scan_id"],
        "file_id": result.get("file_id"),
        "file_name": result.get("file_name"),
        "page": result.get("page", 0),
        "job_id": result.get("job_id"),
        "template_id": result.get("template_id"),
        "template_version": result.get("template_version"),
        "source_path": result.get("source_path") or result.get("input_path"),
        "status": result.get("status"),
        "score": result.get("score"),
        "error": result.get("error"),
        "verified": result.get("verified"),
        "corrected": is_corrected(result),
        "regrade": result.get("regrade"),
        "fields": fields,
        "zones": zones,
        "outputs": outputs,
        "checks": checks,
        "validation": validation,
        "pending": sorted(pending),
        "sheet_review": [
            item for item in result.get("review") or [] if item.get("kind") == "sheet"
        ],
        "audit": (result.get("audit") or [])[-50:],
        # Recorded page/block geometry (block border outlines, other views)
        "geometry_recorded": bool(result.get("geometry")),
        "blocks": (result.get("geometry") or {}).get("blocks") or {},
        "groups": result.get("groups") or {},
    }
