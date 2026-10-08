"""Editor helper endpoints and per-request PDF options (src/api/editor_routes.py)."""

import pytest
from fastapi import HTTPException

from src.api.editor_routes import parse_pdf_params
from src.tests.test_api import make_client, make_sheet, png_bytes, upload_template
from src.synth.render import default_spec


@pytest.fixture(scope="module")
def spec():
    return default_spec(questions=10, roll_digits=3, with_zones=False)


def test_parse_pdf_params():
    assert parse_pdf_params(None, None) is None
    assert parse_pdf_params("", " ") is None
    assert parse_pdf_params("auto", "all") == {"pdf_dpi": "auto", "pdf_page": "1-"}
    assert parse_pdf_params("300", "2") == {"pdf_dpi": 300, "pdf_page": 2}
    assert parse_pdf_params(None, "1,3-4") == {"pdf_page": [1, "3-4"]}
    for dpi, page in (("12", None), ("abc", None), (None, "x"), (None, "0")):
        with pytest.raises(HTTPException):
            parse_pdf_params(dpi, page)


def pdf_of(images):
    fitz = pytest.importorskip("fitz")
    document = fitz.open()
    for image in images:
        data = png_bytes(image)
        h, w = image.shape[:2]
        page = document.new_page(width=w * 72 / 150, height=h * 72 / 150)
        page.insert_image(page.rect, stream=data)
    return document.tobytes()


def test_scan_pdf_pages_from_the_form(tmp_path, spec):
    first, _ = make_sheet(spec, 1)
    second, _ = make_sheet(spec, 2)
    pdf = pdf_of([first, second])
    with make_client(tmp_path) as client:
        template_id = upload_template(client, spec)

        def scan(**data):
            response = client.post(
                "/scans",
                data={"template_id": template_id, **data},
                files=[("files", ("two.pdf", pdf, "application/pdf"))],
            )
            assert response.status_code == 200, response.text
            return response.json()["scans"]

        # config.json default: page 1 only
        assert len(scan()) == 1
        both = scan(pdf_page="all", pdf_dpi="150")
        assert len(both) == 2
        assert both[0]["pdf_params"] == {"pdf_dpi": 150, "pdf_page": "1-"}
        # The Results screen re-renders the same page with the same settings
        rendered = client.get(f"/scans/{both[1]['scan_id']}/render")
        assert rendered.status_code == 200, rendered.text

        bad = client.post(
            "/scans",
            data={"template_id": template_id, "pdf_dpi": "5"},
            files=[("files", ("two.pdf", pdf, "application/pdf"))],
        )
        assert bad.status_code == 400


def test_models(tmp_path, spec):
    with make_client(tmp_path) as client:
        template_id = upload_template(client, spec)
        assert client.get("/templates/nope/models").status_code == 404
        models = client.get(f"/templates/{template_id}/models").json()["models"]
        assert all(m["location"] == "server" for m in models)
