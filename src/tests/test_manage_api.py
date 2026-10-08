"""Folder picker, deletes, bulk accept, queue polling and the original/colour views."""

import json
import random
import time

import cv2
import numpy as np
import pytest

from src.api.manage_routes import workers_warning
from src.api.results import group_highlights
from src.api.storage import read_json, write_json_atomic
from src.api.views_routes import map_overlay
from src.geometry import empty_geometry, map_points, warp_to_aligned
from src.synth.render import default_spec, random_answers, render_sheet
from src.tests.test_api import (
    make_client,
    png_bytes,
    scan_one,
    upload_template,
    wait_for_job,
)


@pytest.fixture(scope="module")
def spec():
    return default_spec(questions=20, roll_digits=4, with_zones=False)


def flagged_sheet(spec, seed=7):
    rng = random.Random(seed)
    answers = random_answers(spec, rng, blank_rate=0.0)
    answers["q3"] = "AC"  # multi-mark goes to review
    answers["q5"] = "BD"
    image, _ = render_sheet(spec, answers, rng=rng)
    return image


# ---------------------------------------------------------------------------
# folder picker
# ---------------------------------------------------------------------------
def test_folder_picker_is_limited_to_allowed_dirs(tmp_path, spec):
    allowed = tmp_path / "scans"
    (allowed / "batch1" / "sub").mkdir(parents=True)
    (allowed / "empty").mkdir()
    image = flagged_sheet(spec)
    cv2.imwrite(str(allowed / "batch1" / "a.png"), image)
    cv2.imwrite(str(allowed / "batch1" / "b.jpg"), image)
    (allowed / "batch1" / "c.pdf").write_bytes(b"%PDF-1.4")
    (allowed / "batch1" / "notes.txt").write_text("x")
    cv2.imwrite(str(allowed / "batch1" / "sub" / "d.png"), image)
    outside = tmp_path / "private"
    outside.mkdir()
    with make_client(tmp_path, allowed_dirs=[allowed.resolve()]) as client:
        roots = client.get("/fs/roots").json()
        assert roots["restricted"] is True
        assert [r["path"] for r in roots["roots"]] == [str(allowed.resolve())]

        listing = client.get("/fs/browse", params={"path": str(allowed)}).json()
        names = [f["name"] for f in listing["folders"]]
        assert names == ["batch1", "empty"]
        batch = listing["folders"][0]
        assert (batch["images"], batch["pdfs"]) == (2, 1)
        # The allowed root is the top: no way up out of it
        assert listing["parent"] is None
        inner = client.get("/fs/browse", params={"path": batch["path"]}).json()
        assert inner["parent"] == str(allowed.resolve())

        refused = client.get("/fs/browse", params={"path": str(outside)})
        assert refused.status_code == 403
        assert "Outside the allowed folders" in refused.json()["detail"]
        missing = client.get("/fs/browse", params={"path": str(allowed / "nope")})
        assert missing.status_code == 404
        assert missing.json()["detail"] == "Folder not found"
        sneaky = client.get(
            "/fs/browse", params={"path": str(allowed / ".." / "private")}
        )
        assert sneaky.status_code == 403

        check = client.get("/fs/check", params={"path": str(allowed / "batch1")}).json()
        assert check["ok"] and (check["images"], check["pdfs"]) == (3, 1)
        flat = client.get(
            "/fs/check",
            params={"path": str(allowed / "batch1"), "recursive": "false"},
        ).json()
        assert (flat["images"], flat["pdfs"]) == (2, 1)
        empty = client.get("/fs/check", params={"path": str(allowed / "empty")}).json()
        assert empty["ok"] is False and "No images" in empty["error"]
        bad = client.get("/fs/check", params={"path": str(outside)}).json()
        assert bad["ok"] is False and "Outside" in bad["error"]

        # Include subfolders off: only the top folder is read; recent folders
        template_id = upload_template(client, spec)
        response = client.post(
            "/jobs",
            data={
                "template_id": template_id,
                "folder": str(allowed / "batch1" / "sub"),
            },
        )
        assert response.status_code == 201
        wait_for_job(client, response.json()["id"])
        response = client.post(
            "/jobs",
            data={
                "template_id": template_id,
                "folder": str(allowed / "batch1"),
                "recursive": "false",
                "workers": "1",
            },
        )
        job = response.json()
        assert job["total_files"] == 3 and job["recursive"] is False
        wait_for_job(client, job["id"])
        recent = client.get("/fs/recent").json()["folders"]
        assert recent[:2] == [
            str((allowed / "batch1").resolve()),
            str((allowed / "batch1" / "sub").resolve()),
        ]


def test_folder_roots_without_restriction(tmp_path):
    with make_client(tmp_path) as client:
        roots = client.get("/fs/roots").json()
        assert roots["restricted"] is False and roots["roots"]
        assert client.get("/capabilities").json()["cpu_count"] >= 1


def test_workers_warning_is_only_a_warning(tmp_path, spec):
    assert workers_warning(2, cpus=4) is None
    assert workers_warning(None, cpus=4) is None
    assert "8 worker processes on 4 CPU cores" in workers_warning(8, cpus=4)
    with make_client(tmp_path) as client:
        template_id = upload_template(client, spec)
        response = client.post(
            "/jobs",
            data={"template_id": template_id, "workers": "512", "start": "false"},
            files=[("files", ("a.png", png_bytes(flagged_sheet(spec)), "image/png"))],
        )
        assert response.status_code == 201
        assert "512 worker processes" in response.json()["warnings"][0]


# ---------------------------------------------------------------------------
# deletes
# ---------------------------------------------------------------------------
def test_delete_scan_is_audited(tmp_path, spec):
    with make_client(tmp_path) as client:
        template_id = upload_template(client, spec)
        keep = scan_one(client, template_id, flagged_sheet(spec, 1), "keep.png")
        gone = scan_one(client, template_id, flagged_sheet(spec, 2), "gone.png")
        scan_dir = client.app.state.ctx.data.scan_dir(gone["scan_id"])
        assert scan_dir.exists()
        response = client.delete(
            f"/results/{gone['scan_id']}", headers={"X-User": "asha"}
        )
        assert response.status_code == 200, response.text
        assert response.json()["by"] == "asha"
        assert not scan_dir.exists()
        assert client.get(f"/scans/{gone['scan_id']}").status_code == 404
        listed = client.get("/results").json()
        assert [i["id"] for i in listed["items"]] == [keep["scan_id"]]
        queue = client.get("/review").json()["items"]
        assert {i["scan_id"] for i in queue} == {keep["scan_id"]}
        audit = client.get("/audit").json()["items"]
        record = next(a for a in audit if a["kind"] == "delete")
        assert record["scan_id"] == gone["scan_id"] and record["user"] == "asha"
        assert json.loads(record["old"])["file_name"] == "gone.png"
        assert client.delete(f"/results/{gone['scan_id']}").status_code == 404


def test_delete_job_removes_its_scans_but_not_the_folder(tmp_path, spec):
    folder = tmp_path / "inbox"
    folder.mkdir()
    for index in range(2):
        cv2.imwrite(str(folder / f"s{index}.png"), flagged_sheet(spec, index))
    with make_client(tmp_path) as client:
        template_id = upload_template(client, spec)
        other = scan_one(client, template_id, flagged_sheet(spec, 9))
        response = client.post(
            "/jobs", data={"template_id": template_id, "folder": str(folder)}
        )
        job = wait_for_job(client, response.json()["id"])
        scans = client.get(f"/scans?job_id={job['id']}").json()["items"]
        assert len(scans) == 2
        response = client.delete(f"/jobs/{job['id']}", headers={"X-User": "ravi"})
        assert response.status_code == 200, response.text
        assert response.json()["scans"] == 2
        assert client.get(f"/jobs/{job['id']}").status_code == 404
        assert client.get(f"/scans?job_id={job['id']}").json()["total"] == 0
        for row in scans:
            assert client.get(f"/scans/{row['id']}").status_code == 404
        # The user's own files stay; other scans stay
        assert sorted(p.name for p in folder.iterdir()) == ["s0.png", "s1.png"]
        assert client.get(f"/scans/{other['scan_id']}").status_code == 200
        audit = client.get("/audit").json()["items"]
        record = next(a for a in audit if a["kind"] == "delete_job")
        assert record["job_id"] == job["id"] and record["user"] == "ravi"
        assert client.delete(f"/jobs/{job['id']}").status_code == 404


# ---------------------------------------------------------------------------
# review queue: bulk accept, polling, states
# ---------------------------------------------------------------------------
def test_accept_bulk_records_who_and_when(tmp_path, spec):
    with make_client(tmp_path) as client:
        template_id = upload_template(client, spec)
        first = scan_one(client, template_id, flagged_sheet(spec, 1))
        second = scan_one(client, template_id, flagged_sheet(spec, 2))
        counts = client.get("/review/counts", params={"name": "q3"}).json()
        assert counts["total"] == 2
        assert counts["all_names"] >= 4
        refused = client.post(
            "/review/accept-bulk", json={"name": "q3", "expected": 1}
        )
        assert refused.status_code == 409
        response = client.post(
            "/review/accept-bulk",
            json={"name": "q3", "expected": 2},
            headers={"X-User": "meena"},
        )
        body = response.json()
        assert response.status_code == 200, response.text
        assert body["accepted"] == 2 and body["remaining"] == 0
        for scan in (first, second):
            result = client.get(f"/scans/{scan['scan_id']}").json()
            log = result["review_log"]["q3"]
            assert log["reviewer"] == "meena" and log["action"] == "bulk_accepted"
            assert log["at"] > 0
            # Accepted as read, nothing deleted: the value is kept
            assert result["fields"]["q3"]["value"] == "AC"
            assert "q3" not in [i["name"] for i in result["review"]]
            assert "q5" in [i["name"] for i in result["review"]]
        audit = client.get("/audit", params={"user": "meena"}).json()["items"]
        assert sorted(a["kind"] for a in audit) == ["bulk_accept", "bulk_accept"]
        assert all(a["old"] == a["new"] == "AC" for a in audit)
        # No training samples for unchecked values
        training = client.app.state.ctx.data.training / "labels.jsonl"
        assert not training.exists() or "q3" not in training.read_text()


def test_queue_polling_and_states(tmp_path, spec):
    with make_client(tmp_path) as client:
        template_id = upload_template(client, spec)
        first = scan_one(client, template_id, flagged_sheet(spec, 1))
        queue = client.get("/review").json()
        since = queue["now"]
        # "now" leaves a small overlap; the screen drops items it already has
        again = client.get("/review", params={"created_after": since}).json()
        assert {i["scan_id"] for i in again["items"]} <= {first["scan_id"]}
        second = scan_one(client, template_id, flagged_sheet(spec, 2))
        new = client.get("/review", params={"created_after": since}).json()["items"]
        assert second["scan_id"] in {i["scan_id"] for i in new}
        assert {i["scan_id"] for i in new} <= {first["scan_id"], second["scan_id"]}
        counts = client.get("/review/counts", params={"since": since}).json()
        assert counts["new"] == len(new)
        later = client.get("/review", params={"created_after": time.time() + 5})
        assert later.json()["items"] == []

        # Another reviewer decides q3 of the first sheet
        client.post(
            f"/scans/{first['scan_id']}/review",
            json={"corrections": {"q3": "A"}},
            headers={"X-User": "other"},
        )
        states = client.post(
            "/review/states",
            json={
                "items": [
                    {"scan_id": first["scan_id"], "name": "q3"},
                    {"scan_id": first["scan_id"], "name": "q5"},
                    {"scan_id": "deadbeef", "name": "q1"},
                ]
            },
        ).json()["items"]
        assert states[0]["state"] == "done" and states[0]["by"] == "other"
        assert states[0]["value"] == "A"
        assert states[1]["state"] == "pending"
        assert states[2]["state"] == "gone"


# ---------------------------------------------------------------------------
# grouped values: per-column highlight
# ---------------------------------------------------------------------------
def test_group_columns_are_highlighted(tmp_path, spec):
    result = {
        "fields": {"roll1": {}, "roll2": {"reviewed": True}, "roll3": {}},
        "groups": {
            "RollNo": {
                "columns": [
                    {"name": "roll1", "state": "multi"},
                    {"name": "roll2", "state": "issue"},
                    {"name": "roll3", "state": "ok"},
                ]
            }
        },
        "review": [{"name": "Phone", "kind": "custom_label", "fields": ["p1", "p2"]}],
    }
    marks = group_highlights(result)
    assert marks == {
        "roll1": ["RollNo: multi"],
        "p1": ["Phone: needs review"],
        "p2": ["Phone: needs review"],
    }
    with make_client(tmp_path) as client:
        template_id = upload_template(client, spec)
        scan = scan_one(client, template_id, flagged_sheet(spec))
        path = client.app.state.ctx.data.scan_dir(scan["scan_id"]) / "result.json"
        stored = read_json(path)
        stored["groups"] = {"Roll": {"columns": [{"name": "roll2", "state": "multi"}]}}
        write_json_atomic(path, stored)
        overlay = client.get(f"/scans/{scan['scan_id']}/overlay").json()
        roll2 = next(f for f in overlay["fields"] if f["name"] == "roll2")
        assert roll2["group_flags"] == ["Roll: multi"]
        q1 = next(f for f in overlay["fields"] if f["name"] == "q1")
        assert q1["group_flags"] == []


# ---------------------------------------------------------------------------
# original and full-colour views replay the recorded geometry
# ---------------------------------------------------------------------------
def test_geometry_helpers_identity_and_resize():
    geometry = empty_geometry(100, 50)
    assert map_points(geometry, [[10, 20]]) == [[10.0, 20.0]]
    resized = {"source_size": [200, 100], "aligned_size": [100, 50]}
    # cv2.resize pixel-centre convention: x' = 0.5 * x - 0.25
    assert np.allclose(map_points(resized, [[100, 50]]), [[49.75, 24.75]])
    back = map_points(resized, [[49.75, 24.75]], "aligned_to_source")
    assert np.allclose(back, [[100.0, 50.0]])
    image = np.zeros((100, 200, 3), np.uint8)
    assert warp_to_aligned(resized, image).shape == (50, 100, 3)


def test_original_view_outlines_land_where_the_engine_read(tmp_path, spec):
    image = flagged_sheet(spec)
    colour = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR) if image.ndim == 2 else image
    colour = colour.copy()
    colour[:, :, 0] = (colour[:, :, 0] * 0.7).astype(np.uint8)  # pink paper
    colour[:, :, 1] = (colour[:, :, 1] * 0.8).astype(np.uint8)
    height, width = colour.shape[:2]
    with make_client(tmp_path) as client:
        template_id = upload_template(client, spec)
        scan = scan_one(client, template_id, image)
        scan_id = scan["scan_id"]
        ctx = client.app.state.ctx
        path = ctx.data.scan_dir(scan_id) / "result.json"
        stored = read_json(path)

        # Without a recorded transform (an old result): plain resize, with a warning
        stored.pop("geometry", None)
        write_json_atomic(path, stored)
        view = client.get(f"/scans/{scan_id}/views/original").json()
        assert view["geometry_recorded"] is False and view["warnings"]

        # A scanner shift + scale: the source is a bigger, shifted copy
        shift = np.array([[1.25, 0, 40], [0, 1.25, 30], [0, 0, 1]], np.float64)
        source = cv2.warpPerspective(
            colour,
            shift,
            (int(width * 1.25) + 80, int(height * 1.25) + 60),
            borderValue=(255, 255, 255),
        )
        cv2.imwrite(stored["source_path"], source)
        q1 = stored["fields"]["q1"]["bubbles"][0]
        stored["geometry"] = {
            "source_size": [source.shape[1], source.shape[0]],
            "rotation": 0,
            "page_homography": np.linalg.inv(shift).tolist(),
            "aligned_size": [width, height],
            "blocks": {
                "MCQ": {
                    "corners": [[10, 10], [110, 10], [110, 60], [10, 60]],
                    "status": "found",
                }
            },
        }
        write_json_atomic(path, stored)

        view = client.get(f"/scans/{scan_id}/views/original").json()
        assert view["geometry_recorded"] is True and not view["warnings"]
        assert (view["width"], view["height"]) == (source.shape[1], source.shape[0])
        quad = view["map"]["fields"]["q1"]["bubbles"][q1["value"]]
        expected = [
            [40 + 1.25 * q1["x"], 30 + 1.25 * q1["y"]],
            [40 + 1.25 * (q1["x"] + q1["w"]), 30 + 1.25 * q1["y"]],
        ]
        assert np.allclose(quad[:2], expected, atol=0.2)
        block = view["map"]["blocks"]["MCQ"]
        assert block["status"] == "found"
        assert np.allclose(block["corners"][0], [52.5, 42.5], atol=0.2)
        picture = client.get(view["image_url"])
        assert picture.status_code == 200
        decoded = cv2.imdecode(np.frombuffer(picture.content, np.uint8), 1)
        assert decoded.shape[:2] == source.shape[:2]

        # Full colour, aligned: the same transform back onto the page
        color = client.get(f"/scans/{scan_id}/views/color").json()
        assert (color["width"], color["height"]) == (width, height)
        png = client.get(f"/scans/{scan_id}/views/color/image?format=png")
        aligned = cv2.imdecode(np.frombuffer(png.content, np.uint8), 1)
        assert aligned.shape == colour.shape
        # Colour is kept (no dropout, no grey) and the page lines up
        inner = (slice(20, -20), slice(20, -20))
        difference = np.abs(aligned[inner].astype(int) - colour[inner].astype(int))
        assert difference.mean() < 6
        assert aligned[:, :, 2].mean() > aligned[:, :, 0].mean() + 20

        overlay = client.get(f"/scans/{scan_id}/overlay").json()
        assert overlay["geometry_recorded"] is True
        assert overlay["blocks"]["MCQ"]["status"] == "found"
        assert client.get(f"/scans/{scan_id}/views/nope").status_code == 404


def test_map_overlay_includes_zones():
    result = {
        "fields": {},
        "zones": {"roll": {"box": [10, 20, 30, 40]}},
    }
    mapped = map_overlay(result, {"source_size": [200, 200], "aligned_size": [100, 100]})
    # cv2.resize's pixel-centre convention moves points by half a pixel
    assert np.allclose(
        mapped["zones"]["roll"], [[20, 40], [80, 40], [80, 120], [20, 120]], atol=0.5
    )
