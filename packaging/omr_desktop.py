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


# Written by the installer (packaging/installer/OMRChecker.iss): data then
# lives in %LOCALAPPDATA%, never in the program folder the uninstaller removes
INSTALLED = (APP_DIR / "installed.ini").is_file()


def default_data_dir():
    portable = APP_DIR / "omr_data"
    if not INSTALLED and writable(portable):
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


class Station:
    """The local server and the remote tunnel the launcher window controls."""

    def __init__(self, args, data_dir):
        self.args = args
        self.data_dir = data_dir
        self.host = args.host
        self.port = None
        self.server = None
        self.starting = False
        self.error = None
        self.tunnel = None
        self.settings_path = data_dir / "remote_access.json"

    # ---- local server
    @property
    def running(self):
        return bool(self.server and self.server.is_alive() and self.server.server.started)

    @property
    def url(self):
        shown = "127.0.0.1" if self.host in ("0.0.0.0", "") else self.host
        return f"http://{shown}:{self.port}/" if self.port else None

    def start_server(self, port=None, host=None):
        """Blocking: loads the engine and serves; returns True once it runs."""
        self.starting, self.error = True, None
        try:
            self.stop_server()
            if host is not None:
                self.host = host
            self.port = pick_port(self.host, port)
            app = build_app(self.data_dir, self.args.workers, self.args.api_key)
            self.server = ServerThread(app, self.host, self.port, self.args.log_level)
            self.server.start()
            if not self.server.wait_started():
                self.error = f"The server could not start on port {self.port}"
                return False
            return True
        except Exception as error:  # shown in the window
            self.error = str(error)
            return False
        finally:
            self.starting = False

    def stop_server(self):
        self.stop_remote()
        server, self.server = self.server, None
        if server:
            server.stop()

    # ---- remote access
    def remote_settings(self):
        from src.api.tunnel import TunnelSettings

        return TunnelSettings.load(self.settings_path)

    def start_remote(self):
        from src.api.tunnel import Tunnel, find_cloudflared

        self.stop_remote()
        self.tunnel = Tunnel(
            find_cloudflared(APP_DIR, RESOURCE_DIR), self.port, self.remote_settings()
        )
        self.tunnel.start()
        return self.tunnel

    def stop_remote(self):
        tunnel, self.tunnel = self.tunnel, None
        if tunnel:
            tunnel.stop()

    def shutdown(self):
        self.stop_server()


def run_gui(args):
    data_dir = Path(args.data_dir) if args.data_dir else default_data_dir()
    print(f"Data folder: {data_dir}")
    print("Loading engine ...", flush=True)
    station = Station(args, data_dir)
    if not station.start_server(args.port):
        print(station.error, file=sys.stderr)
        return 1
    print(f"OMR GUI running at {station.url}  (API docs: {station.url}docs)", flush=True)
    if args.remote:
        from src.api.tunnel import wait_for

        tunnel = station.start_remote()
        if wait_for(tunnel) == "running":
            print(f"Remote address: {tunnel.url or 'see your Cloudflare dashboard'}", flush=True)
        else:
            print(f"Remote access failed: {tunnel.error}", file=sys.stderr)
    if not args.no_browser:
        webbrowser.open(station.url)
    try:
        if args.no_window or not show_window(station):
            while station.server and station.server.is_alive():
                station.server.join(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        station.shutdown()
    return 0


def show_window(station):
    """The control window. Returns False if Tk is unavailable."""
    try:
        import tkinter as tk
        from tkinter import messagebox
    except ImportError:
        return False
    try:
        root = tk.Tk()
    except tk.TclError:  # no display
        return False
    root.title(f"{APP_NAME} {LAUNCHER_VERSION}")
    root.resizable(False, False)
    grey = "#555"

    def copy(text):
        if text:
            root.clipboard_clear()
            root.clipboard_append(text)

    def readonly_entry(parent, width=44):
        entry = tk.Entry(parent, width=width, justify="center", state="readonly")

        def show(text):
            entry.configure(state="normal")
            entry.delete(0, "end")
            entry.insert(0, text or "")
            entry.configure(state="readonly")

        return entry, show

    # ---- local server
    local = tk.LabelFrame(root, text=" Server ", padx=10, pady=6)
    local.pack(fill="x", padx=10, pady=(10, 4))
    row = tk.Frame(local)
    row.pack(fill="x")
    tk.Label(row, text="Port").pack(side="left")
    port_var = tk.StringVar(value=str(station.port or PREFERRED_PORT))
    tk.Entry(row, textvariable=port_var, width=7).pack(side="left", padx=(4, 12))
    lan_var = tk.BooleanVar(value=station.host in ("0.0.0.0", ""))
    tk.Checkbutton(row, text="Other devices on this network can connect", variable=lan_var).pack(side="left")
    server_status = tk.Label(local, text="", fg=grey)
    server_status.pack(anchor="w", pady=(6, 0))
    url_row = tk.Frame(local)
    url_row.pack(fill="x", pady=4)
    url_entry, show_url = readonly_entry(url_row)
    url_entry.pack(side="left")
    tk.Button(url_row, text="Copy", width=6, command=lambda: copy(station.url)).pack(side="left", padx=4)
    buttons = tk.Frame(local)
    buttons.pack(fill="x", pady=(2, 0))

    def start_server():
        try:
            port = int(port_var.get())
            assert 1 <= port <= 65535
        except (ValueError, AssertionError):
            messagebox.showerror(APP_NAME, "Enter a port between 1 and 65535")
            return
        host = "0.0.0.0" if lan_var.get() else "127.0.0.1"

        def work():
            if station.start_server(port, host) and station.port != port:
                station.error = f"port {port} was busy; using {station.port}"

        threading.Thread(target=work, daemon=True).start()
        refresh()

    def stop_server():
        threading.Thread(target=station.stop_server, daemon=True).start()
        root.after(300, refresh)

    start_btn = tk.Button(buttons, text="Start server", width=13, command=start_server)
    stop_btn = tk.Button(buttons, text="Stop server", width=13, command=stop_server)
    open_btn = tk.Button(buttons, text="Open GUI", width=13, command=lambda: webbrowser.open(station.url))
    for button in (start_btn, stop_btn, open_btn):
        button.pack(side="left", padx=(0, 6))

    # ---- remote access
    remote = tk.LabelFrame(root, text=" Remote access (Cloudflare) ", padx=10, pady=6)
    remote.pack(fill="x", padx=10, pady=4)
    mode_label = tk.Label(remote, text="", fg=grey, justify="left")
    mode_label.pack(anchor="w")
    remote_status = tk.Label(remote, text="", fg=grey, justify="left", wraplength=420)
    remote_status.pack(anchor="w", pady=(4, 0))
    remote_row = tk.Frame(remote)
    remote_row.pack(fill="x", pady=4)
    remote_entry, show_remote = readonly_entry(remote_row)
    remote_entry.pack(side="left")
    tk.Button(
        remote_row, text="Copy", width=6,
        command=lambda: copy(station.tunnel and station.tunnel.url),
    ).pack(side="left", padx=4)
    remote_buttons = tk.Frame(remote)
    remote_buttons.pack(fill="x", pady=(2, 0))

    def start_remote():
        station.start_remote()
        refresh()

    def stop_remote():
        station.stop_remote()
        refresh()

    remote_start = tk.Button(remote_buttons, text="Start remote", width=13, command=start_remote)
    remote_stop = tk.Button(remote_buttons, text="Stop remote", width=13, command=stop_remote)
    settings_btn = tk.Button(
        remote_buttons, text="Cloudflare settings…", width=18,
        command=lambda: cloudflare_dialog(root, station, refresh),
    )
    for button in (remote_start, remote_stop, settings_btn):
        button.pack(side="left", padx=(0, 6))

    # ---- footer
    footer = tk.Frame(root)
    footer.pack(fill="x", padx=10, pady=(4, 10))
    tk.Label(footer, text=f"Data: {station.data_dir}", fg=grey).pack(side="left")

    def quit_app():
        root.destroy()

    tk.Button(footer, text="Quit", width=10, command=quit_app).pack(side="right")
    root.protocol("WM_DELETE_WINDOW", quit_app)

    def refresh():
        running, starting = station.running, station.starting
        if starting:
            server_status.configure(text="Starting the server …", fg=grey)
        elif running:
            note = f"   ({station.error})" if station.error else ""
            server_status.configure(text="Running" + note, fg="#1a7f37")
        else:
            server_status.configure(text=station.error or "Stopped", fg="#b42318" if station.error else grey)
        show_url(station.url if running else "")
        start_btn.configure(state="disabled" if starting else "normal", text="Restart server" if running else "Start server")
        stop_btn.configure(state="normal" if running else "disabled")
        open_btn.configure(state="normal" if running else "disabled")

        settings = station.remote_settings()
        mode = settings.mode
        mode_label.configure(
            text="Quick tunnel: a new random trycloudflare.com address each start"
            if mode == "quick"
            else f"Your address: https://{settings.hostname}" if settings.hostname
            else "Your Cloudflare tunnel (address set in the Cloudflare dashboard)"
        )
        tunnel = station.tunnel
        state = tunnel.state if tunnel else "stopped"
        texts = {
            "stopped": ("Not running", grey),
            "starting": ("Connecting to Cloudflare …", grey),
            "running": ("Online: anyone with the address can reach the sign-in page", "#1a7f37"),
            "error": ((tunnel.error if tunnel else "") or "Failed", "#b42318"),
        }
        text, colour = texts.get(state, texts["stopped"])
        remote_status.configure(text=text, fg=colour)
        show_remote(tunnel.url if tunnel and state == "running" else "")
        remote_start.configure(state="normal" if running and state in ("stopped", "error") else "disabled")
        remote_stop.configure(state="normal" if state in ("starting", "running") else "disabled")

    def tick():
        refresh()
        root.after(700, tick)

    tick()
    root.mainloop()
    return True


def cloudflare_dialog(root, station, done):
    """Tunnel token, or API token + hostname; empty = quick tunnel."""
    import tkinter as tk

    from src.api.tunnel import TunnelError, TunnelSettings

    current = station.remote_settings()
    dialog = tk.Toplevel(root)
    dialog.title("Cloudflare settings")
    dialog.resizable(False, False)
    dialog.transient(root)
    frame = tk.Frame(dialog, padx=12, pady=10)
    frame.pack()
    tk.Label(
        frame,
        justify="left",
        wraplength=460,
        text=(
            "Leave everything empty for a quick tunnel (random address, no account).\n\n"
            "For a fixed address on your own domain, fill in ONE of:\n"
            " A) Tunnel token: Cloudflare Zero Trust > Networks > Tunnels > your tunnel "
            "> install command. Point its public hostname at http://localhost:<port>.\n"
            " B) API token + hostname: the station creates the tunnel and the DNS record "
            "itself. Token permissions: Account > Cloudflare Tunnel: Edit, Zone > DNS: Edit."
        ),
    ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
    fields = {}
    for row, (key, label, secret) in enumerate(
        (
            ("tunnel_token", "Tunnel token (A)", True),
            ("api_token", "API token (B)", True),
            ("hostname", "Public hostname", False),
        ),
        start=1,
    ):
        tk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=2)
        var = tk.StringVar(value=getattr(current, key))
        tk.Entry(frame, textvariable=var, width=48, show="•" if secret else "").grid(
            row=row, column=1, pady=2
        )
        fields[key] = var
    tk.Label(frame, text="e.g. omr.example.com (B: required; A: shown in the window)", fg="#555").grid(
        row=4, column=1, sticky="w"
    )
    message = tk.Label(frame, text="", fg="#b42318", wraplength=460, justify="left")
    message.grid(row=5, column=0, columnspan=2, sticky="w")

    def save():
        settings = TunnelSettings(**{k: v.get().strip() for k, v in fields.items()})
        try:
            settings.validate()
        except TunnelError as error:
            message.configure(text=str(error))
            return
        settings.save(station.settings_path)
        dialog.destroy()
        done()

    def clear():
        for var in fields.values():
            var.set("")

    buttons = tk.Frame(frame)
    buttons.grid(row=6, column=0, columnspan=2, sticky="e", pady=(8, 0))
    tk.Button(buttons, text="Use quick tunnel", command=clear).pack(side="left", padx=4)
    tk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side="left", padx=4)
    tk.Button(buttons, text="Save", width=10, command=save).pack(side="left", padx=4)
    tk.Label(
        frame, text="Saved in the data folder (remote_access.json). Restart remote to apply.", fg="#555"
    ).grid(row=7, column=0, columnspan=2, sticky="w", pady=(6, 0))
    dialog.grab_set()


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
        "--remote",
        action="store_true",
        help="Also start remote access (Cloudflare tunnel, see the window's settings)",
    )
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
