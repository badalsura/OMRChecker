"""
Run the API server and web GUI:

    python -m src.api --host 0.0.0.0 --port 8000 --data-dir ./omr_data --workers 8

Bulk jobs use --workers processes; the web server itself runs as one process
(do not start several uvicorn workers on the same data directory).
"""

import argparse
import os


def main(argv=None):
    parser = argparse.ArgumentParser(description="OMR engine REST API and web GUI")
    parser.add_argument("--host", default=os.environ.get("OMR_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("OMR_PORT", 8000))
    )
    parser.add_argument(
        "--data-dir", default=os.environ.get("OMR_DATA_DIR", "./omr_data")
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="Worker processes for bulk jobs (default: one per CPU core)",
    )
    parser.add_argument(
        "--reindex",
        action="store_true",
        help="Rebuild the SQLite index from the result.json files and exit",
    )
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args(argv)

    if args.reindex:
        from src.api.storage import DataDir, ScanIndex

        data = DataDir(args.data_dir)
        index = ScanIndex(data.root / "index.sqlite3")
        count = index.rebuild(data.scans)
        print(f"Indexed {count} scans")
        return

    import uvicorn

    from src.api.app import create_app

    app = create_app(args.data_dir, workers=args.workers)
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
