"""
Portable desktop launcher for the OMR engine (entry point of the Windows exe).

    OMRChecker.exe                         start the API + web GUI, open the browser
    OMRChecker.exe --no-browser --port 8000 --host 0.0.0.0
    OMRChecker.exe --bulk D:\\scans --template exam\\template.json [--out D:\\out] [--workers 8]
    OMRChecker.exe --selftest              headless check: synthetic sheet, barcode, process pool
    OMRChecker.exe --version

Everything stays next to the exe (portable): data in <exe dir>\\omr_data, an
optional Tesseract build in <exe dir>\\tesseract\\tesseract.exe. If the exe
folder is read-only the data dir falls back to %LOCALAPPDATA%\\OMRChecker.

Runs from source too: python packaging/omr_desktop.py --selftest
"""

import argparse
import json
import multiprocessing
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

APP_NAME = "OMRChecker"
LAUNCHER_VERSION = "1.0.0"
PREFERRED_PORT = 8765

FROZEN = getattr(sys, "frozen", False)
# Folder the user sees (portable data lives here)
APP_DIR = (
    Path(sys.executable).resolve().parent
    if FROZEN
    else Path(__file__).resolve().parents[1]
)
# Folder with bundled resources (onefile: temporary _MEIPASS extraction dir)
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))

if not FROZEN and str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))


# --------------------------------------------------------------------- environment
def configure_environment():
    """PATH/TESSDATA for a bundled Tesseract; quiet, single-threaded native libs per worker."""
    candidates = [APP_DIR / "tesseract", RESOURCE_DIR / "tesseract"]
    if os.name == "nt":
        candidates += [
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tesseract-OCR",
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
            / "Tesseract-OCR",
        ]
    exe_name = "tesseract.exe" if os.name == "nt" else "tesseract"
    for folder in candidates:
        if (folder / exe_name).exists():
            os.environ["PATH"] = str(folder) + os.pathsep + os.environ.get("PATH", "")
            tessdata = folder / "tessdata"
            if tessdata.is_dir() and not os.environ.get("TESSDATA_PREFIX"):
                os.environ["TESSDATA_PREFIX"] = str(tessdata)
            try:
                import pytesseract

                pytesseract.pytesseract.tesseract_cmd = str(folder / exe_name)
            except ImportError:
                pass
            break
    # matplotlib (imported by the engine) would otherwise rebuild its font cache
    # in a fresh temp dir on every start of the exe and of every worker process
    os.environ.setdefault("MPLBACKEND", "Agg")
    # (PyInstaller's matplotlib runtime hook points MPLCONFIGDIR at a temp dir)
    if FROZEN or "MPLCONFIGDIR" not in os.environ:
        for cache in (
            APP_DIR / "omr_data" / ".cache" / "matplotlib",
            Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".cache")
            / APP_NAME
            / "matplotlib",
        ):
            if writable(cache):
                os.environ["MPLCONFIGDIR"] = str(cache)
                # Read back by spawned worker processes (see __main__)
                os.environ["OMR_MPLCONFIGDIR"] = str(cache)
                break
    # A packaged GUI must not depend on the console encoding (cp1252 on Win7)
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")


def writable(folder):
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / ".write_test"
        probe.write_text("ok")
        probe.unlink()
        return True
    except OSError:
        return False


def default_data_dir():
    portable = APP_DIR / "omr_data"
    if writable(portable):
        return portable
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_NAME / "omr_data"


def pick_port(host, preferred):
    for port in ([preferred] if preferred else []) + [PREFERRED_PORT, 0]:
        with socket.socket() as sock:
            try:
                sock.bind((host, port))
                return sock.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError("No free TCP port")


# --------------------------------------------------------------------- server
def build_app(data_dir, workers, api_key):
    from src.api.app import create_app

    overrides = {"workers": workers}
    if api_key:
        overrides["api_key"] = api_key
    # create_app serves the bundled in-browser reader at /browser
    return create_app(str(data_dir), **overrides)


class ServerThread(threading.Thread):
    def __init__(self, app, host, port, log_level="warning"):
        super().__init__(name="omr-server", daemon=True)
        import uvicorn

        # log_config=None: the default dictConfig needs a console (absent in --noconsole exes)
        config = uvicorn.Config(
            app, host=host, port=port, log_level=log_level, log_config=None
        )
        self.server = uvicorn.Server(config)

    def run(self):
        self.server.run()

    def wait_started(self, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline and self.is_alive():
            if self.server.started:
                return True
            time.sleep(0.05)
        return False

    def stop(self):
        self.server.should_exit = True
        self.join(timeout=15)


def run_gui(args):
    data_dir = Path(args.data_dir) if args.data_dir else default_data_dir()
    host = args.host
    port = pick_port(host, args.port)
    print(f"Data folder: {data_dir}")
    print("Loading engine ...", flush=True)
    app = build_app(data_dir, args.workers, args.api_key)
    server = ServerThread(app, host, port, args.log_level)
    server.start()
    if not server.wait_started():
        print("The server failed to start", file=sys.stderr)
        return 1
    shown_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
    url = f"http://{shown_host}:{port}/"
    print(f"OMR GUI running at {url}  (API docs: {url}docs)", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        if args.no_window or not show_window(url, data_dir, server):
            while server.is_alive():
                server.join(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


def show_window(url, data_dir, server):
    """A tiny Tk control window. Returns False if Tk is unavailable."""
    try:
        import tkinter as tk
    except ImportError:
        return False
    try:
        root = tk.Tk()
    except tk.TclError:  # no display
        return False
    root.title(f"{APP_NAME} {LAUNCHER_VERSION}")
    root.resizable(False, False)
    pad = {"padx": 10, "pady": 4}
    tk.Label(root, text="OMR engine is running", font=("Segoe UI", 11, "bold")).pack(
        **pad
    )
    entry = tk.Entry(root, width=42, justify="center")
    entry.insert(0, url)
    entry.configure(state="readonly")
    entry.pack(**pad)
    tk.Label(root, text=f"Data: {data_dir}", fg="#555").pack(**pad)
    tk.Label(
        root,
        text="Use Chrome 80+ / Firefox 74+ / Edge 80+ (Win7: Chrome 109, Firefox ESR 115)",
        fg="#555",
    ).pack(**pad)
    buttons = tk.Frame(root)
    buttons.pack(pady=8)

    def quit_app():
        root.destroy()

    def copy_url():
        root.clipboard_clear()
        root.clipboard_append(url)

    tk.Button(
        buttons, text="Open GUI", width=12, command=lambda: webbrowser.open(url)
    ).pack(side="left", padx=4)
    tk.Button(buttons, text="Copy URL", width=12, command=copy_url).pack(
        side="left", padx=4
    )
    tk.Button(buttons, text="Quit", width=12, command=quit_app).pack(
        side="left", padx=4
    )
    root.protocol("WM_DELETE_WINDOW", quit_app)

    def watch():
        if not server.is_alive():
            root.destroy()
        else:
            root.after(1000, watch)

    root.after(1000, watch)
    root.mainloop()
    return True


# --------------------------------------------------------------------- headless
def run_bulk(args):
    from src.batch import main as batch_main

    out = args.out or str(
        Path(args.bulk).resolve().parent / (Path(args.bulk).name + "_omr_results")
    )
    argv = ["--template", args.template, "--input", args.bulk, "--out", out]
    if args.workers:
        argv += ["--workers", str(args.workers)]
    for flag, value in (
        ("--config", args.config),
        ("--evaluation", args.evaluation),
        ("--bubble-model", args.bubble_model),
        ("--icr-model", args.icr_model),
    ):
        if value:
            argv += [flag, value]
    if args.save_images:
        argv.append("--save-images")
    print(f"Results -> {out}", flush=True)
    return batch_main(argv)


def selftest(workers=2):
    """Read synthetic sheets in-process and through a process pool; check barcodes."""
    import random
    import tempfile

    import cv2

    from src.batch import scan_files
    from src.pipeline import OMREngine
    from src.readers.barcode import supported_formats
    from src.synth.render import default_spec, random_answers, render_sheet

    report = {
        "version": LAUNCHER_VERSION,
        "python": sys.version.split()[0],
        "frozen": FROZEN,
    }
    report["opencv"] = cv2.__version__
    formats = supported_formats()
    report["barcode_formats"] = len(formats)
    assert any("128" in f for f in formats), "Code 128 not supported by zxing-cpp"
    assert any("QR" in f for f in formats), "QR not supported by zxing-cpp"
    import numpy as np
    import zxingcpp

    from src.readers.barcode import decode_symbols

    decoded = {}
    for name in ("Code128", "QRCode"):
        fmt = zxingcpp.barcode_format_from_str(name)
        code = np.array(
            zxingcpp.write_barcode_to_image(
                zxingcpp.create_barcode("OMR-12345", fmt), scale=3
            )
        )
        code = np.pad(code.astype(np.uint8), 30, constant_values=255)
        symbols = decode_symbols(code)
        assert symbols and symbols[0].text == "OMR-12345", f"{name} round trip failed"
        decoded[name] = symbols[0].text
        if name == "Code128":
            from src.readers import linear

            found = linear.decode(code)
            assert found and found["text"] == "OMR-12345", "built-in decoder failed"
            decoded["builtin"] = found["text"]
    report["barcode_roundtrip"] = decoded
    from src.readers.barcode import available_engines

    report["barcode_engines"] = available_engines()
    try:
        import onnxruntime

        report["onnxruntime"] = onnxruntime.__version__
    except Exception as error:  # optional on Windows 7
        report["onnxruntime"] = f"unavailable ({type(error).__name__})"
    from src.readers.ocr import tesseract_available

    report["tesseract"] = tesseract_available()
    from src.capabilities import engine_report

    # Which optional engines loaded; missing ones switch their feature off
    report["engines"] = engine_report()

    spec = default_spec(questions=20, roll_digits=4, with_zones=False)
    with tempfile.TemporaryDirectory() as tmp:
        template = Path(tmp) / "template.json"
        # Default pre-processors (timing-mark registration) exercise plugin discovery
        template.write_text(json.dumps(spec.to_template(pre_processors=None)))
        paths, truths = [], []
        for seed in range(3):
            rng = random.Random(seed)
            image, truth = render_sheet(
                spec, random_answers(spec, rng, blank_rate=0.0), rng=rng
            )
            path = Path(tmp) / f"sheet{seed}.png"
            cv2.imwrite(str(path), image)
            paths.append(path)
            truths.append(truth["answers"])

        started = time.perf_counter()
        engine = OMREngine(str(template))
        result = engine.scan_path(str(paths[0]))[0]
        wrong = [k for k, v in truths[0].items() if result.responses.get(k) != v]
        assert not wrong, f"in-process read mismatched {wrong}"
        report["in_process_ms"] = round((time.perf_counter() - started) * 1000)

        started = time.perf_counter()
        results = list(scan_files(paths, template, workers=workers))
        for result, truth in zip(results, truths):
            wrong = [k for k, v in truth.items() if result["responses"].get(k) != v]
            assert not wrong, f"pool read mismatched {wrong}"
        report["pool_workers"] = workers
        report["pool_ms"] = round((time.perf_counter() - started) * 1000)

    import fastapi
    import uvicorn

    report["fastapi"] = fastapi.__version__
    report["uvicorn"] = uvicorn.__version__
    report["static_gui"] = (
        Path(__import__("src.api.app", fromlist=["x"]).STATIC_DIR) / "index.html"
    ).exists()
    assert report["static_gui"], "web GUI files were not bundled"
    browser_dir = __import__("src.api.app", fromlist=["x"]).BROWSER_DIR
    report["browser_app"] = (
        str(browser_dir) if (browser_dir / "omr.js").exists() else None
    )
    report["ok"] = True
    print(json.dumps(report, indent=2))
    return 0


# --------------------------------------------------------------------- main
def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog=APP_NAME, description="OMR engine: web GUI, API and bulk reader"
    )
    parser.add_argument("--version", action="store_true")
    parser.add_argument(
        "--selftest", action="store_true", help="Headless self check, exit code 0 = OK"
    )
    parser.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to serve the LAN")
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help=f"Default: {PREFERRED_PORT} or any free port",
    )
    parser.add_argument("--data-dir", help="Default: <exe folder>\\omr_data")
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="Worker processes (default: all cores)",
    )
    parser.add_argument("--api-key", default=os.environ.get("OMR_API_KEY"))
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument(
        "--no-window", action="store_true", help="No Tk window (server/console mode)"
    )
    parser.add_argument("--log-level", default="warning")
    bulk = parser.add_argument_group("headless bulk mode")
    bulk.add_argument(
        "--bulk", metavar="FOLDER", help="Read every image/PDF in FOLDER and exit"
    )
    bulk.add_argument("--template", help="template.json for --bulk")
    bulk.add_argument(
        "--out", help="Output folder for --bulk (results.csv + results.jsonl)"
    )
    bulk.add_argument("--config")
    bulk.add_argument("--evaluation")
    bulk.add_argument("--bubble-model")
    bulk.add_argument("--icr-model")
    bulk.add_argument("--save-images", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.version:
        print(f"{APP_NAME} {LAUNCHER_VERSION} (Python {sys.version.split()[0]})")
        return 0
    configure_environment()
    if args.selftest:
        return selftest(workers=args.workers or 2)
    if args.bulk:
        if not args.template:
            print("--bulk needs --template path/to/template.json", file=sys.stderr)
            return 2
        return run_bulk(args)
    return run_gui(args)


if __name__ == "__main__":
    # Worker processes of a frozen exe re-run PyInstaller's matplotlib hook,
    # which resets MPLCONFIGDIR to a fresh temp dir; restore the shared cache
    if os.environ.get("OMR_MPLCONFIGDIR"):
        os.environ["MPLCONFIGDIR"] = os.environ["OMR_MPLCONFIGDIR"]
    # Must run before anything else: worker processes of a frozen exe re-enter here
    multiprocessing.freeze_support()
    if sys.platform != "win32":
        # Same semantics as Windows everywhere (and fork is unsafe with server threads)
        try:
            multiprocessing.set_start_method("spawn")
        except RuntimeError:
            pass
    sys.exit(main())
