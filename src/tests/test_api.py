import csv
import io
import json
import random
import time
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app, parse_labels_csv
from src.synth.render import default_spec, random_answers, render_sheet


@pytest.fixture(scope="module")
def spec():
    # Bubble-only sheet keeps the tests fast; zones are covered separately
    return default_spec(questions=20, roll_digits=4, with_zones=False)


def png_bytes(image):
    ok, buffer = cv2.imencode(".png", image)
    assert ok
    return buffer.tobytes()


def make_sheet(spec, seed):
    rng = random.Random(seed)
    answers = random_answers(spec, rng, blank_rate=0.0)
    image, truth = render_sheet(spec, answers, rng=rng)
    return image, truth


def make_client(tmp_path, **overrides):
    app = create_app(tmp_path / "data", workers=1, **overrides)
    return TestClient(app)


def upload_template(client, spec, evaluation=None, headers=None):
    files = [
        (
            "files",
            (
                "template.json",
                json.dumps(spec.to_template(pre_processors=[])),
                "application/json",
            ),
        )
    ]
    if evaluation is not None:
        files.append(
            ("files", ("evaluation.json", json.dumps(evaluation), "application/json"))
        )
    response = client.post(
        "/templates", files=files, data={"name": "Exam A"}, headers=headers or {}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def scan_one(client, template_id, image, name="sheet.png"):
    response = client.post(
        "/scans",
        data={"template_id": template_id},
        files=[("files", (name, png_bytes(image), "image/png"))],
    )
    assert response.status_code == 200, response.text
    scans = response.json()["scans"]
    assert len(scans) == 1
    return scans[0]


def test_template_upload_and_validation(tmp_path, spec):
    with make_client(tmp_path) as client:
        template_id = upload_template(client, spec)
        listing = client.get("/templates").json()["templates"]
        assert [t["id"] for t in listing] == [template_id]
        detail = client.get(f"/templates/{template_id}").json()
        assert detail["template"]["fieldBlocks"].keys() == {"Roll", "MCQ_1"}

        # Invalid templates are rejected with readable errors
        bad = spec.to_template(pre_processors=[])
        bad["fieldBlocks"]["MCQ_1"]["direction"] = "diagonal"
        response = client.post(
            "/templates",
            files=[("files", ("template.json", json.dumps(bad), "application/json"))],
        )
        assert response.status_code == 422
        assert any("direction" in e["path"] for e in response.json()["errors"])

        # Semantic errors (overflowing block) are caught too
        bad = spec.to_template(pre_processors=[])
        bad["fieldBlocks"]["MCQ_1"]["origin"] = [1200, 330]
        response = client.put(f"/templates/{template_id}", json=bad)
        assert response.status_code == 422
        assert "Overflowing" in response.json()["errors"][0]["message"]

        # A valid edit is saved
        good = spec.to_template(pre_processors=[])
        good["fieldBlocks"]["MCQ_1"]["origin"] = [522, 330]
        response = client.put(f"/templates/{template_id}", json={"template": good})
        assert response.status_code == 200, response.text
        assert response.json()["template"]["fieldBlocks"]["MCQ_1"]["origin"] == [
            522,
            330,
        ]

        layout = client.get(f"/templates/{template_id}/layout.png?image=blank")
        assert layout.status_code == 200
        assert layout.content[:4] == b"\x89PNG"


def test_template_zip_upload(tmp_path, spec):
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "exam/template.json", json.dumps(spec.to_template(pre_processors=[]))
        )
        archive.writestr("exam/../../evil.txt", "x")
    with make_client(tmp_path) as client:
        response = client.post(
            "/templates",
            files=[("files", ("exam.zip", buffer.getvalue(), "application/zip"))],
        )
        assert response.status_code == 201, response.text
        assert "template.json" in response.json()["files"]
    assert not (tmp_path / "evil.txt").exists()


def test_sync_scan_crop_and_review(tmp_path, spec):
    image, truth = make_sheet(spec, seed=3)
    evaluation = {
        "source_type": "custom",
        "options": {"questions_in_order": ["q1..5"], "answers_in_order": ["A"] * 5},
        "marking_schemes": {
            "DEFAULT": {"correct": "1", "incorrect": "0", "unmarked": "0"}
        },
    }
    with make_client(tmp_path) as client:
        template_id = upload_template(client, spec, evaluation=evaluation)
        result = scan_one(client, template_id, image)
        assert result["status"] in ("ok", "needs_review")
        for label, answer in truth["answers"].items():
            assert result["responses"][label] == answer, label
        expected_score = sum(truth["answers"][f"q{i}"] == "A" for i in range(1, 6))
        assert result["score"] == expected_score

        scan_id = result["scan_id"]
        assert client.get(f"/scans/{scan_id}").json()["file_id"] == "sheet.png"
        marked = client.get(f"/scans/{scan_id}/image?kind=marked")
        assert marked.status_code == 200 and len(marked.content) > 1000

        crop = client.get(f"/scans/{scan_id}/crop?field=q1&pad=10")
        assert crop.status_code == 200
        assert crop.headers["content-type"] == "image/png"
        decoded = cv2.imdecode(np.frombuffer(crop.content, np.uint8), 0)
        # q1 is a row of 4 bubbles 50px apart, 30px wide, plus padding
        assert decoded.shape == (30 + 20, 150 + 30 + 20)
        assert client.get(f"/scans/{scan_id}/crop?field=nope").status_code == 404

        # Correct q1 to a different value
        new_value = "B" if truth["answers"]["q1"] != "B" else "C"
        response = client.post(
            f"/scans/{scan_id}/review",
            json={"corrections": {"q1": new_value}, "accept": ["q2"]},
        )
        assert response.status_code == 200, response.text
        updated = response.json()
        assert updated["responses"]["q1"] == new_value
        assert updated["fields"]["q1"]["original_value"] == truth["answers"]["q1"]
        assert updated["reviewed"] is True
        assert updated["score"] == expected_score - (truth["answers"]["q1"] == "A") + (
            new_value == "A"
        )
        assert updated["training_records"] == 2
        stored = client.get(f"/scans/{scan_id}").json()
        assert stored["responses"]["q1"] == new_value

        labels_path = tmp_path / "data" / "training" / "labels.jsonl"
        records = [json.loads(line) for line in labels_path.read_text().splitlines()]
        q1 = next(r for r in records if r["name"] == "q1")
        assert q1["label"] == new_value
        assert q1["predicted"] == truth["answers"]["q1"]
        assert q1["action"] == "corrected"
        assert (tmp_path / "data" / "training" / q1["crop"]).exists()
        marked_bubbles = [b["value"] for b in q1["bubbles"] if b["label"] == "marked"]
        assert marked_bubbles == [new_value]
        assert next(r for r in records if r["name"] == "q2")["action"] == "accepted"

        # Invalid corrections are rejected
        response = client.post(
            f"/scans/{scan_id}/review", json={"corrections": {"q1": "Z"}}
        )
        assert response.status_code == 422


def test_review_queue(tmp_path, spec):
    rng = random.Random(7)
    answers = random_answers(spec, rng, blank_rate=0.0)
    answers["q3"] = "AC"  # multi-mark goes to review
    image, _ = render_sheet(spec, answers, rng=rng)
    with make_client(tmp_path) as client:
        template_id = upload_template(client, spec)
        result = scan_one(client, template_id, image)
        assert result["status"] == "needs_review"
        assert "q3" in [item["name"] for item in result["review"]]

        queue = client.get(f"/review?template_id={template_id}").json()
        assert queue["total"] >= 1
        item = next(i for i in queue["items"] if i["name"] == "q3")
        assert item["value"] == "AC"
        assert "multi_marked" in item["flags"]
        assert [o["value"] for o in item["options"]] == ["A", "B", "C", "D"]
        assert client.get(item["crop_url"]).status_code == 200
        summary = client.get("/review/summary").json()
        assert summary["total"] == queue["total"]

        names = [i["name"] for i in result["review"]]
        response = client.post(
            f"/scans/{result['scan_id']}/review",
            json={
                "corrections": {"q3": "A"},
                "accept": [n for n in names if n != "q3"],
            },
        )
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        assert client.get("/review").json()["total"] == 0
        listed = client.get("/scans?status=ok").json()
        assert listed["total"] == 1 and listed["items"][0]["reviewed"] == 1


def test_job_processing_and_csv(tmp_path, spec):
    sheets = [make_sheet(spec, seed) for seed in range(3)]
    folder = tmp_path / "inbox"
    folder.mkdir()
    for index, (image, _) in enumerate(sheets[:2]):
        cv2.imwrite(str(folder / f"sheet{index}.png"), image)
    (folder / "notes.txt").write_text("ignored")
    with make_client(tmp_path) as client:
        template_id = upload_template(client, spec)
        # Upload-based job built in two chunks, then started
        response = client.post(
            "/jobs",
            data={"template_id": template_id, "start": "false"},
            files=[("files", ("a.png", png_bytes(sheets[0][0]), "image/png"))],
        )
        assert response.status_code == 201, response.text
        job_id = response.json()["id"]
        response = client.post(
            f"/jobs/{job_id}/files",
            files=[
                ("files", ("b.png", png_bytes(sheets[1][0]), "image/png")),
                ("files", ("c.png", png_bytes(sheets[2][0]), "image/png")),
                ("files", ("broken.png", b"not an image", "image/png")),
            ],
        )
        assert response.json()["total_files"] == 4
        client.post(f"/jobs/{job_id}/start")
        job = wait_for_job(client, job_id)
        assert job["state"] == "completed"
        assert job["processed_files"] == 4
        assert job["counts"].get("error") == 1
        assert sum(job["counts"].values()) == 4

        response = client.get(f"/jobs/{job_id}/results.csv")
        assert response.status_code == 200
        rows = list(csv.DictReader(io.StringIO(response.text)))
        assert [r["file_name"] for r in rows] == [
            "a.png",
            "b.png",
            "c.png",
            "broken.png",
        ]
        for row, (_, truth) in zip(rows, sheets):
            assert row["status"] in ("ok", "needs_review")
            for label, answer in truth["answers"].items():
                assert row[label] == answer, label
        assert rows[3]["status"] == "error"

        # Folder-based job
        response = client.post(
            "/jobs", data={"template_id": template_id, "folder": str(folder)}
        )
        assert response.status_code == 201, response.text
        job = wait_for_job(client, response.json()["id"])
        assert job["state"] == "completed" and job["total_files"] == 2
        scans = client.get(f"/scans?job_id={job['id']}").json()
        assert scans["total"] == 2
        assert len(client.get("/jobs").json()["jobs"]) == 2


def wait_for_job(client, job_id, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/jobs/{job_id}").json()
        if job["state"] not in ("queued", "running", "uploading"):
            return job
        time.sleep(0.1)
    raise AssertionError("job did not finish")


def test_api_key_enforced(tmp_path, spec):
    with make_client(tmp_path, api_key="secret") as client:
        assert client.get("/health").status_code == 200
        assert client.get("/templates").status_code == 401
        assert (
            client.get("/templates", headers={"X-API-Key": "wrong"}).status_code == 401
        )
        assert (
            client.get("/templates", headers={"X-API-Key": "secret"}).status_code == 200
        )
        assert client.get("/templates?api_key=secret").status_code == 200
        assert client.get("/").status_code == 200  # the GUI itself asks for the key
        upload_template(client, spec, headers={"X-API-Key": "secret"})


def test_static_gui_and_capabilities(tmp_path):
    with make_client(tmp_path) as client:
        for path in ("/", "/ui"):
            response = client.get(path)
            assert response.status_code == 200
            assert "<html" in response.text.lower()
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/static/app.css").status_code == 200
        caps = client.get("/capabilities").json()
        assert any("128" in f for f in caps["barcode_formats"])
        assert "QTYPE_MCQ4" in caps["field_types"]
        assert client.get("/docs").status_code == 200


def test_generate_template(tmp_path, spec, monkeypatch):
    import sys
    import types

    calls = {}

    class FakeResult:
        def __init__(self, images):
            self.template = spec.to_template(pre_processors=[])
            self.reference_image = images[0]
            self.report = {"warnings": [], "needs_verification": [{"name": "q1"}]}

    def fake_generate(images, labels, options):
        calls["labels"] = labels
        calls["options"] = options
        return FakeResult(images)

    module = types.ModuleType("src.template_gen")
    module.generate_template = fake_generate
    monkeypatch.setitem(sys.modules, "src.template_gen", module)
    image, truth = make_sheet(spec, seed=1)
    labels_csv = "file,q1,q2\ns1.png,A,B\ns2.png,C,D\n"
    with make_client(tmp_path) as client:
        response = client.post(
            "/templates/generate",
            files=[
                ("files", ("s1.png", png_bytes(image), "image/png")),
                ("files", ("s2.png", png_bytes(image), "image/png")),
                ("labels", ("labels.csv", labels_csv, "text/csv")),
            ],
            data={"name": "Draft", "options": json.dumps({"k": 1})},
        )
        assert response.status_code == 201, response.text
        detail = response.json()
        assert detail["status"] == "draft"
        assert detail["report"]["needs_verification"] == [{"name": "q1"}]
        assert detail["reference_url"]
        assert calls["labels"] == [{"q1": "A", "q2": "B"}, {"q1": "C", "q2": "D"}]
        assert calls["options"] == {"k": 1}
        reference = client.get(detail["reference_url"])
        assert reference.status_code == 200
        # Saving from the editor turns the draft into a ready template
        response = client.put(f"/templates/{detail['id']}", json=detail["template"])
        assert response.status_code == 200
        assert response.json()["status"] == "ready"


def test_generate_template_unavailable(tmp_path, spec, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "src.template_gen", None)
    image, _ = make_sheet(spec, seed=1)
    with make_client(tmp_path) as client:
        response = client.post(
            "/templates/generate",
            files=[("files", ("s1.png", png_bytes(image), "image/png"))],
        )
        assert response.status_code == 501


def test_parse_labels_csv_by_order():
    labels = parse_labels_csv(b"q1,q2\nA,B\nC,\n", ["x.png", "y.png"])
    assert labels == [{"q1": "A", "q2": "B"}, {"q1": "C", "q2": ""}]


def test_filename_sanitized(tmp_path, spec):
    image, _ = make_sheet(spec, seed=2)
    with make_client(tmp_path) as client:
        template_id = upload_template(client, spec)
        result = scan_one(client, template_id, image, name="../../etc/passwd.png")
        assert result["file_name"] == "passwd.png"
        assert Path(result["input_path"]).parent.name == result["scan_id"]


def test_browser_engine_cors_and_template_files(tmp_path, spec):
    origin = "https://exams.example.com"
    with make_client(tmp_path, cors_origins=[origin]) as client:
        page = client.get("/browser/")
        assert page.status_code == 200
        assert "<html" in page.text.lower()
        assert client.get("/browser/omr.js").status_code == 200

        template_id = upload_template(client, spec)
        assert (
            client.get(f"/templates/{template_id}/files/template.json").status_code
            == 200
        )
        assert (
            client.get(f"/templates/{template_id}/files/../meta.json").status_code
            == 404
        )
        assert (
            client.get(f"/templates/{template_id}/files/missing.png").status_code == 404
        )

        preflight = client.options(
            "/scans",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "x-api-key",
            },
        )
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == origin
        other = client.get("/health", headers={"Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in other.headers


def test_job_pool_follows_each_jobs_worker_count():
    from types import SimpleNamespace

    from src.api.jobs import JobManager

    manager = JobManager(None, None, None, SimpleNamespace(mp_start_method="spawn"))
    try:
        first = manager._get_pool(2)
        assert manager._get_pool(2) is first
        second = manager._get_pool(3)
        assert second is not first
        assert second._max_workers == 3
    finally:
        manager.pool.shutdown(wait=True)


def _xlsx(rows):
    openpyxl = pytest.importorskip("openpyxl")
    book = openpyxl.Workbook()
    for row in rows:
        book.active.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def test_parse_labels_xlsx_and_answer_strings():
    content = _xlsx([["File Name", "Roll"], ["b.jpg", "0123"], ["a.jpg", "0456"]])
    info = {}
    labels = parse_labels_csv(content, ["a.jpg", "b.jpg"], "labels.xlsx", info)
    assert labels == [{"Roll": "0456"}, {"Roll": "0123"}]
    assert info["file_column"] == "File Name"
    labels = parse_labels_csv(b"file name,ANS\nx.png,AB *\n", ["x.png"], "l.csv")
    assert labels == [{"q1": "A", "q2": "B", "q3": "", "q4": "*"}]


def test_generator_routes(tmp_path, spec, monkeypatch):
    import sys
    import types

    blank, _ = make_sheet(spec, seed=3)

    class FakeResult:
        def __init__(self, images):
            self.template = spec.to_template()
            timing = self.template["preProcessors"][0]["options"]
            corner = spec.timing_tracks["left"][0]
            timing["indexPoints"] = [
                {"name": "P1", "center": [float(v) for v in corner], "size": list(timing["markDimensions"])}
            ]
            self.reference_image = blank
            self.config = {"review_params": {"max_unmarked_fill_ratio": 0.35}}
            self.report = {
                "warnings": ["timing tracks look the same upside down", "other"],
                "needs_verification": [],
            }

    module = types.ModuleType("src.template_gen")
    module.generate_template = lambda images, labels, options: FakeResult(images)
    monkeypatch.setitem(sys.modules, "src.template_gen", module)
    image, _ = make_sheet(spec, seed=4)
    labels = _xlsx([["File-Name", "q1"], ["s1.png", "A"]])
    with make_client(tmp_path) as client:
        response = client.post(
            "/templates/generate",
            files=[
                ("files", ("s1.png", png_bytes(image), "image/png")),
                ("labels", ("labels.xlsx", labels, "application/octet-stream")),
            ],
        )
        assert response.status_code == 201, response.text
        detail = response.json()
        tid = detail["id"]
        assert detail["config"] == {"review_params": {"max_unmarked_fill_ratio": 0.35}}
        assert detail["report"]["sheet_names"] == ["s1.png"]
        assert detail["report"]["labels"]["format"] == "xlsx"
        monkeypatch.delitem(sys.modules, "src.template_gen")

        left = np.array(spec.timing_tracks["left"])
        x0, y0 = left.min(axis=0) - 20
        x1, y1 = left.max(axis=0) + 20
        response = client.post(
            f"/templates/{tid}/generator/find-track",
            json={"box": [float(x0), float(y0), float(x1 - x0), float(y1 - y0)]},
        )
        assert response.status_code == 200, response.text
        assert len(response.json()["marks"]) == len(left)
        response = client.post(
            f"/templates/{tid}/generator/find-mark",
            json={"point": [float(left[0][0]), float(left[0][1])]},
        )
        assert response.status_code == 200, response.text
        assert np.hypot(*(np.array(response.json()["center"]) - left[0])) < 3

        response = client.post(f"/templates/{tid}/generator/test-alignment")
        assert response.status_code == 200, response.text
        sheet = response.json()["sheets"][0]
        assert sheet["file"] == "s1.png" and sheet["status"] != "error"
        assert sheet["found"] >= 0.9 * sheet["expected"] > 0

        response = client.post(
            f"/templates/{tid}/generator/acknowledge", json={"warnings": ["other"]}
        )
        assert response.json()["warnings"] == ["timing tracks look the same upside down"]
        assert client.get(f"/templates/{tid}").json()["report"]["acknowledged_warnings"] == ["other"]
        response = client.post(f"/templates/{tid}/generator/acknowledge", json={"undo": True})
        assert len(response.json()["warnings"]) == 2

        verify = f"/templates/{tid}/generator/verify"
        response = client.post(verify, json={"items": ["q1: values", "q2: order"]})
        assert response.json()["verified_items"] == ["q1: values", "q2: order"]
        assert client.get(f"/templates/{tid}").json()["report"]["verified_items"] == [
            "q1: values",
            "q2: order",
        ]
        response = client.post(verify, json={"items": ["q1: values"], "undo": True})
        assert response.json()["verified_items"] == ["q2: order"]
        response = client.post(verify, json={"undo": True})
        assert response.json()["verified_items"] == []
        calibrated = client.post(f"/templates/{tid}/generator/calibrate-index")
        assert calibrated.status_code == 200, calibrated.text
        point = calibrated.json()["points"][0]
        assert point["found"] == 1 and point["spread"] == 0 and point["moved"] < 3
        boxes = client.get(f"/templates/{tid}/generator/printed-boxes")
        assert boxes.status_code == 200 and isinstance(boxes.json()["boxes"], list)


def test_primary_key_duplicates(tmp_path, spec):
    template = spec.to_template(pre_processors=[])
    template["primaryKey"] = ["roll1", "roll2"]
    image, _ = make_sheet(spec, 3)
    other, _ = make_sheet(spec, 4)
    with make_client(tmp_path) as client:
        response = client.post(
            "/templates",
            files=[("files", ("template.json", json.dumps(template), "application/json"))],
            data={"name": "Keyed"},
        )
        tid = response.json()["id"]
        first = scan_one(client, tid, image, "a.png")
        assert first["primary_key"]
        second = scan_one(client, tid, image, "b.png")
        scan_one(client, tid, other, "c.png")
        listing = client.get(f"/results?template_id={tid}&view=duplicates").json()
        assert {item["id"] for item in listing["items"]} == {
            first["scan_id"],
            second["scan_id"],
        }
        overlay = client.get(f"/scans/{first['scan_id']}/overlay").json()
        assert [d["scan_id"] for d in overlay["duplicates"]] == [second["scan_id"]]
        # Deleting one clears the flag on the other
        assert client.delete(f"/results/{second['scan_id']}").status_code < 300
        listing = client.get(f"/results?template_id={tid}&view=duplicates").json()
        assert listing["items"] == []


def test_failed_sheet_aligned_by_hand_or_typed(tmp_path, spec):
    template = spec.to_template()
    image, truth = make_sheet(spec, 5)
    blank = image.copy()
    options = template["preProcessors"][0]["options"]
    w, h = options["markDimensions"]
    for track in options["tracks"].values():
        for x, y in track["marks"]:
            cv2.rectangle(blank, (int(x - w), int(y - h)), (int(x + w), int(y + h)), (255, 255, 255), -1)
    with make_client(tmp_path) as client:
        response = client.post(
            "/templates",
            files=[("files", ("template.json", json.dumps(template), "application/json"))],
            data={"name": "Tracks"},
        )
        tid = response.json()["id"]
        failed = scan_one(client, tid, blank, "failed.png")
        assert failed["status"] == "error"
        sid = failed["scan_id"]
        targets = client.get(f"/scans/{sid}/manual-align").json()
        pw, ph = targets["page_size"]
        assert [pw, ph] == [blank.shape[1], blank.shape[0]]
        assert client.get(f"/scans/{sid}/views/original").status_code == 200
        corners = [[0, 0], [pw - 1, 0], [pw - 1, ph - 1], [0, ph - 1]]
        response = client.post(f"/scans/{sid}/manual-align", json={"points": corners})
        assert response.status_code == 200, response.text
        stored = client.get(f"/scans/{sid}").json()
        assert stored["status"] != "error" and stored["manual_alignment"]["kind"] == "corners"
        wrong = {k: (v, stored["responses"].get(k)) for k, v in truth["answers"].items() if stored["responses"].get(k) != v}
        assert len(wrong) <= 1, wrong
        assert client.get(f"/scans/{sid}/render").json()["geometry_replayed"] is True

        other = scan_one(client, tid, blank, "typed.png")
        response = client.post(
            f"/scans/{other['scan_id']}/manual-values", json={"values": {"q1": "B"}}
        )
        assert response.status_code == 200, response.text
        typed = client.get(f"/scans/{other['scan_id']}").json()
        assert typed["status"] == "ok" and typed["responses"]["q1"] == "B"
        assert typed["manual_entry"] and typed["read_error"]


def test_generator_failure_still_opens_a_draft(tmp_path, spec, monkeypatch):
    import importlib

    # The module the endpoint imports (other tests swap sys.modules entries,
    # which can leave the package attribute pointing at a different copy)
    template_gen = importlib.import_module("src.template_gen")

    def boom(*args, **kwargs):
        raise ValueError("no luck")

    monkeypatch.setattr(template_gen, "generate_template", boom)
    image, _ = make_sheet(spec, 8)
    with make_client(tmp_path) as client:
        response = client.post(
            "/templates/generate",
            files=[("files", ("s1.png", png_bytes(image), "image/png"))],
        )
        assert response.status_code == 201, response.text
        detail = response.json()
        assert "no luck" in detail["report"]["warnings"][0]
        assert client.get(f"/templates/{detail['id']}/reference.png").status_code == 200
