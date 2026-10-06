"""Template folders: storage, validation, layout drawing and an in-process engine pool."""

import io
import json
import shutil
import threading
import time
import zipfile
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

import cv2
import numpy as np

from src.api.storage import (
    new_id,
    read_json,
    safe_filename,
    slugify,
    write_json_atomic,
)
from src.api.worker import build_engine, template_version

META_FILE = "_meta.json"
REFERENCE_NAMES = ("reference.png", "reference.jpg", "reference.jpeg")
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
ASSET_SUFFIXES = IMAGE_SUFFIXES | {".json", ".onnx"}


class TemplateError(Exception):
    def __init__(self, message, errors=None):
        super().__init__(message)
        self.errors = errors or []


def schema_errors(template_json):
    """Readable JSON-schema errors (with the same defaults the reader applies)."""
    from src.defaults import TEMPLATE_DEFAULTS
    from src.schemas import SCHEMA_VALIDATORS
    from src.utils.parsing import OVERRIDE_MERGER

    if not isinstance(template_json, dict):
        return [{"path": "$root", "message": "template.json must be a JSON object"}]
    merged = OVERRIDE_MERGER.merge(deepcopy(TEMPLATE_DEFAULTS), deepcopy(template_json))
    errors = []
    for error in sorted(
        SCHEMA_VALIDATORS["template"].iter_errors(merged), key=lambda e: list(e.path)
    ):
        path = ".".join(str(p) for p in error.path) or "$root"
        errors.append({"path": path, "message": error.message})
    return errors


def validate_template_dir(template_dir):
    """Return a list of {path, message} errors; empty when the folder is usable."""
    template_dir = Path(template_dir)
    template_path = template_dir / "template.json"
    if not template_path.exists():
        return [{"path": "template.json", "message": "template.json is missing"}]
    try:
        template_json = json.loads(template_path.read_text())
    except json.JSONDecodeError as error:
        return [{"path": "template.json", "message": f"Invalid JSON: {error}"}]
    errors = schema_errors(template_json)
    if errors:
        return errors
    for name in ("config.json", "evaluation.json"):
        path = template_dir / name
        if path.exists():
            try:
                json.loads(path.read_text())
            except json.JSONDecodeError as error:
                return [{"path": name, "message": f"Invalid JSON: {error}"}]
    try:
        # Building an engine runs every semantic check (overlaps, overflow,
        # missing assets, config and evaluation validity)
        build_engine(template_dir)
    except SystemExit as error:  # some legacy checks exit
        return [{"path": "$root", "message": f"Template rejected: {error}"}]
    except Exception as error:
        return [{"path": "$root", "message": str(error)}]
    return []


class TemplateStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

    def path(self, template_id):
        safe = safe_filename(template_id, "")
        if not safe or safe != template_id:
            raise KeyError(template_id)
        return self.root / template_id

    def exists(self, template_id):
        try:
            return (self.path(template_id) / "template.json").exists()
        except KeyError:
            return False

    def meta(self, template_id):
        return read_json(self.path(template_id) / META_FILE, {}) or {}

    def write_meta(self, template_id, meta):
        write_json_atomic(self.path(template_id) / META_FILE, meta)

    def template_json(self, template_id):
        return read_json(self.path(template_id) / "template.json")

    def summary(self, template_id):
        directory = self.path(template_id)
        meta = self.meta(template_id)
        template = read_json(directory / "template.json", {}) or {}
        return {
            "id": template_id,
            "name": meta.get("name", template_id),
            "status": meta.get("status", "ready"),
            "created_at": meta.get("created_at"),
            "updated_at": meta.get("updated_at"),
            "field_blocks": len(template.get("fieldBlocks", {}) or {}),
            "zones": len(template.get("zones", {}) or {}),
            "page_dimensions": template.get("pageDimensions"),
            "has_evaluation": (directory / "evaluation.json").exists(),
            "has_config": (directory / "config.json").exists(),
            "has_reference": self.reference_path(template_id) is not None,
        }

    def list(self):
        items = []
        for directory in sorted(self.root.iterdir()):
            if directory.is_dir() and (directory / "template.json").exists():
                items.append(self.summary(directory.name))
        items.sort(key=lambda item: item.get("updated_at") or 0, reverse=True)
        return items

    def files(self, template_id):
        directory = self.path(template_id)
        return sorted(
            str(p.relative_to(directory))
            for p in directory.rglob("*")
            if p.is_file() and p.name != META_FILE and not p.name.startswith(".")
        )

    def allocate_id(self, name):
        base = slugify(name)
        with self.lock:
            candidate = base
            while (self.root / candidate).exists():
                candidate = f"{base}-{new_id()[:6]}"
            (self.root / candidate).mkdir(parents=True)
        return candidate

    # ---- create / update -------------------------------------------------
    def create_from_files(self, name, files, validate=True, status="ready", extra=None):
        """
        files: list of (filename, bytes). A single .zip is extracted.
        Raises TemplateError with readable errors when the folder is invalid.
        """
        staged = {}
        for filename, content in files:
            if filename.lower().endswith(".zip"):
                staged.update(_extract_zip(content))
            else:
                staged[safe_filename(filename)] = content
        if "template.json" not in staged:
            raise TemplateError(
                "template.json is required",
                [{"path": "template.json", "message": "template.json is missing"}],
            )
        try:
            json.loads(staged["template.json"])
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise TemplateError(
                "template.json is not valid JSON",
                [{"path": "template.json", "message": str(error)}],
            )
        name = name or (extra or {}).get("name") or "template"
        template_id = self.allocate_id(name)
        directory = self.root / template_id
        try:
            for relative, content in staged.items():
                target = (directory / relative).resolve()
                if directory.resolve() not in target.parents:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            errors = validate_template_dir(directory) if validate else []
            if errors and status != "draft":
                raise TemplateError("Template is invalid", errors)
            now = time.time()
            meta = {
                "name": name,
                "status": status,
                "created_at": now,
                "updated_at": now,
                **(extra or {}),
            }
            if errors:
                meta["validation_errors"] = errors
            self.write_meta(template_id, meta)
            return template_id, errors
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise

    def update_template(self, template_id, template_json, status=None):
        directory = self.path(template_id)
        errors = schema_errors(template_json)
        if errors:
            raise TemplateError("Template is invalid", errors)
        template_path = directory / "template.json"
        backup = directory / ".template.json.bak"
        previous = template_path.read_bytes() if template_path.exists() else None
        write_json_atomic(template_path, template_json)
        errors = validate_template_dir(directory)
        if errors:
            if previous is not None:
                template_path.write_bytes(previous)
            raise TemplateError("Template is invalid", errors)
        if previous is not None:
            backup.write_bytes(previous)
        meta = self.meta(template_id)
        meta["updated_at"] = time.time()
        meta.pop("validation_errors", None)
        if status:
            meta["status"] = status
        elif meta.get("status") == "draft":
            meta["status"] = "ready"
        self.write_meta(template_id, meta)

    def delete(self, template_id):
        shutil.rmtree(self.path(template_id))

    def reference_path(self, template_id):
        directory = self.path(template_id)
        meta = self.meta(template_id)
        candidates = []
        if meta.get("reference"):
            candidates.append(meta["reference"])
        template = read_json(directory / "template.json", {}) or {}
        for processor in template.get("preProcessors", []) or []:
            reference = (processor.get("options") or {}).get("reference")
            if isinstance(reference, str):
                candidates.append(reference)
        candidates.extend(REFERENCE_NAMES)
        for candidate in candidates:
            path = (directory / candidate).resolve()
            if directory.resolve() in path.parents and path.exists():
                return path
        return None


def _extract_zip(content, max_files=500, max_total=500 * 1024 * 1024):
    staged = {}
    total = 0
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = [n for n in archive.namelist() if not n.endswith("/")]
        # Find the folder that holds template.json and make paths relative to it
        roots = [
            n[: -len("template.json")] for n in names if n.endswith("template.json")
        ]
        prefix = min(roots, key=len) if roots else ""
        for name in names[:max_files]:
            if not name.startswith(prefix) or "__MACOSX" in name:
                continue
            relative = name[len(prefix) :]
            parts = [
                safe_filename(p)
                for p in Path(relative).parts
                if p not in ("", ".", "..")
            ]
            if not parts or parts[-1].startswith("."):
                continue
            info = archive.getinfo(name)
            total += info.file_size
            if total > max_total:
                raise TemplateError("Zip archive is too large")
            staged["/".join(parts)] = archive.read(name)
    return staged


# ---- layout drawing ------------------------------------------------------
def template_geometry(template_json):
    """Bubble and zone rectangles in template coordinates, computed like the reader."""
    from src.constants.common import FIELD_TYPES
    from src.utils.parsing import parse_fields

    bubble_dims = template_json.get("bubbleDimensions", [10, 10])
    blocks = []
    for name, block in (template_json.get("fieldBlocks") or {}).items():
        block = {**FIELD_TYPES.get(block.get("fieldType"), {}), **block}
        dims = block.get("bubbleDimensions", bubble_dims)
        values = block.get("bubbleValues", [])
        try:
            labels = parse_fields(name, block.get("fieldLabels", []))
        except Exception:
            labels = []
        horizontal = block.get("direction", "vertical") == "horizontal"
        ox, oy = block.get("origin", [0, 0])
        bubbles = []
        for f_index, label in enumerate(labels):
            for v_index, value in enumerate(values):
                if horizontal:
                    x = ox + v_index * block.get("bubblesGap", 0)
                    y = oy + f_index * block.get("labelsGap", 0)
                else:
                    x = ox + f_index * block.get("labelsGap", 0)
                    y = oy + v_index * block.get("bubblesGap", 0)
                bubbles.append((round(x), round(y), dims[0], dims[1], label, value))
        blocks.append({"name": name, "bubbles": bubbles})
    zones = [
        {
            "name": name,
            "type": zone.get("type"),
            "box": [*zone["origin"], *zone["dimensions"]],
        }
        for name, zone in (template_json.get("zones") or {}).items()
        if "origin" in zone and "dimensions" in zone
    ]
    return blocks, zones


ZONE_COLORS = {
    "barcode": (200, 90, 20),
    "qrcode": (160, 40, 160),
    "ocr": (30, 140, 30),
    "icr": (20, 120, 220),
}


def draw_layout(template_json, background=None, highlight=()):
    width, height = template_json.get("pageDimensions", [1000, 1400])
    if background is None:
        canvas = np.full((height, width, 3), 255, np.uint8)
    else:
        if background.ndim == 2:
            background = cv2.cvtColor(background, cv2.COLOR_GRAY2BGR)
        canvas = cv2.resize(background, (width, height))
    overlay = canvas.copy()
    blocks, zones = template_geometry(template_json)
    for block in blocks:
        if not block["bubbles"]:
            continue
        color = (0, 0, 255) if block["name"] in highlight else (40, 40, 220)
        xs = [b[0] for b in block["bubbles"]] + [b[0] + b[2] for b in block["bubbles"]]
        ys = [b[1] for b in block["bubbles"]] + [b[1] + b[3] for b in block["bubbles"]]
        cv2.rectangle(
            overlay, (min(xs) - 4, min(ys) - 4), (max(xs) + 4, max(ys) + 4), color, 2
        )
        for x, y, w, h, _, _ in block["bubbles"]:
            cv2.rectangle(overlay, (x, y), (x + w, y + h), (60, 160, 60), 1)
        cv2.putText(
            overlay,
            block["name"],
            (min(xs), max(min(ys) - 10, 12)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            color,
            1,
            cv2.LINE_AA,
        )
    for zone in zones:
        x, y, w, h = zone["box"]
        color = ZONE_COLORS.get(zone["type"], (0, 0, 0))
        if zone["name"] in highlight:
            color = (0, 0, 255)
        cv2.rectangle(overlay, (x, y), (x + w, y + h), color, 2)
        cv2.putText(
            overlay,
            f"{zone['name']} ({zone['type']})",
            (x, max(y - 6, 12)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            color,
            1,
            cv2.LINE_AA,
        )
    return cv2.addWeighted(overlay, 0.85, canvas, 0.15, 0)


# ---- in-process engine pool ---------------------------------------------
class EnginePool:
    """Engines are not thread-safe: hand each request its own, reuse when idle."""

    def __init__(self, store, max_idle_per_template=8):
        self.store = store
        self.max_idle = max_idle_per_template
        self.idle = {}
        self.lock = threading.Lock()

    @contextmanager
    def engine(self, template_id):
        directory = self.store.path(template_id)
        key = (template_id, template_version(directory))
        with self.lock:
            stack = self.idle.get(key)
            engine = stack.pop() if stack else None
        if engine is None:
            engine = build_engine(directory)
        try:
            yield engine
        finally:
            with self.lock:
                # Forget engines of older template versions
                for old in [k for k in self.idle if k[0] == template_id and k != key]:
                    del self.idle[old]
                stack = self.idle.setdefault(key, [])
                if len(stack) < self.max_idle:
                    stack.append(engine)

    def invalidate(self, template_id):
        with self.lock:
            for key in [k for k in self.idle if k[0] == template_id]:
                del self.idle[key]
