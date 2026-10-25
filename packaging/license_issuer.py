"""
License Issuer: the vendor's license admin (tools/license_admin) as one exe.

    OMRChecker-License-Issuer.exe [--key private_key.pem] [--home folder] [--port 8777]

It serves the admin on 127.0.0.1 and opens it in the browser; closing this
console window stops it. No private key is built in: it offers --key, or a
private_key.pem next to the exe, and otherwise asks for one in the page. An
encrypted key is loaded once its passphrase is typed in the page. The
revocation list and a copy of every issued license go to --home (default:
"license-issuer" next to the exe, or %LOCALAPPDATA%\\OMRChecker\\license-issuer
when that folder is read-only).

This is a vendor tool: never give it, or a build of it, to a customer.
"""

import argparse
import os
import socket
import sys
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(ROOT))

TITLE = "OMRChecker License Issuer"


def exe_dir():
    return Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else ROOT


def pick_key(given):
    for path in (given, exe_dir() / "private_key.pem"):
        if path and Path(path).exists():
            return Path(path)
    return None


def writable(folder):
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / ".write-test"
        probe.write_text("")
        probe.unlink()
        return True
    except OSError:
        return False


def pick_home(given):
    if given:
        return Path(given)
    home = exe_dir() / "license-issuer"
    if writable(home):
        return home
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "OMRChecker" / "license-issuer"
    local.mkdir(parents=True, exist_ok=True)
    return local


def free_port(start):
    for port in range(start, start + 50):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("No free port for the License Issuer")


def main(argv=None):
    parser = argparse.ArgumentParser(description=TITLE)
    parser.add_argument("--key", help="private key to offer at start")
    parser.add_argument("--home", help="folder for the revocation list and issued licenses")
    parser.add_argument("--port", type=int, default=8777)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--selftest", action="store_true", help="start, answer one request, exit")
    args = parser.parse_args(argv)

    import uvicorn

    from tools.license_admin import server

    home = pick_home(args.home)
    home.mkdir(parents=True, exist_ok=True)
    key = pick_key(args.key)
    server.configure(key=key, home=home)
    os.chdir(home)  # keys generated in the page land here (./license-keys)
    port = free_port(args.port)
    url = f"http://127.0.0.1:{port}"

    if args.selftest:
        server.create_app()
        if not (server.STATIC_DIR / "index.html").exists():
            print(f"selftest failed: no page at {server.STATIC_DIR}")
            return 1
        loaded = server._state["private_key"] is not None
        print(f"selftest ok: key {key or 'none'}, loaded={loaded}")
        return 0

    if os.name == "nt":
        os.system(f"title {TITLE}")
    print(TITLE)
    print(f"  page:    {url}")
    print(f"  key:     {key or 'none found; load or generate one in the page'}")
    print(f"  folder:  {home}")
    print("Close this window to stop.")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(server.create_app(), host="127.0.0.1", port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
