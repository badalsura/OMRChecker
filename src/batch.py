"""
High-throughput batch reading.

Images are decoded and read inside worker processes (one OMREngine per process
and template, created lazily and reused), so throughput scales with CPU cores
and the parent process only moves file paths and small result dicts.

    for result in scan_files(paths, "forms/exam/template.json", workers=8):
        ...
"""
import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import cv2

# Each worker process keeps its own engines: {(template, config, eval, models): OMREngine}
_ENGINES = {}


def _worker_init():
    # Let processes, not OpenCV threads, provide the parallelism
    cv2.setNumThreads(1)
    os.environ.setdefault("OMP_NUM_THREADS", "1")


def get_engine(engine_args):
    from src.pipeline import OMREngine

    engine = _ENGINES.get(engine_args)
    if engine is None:
        template_path, config_path, evaluation_path, bubble_model, icr_model = engine_args
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
            cv2.imwrite(str(Path(output_dir, f"{stem}_marked.jpg")), result.marked_image)
            cv2.imwrite(str(Path(output_dir, f"{stem}_aligned.png")), result.aligned_image)
        data = result.to_dict()
        data["input_path"] = str(file_path)
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
    workers = workers or os.cpu_count() or 1
    if workers == 1:
        _worker_init()
        for task in tasks:
            yield from _scan_one(task)
        return
    with ProcessPoolExecutor(max_workers=workers, initializer=_worker_init) as pool:
        for page_results in pool.map(_scan_one, tasks, chunksize=chunksize):
            yield from page_results
