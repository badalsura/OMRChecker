"""
Scanning work shared by the synchronous endpoint (threads) and bulk jobs
(worker processes).

Each worker process keeps one OMREngine per template version, created lazily
and reused, and writes its results straight to disk so only small summaries
travel back to the parent process.
"""

import hashlib
import logging
import os
import shutil
import threading
import time
from pathlib import Path

import cv2

from src.api.storage import new_id, scan_dir_for, write_json_atomic

# Image persistence policies
SAVE_ALL = "all"
SAVE_REVIEW = "review"  # only sheets that need review (the reviewer needs crops)
SAVE_NONE = "none"

PNG_FAST = [cv2.IMWRITE_PNG_COMPRESSION, 1]
JPEG_MARKED = [cv2.IMWRITE_JPEG_QUALITY, 80]

# {(template_dir, version): OMREngine} inside each worker process
_ENGINES = {}


def worker_init():
    # Processes, not OpenCV threads, provide the parallelism
    cv2.setNumThreads(1)
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("OMR_ONNX_THREADS", "1")
    # Per-sheet INFO logs cost real time at thousands of sheets per minute
    logging.getLogger("src.logger").setLevel(logging.WARNING)


def template_version(template_dir):
    """Changes whenever a file that affects reading changes."""
    template_dir = Path(template_dir)
    stamps = []
    for name in ("template.json", "config.json", "evaluation.json"):
        path = template_dir / name
        stamps.append(path.stat().st_mtime_ns if path.exists() else 0)
    return tuple(stamps)


VERSIONED_FILES = ("template.json", "config.json", "evaluation.json")
_HASHES = {}
_HASHES_LOCK = threading.Lock()


def template_hash(template_dir):
    """
    Content hash of the files that affect reading, recorded with every scan so
    a result can later be re-rendered with exactly the template it was read with.
    Cached by modification times, so calling it per request is cheap.
    """
    template_dir = Path(template_dir)
    key = (str(template_dir), template_version(template_dir))
    with _HASHES_LOCK:
        if key in _HASHES:
            return _HASHES[key]
    digest = hashlib.sha1()
    for name in VERSIONED_FILES:
        path = template_dir / name
        digest.update(name.encode() + b"\0")
        if path.exists():
            digest.update(path.read_bytes())
        digest.update(b"\0")
    value = digest.hexdigest()[:16]
    with _HASHES_LOCK:
        if len(_HASHES) > 1000:
            _HASHES.clear()
        _HASHES[key] = value
    return value


def archive_template_version(template_dir, versions_root, template_id):
    """
    Keep a copy of the template folder per content hash (small JSON files and
    marker images) under <versions_root>/<template_id>/<hash>/, so editing a
    template never changes how older results re-render. Returns the hash.
    """
    template_dir = Path(template_dir)
    version = template_hash(template_dir)
    target = Path(versions_root) / template_id / version
    if (target / "template.json").exists():
        return version
    staging = target.with_name(f".{version}.{os.getpid()}.{threading.get_ident()}")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    for path in template_dir.rglob("*"):
        relative = path.relative_to(template_dir)
        if path.is_dir() or relative.parts[0].startswith((".", "_")):
            continue
        destination = staging / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
    try:
        os.replace(staging, target)
    except OSError:  # another thread archived it first
        shutil.rmtree(staging, ignore_errors=True)
    return version


def merged_template_overrides(template_path, overrides):
    """
    Regrade overrides are deep-merged into template.json (so
    {"fieldBlocks": {"MCQ": {"bubbleValues": [...]}}} changes one block);
    OMREngine's template_overrides replaces whole top-level keys, so hand it
    the merged top-level values. None still removes a key.
    """
    if not overrides:
        return None
    import json
    from copy import deepcopy

    from src.utils.parsing import OVERRIDE_MERGER

    with open(template_path, encoding="utf-8") as handle:
        original = json.load(handle)
    merged = {}
    for key, value in overrides.items():
        current = original.get(key)
        if isinstance(value, dict) and isinstance(current, dict):
            merged[key] = OVERRIDE_MERGER.merge(deepcopy(current), deepcopy(value))
        else:
            merged[key] = deepcopy(value)
    return merged


def build_engine(template_dir, template_overrides=None, config_overrides=None):
    from src.pipeline import OMREngine

    template_path = Path(template_dir) / "template.json"
    return OMREngine(
        template_path,
        config_overrides=config_overrides,
        template_overrides=merged_template_overrides(template_path, template_overrides),
    )


def get_process_engine(template_dir, version):
    key = (str(template_dir), tuple(version))
    engine = _ENGINES.get(key)
    if engine is None:
        # Drop stale versions of the same template
        for old in [k for k in _ENGINES if k[0] == key[0]]:
            del _ENGINES[old]
        engine = build_engine(template_dir)
        _ENGINES[key] = engine
    return engine


def scan_and_store(engine, file_path, meta, scans_root, save_images, copy_input):
    """
    Read every page of file_path and persist one scan per page.

    meta: template_id, job_id, seq, file_name. Returns the stored result dicts.
    """
    file_path = Path(file_path)
    started = time.time()
    try:
        results = engine.scan_path(file_path, keep_images=save_images != SAVE_NONE)
    except Exception as error:  # pragma: no cover - scan_path already guards pages
        from src.pipeline import STATUS_ERROR, ScanResult

        results = [ScanResult(file_path.name, STATUS_ERROR, error=str(error))]
    stored = []
    for page, result in enumerate(results):
        scan_id = new_id()
        scan_dir = scan_dir_for(scans_root, scan_id)
        scan_dir.mkdir(parents=True, exist_ok=True)
        data = result.to_dict()
        keep = result.aligned_image is not None and (
            save_images == SAVE_ALL
            or (save_images == SAVE_REVIEW and result.status != "ok")
        )
        if keep:
            cv2.imwrite(str(scan_dir / "aligned.png"), result.aligned_image, PNG_FAST)
            if result.marked_image is not None:
                cv2.imwrite(
                    str(scan_dir / "marked.jpg"), result.marked_image, JPEG_MARKED
                )
        input_path = str(file_path)
        source_path = str(file_path.resolve())
        if copy_input:
            target = scan_dir / f"input{file_path.suffix.lower()}"
            _link_or_copy(file_path, target)
            input_path = source_path = str(target.resolve())
        data.update(
            {
                "scan_id": scan_id,
                "template_id": meta.get("template_id"),
                "job_id": meta.get("job_id"),
                "seq": meta.get("seq", 0),
                "page": page,
                "file_name": meta.get("file_name") or file_path.name,
                "input_path": input_path,
                # Absolute path of the original file; re-rendering reads it again
                "source_path": source_path,
                "template_version": meta.get("template_version"),
                "has_images": keep,
                "reviewed": False,
                "review_log": {},
                "created_at": started,
            }
        )
        if len(results) > 1:
            data["file_id"] = f"{data['file_name']}#page{page + 1}"
        else:
            data["file_id"] = data["file_name"]
        write_json_atomic(scan_dir / "result.json", data)
        stored.append(data)
    return stored


def _link_or_copy(source, target):
    try:
        os.link(source, target)
    except OSError:
        shutil.copyfile(source, target)


def job_task(task):
    """Process-pool entry point; returns compact summaries for the index."""
    engine = get_process_engine(task["template_dir"], task["version"])
    stored = scan_and_store(
        engine,
        task["file_path"],
        task,
        task["scans_root"],
        task["save_images"],
        copy_input=False,
    )
    return [summarize(record) for record in stored]


def summarize(record):
    keys = (
        "scan_id",
        "template_id",
        "job_id",
        "seq",
        "file_id",
        "status",
        "score",
        "review",
        "reviewed",
        "has_images",
        "error",
        "created_at",
        "template_version",
    )
    summary = {key: record.get(key) for key in keys}
    # Only the names and flags of failed checks travel to the index
    check_flags = {
        name: check.get("flags")
        for name, check in (record.get("checks") or {}).items()
        if isinstance(check, dict) and check.get("flags")
    }
    if check_flags:
        summary["check_flags"] = check_flags
    return summary
