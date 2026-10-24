"""Watched folders: new complete files are read; half-copied ones wait."""

import os
import time

import cv2
import pytest

from src.api import watch
from src.tests.test_api import make_client, make_sheet, png_bytes, upload_template
from src.synth import default_spec

SUFFIXES = {".png", ".jpg", ".pdf"}


@pytest.fixture(scope="module")
def spec():
    return default_spec(questions=10, roll_digits=3, with_zones=False)


def age(path, seconds):
    then = time.time() - seconds
    os.utime(path, (then, then))


def test_files_are_handed_over_only_when_complete(tmp_path, monkeypatch):
    monkeypatch.setattr(watch, "SETTLE_S", 1)
    folder = tmp_path / "in"
    folder.mkdir()
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    old = folder / "old.png"
    old.write_bytes(b"\x89PNG....IEND\xaeB`\x82")
    (jobs / "j.files").write_text(f"{old}\n")
    state = watch.WatchState(jobs / "j.watch", jobs / "j.files")

    # A JPEG still being copied: no end marker yet
    half = folder / "a.jpg"
    half.write_bytes(b"\xff\xd8" + b"x" * 100)
    (folder / "b.jpg.part").write_bytes(b"\xff\xd8")
    (folder / "~$c.pdf").write_bytes(b"%PDF")
    age(half, 10)
    listing = watch.list_inputs(folder, True, SUFFIXES)
    assert set(listing) == {str(old), str(half)}
    assert state.ready(listing, 0) == []  # first look
    assert state.ready(listing, 5) == []  # stable but incomplete
    # The copy finishes: new size, so it settles again before it is read
    half.write_bytes(b"\xff\xd8" + b"x" * 100 + b"\xff\xd9")
    age(half, 10)
    listing = watch.list_inputs(folder, True, SUFFIXES)
    assert state.ready(listing, 6) == []
    assert state.ready(listing, 6.5) == []  # not settled for SETTLE_S yet
    assert state.ready(listing, 7.5) == [str(half)]
    assert state.ready(listing, 20) == []  # read once

    # Remembered across a restart; a replaced file is read again
    again = watch.WatchState(jobs / "j.watch", jobs / "j.files")
    listing = watch.list_inputs(folder, True, SUFFIXES)
    assert again.ready(listing, 0) == []
    half.write_bytes(b"\xff\xd8" + b"y" * 50 + b"\xff\xd9")
    age(half, 10)
    listing = watch.list_inputs(folder, True, SUFFIXES)
    assert again.ready(listing, 1) == []
    assert again.ready(listing, 3) == [str(half)]


def test_incomplete_file_is_read_after_giving_up(tmp_path, monkeypatch):
    monkeypatch.setattr(watch, "SETTLE_S", 1)
    folder = tmp_path / "in"
    folder.mkdir()
    broken = folder / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4 cut off")
    age(broken, 10)
    state = watch.WatchState(tmp_path / "j.watch", tmp_path / "missing.files")
    listing = watch.list_inputs(folder, False, SUFFIXES)
    assert state.ready(listing, 0) == []
    assert state.ready(listing, 5) == []
    assert state.ready(listing, watch.GIVE_UP_S + 1) == [str(broken)]


def test_watch_job_reads_new_files_until_stopped(tmp_path, spec, monkeypatch):
    monkeypatch.setattr(watch, "SETTLE_S", 0.2)
    folder = tmp_path / "in"
    folder.mkdir()
    image, _ = make_sheet(spec, 1)
    (folder / "first.png").write_bytes(png_bytes(image))
    with make_client(tmp_path) as client:
        template_id = upload_template(client, spec)
        response = client.post(
            "/jobs",
            data={"template_id": template_id, "folder": str(folder), "watch": "true", "watch_interval_s": "1"},
        )
        assert response.status_code == 201, response.text
        job_id = response.json()["id"]

        def wait(condition, timeout=60):
            deadline = time.time() + timeout
            while time.time() < deadline:
                job = client.get(f"/jobs/{job_id}").json()
                if condition(job):
                    return job
                time.sleep(0.2)
            raise AssertionError(f"timed out: {job}")

        job = wait(lambda j: j["state"] == "watching" and j["processed_files"] == 1)
        assert job["options"]["watch"]["active"]
        for name in ("second.png", "third.png"):
            ok, buffer = cv2.imencode(".png", make_sheet(spec, len(name))[0])
            (folder / name).write_bytes(buffer.tobytes())
        job = wait(lambda j: j["state"] == "watching" and j["processed_files"] == 3)
        assert client.get(f"/scans?job_id={job_id}").json()["total"] == 3

        stopped = client.post(f"/jobs/{job_id}/stop-watching").json()
        assert stopped["state"] == "completed"
        assert not stopped["options"]["watch"]["active"]
        (folder / "late.png").write_bytes(png_bytes(image))
        time.sleep(2)
        assert client.get(f"/jobs/{job_id}").json()["total_files"] == 3

        # Watching needs a folder
        response = client.post("/jobs", data={"template_id": template_id, "watch": "true"})
        assert response.status_code == 400
