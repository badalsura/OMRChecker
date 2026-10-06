"""
Export results from the command line.

From the API's data folder (any Results filter; corrected values):

    python -m src.export --data-dir ./omr_data --job <job_id> --format xlsx --out results.xlsx
    python -m src.export --data-dir ./omr_data --template-id exam-a --view flagged \\
        --format pdf --profile profile.json --out flagged.pdf
    python -m src.export --data-dir ./omr_data --job <job_id> --format sql \\
        --sql-url postgresql+psycopg://user:pw@host/db --profile profile.json

From a batch run (python -m src.batch ... writes results.jsonl):

    python -m src.export --jsonl out/results.jsonl --template forms/exam/template.json \\
        --format csv --out results.csv

The profile is a JSON file (see src/export/profile.py and docs/engine-guide.md).
Exit code 0 on success, 2 when the export was refused (e.g. a lossy cast).
"""

import argparse
import json
import re
import sys
from pathlib import Path

from src.export import EXTENSIONS, FORMATS, ExportError, ExportProfile, export_results


def _jsonl_factory(path):
    def factory():
        with open(path) as handle:
            for line in handle:
                line = line.strip()
                if line:
                    yield json.loads(line)

    return factory


def _engine_info(template_path):
    from src.pipeline import OMREngine

    engine = OMREngine(template_path)
    template = engine.template
    return engine, {
        "output_columns": list(template.output_columns),
        "zone_names": [zone.name for zone in template.zones],
        "custom_labels": {k: list(v) for k, v in template.custom_labels.items()},
    }


def _reread_provider(engine):
    def provider(result):
        path = result.get("source_path") or result.get("input_path")
        if not path or not Path(path).exists():
            return None
        match = re.search(r"#page(\d+)$", result.get("file_id") or "")
        page = int(match.group(1)) - 1 if match else int(result.get("page") or 0)
        from src.utils.image import ImageUtils

        images = ImageUtils.load_omr_image(Path(path), engine.tuning_config)
        if page >= len(images):
            return None
        return engine.scan(images[page][1], result.get("file_id") or "x").aligned_image

    return provider


def main(argv=None):
    parser = argparse.ArgumentParser(description="Export OMR results")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--data-dir", help="The API data folder (OMR_DATA_DIR)")
    source.add_argument("--jsonl", help="results.jsonl written by python -m src.batch")
    parser.add_argument("--template", help="template.json (with --jsonl)")
    parser.add_argument("--job", help="Job id (with --data-dir)")
    parser.add_argument("--template-id", help="Template id (with --data-dir)")
    parser.add_argument(
        "--view",
        default="all",
        help="all, flagged, unflagged, reviewed, not_reviewed, verified, corrected, errors",
    )
    parser.add_argument("--flag", help="Only sheets with this flag")
    parser.add_argument("--name", help="Only sheets where this field was flagged")
    parser.add_argument("--format", required=True, choices=FORMATS)
    parser.add_argument("--out", help="Output file (default: results.<ext>)")
    parser.add_argument("--profile", help="Export profile JSON file")
    parser.add_argument("--sql-url", help="SQLAlchemy URL for --format sql")
    args = parser.parse_args(argv)

    profile_data = {}
    if args.profile:
        profile_data = json.loads(Path(args.profile).read_text())
    try:
        profile = ExportProfile.from_dict(profile_data)
    except ExportError as error:
        print(f"Invalid profile: {error}", file=sys.stderr)
        return 2
    out = Path(args.out or f"results.{EXTENSIONS[args.format]}")

    if args.data_dir:
        from src.api.app import Context
        from src.api.settings import Settings

        ctx = Context(Settings.from_env(args.data_dir))
        filters = {
            "job_id": args.job,
            "template_id": args.template_id,
            "view": args.view,
            "flag": args.flag,
            "name": args.name,
        }
        filters = {k: v for k, v in filters.items() if v}
        manager = ctx.exports
        factory = manager.results_factory(filters)
        infos = manager.template_infos(filters)
        provider = manager.image_provider
        total = ctx.index.count_results(**filters)
    else:
        if not args.template:
            parser.error("--jsonl needs --template")
        engine, info = _engine_info(Path(args.template))
        factory = _jsonl_factory(args.jsonl)
        infos = [info]
        provider = _reread_provider(engine)
        total = sum(1 for _ in factory())

    def progress(done):
        if done:
            print(f"{done} rows…", flush=True)

    try:
        report = export_results(
            factory,
            args.format,
            out,
            profile,
            infos,
            image_provider=provider,
            total=total,
            title=out.stem,
            sql_url=args.sql_url,
            progress=progress,
        )
    except ExportError as error:
        if out.exists() and args.format != "sql":
            out.unlink()
        print(f"Export refused: {error}", file=sys.stderr)
        return 2
    for warning in report["warnings"]:
        print(f"warning: {warning}", file=sys.stderr)
    print(f"Wrote {report['rows']} rows to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
