"""Duplicate / rename layouts, warnings confirm, JSON validation and scoring (items 6, 7, 12)."""

import io
import json

import pytest

from src.export.profile import ExportProfile
from src.export.rows import RowBuilder
from src.tests.test_api import make_client, make_sheet, png_bytes, upload_template
from src.synth.render import default_spec


@pytest.fixture(scope="module")
def spec():
    return default_spec(questions=20, roll_digits=4, with_zones=False)


def evaluation(**options):
    base = {
        "questions_in_order": ["q1..5"],
        "answers_in_order": ["A", "B", "C", "D", "A"],
    }
    base.update(options)
    return {
        "source_type": "custom",
        "options": base,
        "marking_schemes": {
            "DEFAULT": {"correct": "4", "incorrect": "-1", "unmarked": "0"}
        },
    }


def preview(client, template_id, evaluation_json, responses):
    response = client.post(
        f"/templates/{template_id}/score-preview",
        json={"evaluation": evaluation_json, "responses": responses},
    )
    assert response.status_code == 200, response.text
    return response.json()


ALL_RIGHT = {"q1": "A", "q2": "B", "q3": "C", "q4": "D", "q5": "A"}


# ---------------------------------------------------------------- item 7
def test_duplicate_copies_files_and_keeps_original(tmp_path, spec):
    with make_client(tmp_path) as client:
        template_id = upload_template(client, spec, evaluation=evaluation())
        image, _ = make_sheet(spec, seed=1)
        reference = client.post(
            f"/templates/{template_id}/reference",
            files={"file": ("ref.png", png_bytes(image), "image/png")},
        )
        assert reference.status_code == 200
        client.post(f"/templates/{template_id}/confirm-warnings")

        # Name taken (case-insensitive) is refused, not overwritten
        taken = client.post(
            f"/templates/{template_id}/duplicate", json={"name": "exam a"}
        )
        assert taken.status_code == 409
        assert client.post(
            f"/templates/{template_id}/duplicate", json={"name": "  "}
        ).status_code == 422

        response = client.post(
            f"/templates/{template_id}/duplicate", json={"name": "Exam A copy"}
        )
        assert response.status_code == 201, response.text
        copy = response.json()
        assert copy["id"] != template_id
        assert copy["name"] == "Exam A copy"
        assert copy["template"] == client.get(f"/templates/{template_id}").json()[
            "template"
        ]
        assert copy["evaluation"] == evaluation()
        assert copy["has_reference"] is True
        assert copy["report_confirmed"]
        assert client.get(copy["reference_url"]).status_code == 200

        # The copy is independent of the original
        changed = dict(copy["template"])
        changed["bubbleDimensions"] = [28, 28]
        assert (
            client.put(f"/templates/{copy['id']}", json=changed).status_code == 200
        )
        original = client.get(f"/templates/{template_id}").json()
        assert original["template"].get("bubbleDimensions") != [28, 28]
        assert len(client.get("/templates").json()["templates"]) == 2

        assert (
            client.post("/templates/nope/duplicate", json={"name": "x"}).status_code
            == 404
        )


def test_rename_keeps_id_and_refuses_duplicates(tmp_path, spec):
    with make_client(tmp_path) as client:
        first = upload_template(client, spec)
        second = client.post(
            f"/templates/{first}/duplicate", json={"name": "Other"}
        ).json()["id"]
        image, _ = make_sheet(spec, seed=2)
        scan = client.post(
            "/scans",
            data={"template_id": first},
            files=[("files", ("s.png", png_bytes(image), "image/png"))],
        ).json()["scans"][0]

        response = client.post(
            f"/templates/{first}/rename",
            json={"name": "Final exam 2026"},
            headers={"X-User": "badal"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["id"] == first
        assert response.json()["name"] == "Final exam 2026"
        names = {t["id"]: t["name"] for t in client.get("/templates").json()["templates"]}
        assert names[first] == "Final exam 2026"
        # Scans stay linked through the unchanged id
        assert client.get(f"/scans/{scan['scan_id']}").json()["template_id"] == first

        clash = client.post(f"/templates/{second}/rename", json={"name": "FINAL exam 2026"})
        assert clash.status_code == 409
        # Renaming to its own name (other case) is fine
        same = client.post(f"/templates/{first}/rename", json={"name": "final exam 2026"})
        assert same.status_code == 200


def test_confirm_warnings_is_persistent(tmp_path, spec):
    with make_client(tmp_path) as client:
        template_id = upload_template(client, spec)
        assert client.get(f"/templates/{template_id}").json()["report_confirmed"] is None
        response = client.post(
            f"/templates/{template_id}/confirm-warnings", headers={"X-User": "badal"}
        )
        assert response.status_code == 200
        detail = client.get(f"/templates/{template_id}").json()
        assert detail["report_confirmed"]["by"] == "badal"


# ---------------------------------------------------------------- item 6
def test_validate_json_without_saving(tmp_path, spec):
    with make_client(tmp_path) as client:
        template_id = upload_template(client, spec)
        saved = client.get(f"/templates/{template_id}").json()["template"]

        ok = client.post(f"/templates/{template_id}/validate", json={}).json()
        assert ok["ok"] is True

        bad = json.loads(json.dumps(saved))
        bad["fieldBlocks"]["MCQ_1"]["direction"] = "diagonal"
        result = client.post(
            f"/templates/{template_id}/validate", json={"template": bad}
        ).json()
        assert result["ok"] is False
        assert any("direction" in e["path"] for e in result["template"])

        # Semantic engine check: a custom label pointing at unknown fields
        bad = json.loads(json.dumps(saved))
        bad["customLabels"] = {"Roll": ["nope1..5"]}
        result = client.post(
            f"/templates/{template_id}/validate", json={"template": bad}
        ).json()
        assert result["ok"] is False and result["template"]

        result = client.post(
            f"/templates/{template_id}/validate",
            json={"config": {"threshold_params": {"MIN_JUMP": "x"}}},
        ).json()
        assert result["config"] and not result["template"]

        result = client.post(
            f"/templates/{template_id}/validate",
            json={"evaluation": {"source_type": "custom", "options": {}}},
        ).json()
        assert result["evaluation"]

        # The saved files are unchanged
        assert client.get(f"/templates/{template_id}").json()["template"] == saved


def test_parse_answer_key_csv_and_xlsx(tmp_path):
    with make_client(tmp_path) as client:
        response = client.post(
            "/templates/answer-key/parse",
            files={"file": ("key.csv", b"q1,A\nq2,B\nq3,\"A,C\"\n", "text/csv")},
        )
        assert response.status_code == 200
        assert response.json()["rows"] == [["q1", "A"], ["q2", "B"], ["q3", "A,C"]]

        openpyxl = pytest.importorskip("openpyxl")
        book = openpyxl.Workbook()
        sheet = book.active
        sheet.append(["Question", "Answer"])
        sheet.append(["q1", "D"])
        sheet.append([2, "C"])
        buffer = io.BytesIO()
        book.save(buffer)
        response = client.post(
            "/templates/answer-key/parse",
            files={"file": ("key.xlsx", buffer.getvalue(), "application/octet-stream")},
        )
        assert response.status_code == 200, response.text
        assert response.json()["rows"] == [["Question", "Answer"], ["q1", "D"], ["2", "C"]]


# ---------------------------------------------------------------- item 12
def test_score_preview_rules(tmp_path, spec):
    with make_client(tmp_path) as client:
        template_id = upload_template(client, spec)
        base = preview(client, template_id, evaluation(), ALL_RIGHT)
        assert base["score"] == 20 and base["max_score"] == 20
        assert base["counts"] == {"correct": 5}

        responses = {**ALL_RIGHT, "q1": "B", "q2": "", "q3": "AC"}
        result = preview(client, template_id, evaluation(), responses)
        verdicts = {q["question"]: q["verdict"] for q in result["questions"]}
        assert verdicts == {
            "q1": "incorrect",
            "q2": "unmarked",
            "q3": "incorrect",
            "q4": "correct",
            "q5": "correct",
        }
        assert result["score"] == 4 + 4 - 1 - 1

        # Multi-marked as unmarked
        result = preview(
            client, template_id, evaluation(multi_marked="unmarked"), responses
        )
        assert result["score"] == 4 + 4 - 1
        # Weighted: 'AC' gets the weight given for it
        weighted = evaluation(multi_marked="weighted")
        weighted["options"]["answers_in_order"][2] = [["C", 4], ["AC", 2]]
        result = preview(client, template_id, weighted, responses)
        assert result["score"] == 4 + 4 - 1 + 2

        # Drop q1, bonus for all on q2
        result = preview(
            client,
            template_id,
            evaluation(drop_questions=["q1"], bonus_all=["q2"]),
            responses,
        )
        verdicts = {q["question"]: q["verdict"] for q in result["questions"]}
        assert verdicts["q1"] == "dropped" and verdicts["q2"] == "bonus"
        assert result["score"] == 0 + 4 - 1 + 4 + 4
        assert result["max_score"] == 16

        # Fractions (+1 / -1/4 / 0) and sections
        sectioned = evaluation()
        sectioned["marking_schemes"] = {
            "DEFAULT": {"correct": "1", "incorrect": "-1/4", "unmarked": "0"},
            "Physics": {
                "questions": ["q4..5"],
                "marking": {"correct": "4", "incorrect": "-1", "unmarked": "0"},
            },
        }
        result = preview(client, template_id, sectioned, responses)
        assert result["sections"] == {"DEFAULT": -0.5, "Physics": 8}
        assert result["section_max"] == {"DEFAULT": 3, "Physics": 8}

        # Legend bands and question ranges
        result = preview(
            client,
            template_id,
            evaluation(
                legend=[{"min": 0, "label": "Fail"}, {"min": 10, "label": "Pass"}]
            ),
            ALL_RIGHT,
        )
        assert result["band"] == "Pass"
        ranged = evaluation(question_ranges=["q1..3"])
        result = preview(client, template_id, ranged, ALL_RIGHT)
        assert [q["question"] for q in result["questions"]] == ["q1", "q2", "q3"]

        # Invalid drafts are refused with readable errors
        bad = evaluation(multi_marked="sometimes")
        response = client.post(
            f"/templates/{template_id}/score-preview",
            json={"evaluation": bad, "responses": ALL_RIGHT},
        )
        assert response.status_code == 422
        missing = client.post(
            f"/templates/{template_id}/score-preview",
            json={"evaluation": evaluation(), "responses": {"q1": "A"}},
        )
        assert missing.status_code == 422


def test_scan_stores_section_scores_and_export_columns(tmp_path, spec):
    image, truth = make_sheet(spec, seed=5)
    answers = [truth["answers"][f"q{i}"] for i in range(1, 6)]
    answers[4] = "A" if answers[4] != "A" else "B"  # one wrong answer
    evaluation_json = {
        "source_type": "custom",
        "options": {
            "questions_in_order": ["q1..5"],
            "answers_in_order": answers,
            "legend": [{"min": 0, "label": "Fail"}, {"min": 5, "label": "Pass"}],
        },
        "marking_schemes": {
            "DEFAULT": {"correct": "1", "incorrect": "0", "unmarked": "0"},
            "PartB": {
                "questions": ["q4..5"],
                "marking": {"correct": "2", "incorrect": "-1", "unmarked": "0"},
            },
        },
    }
    with make_client(tmp_path) as client:
        template_id = upload_template(client, spec, evaluation=evaluation_json)
        result = client.post(
            "/scans",
            data={"template_id": template_id},
            files=[("files", ("s.png", png_bytes(image), "image/png"))],
        ).json()["scans"][0]
        assert result["score"] == 3 + 2 - 1
        assert result["scoring"]["sections"] == {"DEFAULT": 3, "PartB": 1}
        assert result["scoring"]["max_score"] == 7
        assert result["scoring"]["band"] == "Fail"
        assert "questions" not in result["scoring"]

        # read-sheet reads without storing
        before = client.get("/scans").json()["total"]
        read = client.post(
            f"/templates/{template_id}/read-sheet",
            files={"file": ("m.png", png_bytes(image), "image/png")},
        )
        assert read.status_code == 200, read.text
        assert read.json()["responses"]["q1"] == truth["answers"]["q1"]
        assert client.get("/scans").json()["total"] == before

        # preview from a stored scan
        response = client.post(
            f"/templates/{template_id}/score-preview",
            json={"evaluation": evaluation_json, "scan_id": result["scan_id"]},
        )
        assert response.json()["score"] == result["score"]

    profile = ExportProfile.from_dict({})
    from src.export import plan_columns

    columns, _, labels = plan_columns(profile, [], [result])
    keys = [c.key for c in columns]
    assert keys.index("score_DEFAULT") == keys.index("score") + 1
    assert "score_PartB" in keys
    values, _ = RowBuilder(columns, profile, labels).build(result)
    row = dict(zip(keys, values))
    assert float(row["score_PartB"]) == 1


def test_grade_flag_switches_scoring_off(tmp_path, spec):
    image, _ = make_sheet(spec, seed=6)
    with make_client(tmp_path) as client:
        template_id = upload_template(client, spec, evaluation=evaluation(grade=False))
        result = client.post(
            "/scans",
            data={"template_id": template_id},
            files=[("files", ("s.png", png_bytes(image), "image/png"))],
        ).json()["scans"][0]
        assert result["score"] is None
        assert result["scoring"] == {}
