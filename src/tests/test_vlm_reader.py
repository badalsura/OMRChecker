"""
Vision-language model handwriting reader (src/readers/vlm.py). A stand-in
server on a free local port answers like src/readers/vlm_server.py (or an
OpenAI-compatible endpoint), so no model is downloaded.
"""

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np
import pytest

from src.readers import icr, vlm
from src.readers.base import finalize
from src.template import Zone


def boxed(text, boxes):
    width = boxes * 40
    crop = np.full((40, width, 3), 255, np.uint8)
    for i in range(1, boxes):
        crop[:, i * 40] = 0
    for i, ch in enumerate(text):
        if ch != " ":
            cv2.putText(crop, ch, (i * 40 + 8, 32), cv2.FONT_HERSHEY_SIMPLEX, 1.1, 0, 3)
    page = np.full((200, 400, 3), 255, np.uint8)
    page[20:60, 50 : 50 + width] = crop
    zone = Zone("roll", "icr", [50, 20], [width, 40], {"characterBoxes": boxes, "whitelist": "0123456789"})
    return page, zone


@pytest.fixture
def server():
    state = {"reply": "", "model": "mlx-community/PaddleOCR-VL-1.5-4bit", "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def send(self, body):
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path in ("/health", "/v1/models"):
                self.send({"model": state["model"], "ready": True, "data": []})
            else:
                self.send_error(404)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append((self.path, body))
            if self.path == "/v1/chat/completions":
                self.send({"choices": [{"message": {"content": state["reply"]}}]})
            else:
                self.send({"text": state["reply"], "ms": 1.0})

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{httpd.server_address[1]}"
    vlm.reset_cache()
    yield state
    httpd.shutdown()
    vlm.reset_cache()


def settings_for(url, **extra):
    return {"icr_engine": "vlm", "icr_vlm_url": url, "icr_vlm_autostart": False, **extra}


def test_vlm_reads_the_raw_zone(server):
    page, zone = boxed("90 2", 4)
    server["reply"] = " 9 0 2\n"
    result = finalize(icr.read_icr_zone(zone, page, None, settings_for(server["url"])), zone)
    assert result.engine == "vlm" and result.value == "902"
    assert result.details["inked_boxes"] == 3 and not result.needs_review
    path, body = server["requests"][-1]
    # PaddleOCR-VL's own task prompt; the whole zone, box lines included,
    # enlarged 1.6x as measured
    assert path == "/read" and body["prompt"] == "OCR:"
    image = cv2.imdecode(np.frombuffer(base64.b64decode(body["image"]), np.uint8), 0)
    assert image.shape == (64, 256)


def test_prompt_for_other_models():
    assert vlm.prompt_for("qwen3-vl-2b", "0123456789", 12).startswith(
        "This image shows a 12-box handwritten number, one digit per box."
    )
    assert "character" in vlm.prompt_for("qwen2.5-vl-3b", None, None)


def test_centered_digit_matches_mnist_layout():
    from src.ml.classifiers import centered_digit

    box = np.full((40, 30), 255, np.uint8)
    box[5:35, 20:24] = 0  # a "1" off to the right
    digit = centered_digit(box)
    assert digit.shape == (28, 28) and digit.max() <= 1.0
    ys, xs = np.nonzero(digit > 0)
    assert 20 <= ys.max() - ys.min() + 1 <= 21  # tall side fitted to 20 px
    assert abs(xs.mean() - 14) < 1.5  # moved to the centre
    assert not centered_digit(np.full((40, 30), 255, np.uint8)).any()


def test_vlm_count_mismatch_goes_to_review(server):
    page, zone = boxed("90 2", 4)
    server["reply"] = "9002"
    result = finalize(icr.read_icr_zone(zone, page, None, settings_for(server["url"])), zone)
    assert "icr_alignment" in result.flags and result.needs_review
    # Characters outside the whitelist are dropped
    server["reply"] = "9O2."
    result = icr.read_icr_zone(zone, page, None, settings_for(server["url"]))
    assert result.value == "92"


def test_zone_option_picks_the_vlm(server):
    page, zone = boxed("7", 1)
    zone.options["icrEngine"] = "vlm"
    server["reply"] = "7"
    settings = {"icr_vlm_url": server["url"], "icr_vlm_autostart": False}
    assert icr.read_icr_zone(zone, page, None, settings).engine == "vlm"


def test_wrong_model_on_the_server_is_not_used(server):
    page, zone = boxed("7", 1)
    settings = settings_for(server["url"], icr_vlm_model="qwen3-vl-2b", icr_engine="vlm")
    result = icr.read_icr_zone(zone, page, None, dict(settings, paddle_model_dir="/nonexistent"))
    assert result.engine != "vlm"
    assert "Qwen3-VL" in result.details["vlm_unavailable"]
    assert not server["requests"]


def test_no_server_falls_back_to_the_usual_reader():
    page, zone = boxed("7", 1)
    settings = settings_for("http://127.0.0.1:9", paddle_model_dir="/nonexistent")
    vlm.reset_cache()
    result = icr.read_icr_zone(zone, page, None, settings)
    assert result.engine != "vlm" and "vlm_unavailable" in result.details


def test_openai_compatible_endpoint(server):
    page, zone = boxed("12", 2)
    server["reply"] = "12"
    settings = settings_for(server["url"] + "/v1", icr_vlm_model="qwen2.5vl:3b")
    result = icr.read_icr_zone(zone, page, None, settings)
    assert result.value == "12"
    path, body = server["requests"][-1]
    assert path == "/v1/chat/completions" and body["model"] == "qwen2.5vl:3b"
    assert body["messages"][0]["content"][0]["image_url"]["url"].startswith("data:image/png;base64,")


def test_second_opinion_from_the_icr_model(server):
    class Classifier:
        labels = ["<blank>"] + list("0123456789")

        def predict_proba(self, crops):
            probs = np.zeros((len(crops), len(self.labels)))
            probs[:, self.labels.index("1")] = 1.0
            return probs

    page, zone = boxed("12", 2)
    server["reply"] = "12"
    result = icr.read_icr_zone(zone, page, Classifier(), settings_for(server["url"]))
    assert result.value == "12"
    assert result.details["second_reader"]["characters"] == ["1", "1"]
    assert "engine_disagree" in result.flags


def test_digit_pattern_drops_stray_marks_without_a_whitelist(server):
    page, zone = boxed("90 2", 4)
    del zone.options["whitelist"]
    zone.options["pattern"] = r"\d{3}"
    server["reply"] = "9a0.2"
    assert icr.read_icr_zone(zone, page, None, settings_for(server["url"])).value == "902"
