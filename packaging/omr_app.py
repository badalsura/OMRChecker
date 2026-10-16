"""
Desktop app window for the OMR engine: our screens in their own window, no
browser and no server dialog.

    OMRChecker.exe                    open the app (or bring the open one to the front)
    OMRChecker.exe --engine qt        pick the window engine when both are bundled

The engine runs on 127.0.0.1 inside this process (nothing is reachable from
other computers) and stops when the window closes. Two window engines:

    webview2  Microsoft WebView2 through pywebview. A Fixed Version runtime 109
              (the last one for Windows 7/8) in <exe dir>\\webview2 is used when
              present, so every Windows shows the same Chromium 109.
    qt        Qt 5.15 WebEngine through PySide2 (Chromium 83), bundled in the exe.

Runs from source too: python packaging/omr_app.py --engine qt
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
            app = base.build_app(self.data_dir, self.workers, None)
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


def run_webview2(engine, state, instance):
    runtime = webview2_runtime()
    if runtime:
        allow_sandbox(runtime)
        os.environ["WEBVIEW2_BROWSER_EXECUTABLE_FOLDER"] = str(runtime)
    print(f"WebView2 runtime: {runtime or 'installed (Evergreen)'}")
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


# --------------------------------------------------------------------- Qt 5.15
def run_qt(engine, state, instance):
    from PySide2 import QtCore, QtGui, QtWidgets
    from PySide2.QtWebEngineWidgets import (
        QWebEnginePage,
        QWebEngineProfile,
        QWebEngineSettings,
        QWebEngineView,
    )

    QtCore.QCoreApplication.setAttribute(QtCore.Qt.AA_ShareOpenGLContexts)
    QtCore.QCoreApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling)
    QtCore.QCoreApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps)
    app = QtWidgets.QApplication(sys.argv[:1])
    app.setApplicationName(base.DISPLAY_NAME)
    icon_path = base.BRANDING_DIR / "icon.ico"
    if not icon_path.is_file():
        icon_path = base.BRANDING_DIR / "icon.png"
    if icon_path.is_file():
        app.setWindowIcon(QtGui.QIcon(str(icon_path)))

    profile_dir = engine.data_dir / "app_profile" / "qt"
    profile = QWebEngineProfile("omr", app)
    profile.setPersistentStoragePath(str(profile_dir / "storage"))
    profile.setCachePath(str(profile_dir / "cache"))
    profile.setPersistentCookiesPolicy(QWebEngineProfile.ForcePersistentCookies)
    settings = profile.settings()
    settings.setAttribute(QWebEngineSettings.FullScreenSupportEnabled, False)
    settings.setAttribute(QWebEngineSettings.ErrorPageEnabled, True)

    class Page(QWebEnginePage):
        def createWindow(self, _type):
            # Links that open a new window (the user guide) go to the default browser
            target = QWebEnginePage(profile, self)
            target.urlChanged.connect(lambda url: (QtGui.QDesktopServices.openUrl(url), target.deleteLater()))
            return target

    class Window(QtWidgets.QMainWindow):
        show_requested = QtCore.Signal()

        def __init__(self):
            super().__init__()
            self.setWindowTitle(base.DISPLAY_NAME)
            self.view = QWebEngineView(self)
            self.view.setPage(Page(profile, self.view))
            self.view.setContextMenuPolicy(QtCore.Qt.NoContextMenu)
            self.view.page().setBackgroundColor(QtGui.QColor("#f6f7f9"))
            self.setCentralWidget(self.view)
            self.setMinimumSize(960, 600)
            self.show_requested.connect(self.bring_to_front)

        def bring_to_front(self):
            if self.isMinimized():
                self.showNormal()
            self.raise_()
            self.activateWindow()

        def closeEvent(self, event):
            state.values["maximized"] = self.isMaximized()
            if not self.isMaximized():
                geometry = self.geometry()
                state.values.update(
                    x=geometry.x(), y=geometry.y(), width=geometry.width(), height=geometry.height()
                )
            super().closeEvent(event)

    window = Window()
    instance["show"] = window.show_requested.emit

    def download(item):
        # Qt 5.15 has no Save As box in the page (showSaveFilePicker), so every
        # download asks here; the file then streams straight to the chosen path
        folder = QtCore.QStandardPaths.writableLocation(QtCore.QStandardPaths.DownloadLocation)
        suggested = os.path.join(folder, Path(item.path()).name)
        path, _ = QtWidgets.QFileDialog.getSaveFileName(window, "Save as", suggested)
        if not path:
            item.cancel()
            return
        item.setPath(path)
        name = Path(path).name

        def finished():
            ok = item.state() == item.DownloadCompleted
            message = json.dumps([name, ok])
            window.view.page().runJavaScript(
                f"window.dispatchEvent(new CustomEvent('omr-saved', {{detail: {message}}}))"
            )

        item.finished.connect(finished)
        item.accept()

    profile.downloadRequested.connect(download)

    v = state.values
    window.resize(v["width"], v["height"])
    if v["x"] is not None and v["y"] is not None:
        window.move(v["x"], v["y"])
    window.view.setHtml(splash_html())
    if v.get("maximized"):
        window.showMaximized()
    else:
        window.show()

    class Bridge(QtCore.QObject):
        ready = QtCore.Signal()

    bridge = Bridge()

    def loaded():
        if engine.error:
            window.view.setHtml(splash_html("The engine could not start.", engine.error))
        else:
            window.view.setUrl(QtCore.QUrl(engine.url))

    bridge.ready.connect(loaded)

    def boot():
        engine.start()
        bridge.ready.emit()

    threading.Thread(target=boot, name="omr-boot", daemon=True).start()
    app.exec_()


ENGINES = {"webview2": run_webview2, "qt": run_qt}


def available_engines():
    found = []
    try:
        import webview  # noqa: F401

        found.append("webview2")
    except ImportError:
        pass
    try:
        import PySide2.QtWebEngineWidgets  # noqa: F401

        found.append("qt")
    except ImportError:
        pass
    return found


# --------------------------------------------------------------------- start
def log_path():
    return Path(os.environ.get("OMR_APP_LOG") or base.default_data_dir() / "app.log")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=f"{base.DISPLAY_NAME} app window")
    parser.add_argument("--engine", choices=sorted(ENGINES), help="window engine (default: the bundled one)")
    parser.add_argument("--data-dir", help="data folder (default: as the launcher)")
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--version", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.version:
        print(f"{base.DISPLAY_NAME} {base.LAUNCHER_VERSION} app ({', '.join(available_engines())})")
        return 0
    data_dir = Path(args.data_dir) if args.data_dir else base.default_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("OMR_APP_LOG", str(data_dir / "app.log"))
    if sys.stdout is None or sys.stderr is None:
        # A windowed exe has no console: keep messages in the data folder
        stream = open(log_path(), "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or stream
        sys.stderr = sys.stderr or stream

    instance = {}
    if not claim_single_instance(lambda: instance.get("show", lambda: None)()):
        return 0
    base.configure_environment()

    engines = available_engines()
    name = args.engine or (engines[0] if engines else None)
    if name not in engines:
        print(f"No window engine available (wanted {name or 'any'}; bundled: {engines})", file=sys.stderr)
        return 2
    engine = Engine(data_dir, args.workers)
    state = WindowState(data_dir)
    try:
        ENGINES[name](engine, state, instance)
    except Exception:
        traceback.print_exc()
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
