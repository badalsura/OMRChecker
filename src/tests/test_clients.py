"""
End-to-end tests of the Python, Go and Java API clients (clients/) against a
real API server started in a subprocess on a free port.

Go and Java checks are skipped when their toolchains are missing.
"""

import csv
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import cv2
import pytest

from src.synth.render import default_spec, random_answers, render_sheet

ROOT = Path(__file__).resolve().parents[2]
CLIENTS = ROOT / "clients"
sys.path.insert(0, str(CLIENTS / "python"))

from omr_client import OMRApiError, OMRClient, encode_multipart  # noqa: E402


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def assets(tmp_path_factory):
    folder = tmp_path_factory.mktemp("client_assets")
    spec = default_spec(questions=20, roll_digits=4, with_zones=False)
    template = folder / "template.json"
    template.write_text(json.dumps(spec.to_template(pre_processors=[])))
    sheets, truths = [], []
    for seed in range(3):
        rng = random.Random(seed)
        image, truth = render_sheet(
            spec, random_answers(spec, rng, blank_rate=0.0), rng=rng
        )
        path = folder / "sheets" / f"sheet{seed}.png"
        path.parent.mkdir(exist_ok=True)
        cv2.imwrite(str(path), image)
        sheets.append(path)
        truths.append(truth["answers"])
    return {"template": template, "sheets": sheets, "truths": truths}


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("client_server_data")
    port = free_port()
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    log = open(data_dir / "server.log", "w")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "src.api",
            "--port",
            str(port),
            "--data-dir",
            str(data_dir / "omr"),
            "--workers",
            "2",
            "--log-level",
            "warning",
        ],
        cwd=ROOT,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 60
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError((data_dir / "server.log").read_text())
        try:
            urllib.request.urlopen(url + "/health", timeout=1).read()
            break
        except OSError:
            time.sleep(0.25)
    else:
        proc.kill()
        raise RuntimeError("API server did not start")
    yield url
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
    log.close()


def test_multipart_encoder():
    body, content_type = encode_multipart(
        {"a": "1", "flag": False, "skip": None}, [("files", ("x.png", b"\x89PNG"))]
    )
    boundary = content_type.split("boundary=")[1]
    assert body.endswith(f"--{boundary}--\r\n".encode())
    assert b'name="flag"\r\n\r\nfalse' in body
    assert b"skip" not in body
    assert b'filename="x.png"\r\nContent-Type: image/png' in body


def test_python_client(server, assets, tmp_path):
    client = OMRClient(server)
    assert client.health()["status"] == "ok"
    assert any("128" in f for f in client.capabilities()["barcode_formats"])

    template = client.upload_template([assets["template"]], name="py-client")
    assert template["id"] in [t["id"] for t in client.list_templates()]

    result = client.scan(template["id"], [assets["sheets"][0]])["scans"][0]
    for label, answer in assets["truths"][0].items():
        assert result["responses"][label] == answer, label
    assert client.get_scan(result["scan_id"])["scan_id"] == result["scan_id"]

    # In-memory uploads work too
    data = assets["sheets"][1].read_bytes()
    result1 = client.scan(template["id"], [("mem.png", data)])["scans"][0]
    assert result1["file_name"] == "mem.png"

    job = client.create_job(
        template["id"], files=[assets["sheets"][0]], workers=2, start=False
    )
    assert job["state"] == "uploading"
    job = client.upload_job_files(job["id"], assets["sheets"][1:])
    assert job["total_files"] == 3
    client.start_job(job["id"])
    seen = []
    job = client.wait_for_job(job["id"], poll=0.2, callback=seen.append, timeout=120)
    assert job["state"] == "completed" and job["processed_files"] == 3
    assert seen

    out = client.job_results_csv(job["id"], tmp_path / "results.csv")
    rows = list(csv.DictReader(out.open()))
    assert [r["file_name"] for r in rows] == ["sheet0.png", "sheet1.png", "sheet2.png"]
    for row, truth in zip(rows, assets["truths"]):
        for label, answer in truth.items():
            assert row[label] == answer, label

    # Server-side folder job
    job = client.create_job(template["id"], folder=str(assets["sheets"][0].parent))
    job = client.wait_for_job(job["id"], poll=0.2, timeout=120)
    assert job["state"] == "completed" and job["total_files"] == 3

    queue = client.review_queue(job_id=job["id"])
    assert "items" in queue and "total" in queue
    first = assets["truths"][0]
    reviewed = client.submit_review(
        result["scan_id"], corrections={"q1": first["q1"]}, accept=["q2"]
    )
    assert reviewed["reviewed"] is True

    with pytest.raises(OMRApiError) as error:
        client.job_status("doesnotexist")
    assert error.value.status == 404


def test_python_client_results_and_exports(server, assets, tmp_path):
    client = OMRClient(server, user="py-tester")
    template = client.upload_template([assets["template"]], name="py-results")
    scan = client.scan(template["id"], [assets["sheets"][0]])["scans"][0]
    scan_id = scan["scan_id"]
    truth = assets["truths"][0]

    sheet = client.render(scan_id)
    assert sheet["image_source"] == "source" and sheet["width"] > 0
    assert client.render_image(scan_id)[:2] == b"\xff\xd8"
    other = "B" if truth["q1"] != "B" else "C"
    updated = client.correct(scan_id, toggle=[("q1", truth["q1"]), ("q1", other)])
    q1 = next(f for f in updated["fields"] if f["name"] == "q1")
    assert q1["value"] == other and q1["original_value"] == truth["q1"]
    assert client.verify(scan_id)["verified"]["by"] == "py-tester"
    assert client.audit(scan_id)["items"][0]["user"] == "py-tester"
    accuracy = client.accuracy(template_id=template["id"])
    assert accuracy["verified_sheets"] == 1
    listed = list(client.iter_results(page_size=1, template_id=template["id"]))
    assert [row["id"] for row in listed] == [scan_id]
    preview = client.regrade(
        scan_id, {"fieldBlocks": {"MCQ_1": {"bubbleValues": ["D", "C", "B", "A"]}}}
    )
    assert preview["applied"] is False and preview["changes"]
    assert (
        client.set_path_remap(["/nowhere=/elsewhere"])["rules"][0]["to"] == "/elsewhere"
    )
    client.set_path_remap([])

    record = client.export(
        {"template_id": template["id"]},
        "csv",
        tmp_path / "out.csv",
        profile={
            "fields": [{"field": "q1", "header": "Question 1"}],
            "meta": ["file_name"],
        },
        poll=0.2,
        timeout=60,
    )
    rows = list(csv.DictReader(open(record["path"], encoding="utf-8-sig")))
    assert rows == [
        {
            "file_name": "sheet0.png",
            "review_status": "verified",
            "corrected": "true",
            "Question 1": other,
        }
    ]
    with pytest.raises(OMRApiError):
        client.export(
            {"template_id": template["id"]},
            "csv",
            tmp_path / "bad.csv",
            profile={"fields": [{"field": "q1", "type": "int"}], "strictCast": True},
            poll=0.2,
            timeout=60,
        )


def test_python_client_housekeeping(server, assets):
    client = OMRClient(server, user="py-keeper")
    template = client.upload_template([assets["template"]], name="py-keeper")
    folder = str(assets["sheets"][0].parent)
    check = client.check_folder(folder, recursive=False)
    assert check["ok"] and check["images"] >= 3
    assert client.browse_folder(folder)["path"]
    job = client.create_job(template["id"], folder=folder, recursive=False)
    job = client.wait_for_job(job["id"], poll=0.2, timeout=120)
    assert job["state"] == "completed"
    assert folder in client.recent_folders()
    counts = client.review_counts(job_id=job["id"])
    assert "total" in counts and "now" in counts
    accepted = client.accept_review_bulk(job_id=job["id"], before=counts["now"])
    assert accepted["remaining"] == 0 and accepted["by"] == "py-keeper"
    scan_id = client.list_results(job_id=job["id"])["items"][0]["id"]
    assert client.delete_scan(scan_id)["deleted"] == scan_id
    deleted = client.delete_job(job["id"])
    assert deleted["scans"] == job["total_files"] - 1
    assert all(p.exists() for p in assets["sheets"])


def test_python_bulk_folder_cli(server, assets, tmp_path):
    out = tmp_path / "bulk.csv"
    proc = subprocess.run(
        [
            sys.executable,
            str(CLIENTS / "python" / "bulk_folder.py"),
            "--url",
            server,
            "--template",
            str(assets["template"]),
            "--folder",
            str(assets["sheets"][0].parent),
            "--out",
            str(out),
            "--poll",
            "0.2",
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "completed: 3 files" in proc.stdout
    assert len(list(csv.DictReader(out.open()))) == 3


@pytest.mark.skipif(shutil.which("go") is None, reason="Go toolchain not installed")
def test_go_client(server, assets):
    env = dict(
        os.environ,
        OMR_API_URL=server,
        OMR_TEST_TEMPLATE=str(assets["template"]),
        OMR_TEST_SHEETS=os.pathsep.join(str(p) for p in assets["sheets"]),
        OMR_TEST_EXPECT=json.dumps(assets["truths"][0]),
        GOFLAGS="-mod=mod",
    )
    proc = subprocess.run(
        ["go", "test", "-count=1", "-v", "./..."],
        cwd=CLIENTS / "go" / "omrclient",
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    assert "--- PASS: TestAgainstServer" in output, output
    # The example compiles too
    proc = subprocess.run(
        ["go", "vet", "./..."],
        cwd=CLIENTS / "go" / "omrclient",
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.skipif(
    shutil.which("javac") is None or shutil.which("java") is None,
    reason="JDK not installed",
)
def test_java_client(server, assets, tmp_path):
    sources = sorted(str(p) for p in (CLIENTS / "java" / "src").rglob("*.java"))
    classes = tmp_path / "classes"
    proc = subprocess.run(
        ["javac", "--release", "11", "-d", str(classes)] + sources,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    proc = subprocess.run(
        [
            "java",
            "-cp",
            str(classes),
            "io.omrchecker.client.examples.SmokeCheck",
            server,
            str(assets["template"]),
            os.pathsep.join(str(p) for p in assets["sheets"]),
            json.dumps(assets["truths"][0]),
            str(tmp_path / "java.csv"),
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "JAVA CLIENT OK" in proc.stdout
