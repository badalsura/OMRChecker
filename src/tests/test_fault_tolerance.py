"""Bulk reading survives a crashed or hung reader process (src/batch.py,
src/api/jobs.py), resumes a stopped run and reads error sheets again."""

import json
import multiprocessing
import os
import time
from pathlib import Path

import cv2

from src import batch
from src.api.worker import job_task
from src.tests.test_api import (
    make_client,
    make_sheet,
    png_bytes,
    spec,  # noqa: F401 (fixture)
    upload_template,
    wait_for_job,
)


def flaky_read(task):
    """Stand-in reader: crashes on *crash*, hangs on *hang* (in a worker process)."""
    name = Path(task[1]).name
    if "crash" in name:
        os._exit(3)
    if "hang" in name:
        time.sleep(120)
    return [{"file": name}]


def test_bulk_reader_crash_and_hang_become_error_results():
    names = ["a", "b", "crash1", "c", "hang1", "d", "e", "f"]
    tasks = [(None, name, None) for name in names]
    # spawn: forking a test process that runs other threads can deadlock
    context = multiprocessing.get_context("spawn")
    results = list(
        batch._scan_parallel(tasks, 2, 10, read=flaky_read, mp_context=context)
    )
    got = [r.get("file") or r["file_id"] for r in results]
    assert got == names  # input order kept, nothing lost or doubled
    errors = {r["file_id"]: r["error"] for r in results if r.get("status") == "error"}
    assert set(errors) == {"crash1", "hang1"}
    assert "crashed" in errors["crash1"] and "longer than 10" in errors["hang1"]


def test_bulk_resume_keeps_read_sheets_and_rereads_errors(tmp_path, spec):
    folder = tmp_path / "scans"
    folder.mkdir()
    for seed in range(2):
        cv2.imwrite(str(folder / f"s{seed}.png"), make_sheet(spec, seed)[0])
    template = tmp_path / "template.json"
    template.write_text(json.dumps(spec.to_template(pre_processors=[])))
    out = tmp_path / "out"
    args = ["--template", str(template), "--input", str(folder), "--out", str(out), "--workers", "1"]
    batch.main(args)
    lines = (out / "results.jsonl").read_text().splitlines()
    assert len(lines) == 2
    # A crash mid-run: the second sheet ended in an error, a third line is cut short
    first = json.loads(lines[0])
    second = json.loads(lines[1])
    second.update(status="error", error="boom", responses={})
    (out / "results.jsonl").write_text(
        json.dumps(first) + "\n" + json.dumps(second) + "\n" + '{"file_id": "s'
    )
    cv2.imwrite(str(folder / "s2.png"), make_sheet(spec, 2)[0])
    batch.main(args + ["--resume"])
    records = [json.loads(l) for l in (out / "results.jsonl").read_text().splitlines()]
    assert sorted(r["file_id"] for r in records) == ["s0.png", "s1.png", "s2.png"]
    assert all(r["status"] != "error" for r in records)
    assert records[0] == first  # kept, not read again
    assert not (out / "results.jsonl.prev").exists()
    assert len((out / "results.csv").read_text().splitlines()) == 4


def crashing_job_task(task):
    if "crash" in task["file_name"]:
        os._exit(3)
    if "hang" in task["file_name"]:
        time.sleep(120)
    return job_task(task)


def test_job_survives_reader_crash_and_rescans_errors(tmp_path, spec):
    sheets = [make_sheet(spec, seed) for seed in range(3)]
    with make_client(tmp_path) as client:
        manager = client.app.state.ctx.jobs
        manager.task_fn = crashing_job_task
        template_id = upload_template(client, spec)
        files = [
            ("files", ("s0.png", png_bytes(sheets[0][0]), "image/png")),
            ("files", ("crash.png", png_bytes(sheets[1][0]), "image/png")),
            ("files", ("s2.png", png_bytes(sheets[2][0]), "image/png")),
        ]
        job_id = client.post(
            "/jobs", data={"template_id": template_id, "workers": "2"}, files=files
        ).json()["id"]
        job = wait_for_job(client, job_id)
        assert job["state"] == "completed", job["errors"]
        assert job["processed_files"] == 3
        # Time taken includes the reader start-up and carries on across runs
        assert job["elapsed_s"] > 0
        first_elapsed = job["elapsed_s"]
        assert job["counts"].get("error") == 1 and job["pages"] == 3, job["counts"]
        # The crash is fixed: Rescan errors reads only that sheet again
        manager.task_fn = job_task
        response = client.post(f"/jobs/{job_id}/rescan-errors")
        assert response.status_code == 200 and response.json()["rescan_files"] == 1
        job = wait_for_job(client, job_id)
        assert job["state"] == "completed" and not job["counts"].get("error"), job
        assert job["elapsed_s"] >= first_elapsed  # the rescan adds to it (rounded to 0.1 s)
        assert job["pages"] == 3  # replaced, not added
        result_files = list((tmp_path / "data" / "scans").rglob("result.json"))
        assert len(result_files) == 3


def test_job_skips_a_hung_sheet(tmp_path, spec):
    sheets = [make_sheet(spec, seed) for seed in range(3)]
    with make_client(tmp_path, sheet_timeout_s=8) as client:
        client.app.state.ctx.jobs.task_fn = crashing_job_task
        template_id = upload_template(client, spec)
        names = ["s0.png", "hang.png", "s2.png"]
        files = [("files", (n, png_bytes(img), "image/png")) for n, (img, _) in zip(names, sheets)]
        job_id = client.post(
            "/jobs", data={"template_id": template_id, "workers": "2"}, files=files
        ).json()["id"]
        job = wait_for_job(client, job_id)
        assert job["state"] == "completed" and job["pages"] == 3, job["errors"]
        assert job["counts"].get("error") == 1
        assert any("longer than 8" in error for error in job["errors"]), job["errors"]
