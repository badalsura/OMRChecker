"""
Accuracy and throughput benchmark for the OMR engine.

    python -m src.benchmark --template t.json --images scans/ --truth truth.csv --report out.json
    python -m src.benchmark --synthetic 200 --preset scan --workers 4 --report out.json

See src/benchmark/metrics.py for the definitions of the reported numbers.
"""

from src.benchmark.metrics import compute_metrics, format_summary  # noqa: F401
from src.benchmark.runner import PRESETS, run_files, run_synthetic  # noqa: F401
