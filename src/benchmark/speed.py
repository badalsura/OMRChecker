"""
Speed benchmark: how many sheets per second this machine reads with a template.

    python -m src.benchmark.speed --template path/to/template.json --images path/to/scans
    python -m src.benchmark.speed --template ... --images ... --limit 500 --workers 1 8 16

For each worker count it reads the same files with a pool of worker processes
set up exactly like bulk jobs (one engine per process, single-threaded OpenCV),
and prints sheets per second and the average time per stage. Results are not
stored, so it measures reading only: run a real job to include saving.
"""

import argparse
import glob
import os
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".pdf"}
_ENGINE = None


def _init(template):
    from src.api.worker import worker_init
    from src.pipeline import OMREngine

    worker_init()
    global _ENGINE
    _ENGINE = OMREngine(template)


def _read(path):
    started = time.perf_counter()
    results = _ENGINE.scan_path(path, keep_images=False)
    total = (time.perf_counter() - started) * 1000
    stages = {}
    for result in results:
        for key, value in (result.to_dict().get("timings_ms") or {}).items():
            stages[key] = stages.get(key, 0) + value
    return total, [r.status for r in results], stages


def run(template, files, workers):
    from src.utils.cpu import prepare_worker_environment

    prepare_worker_environment()
    with ProcessPoolExecutor(workers, initializer=_init, initargs=(template,)) as pool:
        # Warm up every worker (engine build, first decode) before timing
        list(pool.map(_read, files[: workers * 2]))
        started = time.perf_counter()
        out = list(pool.map(_read, files, chunksize=4))
        wall = time.perf_counter() - started
    statuses, stages = {}, {}
    for _, status_list, stage in out:
        for status in status_list:
            statuses[status] = statuses.get(status, 0) + 1
        for key, value in stage.items():
            stages[key] = stages.get(key, 0) + value
    per_sheet = sum(t for t, _, _ in out) / len(out)
    print(
        f"workers {workers:>3}: {len(files) / wall:7.1f} sheets/s  "
        f"({per_sheet:.0f} ms per sheet in each worker, {wall:.1f} s for {len(files)})  "
        f"status {statuses}"
    )
    print(
        "             average ms per stage: "
        + ", ".join(f"{k} {v / len(out):.0f}" for k, v in stages.items() if k != "total")
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--template", required=True, help="template.json (its folder's config.json is used too)")
    parser.add_argument("--images", required=True, help="folder of scans")
    parser.add_argument("--limit", type=int, default=200, help="files to read (default 200)")
    parser.add_argument("--workers", type=int, nargs="+", default=[1, os.cpu_count() or 1])
    args = parser.parse_args()
    files = sorted(
        p for p in glob.glob(os.path.join(args.images, "**", "*"), recursive=True)
        if Path(p).suffix.lower() in SUFFIXES
    )[: args.limit]
    if not files:
        raise SystemExit(f"No scans found in {args.images}")
    print(f"{len(files)} files, {os.cpu_count()} logical processors")
    for workers in args.workers:
        run(str(Path(args.template).resolve()), files, workers)


if __name__ == "__main__":
    main()
