"""Capability report: which optional engines loaded (plan item 19)."""

import sys

from src import capabilities


def test_report_shape():
    report = capabilities.engine_report()
    for key in ("onnxruntime", "tesseract", "paddleocr", "zxing", "pyzbar"):
        assert set(report[key]) == {"available", "detail"}
        assert isinstance(report[key]["available"], bool)
    assert all(isinstance(v, bool) for v in capabilities.summary(report).values())


def test_missing_engine_turns_off_cleanly(monkeypatch):
    # A None entry in sys.modules makes the import fail, like a DLL error on Windows 7
    monkeypatch.setitem(sys.modules, "onnxruntime", None)
    entry = capabilities.engine_report()["onnxruntime"]
    assert entry["available"] is False
    assert "unavailable" in entry["detail"]


def test_health_lists_engines(tmp_path):
    from fastapi.testclient import TestClient

    from src.api.app import create_app

    with TestClient(create_app(tmp_path / "data", workers=1)) as client:
        body = client.get("/health").json()
    assert body["status"] == "ok"
    assert isinstance(body["engines"]["tesseract"], bool)
    assert "onnxruntime" in body["engines"]
