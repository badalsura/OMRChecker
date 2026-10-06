"""Run the browser engine's Node parity tests (web/omr-browser) when Node is installed."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

BROWSER_DIR = Path(__file__).resolve().parents[2] / "web" / "omr-browser"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_browser_engine_parity(tmp_path):
    # Render fixtures with this interpreter so Node uses the same cv2/zxing install
    subprocess.run(
        [
            sys.executable,
            str(BROWSER_DIR / "test" / "make_fixtures.py"),
            "--out",
            str(tmp_path),
            "--n",
            "4",
        ],
        check=True,
        capture_output=True,
        cwd=BROWSER_DIR.parents[1],
    )
    completed = subprocess.run(
        ["node", "--test", "test/unit.test.js", "test/parity.test.js"],
        cwd=BROWSER_DIR,
        capture_output=True,
        text=True,
        env={**os.environ, "OMR_FIXTURES_DIR": str(tmp_path)},
        timeout=600,
    )
    assert completed.returncode == 0, (
        completed.stdout[-4000:] + completed.stderr[-2000:]
    )
