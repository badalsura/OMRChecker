"""Template editor group cascades (src/api/static/editor_groups.js), run in Node."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_editor_group_cascades():
    result = subprocess.run(
        [
            "node",
            str(ROOT / "tests" / "js" / "editor_groups.test.mjs"),
            str(ROOT / "api" / "static"),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "editor groups ok" in result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_editor_panels_render_without_null_labels():
    result = subprocess.run(
        [
            "node",
            str(ROOT / "tests" / "js" / "editor_panels.test.mjs"),
            (ROOT / "api" / "static").as_uri(),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "editor panels ok" in result.stdout
