"""
Scanning work shared by the synchronous endpoint (threads) and bulk jobs
(worker processes).

Each worker process keeps one OMREngine per template version, created lazily
and reused, and writes its results straight to disk so only small summaries
travel back to the parent process.
"""

import os
import shutil
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


def template_version(template_dir):
    """Changes whenever a file that affects reading changes."""
    template_dir = Path(template_dir)
    stamps = []
    for name in ("template.json", "config.json", "evaluation.json"):
        path = template_dir / name
        stamps.append(path.stat().st_mtime_ns if path.exists() else 0)
    return tuple(stamps)


def build_engine(template_dir):
    from src.pipeline import OMREngine

    return OMREngine(Path(template_dir) / "template.json")


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
        if copy_input:
            target = scan_dir / f"input{file_path.suffix.lower()}"
            _link_or_copy(file_path, target)
            input_path = str(target)
        data.update(
            {
                "scan_id": scan_id,
                "template_id": meta.get("template_id"),
                "job_id": meta.get("job_id"),
                "seq": meta.get("seq", 0),
                "page": page,
                "file_name": meta.get("file_name") or file_path.name,
                "input_path": input_path,
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
    )
    return {key: record.get(key) for key in keys}
