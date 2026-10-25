"""
High-throughput batch reading.

Images are decoded and read inside worker processes (one OMREngine per process
and template, created lazily and reused), so throughput scales with CPU cores
and the parent process only moves file paths and small result dicts.

    for result in scan_files(paths, "forms/exam/template.json", workers=8):
        ...

A sheet that fails, crashes its reader process or hangs (sheet_timeout) is
returned as an error result and the run carries on; --resume continues an
interrupted run and reads its error sheets again.
"""

import logging
import os
from collections import deque
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

import cv2

from src.readers.image_zone import save_zone_images

# Seconds a sheet may take before it is skipped (0 = no limit)
DEFAULT_SHEET_TIMEOUT = 300
# The run stops after this many reader pool restarts
MAX_POOL_RESTARTS = 20

# Each worker process keeps its own engines: {(template, config, eval, models): OMREngine}
_ENGINES = {}


def _worker_init(log_level=logging.WARNING):
    from src.utils.cpu import prepare_worker_environment

    prepare_worker_environment()
    # Let processes, not OpenCV threads, provide the parallelism
    cv2.setNumThreads(1)
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("OMR_ONNX_THREADS", "1")
    # Per-sheet INFO logging through rich costs more than reading the sheet
    logging.getLogger("src.logger").setLevel(log_level)


def get_engine(engine_args):
    from src.pipeline import OMREngine

    engine = _ENGINES.get(engine_args)
    if engine is None:
        (
            template_path,
            config_path,
            evaluation_path,
            bubble_model,
            icr_model,
        ) = engine_args
        engine = OMREngine(
            template_path,
            config_path=config_path,
            evaluation_path=evaluation_path,
            bubble_model_path=bubble_model,
            icr_model_path=icr_model,
        )
        _ENGINES[engine_args] = engine
    return engine


def error_payload(file_path, message):
    """The result of a file that could not be read."""
    from src.pipeline import STATUS_ERROR, ScanResult

    data = ScanResult(Path(file_path).name, STATUS_ERROR, error=message).to_dict()
    data["input_path"] = str(file_path)
    data["source_path"] = str(Path(file_path).resolve())
    return [data]


def _scan_one(task):
    try:
        return _read_one(task)
    except Exception as error:
        return error_payload(task[1], str(error))


def _read_one(task):
    engine_args, file_path, output_dir = task
    engine = get_engine(engine_args)
    results = engine.scan_path(file_path, keep_images=output_dir is not None)
    payload = []
    for result in results:
        if output_dir is not None and result.marked_image is not None:
            stem = Path(result.file_id).stem
            cv2.imwrite(
                str(Path(output_dir, f"{stem}_marked.jpg")), result.marked_image
            )
            cv2.imwrite(
                str(Path(output_dir, f"{stem}_aligned.png")), result.aligned_image
            )
        if output_dir is not None:
            save_zone_images(result.zone_images, Path(output_dir, "ZoneImages"))
        data = result.to_dict()
        data["input_path"] = str(file_path)
        data["source_path"] = str(Path(file_path).resolve())
        payload.append(data)
    return payload


def scan_files(
    file_paths,
    template_path,
    config_path=None,
    evaluation_path=None,
    bubble_model_path=None,
    icr_model_path=None,
    workers=None,
    image_output_dir=None,
    chunksize=4,
    sheet_timeout=DEFAULT_SHEET_TIMEOUT,
):
    """Yield result dicts (one per page) in input order. chunksize is kept
    for callers; files are handed to the workers one at a time."""
    engine_args = tuple(
        str(p) if p else None
        for p in (
            template_path,
            config_path,
            evaluation_path,
            bubble_model_path,
            icr_model_path,
        )
    )
    if image_output_dir is not None:
        Path(image_output_dir).mkdir(parents=True, exist_ok=True)
    tasks = [(engine_args, str(path), image_output_dir) for path in file_paths]
    from src.utils.cpu import default_workers, prepare_worker_environment

    workers = workers or default_workers()
    if workers == 1:
        _worker_init()
        for task in tasks:
            yield from _scan_one(task)
        return
    prepare_worker_environment()
    yield from _scan_parallel(tasks, workers, sheet_timeout)


def _kill_pool(pool):
    for process in list((getattr(pool, "_processes", None) or {}).values()):
        try:
            process.terminate()
        except Exception:
            pass
    try:
        pool.shutdown(wait=False)
    except Exception:
        pass


def _scan_parallel(tasks, workers, sheet_timeout, read=_scan_one, mp_context=None):
    """Read tasks on a process pool, results in input order. When a reader
    crashes or a sheet hangs, the pool is restarted and the sheets in flight
    are read again one at a time; the one that fails alone becomes an error."""
    pending = deque(tasks)
    suspects = deque()
    window = deque()  # (task, future), oldest first
    depth = workers * 4
    restarts = 0

    def new_pool():
        return ProcessPoolExecutor(
            max_workers=workers, mp_context=mp_context, initializer=_worker_init
        )

    pool = new_pool()
    isolated = False
    try:
        while pending or suspects or window:
            try:
                if isolated and window:
                    pass  # waiting on the one sheet read alone
                elif suspects:
                    task = suspects.popleft()
                    try:
                        window.append((task, pool.submit(read, task)))
                    except BrokenProcessPool:
                        suspects.appendleft(task)
                        raise
                    isolated = True
                else:
                    isolated = False
                    while pending and len(window) < depth:
                        task = pending.popleft()
                        try:
                            window.append((task, pool.submit(read, task)))
                        except BrokenProcessPool:
                            pending.appendleft(task)
                            raise
                task, future = window[0]
                payload = future.result(timeout=sheet_timeout or None)
                window.popleft()
                yield from payload
            except (BrokenProcessPool, FuturesTimeout) as error:
                hung = isinstance(error, FuturesTimeout)
                lost = [task for task, _ in window]
                window.clear()
                _kill_pool(pool)
                if isolated and len(lost) == 1:
                    why = f"took longer than {sheet_timeout:g} s" if hung else "crashed the reader"
                    yield from error_payload(
                        lost[0][1], f"Reading this sheet {why}; it was skipped"
                    )
                else:
                    suspects.extend(lost)
                restarts += 1
                if restarts > MAX_POOL_RESTARTS:
                    raise RuntimeError(
                        "The reader processes kept crashing; run again with --resume"
                    )
                pool = new_pool()
    except BaseException:
        # Stopped early (error, Ctrl+C, generator closed): don't wait for the readers
        _kill_pool(pool)
        raise
    pool.shutdown(wait=True)


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".pdf"}


def collect_files(inputs):
    files = []
    for item in inputs:
        path = Path(item)
        if path.is_dir():
            files.extend(
                sorted(p for p in path.rglob("*") if p.suffix.lower() in IMAGE_SUFFIXES)
            )
        elif path.suffix.lower() in IMAGE_SUFFIXES:
            files.append(path)
    return files


def read_previous(jsonl_path):
    """Results of an earlier run worth keeping: every page of each file that
    has no error page. A line cut short by a crash is dropped."""
    import json

    records = []
    with open(jsonl_path) as handle:
        for line in handle:
            try:
                records.append(json.loads(line))
            except ValueError:
                pass
    for record in records:
        record.setdefault("source_path", str(Path(record.get("input_path", "")).resolve()))
    failed = {r["source_path"] for r in records if r.get("status") == "error"}
    return [r for r in records if r["source_path"] not in failed]


def main(argv=None):
    import argparse
    import csv
    import json
    import time

    parser = argparse.ArgumentParser(
        description="Read many OMR sheets in parallel and write CSV + JSONL results"
    )
    parser.add_argument("--template", required=True, help="Path to template.json")
    parser.add_argument("--input", required=True, nargs="+", help="Files or folders")
    parser.add_argument("--out", required=True, help="Output folder")
    parser.add_argument("--config", help="config.json (default: next to template)")
    parser.add_argument("--evaluation", help="evaluation.json for scoring")
    parser.add_argument("--bubble-model", help="ONNX bubble classifier")
    parser.add_argument("--icr-model", help="ONNX ICR character classifier")
    parser.add_argument("--workers", type=int, default=None, help="Default: all cores")
    parser.add_argument(
        "--save-images", action="store_true", help="Also save aligned/marked images"
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Continue a stopped run in --out: keep the sheets already read and "
        "read the rest, including the ones that ended in an error",
    )
    parser.add_argument(
        "--sheet-timeout",
        type=float,
        default=DEFAULT_SHEET_TIMEOUT,
        help=f"Skip a sheet still being read after this many seconds (default "
        f"{DEFAULT_SHEET_TIMEOUT}; 0 = no limit; needs 2+ workers)",
    )
    args = parser.parse_args(argv)

    # Licensed before any reading starts (bulk is a major operation).
    from src.licensing import gate
    from src.licensing.verify import LicenseError

    try:
        gate.require_license()
    except LicenseError as error:
        print(f"OMRChecker is not licensed on this machine: {error}", flush=True)
        print("Activate in the app, or see docs/licensing.md.", flush=True)
        return 3

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    files = collect_files(args.input)
    jsonl_path = out_dir / "results.jsonl"
    # The earlier run's results while they are copied back (kept until then,
    # so a crash during --resume loses nothing)
    previous = out_dir / "results.jsonl.prev"
    kept = []
    if args.resume and (previous.exists() or jsonl_path.exists()):
        if not previous.exists():
            os.replace(jsonl_path, previous)
        kept = read_previous(previous)
        done = {record.get("source_path") for record in kept}
        files = [f for f in files if str(Path(f).resolve()) not in done]
        print(
            f"Resuming: {len(done)} files already read, {len(files)} to read",
            flush=True,
        )
    started = time.perf_counter()
    counts = {}
    columns = None
    with open(jsonl_path, "w") as jsonl, open(
        out_dir / "results.csv", "w", newline=""
    ) as csv_file:
        writer = csv.writer(csv_file, quoting=csv.QUOTE_NONNUMERIC)

        def write(result):
            nonlocal columns
            jsonl.write(json.dumps(result) + "\n")
            if columns is None and result["responses"]:
                columns = list(result["responses"].keys())
                writer.writerow(
                    ["file_id", "input_path", "status", "score", "needs_review"]
                    + columns
                )
            if columns is not None:
                writer.writerow(
                    [
                        result["file_id"],
                        result["input_path"],
                        result["status"],
                        result["score"],
                        "|".join(item["name"] for item in result["review"]),
                    ]
                    + [result["responses"].get(c, "") for c in columns]
                )

        for result in kept:
            write(result)
        jsonl.flush()
        csv_file.flush()
        if kept or previous.exists():
            os.fsync(jsonl.fileno())
            previous.unlink()
        for index, result in enumerate(
            scan_files(
                files,
                args.template,
                args.config,
                args.evaluation,
                args.bubble_model,
                args.icr_model,
                workers=args.workers,
                image_output_dir=str(out_dir / "images") if args.save_images else None,
                sheet_timeout=args.sheet_timeout,
            ),
            1,
        ):
            write(result)
            # On disk at once, so a crash loses nothing and --resume can continue
            jsonl.flush()
            csv_file.flush()
            counts[result["status"]] = counts.get(result["status"], 0) + 1
            if index % 500 == 0:
                rate = index / (time.perf_counter() - started)
                print(f"{index} sheets, {rate:.1f} sheets/s, {counts}", flush=True)
    elapsed = time.perf_counter() - started
    total = sum(counts.values())
    print(
        f"Done: {total} sheets in {elapsed:.1f}s ({total / max(elapsed, 1e-9):.1f} sheets/s). {counts}"
    )
    # Sheets that failed before reading (no responses) still appear in results.jsonl
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
