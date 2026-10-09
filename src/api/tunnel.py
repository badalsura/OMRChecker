"""
Remote access to the station through a Cloudflare Tunnel (cloudflared).

Three ways, picked by what the user filled in (TunnelSettings):

* nothing: a quick tunnel. cloudflared prints a random
  https://<words>.trycloudflare.com address, read from its output.
* a tunnel token (Zero Trust dashboard > Networks > Tunnels > your tunnel):
  cloudflared runs that tunnel; its public hostname is set in the dashboard to
  point at http://localhost:<port>.
* a Cloudflare API token plus a hostname on a domain in that account: the
  station sets the tunnel up itself through the official Cloudflare API
  (creates or reuses the tunnel "omrchecker-<hostname>", points its ingress at
  the current port and adds the DNS record), then runs it. The API token needs
  Account > Cloudflare Tunnel: Edit and Zone > DNS: Edit.

cloudflared is looked for next to the exe (bundled), then on PATH.
Standard library only; Python 3.8 compatible.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

API = "https://api.cloudflare.com/client/v4"
QUICK_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
HOSTNAME = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$")


class TunnelError(Exception):
    pass


@dataclass
class TunnelSettings:
    """What the user entered; empty means a quick tunnel."""

    tunnel_token: str = ""
    api_token: str = ""
    hostname: str = ""

    @property
    def mode(self):
        if self.api_token.strip():
            return "api"
        if self.tunnel_token.strip():
            return "token"
        return "quick"

    def validate(self):
        host = self.hostname.strip().lower()
        if self.mode == "api" and not HOSTNAME.match(host):
            raise TunnelError("Enter the public hostname, e.g. omr.example.com")
        if host and not HOSTNAME.match(host):
            raise TunnelError(f"'{self.hostname}' is not a valid hostname")

    @classmethod
    def load(cls, path):
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        return cls(**{k: str(data.get(k) or "") for k in ("tunnel_token", "api_token", "hostname")})

    def save(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")


def find_cloudflared(*folders):
    name = "cloudflared.exe" if os.name == "nt" else "cloudflared"
    for folder in folders:
        for candidate in (Path(folder) / "cloudflared" / name, Path(folder) / name):
            if candidate.is_file():
                return str(candidate)
    return shutil.which("cloudflared")


# ---------------------------------------------------------------- Cloudflare API
class CloudflareAPI:
    def __init__(self, token, opener=None):
        self.token = token.strip()
        self.opener = opener or urllib.request.urlopen

    def call(self, method, path, body=None):
        request = urllib.request.Request(
            API + path,
            data=None if body is None else json.dumps(body).encode("utf-8"),
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": "omrchecker",
            },
        )
        try:
            with self.opener(request, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            try:
                data = json.loads(error.read().decode("utf-8"))
            except ValueError:
                raise TunnelError(f"Cloudflare API: HTTP {error.code}") from None
        except (urllib.error.URLError, OSError) as error:
            raise TunnelError(f"Cannot reach the Cloudflare API: {error}") from None
        if not data.get("success"):
            messages = "; ".join(e.get("message", "") for e in data.get("errors") or [])
            raise TunnelError(f"Cloudflare API: {messages or 'request failed'}")
        return data.get("result")

    def zone_for(self, hostname):
        """The account zone the hostname belongs to (longest matching suffix)."""
        parts = hostname.split(".")
        for start in range(len(parts) - 1):
            name = ".".join(parts[start:])
            zones = self.call("GET", f"/zones?name={name}")
            if zones:
                return zones[0]
        raise TunnelError(
            f"No domain of {hostname} is in this Cloudflare account (or the token can't see it)"
        )

    def setup(self, hostname, port):
        """Create or reuse the tunnel, route hostname to the port; returns its run token."""
        hostname = hostname.strip().lower()
        zone = self.zone_for(hostname)
        account = zone["account"]["id"]
        name = "omrchecker-" + hostname.replace(".", "-")
        found = self.call(
            "GET", f"/accounts/{account}/cfd_tunnel?name={name}&is_deleted=false"
        )
        if found:
            tunnel = found[0]
        else:
            tunnel = self.call(
                "POST",
                f"/accounts/{account}/cfd_tunnel",
                {"name": name, "config_src": "cloudflare"},
            )
        tunnel_id = tunnel["id"]
        self.call(
            "PUT",
            f"/accounts/{account}/cfd_tunnel/{tunnel_id}/configurations",
            {
                "config": {
                    "ingress": [
                        {"hostname": hostname, "service": f"http://localhost:{port}"},
                        {"service": "http_status:404"},
                    ]
                }
            },
        )
        record = {
            "type": "CNAME",
            "name": hostname,
            "content": f"{tunnel_id}.cfargotunnel.com",
            "proxied": True,
            "comment": "OMRChecker station",
        }
        existing = self.call(
            "GET", f"/zones/{zone['id']}/dns_records?name={hostname}"
        )
        if existing:
            if existing[0].get("content") != record["content"] or existing[0]["type"] != "CNAME":
                self.call("PUT", f"/zones/{zone['id']}/dns_records/{existing[0]['id']}", record)
        else:
            self.call("POST", f"/zones/{zone['id']}/dns_records", record)
        return self.call("GET", f"/accounts/{account}/cfd_tunnel/{tunnel_id}/token")


# ---------------------------------------------------------------- the process
class Tunnel:
    """One cloudflared process. url is set once the tunnel is reachable."""

    def __init__(self, cloudflared, port, settings, on_change=None, api=None):
        self.cloudflared = cloudflared
        self.port = port
        self.settings = settings
        self.on_change = on_change or (lambda tunnel: None)
        self.api = api
        self.process = None
        self.token = ""
        self.url = None
        self.error = None
        self.log = []
        self.state = "stopped"

    def _set(self, state, error=None):
        self.state, self.error = state, error
        self.on_change(self)

    def command(self):
        base = [self.cloudflared, "tunnel", "--no-autoupdate"]
        if self.settings.mode == "quick":
            return base + ["--url", f"http://127.0.0.1:{self.port}"], {}
        # The token goes in the environment, not the command line
        return base + ["run"], {"TUNNEL_TOKEN": self.token}

    def start(self):
        """Start in the background; on_change reports starting / running / error."""
        if not self.cloudflared:
            self._set("error", "cloudflared was not found (it ships next to the exe)")
            return
        try:
            self.settings.validate()
        except TunnelError as error:
            self._set("error", str(error))
            return
        self._set("starting")
        threading.Thread(target=self._run, name="omr-tunnel", daemon=True).start()

    def _run(self):
        mode = self.settings.mode
        try:
            if mode == "api":
                api = self.api or CloudflareAPI(self.settings.api_token)
                self.token = api.setup(self.settings.hostname, self.port)
            elif mode == "token":
                self.token = self.settings.tunnel_token.strip()
            command, extra_env = self.command()
            flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
            process = self.process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=dict(os.environ, **extra_env),
                creationflags=flags,
            )
        except (TunnelError, OSError) as error:
            if self.state != "stopped":
                self._set("error", str(error))
            return
        if self.state == "stopped":  # stopped while setting up
            process.terminate()
            return
        if mode != "quick" and self.settings.hostname.strip():
            self.url = "https://" + self.settings.hostname.strip().lower()
        for raw in process.stdout:
            line = raw.decode("utf-8", "replace").rstrip()
            self.log = (self.log + [line])[-200:]
            if self.state != "starting":
                continue
            found = QUICK_URL.search(line)
            if mode == "quick" and found and "api.trycloudflare.com" not in found.group(0):
                self.url = found.group(0)
            if "Registered tunnel connection" in line and (self.url or mode != "quick"):
                self._set("running")
        code = process.wait()
        if self.state != "stopped":
            reason = next(
                (l for l in reversed(self.log) if " ERR " in l or "error" in l.lower()),
                f"cloudflared exited ({code})",
            )
            self._set("error", reason[-300:])

    def stop(self):
        process, self.process = self.process, None
        self.state = "stopped"
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        self.url = None
        self.on_change(self)


def wait_for(tunnel, timeout=60):
    """Block until the tunnel runs or fails (for scripts and tests)."""
    deadline = time.time() + timeout
    while time.time() < deadline and tunnel.state == "starting":
        time.sleep(0.1)
    return tunnel.state


if __name__ == "__main__":  # python -m src.api.tunnel 8765
    t = Tunnel(find_cloudflared(), int(sys.argv[1]), TunnelSettings())
    t.start()
    print(wait_for(t), t.url or t.error)
