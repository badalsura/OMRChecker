"""
Regenerate clients/openapi.json from the FastAPI app.

    python clients/export_openapi.py            # writes clients/openapi.json
    python clients/export_openapi.py out.json

Other client generators (openapi-generator, oapi-codegen, ...) can consume the
spec directly if the hand-written clients in this folder are not enough.
"""

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def export(out_path):
    from src.api.app import create_app

    with tempfile.TemporaryDirectory() as data_dir:
        app = create_app(data_dir, workers=1)
        spec = app.openapi()
    Path(out_path).write_text(json.dumps(spec, indent=2, sort_keys=False) + "\n")
    return spec


if __name__ == "__main__":
    target = (
        sys.argv[1] if len(sys.argv) > 1 else Path(__file__).with_name("openapi.json")
    )
    spec = export(target)
    print(f"Wrote {target}: {len(spec['paths'])} paths, OpenAPI {spec['openapi']}")
