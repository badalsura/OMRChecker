"""
Vision-language models (VLMs) as the handwriting reader of ICR zones.

On the TERI roll numbers PaddleOCR-VL 1.5 read 85% of the whole numbers
exactly, against 22% for PaddleOCR line recognition and 78% for the best
per-box digit classifier. A VLM reads the raw zone crop, box lines and all.

The model runs in one local server process (src/readers/vlm_server.py) shared
by every worker; this module is its client. ocr_params:

    "icr_engine": "vlm",
    "icr_vlm_model": "paddleocr-vl",        (or qwen3-vl-2b, qwen2.5-vl-3b, an id)
    "icr_vlm_url": "http://127.0.0.1:8765",
    "icr_vlm_autostart": true,              start the server when none answers
    "icr_vlm_timeout": 60

A URL ending in /v1 is treated as an OpenAI-compatible chat endpoint instead
(Ollama, vLLM, LM Studio), so a GPU machine elsewhere can do the reading.
"""

import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

import cv2

from src.logger import logger

DEFAULT_URL = "http://127.0.0.1:8765"
DEFAULT_MODEL = "paddleocr-vl"
# First start downloads the model (0.7-2 GB) before the server answers
START_TIMEOUT = 600
ROOT = Path(__file__).resolve().parents[2]

_ENGINES = {}
_LOCK = threading.Lock()


# The crop is enlarged before reading; 1.6x was the setting measured on TERI
SCALE = 1.6


def prompt_for(model, whitelist=None, count=None):
    """
    PaddleOCR-VL takes its own task prompt. The others get the prompt measured
    on 12-box roll numbers, with the zone's box count.
    """
    if "paddleocr-vl" in str(model).lower():
        return "OCR:"
    digits = bool(whitelist) and set(whitelist) <= set("0123456789")
    kind = "digit" if digits else "character"
    if count:
        return (
            f"This image shows a {count}-box handwritten "
            f"{'number' if digits else 'text'}, one {kind} per box. "
            f"Reply with exactly the {kind}s written, left to right, nothing else."
        )
    return f"Read the handwritten {kind}s in this image. Reply with them only, nothing else."


def encode_png(image):
    if SCALE != 1:
        image = cv2.resize(image, None, fx=SCALE, fy=SCALE, interpolation=cv2.INTER_CUBIC)
    ok, data = cv2.imencode(".png", image)
    if not ok:
        raise ValueError("could not encode the zone crop")
    return base64.b64encode(data.tobytes()).decode("ascii")


class VlmEngine:
    def __init__(self, model=DEFAULT_MODEL, url=DEFAULT_URL, autostart=True,
                 timeout=60, python=None):
        self.model = model or DEFAULT_MODEL
        self.url = (url or DEFAULT_URL).rstrip("/")
        self.openai = self.url.endswith("/v1")
        self.autostart = autostart
        self.timeout = timeout
        self.python = python or sys.executable
        self.unavailable_reason = None
        self._checked = 0.0

    # ------------------------------------------------------------ server
    def _request(self, path, body=None, timeout=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self.url + path,
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=timeout or self.timeout) as reply:
            return json.loads(reply.read() or b"{}")

    def health(self):
        """The server's /health answer, or None when nothing answers."""
        if self.openai:
            try:
                return self._request("/models", timeout=3)
            except Exception:
                return None
        try:
            return self._request("/health", timeout=3)
        except Exception:
            return None

    def _local(self):
        return urlparse(self.url).hostname in ("127.0.0.1", "localhost", "::1")

    def _start_server(self):
        """Start vlm_server once for all worker processes (a lock file decides
        which one) and wait until it answers."""
        if sys.platform != "darwin":
            return "the built-in VLM server needs Apple Silicon (mlx-vlm)"
        import fcntl

        port = urlparse(self.url).port or 80
        lock_path = Path(tempfile.gettempdir()) / f"omr-vlm-{port}.lock"
        with open(lock_path, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if self.health() is None:
                log = Path(tempfile.gettempdir()) / f"omr-vlm-{port}.log"
                logger.info(f"Starting the VLM server ({self.model}); log: {log}")
                with open(log, "ab") as out:
                    self.process = subprocess.Popen(
                        [self.python, "-m", "src.readers.vlm_server",
                         "--model", self.model, "--port", str(port)],
                        cwd=str(ROOT),
                        stdout=out,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                deadline = time.time() + START_TIMEOUT
                while time.time() < deadline:
                    if self.health() is not None:
                        break
                    if self.process.poll() is not None:
                        return f"the VLM server exited; see {log}"
                    time.sleep(1)
                else:
                    return f"the VLM server did not answer within {START_TIMEOUT}s; see {log}"
        return None

    @property
    def available(self):
        # Checked once; a missing server is looked for again after a minute
        if not self._checked or (
            self.unavailable_reason and time.time() - self._checked > 60
        ):
            with _LOCK:
                if not self._checked or self.unavailable_reason:
                    self.unavailable_reason = self._check()
                    self._checked = time.time()
                    if self.unavailable_reason:
                        logger.warning(f"VLM reader unavailable: {self.unavailable_reason}")
        return self.unavailable_reason is None

    def _check(self):
        answer = self.health()
        if answer is None and self.autostart and self._local() and not self.openai:
            reason = self._start_server()
            if reason:
                return reason
            answer = self.health()
        if answer is None:
            return f"nothing answers at {self.url}"
        if not self.openai:
            from src.readers.vlm_server import model_id

            running = answer.get("model")
            if running and running != model_id(self.model):
                return f"the server at {self.url} runs {running}, not {model_id(self.model)}"
        return None

    # ------------------------------------------------------------ reading
    def read(self, image, prompt, max_tokens=32):
        png = encode_png(image)
        if self.openai:
            reply = self._request("/chat/completions", {
                "model": self.model,
                "temperature": 0,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": [
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/png;base64,{png}"}},
                    {"type": "text", "text": prompt},
                ]}],
            })
            return reply["choices"][0]["message"]["content"] or ""
        reply = self._request(
            "/read", {"image": png, "prompt": prompt, "max_tokens": max_tokens}
        )
        if "error" in reply:
            raise RuntimeError(reply["error"])
        return reply.get("text") or ""


def get_engine(settings, zone=None):
    options = zone.options if zone is not None else {}
    key = (
        options.get("vlmModel") or settings.get("icr_vlm_model") or DEFAULT_MODEL,
        settings.get("icr_vlm_url") or DEFAULT_URL,
        bool(settings.get("icr_vlm_autostart", True)),
        float(settings.get("icr_vlm_timeout") or 60),
        os.environ.get("OMR_VLM_PYTHON") or None,
    )
    if key not in _ENGINES:
        _ENGINES[key] = VlmEngine(*key)
    return _ENGINES[key]


def reset_cache():
    _ENGINES.clear()


def capabilities(settings):
    """For the GUI: is a VLM server answering now (never starts one)."""
    engine = VlmEngine(
        settings.get("icr_vlm_model") or DEFAULT_MODEL,
        settings.get("icr_vlm_url") or DEFAULT_URL,
        autostart=False,
    )
    from src.readers.vlm_server import MODELS

    return {
        "running": engine.health() is not None,
        "url": engine.url,
        "model": engine.model,
        "models": sorted(MODELS),
        "autostart": bool(settings.get("icr_vlm_autostart", True)),
    }
