"""Colour tools API (/tools/*), colour templates over the REST API and the editor JS."""

import json
import random
import shutil
import subprocess
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.color import parse_hex
from src.synth import default_spec, random_answers, render_sheet

PINK = "#E8618C"


@pytest.fixture(scope="module")
def spec():
    return default_spec(questions=20, roll_digits=4, with_zones=False)


def png(image):
    ok, buffer = cv2.imencode(".png", image)
    assert ok
    return buffer.tobytes()


def colour_sheet(spec, seed, ink_color="#1F3C9A"):
    rng = random.Random(seed)
    answers = random_answers(spec, rng, blank_rate=0.0)
    return render_sheet(
        spec, answers, rng=rng, mark_style="pen", print_color=PINK, ink_color=ink_color
    )


def upload(client, template):
    response = client.post(
        "/templates",
        files=[("files", ("template.json", json.dumps(template), "application/json"))],
        data={"name": "Colour exam"},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_colors_preview_and_suggest(tmp_path, spec):
    image, _ = colour_sheet(spec, 1)
    with TestClient(create_app(tmp_path / "data", workers=1)) as client:
        response = client.post(
            "/tools/colors", files={"file": ("sheet.png", png(image), "image/png")}
        )
        assert response.status_code == 200, response.text
        palette = response.json()
        assert palette["width"] == image.shape[1] and palette["current"] is None
        pink = [c for c in palette["colors"] if c["label_guess"] == "pink"]
        assert pink and pink[0]["suggestion"]["settings"]["mode"] == "red"

        template = spec.to_template(pre_processors=[])
        template["colorDropout"] = {"mode": "red"}
        template_id = upload(client, template)
        palette = client.post(
            "/tools/colors",
            files={"file": ("sheet.png", png(image), "image/png")},
            data={"template_id": template_id, "k": "4"},
        ).json()
        assert palette["current"] == {"mode": "red"}
        missing = client.post(
            "/tools/colors",
            files={"file": ("sheet.png", png(image), "image/png")},
            data={"template_id": "nope"},
        )
        assert missing.status_code == 404
        bad = client.post(
            "/tools/colors", files={"file": ("x.png", b"not an image", "image/png")}
        )
        assert bad.status_code == 400

        preview = client.post(
            "/tools/dropout-preview",
            files={"file": ("sheet.png", png(image), "image/png")},
            data={"settings": json.dumps({"mode": "red"}), "max_width": "400"},
        )
        assert preview.status_code == 200
        assert preview.headers["content-type"] == "image/png"
        assert json.loads(preview.headers["x-dropout"]) == {
            "mode": "red",
            "strength": 1.0,
        }
        grey = cv2.imdecode(np.frombuffer(preview.content, np.uint8), -1)
        assert grey.ndim == 2 and grey.shape[1] == 400
        for settings in ("grey", '"grey"', ""):
            plain = client.post(
                "/tools/dropout-preview",
                files={"file": ("sheet.png", png(image), "image/png")},
                data={"settings": settings},
            )
            assert plain.status_code == 200
            assert json.loads(plain.headers["x-dropout"]) == {"mode": "grey"}
        # The red preview has less ink than plain grey (pink print removed)
        plain_img = cv2.imdecode(np.frombuffer(plain.content, np.uint8), -1)
        plain_img = cv2.resize(plain_img, (grey.shape[1], grey.shape[0]))
        assert grey.mean() > plain_img.mean()
        invalid = client.post(
            "/tools/dropout-preview",
            files={"file": ("sheet.png", png(image), "image/png")},
            data={"settings": json.dumps({"mode": "purple"})},
        )
        assert invalid.status_code == 400

        suggestion = client.post("/tools/dropout-suggest", json={"target": PINK})
        assert suggestion.status_code == 200
        assert suggestion.json()["settings"]["mode"] == "red"
        assert (
            client.post("/tools/dropout-suggest", json={"target": "pink"}).status_code
            == 400
        )


def test_colour_pdf_upload_for_tools(tmp_path, spec):
    fitz = pytest.importorskip("fitz")
    image, _ = colour_sheet(spec, 2)
    path = tmp_path / "sheet.png"
    cv2.imwrite(str(path), image)
    document = fitz.open()
    page = document.new_page(width=600, height=850)
    page.insert_image(page.rect, filename=str(path))
    content = document.tobytes()
    with TestClient(create_app(tmp_path / "data", workers=1)) as client:
        palette = client.post(
            "/tools/colors", files={"file": ("sheet.pdf", content, "application/pdf")}
        ).json()
        assert any(c["label_guess"] == "pink" for c in palette["colors"])


def test_scans_endpoint_reads_colour_templates(tmp_path, spec):
    # Pink pen on pink print: the red dropout removes the marks, grey keeps them
    image, truth = colour_sheet(spec, 3, ink_color=PINK)
    with TestClient(create_app(tmp_path / "data", workers=1)) as client:
        template = spec.to_template(pre_processors=[])
        template["colorDropout"] = "red"
        template_id = upload(client, template)
        result = client.post(
            "/scans",
            data={"template_id": template_id},
            files=[("files", ("sheet.png", png(image), "image/png"))],
        ).json()["scans"][0]
        assert all(result["responses"][k] == "" for k in truth["answers"])

        # Turn colour removal off from the editor (PUT) and rescan
        template["colorDropout"] = "grey"
        saved = client.put(f"/templates/{template_id}", json={"template": template})
        assert saved.status_code == 200, saved.text
        result = client.post(
            "/scans",
            data={"template_id": template_id},
            files=[("files", ("sheet.png", png(image), "image/png"))],
        ).json()["scans"][0]
        assert all(result["responses"][k] == v for k, v in truth["answers"].items())

        template["colorDropout"] = {"mode": "color"}  # colour mode needs a colour
        rejected = client.put(f"/templates/{template_id}", json={"template": template})
        assert rejected.status_code in (400, 422)


def test_static_colour_panel_served(tmp_path):
    with TestClient(create_app(tmp_path / "data", workers=1)) as client:
        assert client.get("/static/colors.js").status_code == 200
        assert client.get("/static/colors.css").status_code == 200
        assert "ColourPanel" in client.get("/static/editor.js").text


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_colour_panel_helpers_in_node():
    static = Path(__file__).resolve().parents[1] / "api" / "static"
    script = (
        "const c = await import(%s);"
        "const e = await import(%s);"
        "const round = c.dropoutFromControls(c.controlsFromDropout("
        "{mode: 'color', color: '#e86', tolerance: 40.4, strength: 0.5}));"
        "console.log(JSON.stringify({round, grey: c.dropoutFromControls("
        "c.controlsFromDropout('gray')), red: c.controlsFromDropout('red').mode,"
        "editor: typeof e.TemplateEditor}));"
    ) % (
        json.dumps((static / "colors.js").as_uri()),
        json.dumps((static / "editor.js").as_uri()),
    )
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    out = json.loads(completed.stdout.strip().splitlines()[-1])
    assert out["round"] == {
        "mode": "color",
        "strength": 0.5,
        "color": "#EE8866",
        "tolerance": 40,
    }
    assert out["grey"] is None and out["red"] == "red"
    assert out["editor"] == "function"


def test_parse_hex_roundtrip():
    assert parse_hex(PINK) == (0x8C, 0x61, 0xE8)
