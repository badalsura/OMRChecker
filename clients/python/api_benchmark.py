"""
Measure bulk throughput end to end through the API (folder job -> CSV).

Run from the repository root (needs the engine for synthetic sheets):

    python clients/python/api_benchmark.py --n 300 --preset phone --workers 8
    python clients/python/api_benchmark.py --url http://host:8000 --template exam \
        --folder /data/scans       # real sheets on an already running server

Without --url a server is started on a free port with a temporary data dir.
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

from omr_client import OMRClient  # noqa: E402


def make_synthetic(folder, n, preset):
    import cv2

    from src.benchmark.runner import PRESETS, render_synthetic

    spec, sheets = render_synthetic(n, preset=preset, seed=1)
    pre_processors = None if PRESETS[preset]["register"] else []
    template = Path(folder) / "template.json"
    template.write_text(json.dumps(spec.to_template(pre_processors=pre_processors)))
    images = Path(folder) / "sheets"
    images.mkdir(parents=True, exist_ok=True)
    truths = {}
    for name, image, values in sheets:
        name = Path(name).with_suffix(".jpg").name
        cv2.imwrite(str(images / name), image, [cv2.IMWRITE_JPEG_QUALITY, 85])
        truths[name] = values
    return template, images, truths


def start_server(data_dir, workers):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    cmd = [
        sys.executable,
        "-m",
        "src.api",
        "--port",
        str(port),
        "--data-dir",
        str(data_dir),
        "--log-level",
        "warning",
    ]
    if workers:
        cmd += ["--workers", str(workers)]
    proc = subprocess.Popen(cmd, cwd=ROOT)
    url = f"http://127.0.0.1:{port}"
    for _ in range(240):
        try:
            urllib.request.urlopen(url + "/health", timeout=1)
            return proc, url
        except OSError:
            time.sleep(0.25)
    proc.kill()
    raise RuntimeError("server did not start")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url")
    parser.add_argument("--api-key")
    parser.add_argument("--template", help="template id (with --url) or template.json")
    parser.add_argument("--folder", help="server-side folder of sheets")
    parser.add_argument("--n", type=int, default=300)
    parser.add_argument("--preset", default="phone", choices=["clean", "scan", "phone"])
    parser.add_argument("--workers", type=int, default=0)
    parser.add_argument(
        "--save-images", default="none", choices=["all", "review", "none"]
    )
    args = parser.parse_args(argv)

    work = Path(tempfile.mkdtemp(prefix="omr_api_bench_"))
    truths = None
    proc = None
    if args.folder:
        template, folder = args.template, args.folder
    else:
        print(f"Rendering {args.n} synthetic '{args.preset}' sheets ...", flush=True)
        template, folder, truths = make_synthetic(work, args.n, args.preset)
    try:
        url = args.url
        if not url:
            proc, url = start_server(work / "data", args.workers)
        client = OMRClient(url, api_key=args.api_key)
        template_id = template
        if str(template).endswith(".json"):
            template_id = client.upload_template([template], name="bench")["id"]

        started = time.monotonic()
        job = client.create_job(
            template_id,
            folder=str(folder),
            workers=args.workers or None,
            save_images=args.save_images,
        )
        job = client.wait_for_job(job["id"], poll=0.5)
        job_time = time.monotonic() - started
        csv_path = client.job_results_csv(job["id"], work / "results.csv")
        total = time.monotonic() - started

        files = job["processed_files"]
        report = {
            "files": files,
            "state": job["state"],
            "counts": job["counts"],
            "workers": client.capabilities()["workers"]
            if not args.workers
            else args.workers,
            "cpu_count": os.cpu_count(),
            "seconds_to_complete": round(job_time, 2),
            "seconds_incl_csv": round(total, 2),
            "sheets_per_min": round(files / job_time * 60),
            "server_reported_per_s": job.get("throughput_per_s"),
        }
        if truths:
            import csv

            wrong = checked = 0
            for row in csv.DictReader(open(csv_path)):
                for label, value in truths.get(row["file_name"], {}).items():
                    if label in row:
                        checked += 1
                        wrong += row[label] != value
            report["field_accuracy"] = round(1 - wrong / max(checked, 1), 5)
        print(json.dumps(report, indent=2))
        return 0 if job["state"] == "completed" else 1
    finally:
        if proc is not None:
            proc.terminate()
            proc.wait(timeout=30)


if __name__ == "__main__":
    raise SystemExit(main())
