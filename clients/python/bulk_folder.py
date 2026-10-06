"""
Submit a server-side folder as a bulk job, stream progress, save the CSV.

    python bulk_folder.py --url http://127.0.0.1:8000 --template exam \
        --folder D:\\scans\\day1 --out day1.csv [--api-key KEY] [--workers 8]

--template is a template id already on the server, or a template.json path
(uploaded first, together with config.json / evaluation.json next to it).
The folder must be readable by the server (and inside OMR_ALLOWED_DIRS if set).
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from omr_client import OMRClient  # noqa: E402


def resolve_template(client, template):
    path = Path(template)
    if path.suffix.lower() in (".json", ".zip") and path.exists():
        files = [path]
        if path.suffix.lower() == ".json":
            for extra in ("config.json", "evaluation.json"):
                if (path.parent / extra).exists():
                    files.append(path.parent / extra)
        uploaded = client.upload_template(files, name=path.parent.name or path.stem)
        print(f"Uploaded template as '{uploaded['id']}'")
        return uploaded["id"]
    return template


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--api-key")
    parser.add_argument("--template", required=True)
    parser.add_argument("--folder", required=True, help="Folder on the server")
    parser.add_argument("--out", default="results.csv")
    parser.add_argument("--workers", type=int)
    parser.add_argument(
        "--save-images", default="review", choices=["all", "review", "none"]
    )
    parser.add_argument("--poll", type=float, default=2.0)
    args = parser.parse_args(argv)

    client = OMRClient(args.url, api_key=args.api_key)
    template_id = resolve_template(client, args.template)
    job = client.create_job(
        template_id,
        folder=args.folder,
        workers=args.workers,
        save_images=args.save_images,
    )
    print(f"Job {job['id']}: {job['total_files']} files")
    started = time.monotonic()

    def progress(job):
        rate = job.get("throughput_per_s") or 0
        eta = job.get("eta_s")
        print(
            f"\r{job['state']:<10} {job['processed_files']}/{job['total_files']} "
            f"({rate * 60:.0f}/min, eta {eta if eta is not None else '?'}s) "
            f"{job.get('counts', {})}",
            end="",
            flush=True,
        )

    job = client.wait_for_job(job["id"], poll=args.poll, callback=progress)
    elapsed = time.monotonic() - started
    print()
    client.job_results_csv(job["id"], args.out)
    print(
        f"{job['state']}: {job['processed_files']} files in {elapsed:.1f}s "
        f"({job['processed_files'] / max(elapsed, 1e-9) * 60:.0f} sheets/min); "
        f"{job.get('pending_review', 0)} items waiting for review; CSV -> {args.out}"
    )
    for message in job.get("errors", [])[:10]:
        print("  error:", message)
    return 0 if job["state"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
