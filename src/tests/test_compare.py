"""Compare tab (src/api/compare.py, compare_routes.py): reading an external
scanner's file, joining it to our sheets, the mismatch rules, the reconciled
layer with its locks and audit trail, and the exports."""

import csv
import io
import json
import time

import pytest

from src.api import compare
from src.api.storage import read_json
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


def test_template_names_reads_zones_keyed_by_name():
    template = {
        "fieldBlocks": {
            "MCQ": {"fieldType": "QTYPE_MCQ4", "fieldLabels": ["q1..3"]},
            "Roll": {"fieldType": "QTYPE_INT", "fieldLabels": ["roll1..2"]},
        },
        "customLabels": {"Roll": ["roll1..2"]},
        "zones": {
            "imprint_scanner": {
                "type": "imprint",
                "origin": [0, 0],
                "dimensions": [10, 10],
            },
            "barcode": {"type": "barcode", "origin": [0, 0], "dimensions": [10, 10]},
        },
    }
    names, questions, zones, imprint = compare.template_names(template)
    assert zones == ["imprint_scanner", "barcode"] and imprint == ["imprint_scanner"]
    # Fields inside a custom label stay comparable one by one
    assert names == [
        "Roll",
        "q1",
        "q2",
        "q3",
        "roll1",
        "roll2",
        "imprint_scanner",
        "barcode",
    ]
    assert compare.output_names(template) == [
        "Roll",
        "q1",
        "q2",
        "q3",
        "imprint_scanner",
        "barcode",
    ]
    assert questions == ["q1", "q2", "q3"]
    key = compare.suggest_key(["SEQ", "Q1"], names, imprint)
    assert key["ours"] == "imprint_scanner"


def test_packed_answer_columns_and_names_given_by_hand():
    # One column of all answers, and two columns of five answers each
    content = b"ID,ANSWERS\n7,AB *D\n"
    columns, rows = compare.read_table(content, {"split": "ANSWERS q1..q5"})
    assert columns == ["ID", "q1", "q2", "q3", "q4", "q5"]
    assert rows == [["7", "A", "B", " ", "*", "D"]]
    content = b"7;ABCDA;BC DA\n"
    parse = {"header_row": 0, "names": "SEQ, A1, A2", "split": "A1 q1..q5\nA2 q6..q10"}
    columns, rows = compare.read_table(content, parse)
    assert columns == ["SEQ"] + [f"q{i}" for i in range(1, 11)]
    assert rows[0][6:] == ["B", "C", " ", "D", "A"]
    # Separated values and a header further down the file
    content = b"Exported by ScanTools\n\nSEQ,RESP\n9,A|B||C\n"
    columns, rows = compare.read_table(
        content, {"header_row": 3, "split": "RESP q1..q4 sep=|"}
    )
    assert columns == ["SEQ", "q1", "q2", "q3", "q4"] and rows == [
        ["9", "A", "B", "", "C"]
    ]
    with pytest.raises(compare.CompareError):
        compare.read_table(b"A,B\n1,2\n", {"split": "NOPE q1..q2"})
    with pytest.raises(compare.CompareError):
        compare.read_table(b"A,B\n1,2\n", {"names": "a, b, c"})


def test_fields_inside_a_custom_label_are_mapped_one_by_one(tmp_path, spec):
    template = spec.to_template(pre_processors=[])
    template["customLabels"] = {"ans": ["q1..20"]}
    names, questions, _, _ = compare.template_names(template)
    assert "q1" in names and "ans" in names and "q20" in questions
    mapping = compare.suggest_mapping(["SEQ", "Q1", "ANS"], names, questions)
    assert mapping == {"Q1": "q1", "ANS": "ans"}
    # Both the label and its fields mapped: the label is left out
    pairs = compare.compared_names({"Q1": "q1", "ANS": "ans"}, RULES, questions)
    assert len(pairs) == 2


def test_manual_value_and_profiles(compared):
    client, template_id, results, preview = compared
    session = create(client, template_id, preview)
    base = f"/compare/sessions/{session['id']}"
    sheet = client.post(f"{base}/claim", json={}).json()["sheet"]
    saved = client.post(
        f"{base}/sheets/{sheet['scan_id']}",
        json={"decisions": {"q1": {"source": "manual", "value": "D"}, "q2": "ours"}},
    ).json()
    assert saved["state"] == "done"
    rows = list(csv.DictReader(io.StringIO(client.get(f"{base}/export").text)))
    assert next(r for r in rows if r["scan_id"] == sheet["scan_id"])["q1"] == "D"
    audit = client.get(f"{base}/export?kind=audit").text
    assert ",D,manual,decide" in audit
    bad = client.post(
        f"{base}/sheets/{sheet['scan_id']}", json={"decisions": {"q1": "theirs"}}
    )
    assert bad.status_code == 422

    # Read again with other settings, without uploading
    again = client.post(
        f"/compare/preview/{session['id']}x",
        json={"template_id": template_id, "parse": {}},
    )
    assert again.status_code == 404
    profile = {
        "name": "OpScan layout",
        "template_id": template_id,
        "parse": {"header_row": 1},
        "mapping": preview["mapping"],
        "key": {"column": "SEQ", "ours": "file", "normalize": "digits"},
    }
    assert client.post("/compare/profiles", json=profile).status_code == 201
    listed = client.get("/compare/profiles").json()["profiles"]
    assert (
        listed[0]["name"] == "OpScan layout"
        and listed[0]["settings"]["key"]["column"] == "SEQ"
    )
    assert client.delete("/compare/profiles/OpScan layout").json() == {
        "deleted": "OpScan layout"
    }


def test_reparse_a_previewed_file(compared):
    client, template_id, results, preview = compared
    again = client.post(
        f"/compare/preview/{preview['upload']}",
        json={"template_id": template_id, "parse": {"header_row": 0}},
    ).json()
    assert again["columns"][0] == "C1" and again["count"] == preview["count"] + 1


def test_release_matched_export_and_a_key_from_our_fields(compared):
    client, template_id, results, preview = compared
    session = create(client, template_id, preview)
    base = f"/compare/sessions/{session['id']}"
    sheet = client.post(f"{base}/claim", json={}, headers={"X-User": "asha"}).json()[
        "sheet"
    ]
    # The first sheet is the first by file name
    assert sheet["file_name"] == "100001.png"
    refused = client.post(
        f"{base}/sheets/{sheet['scan_id']}/release", headers={"X-User": "ravi"}
    )
    assert refused.status_code == 409
    assert (
        client.post(
            f"{base}/sheets/{sheet['scan_id']}/release", headers={"X-User": "asha"}
        ).status_code
        == 200
    )
    everything = client.get(f"{base}/export").text.strip().splitlines()
    matched = (
        client.get(f"{base}/export", params={"matched": "true"})
        .text.strip()
        .splitlines()
    )
    assert len(everything) == len(matched) == 4  # all three sheets have a row
    # Joined on one of our fields (read once, then from the cache)
    key = {"column": "Q001", "ours": "q1", "normalize": "text"}
    for _ in range(2):
        patched = client.patch(base, json={"key": key})
        assert patched.status_code == 200, patched.text
    assert client.app.state.ctx.compare.cached_keys("q1")


def test_opscan_dat_layout_with_a_dos_end_of_file_mark():
    # ScanTools Plus .dat: fixed width, no header, Ctrl-Z after the last record
    record = (
        "0011"
        + "00001"
        + "001101926001"
        + " " * 17
        + "N "
        + "993100270006"
        + "1"
        + "2"
        + "AB*D"
        + " " * 176
    )
    content = (
        record + "\r\n" + record.replace("00001", "00002", 1) + "\r\n\x1a"
    ).encode()
    spec = "SEQ 4 6\nrollno 41 12\nlang 53 1\nlevel 54 1\nq1..q200 55 1"
    columns, rows = compare.read_table(
        content, {"format": "fixed", "header_row": 0, "fixed": spec}
    )
    assert (
        columns[:5] == ["SEQ", "rollno", "lang", "level", "q1"] and len(columns) == 204
    )
    assert len(rows) == 2 and rows[1][0] == "100002"
    assert rows[0][1:8] == ["993100270006", "1", "2", "A", "B", "*", "D"]


def test_level_bands_limit_the_questions_compared(compared):
    assert compare.parse_bands("1: q1..q3; 2=q4..q5") == {
        "1": {"q1", "q2", "q3"},
        "2": {"q4", "q5"},
    }
    bands = compare.parse_bands("1: q1..q2\n2: q3..q4")
    assert compare.sheet_band({"band_source": "theirs"}, bands, "2", "1") == {
        "q3",
        "q4",
    }
    assert compare.sheet_band({"band_source": "either"}, bands, "2", "1") == {
        "q1",
        "q2",
        "q3",
        "q4",
    }
    assert compare.sheet_band({"band_source": "theirs"}, bands, " ", "1") is None
    # Default: their level, ours where theirs is blank or a multi-mark
    assert compare.sheet_band({}, bands, "*", "1") == {"q1", "q2"}
    assert compare.sheet_band({}, bands, "2", "1") == {"q3", "q4"}
    assert compare.sheet_band({}, bands, "", "") is None
    with pytest.raises(compare.CompareError):
        compare.parse_bands("q1..q5")

    client, template_id, results, preview = compared
    session = create(client, template_id, preview)
    assert session["summary"]["mismatches"] == 3
    # Their q20 says which band each sheet answers: only q1 is compared
    band = {
        "band_field": "q20",
        "band_source": "theirs",
        "band_map": "A: q1; B: q1; C: q1; D: q1",
    }
    patched = client.patch(
        f"/compare/sessions/{session['id']}", json={"rules": band}
    ).json()
    assert patched["summary"]["mismatches"] == 1


def test_a_multi_mark_code_on_both_sides_agrees():
    rules = compare.DEFAULT_RULES
    ours = compare.our_value("*", "", False, False, rules)
    assert ours == (compare.MULTI, "")
    assert compare.differs(compare.external_value("*", rules), ours, rules) is False


def test_a_file_read_again_compares_its_newest_scan(compared, spec):
    client, template_id, results, preview = compared
    newer = scan_one(
        client, template_id, flagged_sheet(spec, seed=1), name="100001.png"
    )
    session = create(client, template_id, preview)
    base = f"/compare/sessions/{session['id']}"
    states = session["progress"]["sheets"]
    assert states.get("replaced") == 1 and states["pending"] == 2, states
    sheet = client.post(f"{base}/claim", json={}).json()["sheet"]
    assert sheet["scan_id"] == newer["scan_id"]
    # Or none of them
    patched = client.patch(base, json={"rules": {"duplicates": "skip"}}).json()
    assert patched["progress"]["sheets"].get("duplicate") == 2


def test_choices_saved_to_our_results_and_a_sheet_compared_again(compared):
    client, template_id, results, preview = compared
    session = create(client, template_id, preview, rules={"save_to": "results"})
    base = f"/compare/sessions/{session['id']}"
    sheet = client.post(f"{base}/claim", json={}).json()["sheet"]
    scan_id = sheet["scan_id"]
    assert sheet["save_to"] == "results"
    theirs = next(i["external"] for i in sheet["items"] if i["name"] == "q1")
    saved = client.post(
        f"{base}/sheets/{scan_id}", json={"decisions": {"q1": "external"}}
    )
    assert saved.json()["written"] == ["q1"]
    assert client.get(f"/scans/{scan_id}").json()["responses"]["q1"] == theirs
    # Undone: our result goes back to the value as read
    undone = client.post(f"{base}/sheets/{scan_id}", json={"decisions": {"q1": None}})
    assert undone.json()["written"] == ["q1"]
    assert (
        client.get(f"/scans/{scan_id}").json()["responses"]["q1"]
        == results[0]["responses"]["q1"]
    )
    # Corrected in Results, then compared again: only q2 still differs
    client.post(f"/scans/{scan_id}/corrections", json={"changes": {"q1": theirs}})
    again = client.post(f"{base}/sheets/{scan_id}/recompare").json()
    assert [i["name"] for i in again["sheet"]["items"]] == ["q2"]


def test_a_comparison_is_written_a_part_at_a_time(compared):
    client, template_id, results, preview = compared
    session = create(client, template_id, preview)
    ctx = client.app.state.ctx
    full = ctx.compare.session(session["id"])
    plan = compare.prepare(
        full,
        compare.load_source(ctx.data.root / "compare" / full["id"]),
        ctx.templates.template_json(template_id),
    )
    scans = list(ctx.index.iter_results(template_id=template_id))
    load = lambda scan_id: read_json(
        ctx.data.scan_dir(scan_id) / "result.json"
    )  # noqa: E731
    seen = []
    summary = compare.run(
        ctx.compare,
        full,
        plan,
        scans,
        load,
        {},
        progress=lambda done, total, _: seen.append((done, total)),
        chunk=1,
    )
    assert seen[:3] == [(1, 3), (2, 3), (3, 3)] and summary["mismatches"] == 3
    # A newer comparison of the session took over: this one stops
    assert (
        compare.run(ctx.compare, full, plan, scans, load, {}, current=lambda: False)
        is None
    )


def test_a_long_comparison_answers_at_once_and_counts_as_it_goes(compared, monkeypatch):
    from src.api import compare_routes

    client, template_id, results, preview = compared
    monkeypatch.setattr(compare_routes, "BUILD_WAIT_SECONDS", 0)
    session = create(client, template_id, preview)
    deadline = time.time() + 20
    while session.get("building") and time.time() < deadline:
        time.sleep(0.05)
        session = client.get(f"/compare/sessions/{session['id']}").json()
    assert not session.get("building")
    assert session["summary"]["sheets"] == 3 and session["summary"]["mismatches"] == 3
