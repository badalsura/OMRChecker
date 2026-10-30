"""
Desktop app window for the OMR engine: our screens in their own window, no
browser and no server dialog.

    OMRChecker.exe          open the app (or bring the open one to the front)

The engine runs on 127.0.0.1 inside this process (nothing is reachable from
other computers) and stops when the window closes. The window is Microsoft
WebView2 through pywebview:

    modern build  (Windows 10/11) the WebView2 runtime Windows installs and updates
    legacy build  (Windows 7/8.1) a Fixed Version runtime 109, the last one for
                  Windows 7/8, in <exe dir>\\webview2

The screens must keep working on Chromium 109 (the legacy build).

Runs from source too: python packaging/omr_app.py
"""

import argparse
import base64
import json
import multiprocessing
import os
import socket
import sys
import threading
import traceback
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import omr_desktop as base  # noqa: E402

# Fixed so the page keeps its saved settings (localStorage is per port); the
# browser launcher uses 8765, so both can run side by side
APP_PORT = 8766
# A second start sends "show" here and exits; the open window comes to the front
LOCK_PORT = 47651
WEBVIEW2_VERSION = "109.0.1518.78"


# --------------------------------------------------------------------- one copy only
def claim_single_instance(on_show):
    """True for the first copy (which then listens for "show"); False otherwise."""
    lock = socket.socket()
    try:
        lock.bind(("127.0.0.1", LOCK_PORT))
    except OSError:
        try:
            with socket.create_connection(("127.0.0.1", LOCK_PORT), timeout=2) as other:
                other.sendall(b"show")
        except OSError:
            pass
        return False
    lock.listen(4)

    def serve():
        while True:
            try:
                conn, _ = lock.accept()
            except OSError:
                return
            with conn:
                if conn.recv(16) == b"show":
                    try:
                        on_show()
                    except Exception:
                        traceback.print_exc()

    threading.Thread(target=serve, name="omr-instance", daemon=True).start()
    return True


# --------------------------------------------------------------------- engine
class Engine:
    """The local server behind the window: started in the background."""

    def __init__(self, data_dir, workers=None):
        self.data_dir = data_dir
        self.workers = workers
        self.server = None
        self.port = None
        self.error = None
        self.ready = threading.Event()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/?desktop=1"

    def start(self):
        try:
            self.port = base.pick_port("127.0.0.1", APP_PORT)
            # Local to this computer: no API key, whatever the environment says
            os.environ.pop("OMR_API_KEY", None)
            app = base.build_app(self.data_dir, self.workers, None, desktop=True)
            self.server = base.ServerThread(app, "127.0.0.1", self.port)
            self.server.start()
            if not self.server.wait_started(timeout=120):
                self.error = f"The engine could not start on port {self.port}."
        except Exception as error:
            traceback.print_exc()
            self.error = str(error) or error.__class__.__name__
        self.ready.set()

    def stop(self):
        server, self.server = self.server, None
        if server:
            server.stop()


# --------------------------------------------------------------------- pages shown before ours
def _icon_data_uri():
    for name in (base.BRANDING.get("icon") or "icon.png", "icon.png"):
        path = base.BRANDING_DIR / name
        if path.is_file():
            return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()
    return ""


def splash_html(message="Starting…", error=None):
    icon = _icon_data_uri()
    detail = (
        f'<p class="error">{_escape(error)}</p><p>Close this window and start the app again. '
        f"Details are in {_escape(str(log_path()))}</p>"
        if error
        else '<div class="bar"><div></div></div>'
    )
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{_escape(base.DISPLAY_NAME)}</title>
<style>
html,body{{margin:0;height:100%;background:#f6f7f9;color:#1f2933;font:14px "Segoe UI",system-ui,sans-serif;
user-select:none;cursor:default}}
main{{height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:14px}}
img{{width:96px;height:96px}} h1{{font-size:20px;font-weight:600;margin:0}} p{{margin:0;color:#52606d;max-width:520px;text-align:center}}
.error{{color:#b42318}}
.bar{{width:220px;height:4px;background:#e4e7eb;border-radius:2px;overflow:hidden}}
.bar div{{width:40%;height:100%;background:#2563eb;border-radius:2px;animation:slide 1.2s ease-in-out infinite}}
@keyframes slide{{0%{{margin-left:-40%}}100%{{margin-left:100%}}}}
</style></head><body oncontextmenu="return false"><main>
{f'<img src="{icon}" alt="">' if icon else ''}<h1>{_escape(base.DISPLAY_NAME)}</h1>
<p>{_escape(message)}</p>{detail}</main></body></html>"""


def _escape(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# --------------------------------------------------------------------- second windows
# Links that open a new window: our own pages (Help, API docs, a scan's JSON, a
# template layout) open in another app window; other websites in the browser
CHILD_TITLES = {"/help": "Help", "/docs": "API docs", "/redoc": "API docs"}


def own_page(engine, url):
    return bool(engine.port) and str(url).startswith(f"http://127.0.0.1:{engine.port}/")


def child_title(url):
    from urllib.parse import urlsplit

    path = urlsplit(str(url)).path.rstrip("/") or "/"
    part = CHILD_TITLES.get(path)
    return f"{base.DISPLAY_NAME} – {part}" if part else base.DISPLAY_NAME


def open_outside(url):
    import webbrowser

    webbrowser.open(str(url))


# --------------------------------------------------------------------- window size and place
class WindowState:
    def __init__(self, data_dir):
        self.path = data_dir / "app_window.json"
        self.values = {"width": 1280, "height": 820, "x": None, "y": None, "maximized": False}
        try:
            self.values.update(json.loads(self.path.read_text("utf-8")))
        except (OSError, ValueError):
            pass

    def save(self):
        try:
            self.path.write_text(json.dumps(self.values), "utf-8")
        except OSError:
            pass


# --------------------------------------------------------------------- WebView2
def webview2_runtime():
    for folder in (base.APP_DIR / "webview2", base.RESOURCE_DIR / "webview2"):
        if (folder / "msedgewebview2.exe").is_file():
            return folder
        # an expanded .cab keeps the runtime one folder down
        for child in folder.glob("*/msedgewebview2.exe") if folder.is_dir() else ():
            return child.parent
    return None


def allow_sandbox(runtime):
    """WebView2's sandboxed processes run as app containers on Windows 10 and
    later and must be able to read a Fixed Version runtime, or the page stays
    blank. Grant them read access once (Microsoft's documented step; the SIDs do
    not exist on Windows 7/8, where nothing is needed)."""
    marker = runtime / ".omr-sandbox-ok"
    if marker.exists() or sys.platform != "win32" or sys.getwindowsversion().major < 10:
        return
    import subprocess

    ok = True
    for sid in ("*S-1-15-2-2", "*S-1-15-2-1"):  # ALL RESTRICTED / ALL APPLICATION PACKAGES
        done = subprocess.run(
            ["icacls", str(runtime), "/grant", f"{sid}:(OI)(CI)(RX)", "/Q"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        ok = ok and done.returncode == 0
    if ok:
        try:
            marker.write_text("", "utf-8")
        except OSError:
            pass
    else:
        print(f"Could not grant the WebView2 sandbox read access to {runtime}", file=sys.stderr)


@contextmanager
def _runtime_visible_to_pywebview(version):
    """pywebview picks WebView2 only when an installed (Evergreen) runtime shows in
    the registry; a bundled Fixed Version runtime never does. Answer its check
    (pywebview 4.4.1 winforms._is_chromium) while it runs; every other registry
    read passes through."""
    import winreg

    real_open, real_query = winreg.OpenKey, winreg.QueryValueEx

    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def Close(self):
            pass

    fake = Key()

    def open_key(key, sub_key, *args, **kwargs):
        if "EdgeUpdate\\Clients\\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" in str(sub_key):
            return fake
        return real_open(key, sub_key, *args, **kwargs)

    def query(key, name):
        if key is fake:
            return version, winreg.REG_SZ
        return real_query(key, name)

    winreg.OpenKey, winreg.QueryValueEx = open_key, query
    try:
        yield
    finally:
        winreg.OpenKey, winreg.QueryValueEx = real_open, real_query


class MissingPart(Exception):
    """Something Windows must have for the window: shown to the user, not just logged."""

    def __init__(self, message, url):
        super().__init__(message)
        self.url = url


DOTNET_URL = "https://dotnet.microsoft.com/download/dotnet-framework/net48"
WEBVIEW2_URL = "https://developer.microsoft.com/microsoft-edge/webview2/"


def _registry_value(root, path, name):
    import winreg

    try:
        with winreg.OpenKey(root, path) as key:
            return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def check_windows_parts(runtime):
    """.NET 4.7.2+ (pythonnet) and a WebView2 runtime, before loading either."""
    if sys.platform != "win32":
        return
    import winreg

    release = _registry_value(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full", "Release")
    if not isinstance(release, int) or release < 461808:
        raise MissingPart(
            f"{base.DISPLAY_NAME} needs Microsoft .NET Framework 4.8, which is not installed on this computer.\n\n"
            "Install it (an administrator may be needed, and Windows may ask to restart), then start the app again.\n\n"
            "Open the download page now?",
            DOTNET_URL,
        )
    if runtime:
        return
    client = r"Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    found = [
        _registry_value(winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\WOW6432Node\\" + client, "pv"),
        _registry_value(winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\" + client, "pv"),
        _registry_value(winreg.HKEY_CURRENT_USER, "Software\\" + client, "pv"),
    ]
    if not any(v and v != "0.0.0.0" for v in found):
        raise MissingPart(
            f"{base.DISPLAY_NAME} needs the Microsoft Edge WebView2 Runtime, which is not installed on this computer.\n\n"
            "Install the Evergreen runtime, or use the legacy download of the app, which carries its own.\n\n"
            "Open the download page now?",
            WEBVIEW2_URL,
        )


def tell_user(message, url=None):
    """A plain Windows message box (a windowed exe has no console)."""
    print(message, file=sys.stderr)
    if sys.platform != "win32":
        return
    import ctypes

    MB_OK, MB_YESNO, MB_ICONWARNING, IDYES = 0x0, 0x4, 0x30, 6
    style = (MB_YESNO if url else MB_OK) | MB_ICONWARNING
    answer = ctypes.windll.user32.MessageBoxW(None, message, base.DISPLAY_NAME, style)
    if url and answer == IDYES:
        open_outside(url)


DOTNET_PACKAGES = ("pythonnet", "clr_loader", "webview")


def unblock_dotnet_parts():
    """A zip extracted with Windows' Extract All passes its "downloaded from the
    internet" mark (a Zone.Identifier stream) on to every file, and .NET
    Framework refuses to load a marked assembly: pywebview's bridge then fails
    with "Failed to resolve Python.Runtime.Loader.Initialize". Drop the mark
    from the .NET parts before they load."""
    if sys.platform != "win32":
        return
    for root in {base.APP_DIR, base.RESOURCE_DIR}:
        for name in DOTNET_PACKAGES:
            folder = root / name
            if not folder.is_dir():
                continue
            for dll in folder.rglob("*.dll"):
                try:
                    os.remove(f"{dll}:Zone.Identifier")
                except OSError:
                    pass  # not marked, or a read-only install


def blocked_by_windows(error):
    return "Python.Runtime" in str(error)


def run_webview2(engine, state, instance):
    runtime = webview2_runtime()
    check_windows_parts(runtime)
    if runtime:
        allow_sandbox(runtime)
        os.environ["WEBVIEW2_BROWSER_EXECUTABLE_FOLDER"] = str(runtime)
    print(f"WebView2 runtime: {runtime or 'installed (Evergreen)'}")
    unblock_dotnet_parts()
    import webview

    if runtime:
        with _runtime_visible_to_pywebview(WEBVIEW2_VERSION):
            import webview.platforms.winforms as winforms  # noqa: F401
    else:
        import webview.platforms.winforms as winforms  # noqa: F401
    if getattr(winforms, "renderer", "") != "edgechromium":
        raise RuntimeError(
            "WebView2 is not available on this computer (it needs .NET Framework 4.7.2 "
            "and the WebView2 runtime)."
        )

    v = state.values
    window = webview.create_window(
        base.DISPLAY_NAME,
        html=splash_html(),
        width=v["width"],
        height=v["height"],
        x=v["x"],
        y=v["y"],
        min_size=(960, 600),
        maximized=bool(v.get("maximized")),
        background_color="#f6f7f9",
        text_select=True,
    )

    def resized(width, height):
        if not v.get("maximized"):
            v["width"], v["height"] = width, height

    def moved(x, y):
        if not v.get("maximized"):
            v["x"], v["y"] = x, y

    def maximized():
        v["maximized"] = True

    def restored():
        v["maximized"] = False

    window.events.resized += resized
    window.events.moved += moved
    window.events.maximized += maximized
    window.events.restored += restored
    instance["show"] = lambda: (window.restore(), window.show())

    children = {}

    def open_child(url):
        known = children.get(url)
        if known:
            known.restore()
            known.show()
            return
        child = webview.create_window(child_title(url), url, width=1100, height=800, min_size=(640, 480), text_select=True)
        children[url] = child
        child.events.closed += lambda: children.pop(url, None)

    def new_window(browser, _sender, args):
        args.set_Handled(True)
        url = str(args.get_Uri())
        if own_page(engine, url):
            # create_window must not run on the window thread (pywebview 4.4.1)
            threading.Thread(target=open_child, args=(url,), daemon=True).start()
        else:
            open_outside(url)

    from webview.platforms import edgechromium

    edgechromium.EdgeChrome.on_new_window_request = new_window

    def boot():
        engine.start()
        if engine.error:
            window.load_html(splash_html("The engine could not start.", engine.error))
        else:
            window.load_url(engine.url)

    webview.start(
        boot,
        gui="edgechromium",
        debug=bool(os.environ.get("OMR_APP_DEBUG")),
        private_mode=False,
        storage_path=str(engine.data_dir / "app_profile" / "webview2"),
    )


def webview_available():
    try:
        import webview  # noqa: F401

        return True
    except ImportError:
        return False


# --------------------------------------------------------------------- start
def log_path():
    return Path(os.environ.get("OMR_APP_LOG") or base.default_data_dir() / "app.log")


def no_console(stream):
    """PyInstaller leaves a windowed exe without streams; Nuitka points them at NUL."""
    if stream is None:
        return True
    return str(getattr(stream, "name", "")).rstrip(":").lower() in ("nul", os.devnull.lower())


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=f"{base.DISPLAY_NAME} app window")
    parser.add_argument("--data-dir", help="data folder (default: as the launcher)")
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--version", action="store_true")
    parser.add_argument(
        "--selftest", action="store_true", help="headless engine check (as the launcher's); result in the exit code and app.log"
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.version:
        print(f"{base.DISPLAY_NAME} {base.LAUNCHER_VERSION} app (WebView2 runtime: {webview2_runtime() or 'installed'})")
        return 0
    data_dir = Path(args.data_dir) if args.data_dir else base.default_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("OMR_APP_LOG", str(data_dir / "app.log"))
    if no_console(sys.stdout) or no_console(sys.stderr):
        # A windowed exe has no console: keep messages in the data folder
        stream = open(log_path(), "a", encoding="utf-8", buffering=1)
        if no_console(sys.stdout):
            sys.stdout = stream
        if no_console(sys.stderr):
            sys.stderr = stream

    if args.selftest:
        base.configure_environment()
        return base.selftest(workers=args.workers or 2)

    instance = {}
    if not claim_single_instance(lambda: instance.get("show", lambda: None)()):
        return 0
    base.configure_environment()

    if not webview_available():
        print("pywebview is not installed (packaging/requirements-app.txt)", file=sys.stderr)
        return 2
    engine = Engine(data_dir, args.workers)
    state = WindowState(data_dir)
    try:
        run_webview2(engine, state, instance)
    except MissingPart as missing:
        tell_user(str(missing), missing.url)
        return 3
    except Exception as error:
        traceback.print_exc()
        if blocked_by_windows(error):
            tell_user(
                f"Windows blocked part of {base.DISPLAY_NAME} because it was downloaded from the internet.\n\n"
                "Delete this folder, right-click the downloaded zip, choose Properties, tick Unblock, "
                f"then extract it again.\n\nDetails are in {log_path()}"
            )
            return 1
        tell_user(f"{base.DISPLAY_NAME} could not open its window.\n\nDetails are in {log_path()}")
        return 1
    finally:
        state.save()
        engine.stop()
    return 0


if __name__ == "__main__":
    if os.environ.get("OMR_MPLCONFIGDIR"):
        os.environ["MPLCONFIGDIR"] = os.environ["OMR_MPLCONFIGDIR"]
    multiprocessing.freeze_support()
    if sys.platform != "win32":
        try:
            multiprocessing.set_start_method("spawn")
        except RuntimeError:
            pass
    sys.exit(main())
