"""
Run the engine over labelled sheets (files on disk or synthetic sheets rendered
in memory) and compute the benchmark metrics.
"""

import json
import multiprocessing
import os
import random
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from src.benchmark.metrics import compute_metrics
from src.ml.dataset import iter_images, load_truth, lookup_truth

# Synthetic difficulty presets: augment() options (None = the clean render) and
# whether the sheet needs registration (the clean render is already in template
# coordinates, the others are skewed, padded and need the timing-mark alignment)
PRESETS = {
    "clean": {"augment": None, "register": False},
    "scan": {
        "augment": dict(
            rotation=1.0,
            perspective=0.005,
            blur=1.0,
            noise=4.0,
            shadow=0.15,
            jpeg_quality=(75, 95),
            background=True,
        ),
        "register": True,
    },
    "phone": {
        "augment": dict(
            rotation=4.0,
            perspective=0.04,
            blur=1.5,
            noise=8.0,
            shadow=0.4,
            jpeg_quality=(50, 85),
            background=True,
        ),
        "register": True,
    },
}


def template_column_fields(template_path):
    """{custom label: [field labels]} from a template, for flagging custom columns."""
    try:
        data = json.loads(Path(template_path).read_text())
    except (OSError, ValueError):
        return {}
    from src.utils.parsing import parse_fields

    return {
        label: parse_fields(f"Custom Label: {label}", strings)
        for label, strings in (data.get("customLabels") or {}).items()
    }


def run_files(
    template_path,
    images,
    truth,
    bubble_model_path=None,
    icr_model_path=None,
    config_path=None,
    workers=1,
    worst=50,
):
    """Benchmark sheets on disk. `truth` is a CSV/JSON path or a loaded dict."""
    from src.batch import scan_files

    if not isinstance(truth, dict):
        truth = load_truth(truth)
    paths = iter_images(images)
    records = []
    previous_threads = os.environ.get("OMR_ONNX_THREADS")
    if workers != 1:
        # Forked workers inherit this: one inference thread per worker process
        os.environ.setdefault("OMR_ONNX_THREADS", "1")
    started = time.perf_counter()
    try:
        for result in scan_files(
            paths,
            template_path,
            config_path=config_path,
            bubble_model_path=bubble_model_path,
            icr_model_path=icr_model_path,
            workers=workers,
        ):
            source = result.get("input_path") or result["file_id"]
            values = lookup_truth(truth, result["file_id"])
            if values is None:
                values = lookup_truth(truth, Path(source).name)
            records.append(
                {"file_id": result["file_id"], "result": result, "truth": values}
            )
    finally:
        if previous_threads is None:
            os.environ.pop("OMR_ONNX_THREADS", None)
    wall = time.perf_counter() - started
    metrics = compute_metrics(
        records,
        column_fields=template_column_fields(template_path),
        wall_seconds=wall,
        workers=workers,
        worst=worst,
    )
    metrics["config"] = {
        "mode": "files",
        "template": str(template_path),
        "images": str(images),
        "bubble_model": str(bubble_model_path) if bubble_model_path else None,
        "icr_model": str(icr_model_path) if icr_model_path else None,
    }
    return metrics


# --------------------------------------------------------------------------- synthetic


def render_synthetic(
    n,
    preset="clean",
    seed=0,
    mark_style="mixed",
    with_zones=False,
    blank_rate=0.05,
    multi_rate=0.02,
    erasures=4,
    questions=40,
):
    """Return (spec, [(file_id, image, truth_values)]) for n synthetic sheets."""
    from src.synth import augment, default_spec, random_answers, render_sheet

    if preset not in PRESETS:
        raise ValueError(f"unknown preset {preset!r}; choose from {sorted(PRESETS)}")
    options = PRESETS[preset]["augment"]
    spec = default_spec(questions=questions, with_zones=with_zones)
    rng = random.Random(seed)
    sheets = []
    for index in range(n):
        answers = random_answers(
            spec, rng, blank_rate=blank_rate, multi_rate=multi_rate
        )
        image, truth = render_sheet(
            spec, answers, rng=rng, mark_style=mark_style, erasures=erasures
        )
        if options:
            image, _ = augment(image, rng, **options)
        values = dict(truth["answers"])
        values.update(truth["zones"])
        sheets.append((f"synthetic_{preset}_{index:05d}.png", image, values))
    return spec, sheets


def _scan_array(task):
    from src.batch import get_engine

    engine_args, file_id, image = task
    result = get_engine(engine_args).scan(image, file_id, keep_images=False)
    return result.to_dict()


def _init_worker(engine_args=None, pooled=True):
    from src.batch import _worker_init, get_engine

    _worker_init()
    # One process per core already; a thread pool per process would oversubscribe
    if pooled:
        os.environ.setdefault("OMR_ONNX_THREADS", "1")
    if engine_args is not None:
        # Build the engine up front so template loading is not timed as reading
        get_engine(engine_args)


def _ready(_):
    return os.getpid()


def run_synthetic(
    n,
    preset="clean",
    seed=0,
    bubble_model_path=None,
    icr_model_path=None,
    workers=1,
    mark_style="mixed",
    with_zones=False,
    worst=50,
    **render_options,
):
    """Render n synthetic sheets in memory, scan them and compute the metrics."""
    render_started = time.perf_counter()
    spec, sheets = render_synthetic(
        n,
        preset,
        seed,
        mark_style=mark_style,
        with_zones=with_zones,
        **render_options,
    )
    render_seconds = time.perf_counter() - render_started
    pre_processors = None if PRESETS[preset]["register"] else []
    with tempfile.TemporaryDirectory(prefix="omr_bench_") as tmp:
        template_path = Path(tmp, "template.json")
        template_path.write_text(
            json.dumps(spec.to_template(pre_processors=pre_processors))
        )
        engine_args = tuple(
            str(p) if p else None
            for p in (template_path, None, None, bubble_model_path, icr_model_path)
        )
        tasks = [(engine_args, file_id, image) for file_id, image, _ in sheets]
        if workers <= 1:
            _init_worker(engine_args, pooled=False)
            started = time.perf_counter()
            results = [_scan_array(task) for task in tasks]
            wall = time.perf_counter() - started
        else:
            # "spawn": the parent has used OpenCV's thread pool while rendering, and
            # forking a process with live OpenCV threads can deadlock the children
            context = multiprocessing.get_context("spawn")
            with ProcessPoolExecutor(
                max_workers=workers,
                mp_context=context,
                initializer=_init_worker,
                initargs=(engine_args,),
            ) as pool:
                # Start every worker (and its engine) before the clock starts
                list(pool.map(_ready, range(workers * 4)))
                started = time.perf_counter()
                results = list(pool.map(_scan_array, tasks, chunksize=2))
                wall = time.perf_counter() - started
    records = [
        {"file_id": file_id, "result": result, "truth": values}
        for (file_id, _, values), result in zip(sheets, results)
    ]
    metrics = compute_metrics(records, wall_seconds=wall, workers=workers, worst=worst)
    metrics["config"] = {
        "mode": "synthetic",
        "sheets": n,
        "preset": preset,
        "seed": seed,
        "mark_style": mark_style,
        "with_zones": with_zones,
        "render_seconds": round(render_seconds, 2),
        "bubble_model": str(bubble_model_path) if bubble_model_path else None,
        "icr_model": str(icr_model_path) if icr_model_path else None,
        "cpu_count": os.cpu_count(),
    }
    return metrics
