"""Results screen API: render, corrections, verification, regrade, remap, accuracy."""

import json
import random
import shutil
import sqlite3
from pathlib import Path

import cv2
import numpy as np
import pytest

from src.api.results import remap_path, resolve_source, toggled_value
from src.api.storage import ScanIndex
from src.synth.render import default_spec, random_answers, render_sheet
from src.tests.test_api import make_client, png_bytes, wait_for_job


@pytest.fixture(scope="module")
def spec():
    return default_spec(questions=20, roll_digits=4, with_zones=False)


def template_json(spec):
    template = spec.to_template(pre_processors=[])
    template["customLabels"] = {"RollNo": ["roll1..4"]}
    return template


def upload(client, template):
    response = client.post(
        "/templates",
        files=[("files", ("template.json", json.dumps(template), "application/json"))],
        data={"name": "Exam R"},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def sheet(spec, seed, **overrides):
    rng = random.Random(seed)
    answers = random_answers(spec, rng, blank_rate=0.0)
    answers.update(overrides)
    image, truth = render_sheet(spec, answers, rng=rng)
    return image, truth


def scan(client, template_id, image, name="sheet.png"):
    response = client.post(
        "/scans",
        data={"template_id": template_id},
        files=[("files", (name, png_bytes(image), "image/png"))],
    )
    assert response.status_code == 200, response.text
    return response.json()["scans"][0]


def test_scan_records_source_and_template_version(tmp_path, spec):
    image, _ = sheet(spec, 1)
    with make_client(tmp_path) as client:
        template_id = upload(client, template_json(spec))
        result = scan(client, template_id, image)
        assert Path(result["source_path"]).is_absolute()
        assert Path(result["source_path"]).exists()
        version = result["template_version"]
        assert version and len(version) == 16
        archived = tmp_path / "data" / "template_versions" / template_id / version
        assert (archived / "template.json").exists()


def test_render_overlay_and_image(tmp_path, spec):
    image, truth = sheet(spec, 2)
    with make_client(tmp_path) as client:
        template_id = upload(client, template_json(spec))
        result = scan(client, template_id, image)
        scan_id = result["scan_id"]
        rendered = client.get(f"/scans/{scan_id}/render").json()
        assert rendered["image_source"] == "source"
        assert rendered["exact_template"] is True
        assert rendered["drift"] == []
        assert rendered["width"] == spec.page[0] and rendered["height"] == spec.page[1]
        q1 = next(f for f in rendered["fields"] if f["name"] == "q1")
        assert [b["value"] for b in q1["bubbles"]] == ["A", "B", "C", "D"]
        assert [b["value"] for b in q1["bubbles"] if b["marked"]] == [
            truth["answers"]["q1"]
        ]
        bubble = q1["bubbles"][0]
        assert bubble["cx"] == bubble["x"] + bubble["w"] / 2
        roll = next(o for o in rendered["outputs"] if o["name"] == "RollNo")
        assert roll["parts"] == ["roll1", "roll2", "roll3", "roll4"]
        assert roll["value"] == "".join(
            truth["answers"][f"roll{i}"] for i in range(1, 5)
        )

        picture = client.get(rendered["image_url"])
        assert picture.status_code == 200
        assert picture.headers["content-type"] == "image/jpeg"
        decoded = cv2.imdecode(np.frombuffer(picture.content, np.uint8), 0)
        assert decoded.shape == (spec.page[1], spec.page[0])
        png = client.get(f"/scans/{scan_id}/render/image?format=png")
        assert png.content[:4] == b"\x89PNG"
        inline = client.get(f"/scans/{scan_id}/render?inline=true").json()
        assert inline["image_data"].startswith("data:image/jpeg;base64,")

        overlay = client.get(f"/scans/{scan_id}/overlay").json()
        assert len(overlay["fields"]) == len(rendered["fields"])

        # Template edited afterwards: the archived version still renders exactly
        edited = template_json(spec)
        edited["fieldBlocks"]["MCQ_1"]["bubbleValues"] = ["W", "X", "Y", "Z"]
        assert client.put(f"/templates/{template_id}", json=edited).status_code == 200
        ctx = client.app.state.ctx
        ctx.results.renders.items.clear()
        again = client.get(f"/scans/{scan_id}/render").json()
        assert again["exact_template"] is True
        assert again["template_version_used"] == result["template_version"]
        assert again["drift"] == []


def test_toggle_edit_audit_and_verify(tmp_path, spec):
    image, truth = sheet(spec, 3, q1="A")
    with make_client(tmp_path) as client:
        template_id = upload(client, template_json(spec))
        result = scan(client, template_id, image)
        scan_id = result["scan_id"]
        headers = {"X-User": "alice"}
        url = f"/scans/{scan_id}/corrections"

        data = client.post(
            url, json={"toggle": [{"field": "q1", "value": "C"}]}, headers=headers
        ).json()
        q1 = next(f for f in data["fields"] if f["name"] == "q1")
        assert q1["value"] == "AC" and q1["original_value"] == "A" and q1["corrected"]
        assert [b["value"] for b in q1["bubbles"] if b["marked"]] == ["A", "C"]
        assert next(o for o in data["outputs"] if o["name"] == "q1")["value"] == "AC"

        data = client.post(
            url, json={"toggle": [{"field": "q1", "value": "A"}]}, headers=headers
        ).json()
        assert next(f for f in data["fields"] if f["name"] == "q1")["value"] == "C"
        data = client.post(url, json={"toggle": [{"field": "q1", "value": "C"}]}).json()
        q1 = next(f for f in data["fields"] if f["name"] == "q1")
        assert q1["value"] == "" and q1["original_value"] == "A"

        # Typed values: a roll digit changes the concatenated custom label
        old_digit = truth["answers"]["roll1"]
        new_digit = "7" if old_digit != "7" else "3"
        data = client.post(
            url, json={"changes": {"roll1": new_digit, "q1": "A"}}, headers=headers
        ).json()
        roll = next(o for o in data["outputs"] if o["name"] == "RollNo")
        assert roll["value"][0] == new_digit and roll["corrected"]
        # q1 back to its original read: no longer counted as corrected
        assert not next(f for f in data["fields"] if f["name"] == "q1")["corrected"]

        bad = client.post(url, json={"changes": {"q2": "Z"}})
        assert bad.status_code == 422
        bad = client.post(url, json={"toggle": [{"field": "q2", "value": "Z"}]})
        assert bad.status_code == 422

        audit = client.get(f"/scans/{scan_id}/audit").json()
        assert audit["total"] == 5
        newest = audit["items"][0]
        assert newest["user"] == "alice" and newest["source"] == "results"
        users = {item["user"] for item in audit["items"]}
        assert users == {"alice", "local"}
        stored = client.get(f"/scans/{scan_id}").json()
        assert len(stored["audit"]) == 5
        assert stored["responses"]["RollNo"][0] == new_digit

        listing = client.get("/results?view=corrected").json()
        assert listing["total"] == 1
        assert client.get("/results?view=reviewed").json()["total"] == 0

        # Verify: every field checked; feeds accuracy and training data
        verified = client.post(f"/scans/{scan_id}/verify", headers=headers).json()
        assert verified["verified"]["by"] == "alice"
        assert verified["training_records"] >= 1
        assert client.get("/results?view=reviewed").json()["total"] == 1
        accuracy = client.get(f"/results/accuracy?template_id={template_id}").json()
        assert accuracy["verified_sheets"] == 1
        fields_total = len(stored["fields"])
        flagged = {i["name"] for i in stored.get("read_review") or []}
        auto = fields_total - len(flagged)
        assert accuracy["auto_accepted_fields"] == auto
        expected_ok = auto - (0 if "roll1" in flagged else 1)
        assert accuracy["auto_correct_fields"] == expected_ok
        roll1 = next(f for f in accuracy["fields"] if f["name"] == "roll1")
        assert roll1["corrected"] == 1
        labels = (
            (tmp_path / "data" / "training" / "labels.jsonl").read_text().splitlines()
        )
        assert any(json.loads(line)["name"] == "roll1" for line in labels)

        # Index rebuild keeps flags, verification and accuracy rows
        ctx = client.app.state.ctx
        assert ctx.index.rebuild(ctx.data.scans) == 1
        again = client.get(f"/results/accuracy?template_id={template_id}").json()
        assert again["auto_correct_fields"] == expected_ok
        assert client.get("/results?view=verified").json()["total"] == 1

        undone = client.delete(f"/scans/{scan_id}/verify").json()
        assert not undone["verified"]
        assert client.get("/results/accuracy").json()["verified_sheets"] == 0


def test_listing_filters_and_neighbours(tmp_path, spec):
    with make_client(tmp_path) as client:
        template_id = upload(client, template_json(spec))
        ids = []
        for seed in range(3):
            overrides = {"q3": "AC"} if seed == 1 else {}
            image, _ = sheet(spec, 10 + seed, **overrides)
            ids.append(scan(client, template_id, image, f"s{seed}.png")["scan_id"])
        listing = client.get("/results?order=asc").json()
        assert [i["id"] for i in listing["items"]] == ids
        flagged = client.get("/results?view=flagged").json()
        assert ids[1] in [i["id"] for i in flagged["items"]]
        by_flag = client.get("/results?flag=multi_marked").json()
        assert [i["id"] for i in by_flag["items"]] == [ids[1]]
        by_name = client.get("/results?name=q3").json()
        assert [i["id"] for i in by_name["items"]] == [ids[1]]
        assert client.get("/results?file=s2").json()["items"][0]["id"] == ids[2]
        facets = client.get(f"/results/facets?template_id={template_id}").json()
        assert any(f["flag"] == "multi_marked" for f in facets["flags"])
        assert facets["views"]["all"] == 3
        nb = client.get(f"/results/neighbours?scan_id={ids[1]}&order=asc").json()
        assert nb == {"previous": ids[0], "next": ids[2]}
        nb = client.get(f"/results/neighbours?scan_id={ids[1]}").json()
        assert nb == {"previous": ids[2], "next": ids[0]}
        page = client.get("/results?limit=2&offset=2&order=asc").json()
        assert page["total"] == 3 and [i["id"] for i in page["items"]] == [ids[2]]
        assert client.get("/results?view=bogus").status_code == 422


def test_moved_folder_remap_and_fallback(tmp_path, spec):
    folder = tmp_path / "inbox"
    folder.mkdir()
    image, _ = sheet(spec, 20)
    cv2.imwrite(str(folder / "a.png"), image)
    with make_client(tmp_path) as client:
        template_id = upload(client, template_json(spec))
        response = client.post(
            "/jobs",
            data={
                "template_id": template_id,
                "folder": str(folder),
                "save_images": "none",
            },
        )
        job = wait_for_job(client, response.json()["id"])
        scan_id = client.get(f"/results?job_id={job['id']}").json()["items"][0]["id"]
        stored = client.get(f"/scans/{scan_id}").json()
        assert stored["source_path"] == str((folder / "a.png").resolve())

        moved = tmp_path / "archive" / "inbox-2026"
        moved.parent.mkdir()
        shutil.move(str(folder), str(moved))
        ctx = client.app.state.ctx
        ctx.results.renders.items.clear()
        missing = client.get(f"/scans/{scan_id}/render")
        assert missing.status_code == 404
        assert "path remap" in missing.json()["detail"]

        # Per-job remap
        patched = client.patch(
            f"/jobs/{job['id']}",
            json={"path_remap": [{"from": str(folder), "to": str(moved)}]},
        )
        assert patched.status_code == 200, patched.text
        rendered = client.get(f"/scans/{scan_id}/render").json()
        assert rendered["image_source"] == "source"
        assert rendered["resolved_path"] == str(moved / "a.png")

        # Global remap (string form)
        client.patch(f"/jobs/{job['id']}", json={"path_remap": []})
        response = client.put(
            "/settings/path-remap", json={"rules": [f"{folder}={moved}"]}
        )
        assert response.json()["rules"] == [{"from": str(folder), "to": str(moved)}]
        assert client.get(f"/scans/{scan_id}/render").json()["image_source"] == "source"

        # Without the file, a stored aligned image is the fallback
        client.put("/settings/path-remap", json={"rules": []})
        ctx.results.renders.items.clear()
        cv2.imwrite(
            str(ctx.data.scan_dir(scan_id) / "aligned.png"),
            np.full((50, 40), 255, np.uint8),
        )
        rendered = client.get(f"/scans/{scan_id}/render").json()
        assert rendered["image_source"] == "stored" and rendered["warnings"]
        assert (
            client.patch(f"/jobs/{job['id']}", json={"state": "x"}).status_code == 422
        )


def test_regrade_preview_and_apply(tmp_path, spec):
    image, truth = sheet(spec, 30, q1="A", q2="B")
    with make_client(tmp_path) as client:
        template_id = upload(client, template_json(spec))
        scan_id = scan(client, template_id, image)["scan_id"]
        client.post(f"/scans/{scan_id}/corrections", json={"changes": {"q2": "D"}})
        overrides = {"fieldBlocks": {"MCQ_1": {"bubbleValues": ["D", "C", "B", "A"]}}}
        preview = client.post(
            f"/scans/{scan_id}/regrade", json={"template_overrides": overrides}
        ).json()
        assert preview["applied"] is False
        changed = {c["name"]: c for c in preview["changes"]}
        assert changed["q1"]["before"] == "A" and changed["q1"]["after"] == "D"
        assert client.get(preview["image_url"]).status_code == 200
        # Nothing stored yet
        assert client.get(f"/scans/{scan_id}").json()["responses"]["q1"] == "A"

        applied = client.post(
            f"/scans/{scan_id}/regrade",
            json={"template_overrides": overrides, "apply": True},
            headers={"X-User": "bob"},
        ).json()
        assert applied["applied"] is True
        stored = client.get(f"/scans/{scan_id}").json()
        assert stored["responses"]["q1"] == "D"
        # The manual correction survives the regrade
        assert stored["responses"]["q2"] == "D"
        assert stored["regrade"]["template_overrides"] == overrides
        assert stored["history"][0]["responses"]["q1"] == "A"
        # Re-rendering later uses the same overrides: no drift
        client.app.state.ctx.results.renders.items.clear()
        rendered = client.get(f"/scans/{scan_id}/render").json()
        assert rendered["drift"] == []
        audit = client.get(f"/scans/{scan_id}/audit").json()["items"]
        assert audit[0]["source"] == "regrade" and audit[0]["user"] == "bob"

        bad = client.post(
            f"/scans/{scan_id}/regrade",
            json={
                "template_overrides": {
                    "fieldBlocks": {"MCQ_1": {"direction": "diagonal"}}
                }
            },
        )
        assert bad.status_code == 422


def test_queue_review_is_audited(tmp_path, spec):
    image, _ = sheet(spec, 40, q3="AC")
    with make_client(tmp_path) as client:
        template_id = upload(client, template_json(spec))
        scan_id = scan(client, template_id, image)["scan_id"]
        client.post(
            f"/scans/{scan_id}/review",
            json={"corrections": {"q3": "A"}},
            headers={"X-User": "carol"},
        )
        audit = client.get("/audit").json()
        assert audit["total"] == 1
        item = audit["items"][0]
        assert (item["user"], item["source"], item["old"], item["new"]) == (
            "carol",
            "queue",
            "AC",
            "A",
        )


def test_remap_rules(tmp_path):
    rule = {"from": "D:\\Old\\Scans", "to": "E:\\new"}
    assert remap_path("d:\\old\\scans\\b\\x.png", rule) == "E:\\new\\b\\x.png"
    assert remap_path("D:\\Old\\ScansX\\x.png", rule) is None
    assert (
        remap_path("/data/in/a.png", {"from": "/data/in", "to": "/srv/x/"})
        == "/srv/x/a.png"
    )
    assert remap_path("/data/inbox/a.png", {"from": "/data/in", "to": "/x"}) is None
    target = tmp_path / "x.png"
    target.write_bytes(b"1")
    found, tried = resolve_source(
        "/gone/x.png", [{"from": "/gone", "to": str(tmp_path)}]
    )
    assert found == target and tried[-1] == "/gone/x.png"


def test_toggled_value_order_and_empty_value():
    field = {"bubbles": [{"value": v} for v in "ABCD"]}
    assert toggled_value(field, "C", "A") == "AC"
    assert toggled_value(field, "AC", "C") == "A"
    assert toggled_value(field, "A", "A", empty_value="-") == "-"
    assert toggled_value(field, "-", "B", empty_value="-") == "B"


def test_old_index_is_migrated(tmp_path):
    path = tmp_path / "index.sqlite3"
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE scans (id TEXT PRIMARY KEY, template_id TEXT, job_id TEXT, "
        "seq INTEGER, file_name TEXT, status TEXT, score REAL, review_count INTEGER "
        "DEFAULT 0, reviewed INTEGER DEFAULT 0, has_images INTEGER DEFAULT 0, "
        "error TEXT, created_at REAL)"
    )
    conn.execute("INSERT INTO scans (id, status) VALUES ('abc', 'ok')")
    conn.commit()
    conn.close()
    index = ScanIndex(path)
    items, total = index.list_results(view="flagged")
    assert total == 0
    assert index.list_results()[1] == 1
    index.close()


def test_regrade_overrides_deep_merge(spec):
    """Regrade overrides deep-merge into template.json (one block changes)."""
    from src.api.worker import build_engine, merged_template_overrides

    image, truth = sheet(spec, 50, q1="A")
    directory = Path(__file__).parent / "__tmp_override_template"
    directory.mkdir(exist_ok=True)
    try:
        (directory / "template.json").write_text(json.dumps(template_json(spec)))
        overrides = {"fieldBlocks": {"MCQ_1": {"bubbleValues": ["D", "C", "B", "A"]}}}
        merged = merged_template_overrides(directory / "template.json", overrides)
        assert set(merged["fieldBlocks"]) == set(template_json(spec)["fieldBlocks"])
        plain = build_engine(directory).scan(image, "x")
        flipped = build_engine(directory, overrides).scan(image, "x")
        assert plain.responses["q1"] == "A"
        assert flipped.responses["q1"] == "D"
        # The override does not leak into engines built afterwards
        assert build_engine(directory).scan(image, "x").responses["q1"] == "A"
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_results_screen_is_served(tmp_path):
    with make_client(tmp_path) as client:
        page = client.get("/").text
        assert 'data-tab="results"' in page and 'id="res-canvas"' in page
        for name in ("results.js", "export.js"):
            response = client.get(f"/static/{name}")
            assert response.status_code == 200
            assert "export function" in response.text


def test_corrections_rerun_template_rules(tmp_path, spec):
    """Checks/validation re-run after edits; check and custom-label items are
    editable in the Results screen and the review queue."""
    template = template_json(spec)
    template["validate"] = {"RollNo": {"length": 4, "allowGaps": False}}
    template["checks"] = [{"name": "q_agree", "sources": ["q1", "q2"]}]
    image, truth = sheet(spec, 60, q1="A", q2="B", roll2="")
    with make_client(tmp_path) as client:
        template_id = upload(client, template)
        result = scan(client, template_id, image)
        scan_id = result["scan_id"]
        kinds = {item["name"]: item["kind"] for item in result["review"]}
        assert kinds["RollNo"] == "custom_label" and kinds["q_agree"] == "check"
        assert result["responses"]["q_agree"] == "A"

        # Both rule items show in the queue, with what the reviewer needs
        queue = client.get("/review", params={"scan_id": scan_id}).json()["items"]
        by_name = {item["name"]: item for item in queue}
        assert by_name["q_agree"]["candidates"] == [
            {"source": "q1", "value": "A"},
            {"source": "q2", "value": "B"},
        ]
        assert by_name["RollNo"]["fields"] == ["roll1", "roll2", "roll3", "roll4"]
        assert "gap at position 2" in by_name["RollNo"]["reasons"]
        assert (
            client.get("/review", params={"scan_id": scan_id, "kind": "check"}).json()[
                "total"
            ]
            == 1
        )

        # A custom label is split over its columns; validation passes again
        answers = truth["answers"]
        fixed = answers["roll1"] + "7" + answers["roll3"] + answers["roll4"]
        body = client.post(
            f"/scans/{scan_id}/corrections",
            json={"changes": {"RollNo": fixed}},
            headers={"X-User": "ana"},
        ).json()
        outputs = {o["name"]: o for o in body["outputs"]}
        assert outputs["RollNo"]["value"] == fixed
        assert outputs["RollNo"]["kind"] == "custom_label"
        assert body["validation"]["RollNo"]["ok"] is True
        assert "RollNo" not in body["pending"]
        roll2 = next(f for f in body["fields"] if f["name"] == "roll2")
        assert roll2["value"] == "7" and roll2["original_value"] == ""
        audit = client.get(f"/scans/{scan_id}/audit").json()["items"]
        assert {(a["name"], a["kind"]) for a in audit} >= {
            ("roll2", "field"),
            ("RollNo", "custom_label"),
        }

        # A check is corrected by its output column; the typed value survives
        # rules re-running after another edit
        body = client.post(
            f"/scans/{scan_id}/corrections", json={"changes": {"q_agree": "B"}}
        ).json()
        assert outputs["q_agree"]["kind"] == "check"
        assert body["checks"]["q_agree"]["value"] == "B"
        assert body["checks"]["q_agree"]["manual"] is True
        assert body["pending"] == [] and body["status"] == "ok"
        body = client.post(
            f"/scans/{scan_id}/corrections",
            json={"toggle": [{"field": "q5", "value": truth["answers"]["q5"]}]},
        ).json()
        assert body["checks"]["q_agree"]["value"] == "B"
        assert {o["name"]: o["value"] for o in body["outputs"]}["q_agree"] == "B"

        # An edit that breaks a rule again puts the item back in the queue ...
        body = client.post(
            f"/scans/{scan_id}/corrections",
            json={"changes": {"RollNo": fixed[0] + " " + fixed[2:]}},
        ).json()
        assert "RollNo" in body["pending"] and body["status"] == "needs_review"
        queue = client.get("/review", params={"scan_id": scan_id}).json()
        assert [item["name"] for item in queue["items"]] == ["RollNo"]
        # ... until a person accepts it (review queue endpoint)
        reviewed = client.post(
            f"/scans/{scan_id}/review", json={"accept": ["RollNo"]}
        ).json()
        assert reviewed["review"] == [] and reviewed["status"] == "ok"
        assert client.get("/review", params={"scan_id": scan_id}).json()["total"] == 0

        # Exports carry the rule outputs as corrected
        record = client.post(
            "/exports",
            json={
                "format": "csv",
                "filters": {"template_id": template_id},
                "wait": True,
                "profile": {
                    "fields": [{"field": "RollNo"}, {"field": "q_agree"}],
                    "meta": [],
                    "includeReviewStatus": False,
                    "includeCorrected": False,
                    "includeFieldCorrected": True,
                },
            },
        ).json()
        assert record["state"] == "completed", record
        lines = client.get(record["download_url"]).text.lstrip("\ufeff").splitlines()
        assert lines[0] == "RollNo,RollNo_corrected,q_agree,q_agree_corrected"
        # RollNo is back to what was read (roll2 blank again); q_agree was typed
        assert lines[1] == f"{fixed[0] + fixed[2:]},false,B,true"

        # Unknown names are refused before anything changes
        response = client.post(
            f"/scans/{scan_id}/corrections", json={"changes": {"nope": "1"}}
        )
        assert response.status_code == 422
        response = client.post(
            f"/scans/{scan_id}/corrections", json={"changes": {"RollNo": "12345"}}
        )
        assert response.status_code == 422 and "longer" in response.text


def test_sheet_level_items_are_kept_and_dismissed(tmp_path, spec):
    """Sheet items (kind "sheet") survive rule re-runs and are settled by accepting."""
    template = template_json(spec)
    template["validate"] = {"RollNo": {"length": 4}}
    config = {"review_params": {"min_marked_bubbles": 1000}}
    image, truth = sheet(spec, 70)
    with make_client(tmp_path) as client:
        response = client.post(
            "/templates",
            files=[
                ("files", ("template.json", json.dumps(template), "application/json")),
                ("files", ("config.json", json.dumps(config), "application/json")),
            ],
            data={"name": "Exam S"},
        )
        assert response.status_code == 201, response.text
        result = scan(client, response.json()["id"], image)
        scan_id = result["scan_id"]
        assert [i["name"] for i in result["review"] if i["kind"] == "sheet"] == [
            "too_few_marks"
        ]
        queue = client.get(
            "/review", params={"scan_id": scan_id, "kind": "sheet"}
        ).json()
        assert queue["total"] == 1
        assert "fewer than 1000" in queue["items"][0]["reasons"][0]

        # An edit re-runs the rules; the sheet item stays pending
        q1 = truth["answers"]["q1"]
        body = client.post(
            f"/scans/{scan_id}/corrections",
            json={"toggle": [{"field": "q1", "value": q1}]},
        ).json()
        assert [i["name"] for i in body["sheet_review"]] == ["too_few_marks"]
        assert body["status"] == "needs_review"
        response = client.post(
            f"/scans/{scan_id}/corrections", json={"changes": {"too_few_marks": "x"}}
        )
        assert response.status_code == 422 and "accept" in response.text

        body = client.post(
            f"/scans/{scan_id}/corrections", json={"accept": ["too_few_marks"]}
        ).json()
        assert body["sheet_review"] == [] and body["status"] == "ok"
        body = client.post(
            f"/scans/{scan_id}/corrections",
            json={"toggle": [{"field": "q1", "value": q1}]},
        ).json()
        assert body["sheet_review"] == [] and body["status"] == "ok"
        assert client.get("/review", params={"scan_id": scan_id}).json()["total"] == 0


def test_colour_template_renders_and_regrades(tmp_path, spec):
    """A colorDropout template re-reads its source in colour; regrade can swap
    the dropout (a whole-key override) without touching the template."""
    template = template_json(spec)
    template["colorDropout"] = {"mode": "red", "strength": 1}
    image, truth = sheet(spec, 80)
    colour = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    with make_client(tmp_path) as client:
        template_id = upload(client, template)
        result = scan(client, template_id, colour)
        scan_id = result["scan_id"]
        rendered = client.get(f"/scans/{scan_id}/render").json()
        assert rendered["image_source"] == "source" and rendered["width"] > 0
        q1 = next(f for f in rendered["fields"] if f["name"] == "q1")
        assert q1["value"] == truth["answers"]["q1"]
        preview = client.post(
            f"/scans/{scan_id}/regrade",
            json={"template_overrides": {"colorDropout": "grey"}},
        ).json()
        assert preview["applied"] is False
        assert preview["changes"] == []  # a grey sheet reads the same either way
