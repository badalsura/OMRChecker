"""
A small local server that keeps one vision-language model (VLM) loaded for the
handwriting reader (src/readers/vlm.py).

Every worker process sends its zone crops here instead of loading its own
copy of the model: one copy in memory (about 1.3 GB for PaddleOCR-VL 1.5 at
4 bits), one queue on the GPU. Runs on Apple Silicon through mlx-vlm:

    pip install mlx-vlm
    python -m src.readers.vlm_server --model paddleocr-vl --port 8765

The reader starts it by itself when ocr_params.icr_vlm_autostart is on (the
default) and nothing answers on icr_vlm_url.

    GET  /health -> {"model": "...", "ready": true}
    POST /read   {"image": "<base64 PNG>", "prompt": "...", "max_tokens": 32}
              -> {"text": "...", "ms": 140.2}
"""

import argparse
import base64
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Short names for the models tested on the TERI roll numbers (whole number
# exactly right: PaddleOCR-VL 89%, Qwen2.5-VL 3B 85.5%, Qwen3-VL 2B 84.5%)
MODELS = {
    "paddleocr-vl": "mlx-community/PaddleOCR-VL-1.5-4bit",
    "qwen3-vl-2b": "mlx-community/Qwen3-VL-2B-Instruct-4bit",
    "qwen2.5-vl-3b": "mlx-community/Qwen2.5-VL-3B-Instruct-4bit",
}


def model_id(name):
    return MODELS.get(name, name)


class Model:
    """One mlx-vlm model; generate() runs one image at a time."""

    def __init__(self, name):
        from mlx_vlm import load
        from mlx_vlm.utils import load_config

        self.name = model_id(name)
        self.model, self.processor = load(self.name)
        self.config = load_config(self.name)
        self.lock = threading.Lock()

    def generate(self, png, prompt, max_tokens=32):
        from mlx_vlm import generate
        from mlx_vlm.prompt_utils import apply_chat_template

        formatted = apply_chat_template(
            self.processor, self.config, prompt, num_images=1
        )
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
            handle.write(png)
        try:
            with self.lock:
                out = generate(
                    self.model,
                    self.processor,
                    formatted,
                    [handle.name],
                    max_tokens=max_tokens,
                    temperature=0.0,
                    verbose=False,
                )
        finally:
            os.unlink(handle.name)
        # Older mlx-vlm returns the text, newer a GenerationResult
        return getattr(out, "text", out)


def make_handler(model):
    class Handler(BaseHTTPRequestHandler):
        def send(self, code, body):
            data = json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/health":
                self.send(200, {"model": model.name, "ready": True})
            else:
                self.send(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/read":
                self.send(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                request = json.loads(self.rfile.read(length) or b"{}")
                png = base64.b64decode(request["image"])
                started = time.perf_counter()
                text = model.generate(
                    png, request["prompt"], int(request.get("max_tokens") or 32)
                )
                ms = (time.perf_counter() - started) * 1000
                self.send(200, {"text": text, "ms": round(ms, 1)})
            except Exception as error:  # report it; keep serving other zones
                self.send(500, {"error": str(error)})

        def log_message(self, *args):
            pass

    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--model", default="paddleocr-vl")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    model = Model(args.model)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(model))
    print(f"{model.name} ready on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
