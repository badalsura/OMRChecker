"""Command line entry point: python -m src.benchmark --help"""

import argparse
import json
import sys
from pathlib import Path

from src.benchmark.metrics import format_summary
from src.benchmark.runner import PRESETS, run_files, run_synthetic


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m src.benchmark",
        description="Measure OMR accuracy, review rate and throughput against ground truth.",
    )
    source = parser.add_argument_group("labelled files")
    source.add_argument("--template", help="template.json")
    source.add_argument(
        "--images", help="folder (searched recursively) or a single image/PDF"
    )
    source.add_argument(
        "--truth",
        help="truth CSV (file_name + one column per output/field/zone) or JSON",
    )
    source.add_argument("--config", help="config.json (default: next to the template)")
    synth = parser.add_argument_group("synthetic sheets (no files needed)")
    synth.add_argument(
        "--synthetic", type=int, metavar="N", help="render and read N sheets"
    )
    synth.add_argument("--preset", choices=sorted(PRESETS), default="clean")
    synth.add_argument(
        "--mark-style",
        choices=["pen", "pencil", "partial", "tick", "mixed"],
        default="mixed",
    )
    synth.add_argument(
        "--zones", action="store_true", help="include barcode/QR/OCR/ICR zones"
    )
    synth.add_argument("--seed", type=int, default=0)
    synth.add_argument("--blank-rate", type=float, default=0.05)
    synth.add_argument("--multi-rate", type=float, default=0.02)
    synth.add_argument("--erasures", type=int, default=4)
    parser.add_argument("--bubble-model", help="ONNX bubble classifier")
    parser.add_argument("--icr-model", help="ONNX ICR classifier")
    parser.add_argument("--workers", type=int, default=1, help="worker processes")
    parser.add_argument(
        "--worst", type=int, default=50, help="errors listed in the report"
    )
    parser.add_argument("--report", help="write the full JSON report here")
    parser.add_argument("--quiet", action="store_true", help="no console summary")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.synthetic:
        metrics = run_synthetic(
            args.synthetic,
            preset=args.preset,
            seed=args.seed,
            bubble_model_path=args.bubble_model,
            icr_model_path=args.icr_model,
            workers=args.workers,
            mark_style=args.mark_style,
            with_zones=args.zones,
            worst=args.worst,
            blank_rate=args.blank_rate,
            multi_rate=args.multi_rate,
            erasures=args.erasures,
        )
        title = f"Synthetic benchmark ({args.synthetic} sheets, preset {args.preset})"
    else:
        if not (args.template and args.images and args.truth):
            parser.error("give --template, --images and --truth, or --synthetic N")
        metrics = run_files(
            args.template,
            args.images,
            args.truth,
            bubble_model_path=args.bubble_model,
            icr_model_path=args.icr_model,
            config_path=args.config,
            workers=args.workers,
            worst=args.worst,
        )
        title = f"Benchmark {args.images}"
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps(metrics, indent=2))
    if not args.quiet:
        print(format_summary(metrics, title))
        if args.report:
            print(f"\nFull report: {args.report}")
    return metrics


if __name__ == "__main__":
    main()
    sys.exit(0)
