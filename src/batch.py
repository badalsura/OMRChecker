"""
High-throughput batch reading.

Images are decoded and read inside worker processes (one OMREngine per process
and template, created lazily and reused), so throughput scales with CPU cores
and the parent process only moves file paths and small result dicts.

    for result in scan_files(paths, "forms/exam/template.json", workers=8):
        ...
"""

import logging
import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import cv2

from src.readers.image_zone import save_zone_images

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


def _scan_one(task):
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
):
    """Yield result dicts (one per page) in input order."""
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
    with ProcessPoolExecutor(max_workers=workers, initializer=_worker_init) as pool:
        for page_results in pool.map(_scan_one, tasks, chunksize=chunksize):
            yield from page_results


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
    args = parser.parse_args(argv)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    files = collect_files(args.input)
    started = time.perf_counter()
    counts = {}
    columns = None
    with open(out_dir / "results.jsonl", "w") as jsonl, open(
        out_dir / "results.csv", "w", newline=""
    ) as csv_file:
        writer = csv.writer(csv_file, quoting=csv.QUOTE_NONNUMERIC)
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
            ),
            1,
        ):
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
