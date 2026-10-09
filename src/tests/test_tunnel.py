"""Remote access: quick tunnel address, token mode and the Cloudflare API set-up."""

import io
import json
import os
import stat
import sys

import pytest

from src.api.tunnel import CloudflareAPI, Tunnel, TunnelSettings, wait_for

pytestmark = pytest.mark.skipif(os.name == "nt", reason="fake cloudflared is a shell script")


def fake_cloudflared(tmp_path, lines):
    script = tmp_path / "cloudflared"
    body = "\n".join(f"print({line!r}, flush=True)" for line in lines)
    script.write_text(
        f"#!{sys.executable}\nimport os, time\n"
        f"print('token=' + os.environ.get('TUNNEL_TOKEN', ''), flush=True)\n{body}\ntime.sleep(30)\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def test_quick_tunnel_reports_its_address(tmp_path):
    exe = fake_cloudflared(tmp_path, [
        "INF Requesting new quick Tunnel on trycloudflare.com...",
        "INF |  https://brave-lion-tea.trycloudflare.com  |",
        "INF Registered tunnel connection connIndex=0",
    ])
    tunnel = Tunnel(exe, 8765, TunnelSettings())
    tunnel.start()
    assert wait_for(tunnel, 20) == "running"
    assert tunnel.url == "https://brave-lion-tea.trycloudflare.com"
    assert "--url" in tunnel.command()[0]
    tunnel.stop()
    assert tunnel.state == "stopped" and tunnel.url is None


def test_token_tunnel_keeps_the_token_off_the_command_line(tmp_path):
    exe = fake_cloudflared(tmp_path, ["INF Registered tunnel connection connIndex=0"])
    tunnel = Tunnel(exe, 8765, TunnelSettings(tunnel_token="secret-token", hostname="OMR.example.com"))
    tunnel.start()
    assert wait_for(tunnel, 20) == "running"
    assert tunnel.url == "https://omr.example.com"
    assert "token=secret-token" in tunnel.log
    assert "secret-token" not in " ".join(tunnel.command()[0])
    tunnel.stop()


def test_settings_validation_and_storage(tmp_path):
    assert TunnelSettings().mode == "quick"
    with pytest.raises(Exception):
        TunnelSettings(api_token="x").validate()  # the API needs a hostname
    settings = TunnelSettings(api_token="x", hostname="omr.example.com")
    settings.save(tmp_path / "remote.json")
    assert TunnelSettings.load(tmp_path / "remote.json") == settings
    assert TunnelSettings.load(tmp_path / "missing.json").mode == "quick"
    tunnel = Tunnel(None, 1, TunnelSettings())
    tunnel.start()
    assert tunnel.state == "error" and "cloudflared" in tunnel.error


def test_api_creates_the_tunnel_route_and_dns_record():
    calls = []

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def opener(request, timeout):
        url = request.full_url.replace("https://api.cloudflare.com/client/v4", "")
        body = json.loads(request.data) if request.data else None
        calls.append((request.get_method(), url, body))
        if url == "/zones?name=omr.exam.example.org":
            result = []
        elif url == "/zones?name=exam.example.org":
            result = []
        elif url == "/zones?name=example.org":
            result = [{"id": "Z1", "account": {"id": "A1"}}]
        elif url.startswith("/accounts/A1/cfd_tunnel?name="):
            result = []
        elif url == "/accounts/A1/cfd_tunnel":
            result = {"id": "T1"}
        elif url.endswith("/token"):
            result = "run-token"
        elif url.startswith("/zones/Z1/dns_records?name="):
            result = []
        else:
            result = {}
        return Response(json.dumps({"success": True, "result": result}).encode())

    token = CloudflareAPI("api", opener).setup("omr.exam.example.org", 8800)
    assert token == "run-token"
    methods = {(m, u) for m, u, _ in calls}
    assert ("POST", "/accounts/A1/cfd_tunnel") in methods
    config = next(b for m, u, b in calls if u.endswith("/configurations"))
    assert config["config"]["ingress"][0] == {
        "hostname": "omr.exam.example.org", "service": "http://localhost:8800"
    }
    dns = next(b for m, u, b in calls if m == "POST" and u == "/zones/Z1/dns_records")
    assert dns["content"] == "T1.cfargotunnel.com" and dns["proxied"]
