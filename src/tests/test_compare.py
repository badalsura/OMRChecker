"""Compare tab (src/api/compare.py, compare_routes.py): reading an external
scanner's file, joining it to our sheets, the mismatch rules, the reconciled
layer with its locks and audit trail, and the exports."""

import csv
import io
import json

import pytest

from src.api import compare
from src.synth.render import default_spec
from src.tests.test_api import make_client, scan_one, upload_template
from src.tests.test_manage_api import flagged_sheet

RULES = compare.DEFAULT_RULES


@pytest.fixture(scope="module")
def spec():
    return default_spec(questions=20, roll_digits=4, with_zones=False)


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------
def test_read_csv_keeps_blank_cells_and_sniffs_the_delimiter():
    columns, rows = compare.read_table(b"SEQ;Q1;Q2\nS0001; ;*\nS0002;A;B\n")
    assert columns == ["SEQ", "Q1", "Q2"]
    assert rows == [["S0001", " ", "*"], ["S0002", "A", "B"]]
    columns, rows = compare.read_table(b"1,A\n2,B\n", {"header": False})
    assert columns == ["C1", "C2"] and len(rows) == 2


def test_read_fixed_width_with_a_range():
    text = "000123AB D\n000124 *CA\n"
    spec = "SEQ 1 6\nq1..q4 7 1"
    columns, rows = compare.read_table(text.encode(), {"fixed": spec, "header": False})
    assert columns == ["SEQ", "q1", "q2", "q3", "q4"]
    assert rows[0] == ["000123", "A", "B", " ", "D"]
    assert rows[1][1:3] == [" ", "*"]
    with pytest.raises(compare.CompareError):
        compare.parse_fixed_spec("q1 x 1")


def test_suggested_mapping_and_key():
    names = ["Roll", "q1", "q2", "q10", "imprint_no"]
    questions = ["q1", "q2", "q10"]
    mapping = compare.suggest_mapping(
        ["SEQ", "ROLL", "Q001", "2", "Q10", "X"], names, questions
    )
    assert mapping == {"ROLL": "Roll", "Q001": "q1", "2": "q2", "Q10": "q10"}
    key = compare.suggest_key(["SEQ", "ROLL"], names, ["imprint_no"])
    assert key == {"column": "SEQ", "ours": "imprint_no", "normalize": "digits"}
    assert compare.suggest_key(["FILE", "SEQ"], names, [])["ours"] == "file"


def test_keys_normalize_like_the_file_name_check():
    assert compare.key_of("S0100001", "digits") == "100001"
    assert compare.key_of("batch/100001.jpg", "digits", True) == "100001"
    assert compare.key_of("100001_p1.png", "digits", True) == "100001"
    assert compare.key_of("Sheet7.JPG", "text") == "sheet7"


def test_value_rules():
    def check(ext, ours, question=True, multi_flag=False, **rules):
        merged = {**RULES, **rules}
        return compare.differs(
            compare.external_value(ext, merged, question),
            compare.our_value(ours, "", question, multi_flag, merged),
            merged,
        )

    assert check("A", "A") is False
    assert check("b", "B") is False  # case ignored by default
    assert check("A", "B") is True
    assert check(" ", "") is False and check("-", "") is False
    assert check(" ", "A") is True
    assert check(" ", "A", blank="ignore") is None
    # Any multi-mark equals any other unless both list their options
    assert check("*", "AC") is False
    assert check("*", "A") is True
    assert check("AC", "CA", multi="exact") is False
    assert check("AB", "AC", multi="exact") is True
    assert check("*", "AC", multi="ignore") is None
    # A roll number: spaces inside ignored, not a question
    assert check("12 45", "1245", question=False) is False
    assert check("12 45", "1245", question=False, spaces=False) is True


# ---------------------------------------------------------------------------
# the API
# ---------------------------------------------------------------------------
def external_csv(results, changes):
    """An OpScan-style CSV of our own reads, with changes {(file, field): value}."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["SEQ", "FILE"] + [f"Q{i:03d}" for i in range(1, 21)])
    for result in results:
        file_name = result["file_id"]
        cells = []
        for i in range(1, 21):
            value = result["responses"][f"q{i}"]
            value = "*" if len(value) > 1 else (value or " ")
            cells.append(changes.get((file_name, f"q{i}"), value))
        writer.writerow([f"S{file_name.split('.')[0]}", file_name] + cells)
    writer.writerow(["S999999", "999999.png"] + ["A"] * 20)
    return buffer.getvalue().encode()


@pytest.fixture
def compared(tmp_path, spec):
    client = make_client(tmp_path).__enter__()
    template_id = upload_template(client, spec)
    results = [
        scan_one(client, template_id, flagged_sheet(spec, seed=s), name=f"10000{s}.png")
        for s in (1, 2, 3)
    ]
    first, second = results[0]["file_id"], results[1]["file_id"]
    flip = lambda v: "B" if v != "B" else "C"  # noqa: E731
    changes = {
        (first, "q1"): flip(results[0]["responses"]["q1"]),
        (first, "q2"): " ",
        (second, "q4"): flip(results[1]["responses"]["q4"]),
        (second, "q3"): "*",  # ours is multi-marked too (AC): agrees
    }
    preview = client.post(
        "/compare/preview",
        data={"template_id": template_id, "parse": "{}"},
        files={"file": ("opscan.csv", external_csv(results, changes), "text/csv")},
    )
    assert preview.status_code == 200, preview.text
    yield client, template_id, results, preview.json()
    client.__exit__(None, None, None)


def create(client, template_id, preview, **extra):
    body = {
        "upload": preview["upload"],
        "template_id": template_id,
        "mapping": preview["mapping"],
        "key": preview["key"],
        **extra,
    }
    response = client.post("/compare/sessions", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def test_preview_suggests_the_mapping_and_the_file_join(compared):
    client, template_id, results, preview = compared
    assert preview["count"] == 4 and preview["columns"][:2] == ["SEQ", "FILE"]
    assert preview["mapping"]["Q001"] == "q1" and len(preview["mapping"]) == 20
    assert preview["key"] == {"column": "FILE", "ours": "file", "normalize": "text"}
    assert "q1" in preview["questions"] and "roll1" not in preview["questions"]


def test_session_counts_only_real_mismatches(compared):
    client, template_id, results, preview = compared
    session = create(
        client,
        template_id,
        preview,
        key={"column": "SEQ", "ours": "file", "normalize": "digits"},
    )
    summary, progress = session["summary"], session["progress"]
    assert summary["matched"] == 3 and summary["unmatched_external"] == 1
    assert summary["mismatches"] == 3  # q1 and q2 on the first sheet, q4 on the second
    assert progress["sheets"] == {"pending": 2, "clean": 1}
    unmatched = client.get(
        f"/compare/sessions/{session['id']}/export?kind=unmatched"
    ).text
    assert "999999" in unmatched and "no sheet" in unmatched
    # Blank against a mark ignored: q2 drops out
    patched = client.patch(
        f"/compare/sessions/{session['id']}", json={"rules": {"blank": "ignore"}}
    ).json()
    assert patched["summary"]["mismatches"] == 2


def test_review_locks_decides_undoes_and_exports(compared):
    client, template_id, results, preview = compared
    session = create(client, template_id, preview)
    base = f"/compare/sessions/{session['id']}"
    sheet = client.post(f"{base}/claim", json={}, headers={"X-User": "asha"}).json()[
        "sheet"
    ]
    assert sheet["file_name"] == results[0]["file_id"] and sheet["position"] == 1
    names = [item["name"] for item in sheet["items"]]
    assert names == ["q1", "q2"]
    assert all(item["box"] for item in sheet["items"])
    page = client.get(sheet["page"])
    assert page.status_code == 200 and page.headers["content-type"] == "image/jpeg"

    # A second reviewer gets the next sheet, and cannot save the locked one
    other = client.post(f"{base}/claim", json={}, headers={"X-User": "ravi"}).json()[
        "sheet"
    ]
    assert other["file_name"] == results[1]["file_id"]
    refused = client.post(
        f"{base}/sheets/{sheet['scan_id']}",
        json={"decisions": {"q1": "ours"}},
        headers={"X-User": "ravi"},
    )
    assert refused.status_code == 409 and "asha" in refused.json()["detail"]

    saved = client.post(
        f"{base}/sheets/{sheet['scan_id']}",
        json={"decisions": {"q1": "external", "q2": "ours"}},
        headers={"X-User": "asha"},
    ).json()
    assert saved["state"] == "done" and saved["progress"]["decided"] == 2
    # Nothing left for asha but the sheet ravi holds
    assert (
        client.post(
            f"{base}/claim", json={"after": sheet["ord"]}, headers={"X-User": "asha"}
        ).json()["sheet"]
        is None
    )
    # Going back reopens a decided sheet; undoing one choice makes it partial
    back = client.post(
        f"{base}/claim",
        json={"after": other["ord"], "direction": "prev"},
        headers={"X-User": "asha"},
    )
    assert back.json()["sheet"]["scan_id"] == sheet["scan_id"]
    assert back.json()["sheet"]["items"][0]["source"] == "external"
    undone = client.post(
        f"{base}/sheets/{sheet['scan_id']}",
        json={"decisions": {"q2": None}},
        headers={"X-User": "asha"},
    ).json()
    assert undone["state"] == "partial"

    reconciled = list(csv.DictReader(io.StringIO(client.get(f"{base}/export").text)))
    first = next(row for row in reconciled if row["scan_id"] == sheet["scan_id"])
    external_q1 = next(i["external"] for i in sheet["items"] if i["name"] == "q1")
    assert first["q1"] == external_q1 and first["unresolved"] == "q2"
    assert first["compare_status"] == "partial" and first["external_kept"] == "1"
    audit = list(
        csv.DictReader(io.StringIO(client.get(f"{base}/export?kind=audit").text))
    )
    assert [(a["user"], a["field"], a["action"]) for a in audit] == [
        ("asha", "q1", "decide"),
        ("asha", "q2", "decide"),
        ("asha", "q2", "undo"),
    ]
    # Read results are untouched
    assert (
        client.get(f"/scans/{sheet['scan_id']}").json()["responses"]["q1"]
        == results[0]["responses"]["q1"]
    )


def test_rebuild_keeps_decisions_whose_values_did_not_change(compared):
    client, template_id, results, preview = compared
    session = create(client, template_id, preview)
    base = f"/compare/sessions/{session['id']}"
    sheet = client.post(f"{base}/claim", json={}).json()["sheet"]
    client.post(
        f"{base}/sheets/{sheet['scan_id']}",
        json={"decisions": {"q1": "ours", "q2": "ours"}},
    )
    rebuilt = client.post(f"{base}/rebuild").json()
    assert (
        rebuilt["progress"]["decided"] == 2
        and rebuilt["progress"]["sheets"]["done"] == 1
    )
    listing = client.get(
        "/compare/sessions", params={"template_id": template_id}
    ).json()["sessions"]
    assert [s["id"] for s in listing] == [session["id"]]
    assert client.delete(base).json() == {"deleted": session["id"]}
    assert client.get(base).status_code == 404


def test_bad_mapping_and_missing_key_are_refused(compared):
    client, template_id, results, preview = compared
    body = {
        "upload": preview["upload"],
        "template_id": template_id,
        "mapping": {"Q001": "nope"},
    }
    assert client.post("/compare/sessions", json=body).status_code == 422
    body = {
        "upload": preview["upload"],
        "template_id": template_id,
        "mapping": preview["mapping"],
        "key": {"column": "MISSING", "ours": "file"},
    }
    response = client.post("/compare/sessions", json=body)
    assert response.status_code in (404, 422)
    assert json.loads(response.text)["detail"]
