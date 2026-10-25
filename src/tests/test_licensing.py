"""
RSA license: signing/verification, machine binding, expiry, tamper and
forgery resistance, and the API activation flow.

Licensing is disabled for the rest of the suite (conftest). These tests
re-enable it with their own ephemeral key pair and a temp license dir, so they
exercise the real gate without touching a developer's installed license.
"""

import base64
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from src.licensing import gate
from src.licensing.model import LicensePayload, signing_bytes
from src.licensing.verify import Reason, verify_license_data, verify_license_file


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def sign_payload(payload: dict, private_key) -> str:
    sig = private_key.sign(
        signing_bytes(payload),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode("ascii")


def make_license(private_key, **overrides) -> dict:
    now = datetime.now(timezone.utc)
    payload = LicensePayload(
        license_id=overrides.get("license_id", "test-1"),
        customer=overrides.get("customer", "Test Co"),
        issued_at=overrides.get("issued_at", _iso(now - timedelta(days=1))),
        expires_at=overrides.get("expires_at", _iso(now + timedelta(days=365))),
        machine=overrides.get("machine", None),
        features=overrides.get("features", ["core", "ocr"]),
        seats=overrides.get("seats", 1),
        edition=overrides.get("edition", "standard"),
    ).to_dict()
    return {
        "payload": payload,
        "signature": sign_payload(payload, private_key),
        "key_id": "v1",
    }


@pytest.fixture
def keys(tmp_path):
    """An ephemeral key pair + the app pointed at the public key and a temp
    license dir, with the gate enabled."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub_path = tmp_path / "public_key.pem"
    pub_path.write_bytes(
        private_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return private_key, pub_path


@pytest.fixture
def licensed_env(monkeypatch, tmp_path, keys):
    private_key, pub_path = keys
    monkeypatch.setenv("OMR_LICENSE_DISABLE", "0")
    # The gate is active only in a packaged build by default; force it on so the
    # source test run exercises it (see gate.licensing_active).
    monkeypatch.setenv("OMR_LICENSE_ENFORCE", "1")
    monkeypatch.setenv("OMR_LICENSE_PUBKEY", str(pub_path))
    monkeypatch.setenv("OMR_LICENSE_DIR", str(tmp_path / "licdir"))
    monkeypatch.setenv("OMR_LICENSE_FILE", str(tmp_path / "licdir" / "license.omrlic"))
    gate.clear_cache()
    yield private_key
    gate.clear_cache()


# ----------------------------------------------------------------- signature core
def test_a_valid_floating_license_verifies(licensed_env):
    doc = make_license(licensed_env)  # machine=None -> floating
    status = verify_license_data(doc, _pubkey(), check_clock=False)
    assert status.valid and status.reason is Reason.VALID
    assert status.payload.customer == "Test Co"


def test_editing_the_payload_breaks_the_signature(licensed_env):
    doc = make_license(licensed_env, features=["core"])
    doc["payload"]["features"].append("vlm")  # grant a feature we did not buy
    status = verify_license_data(doc, _pubkey(), check_clock=False)
    assert not status.valid and status.reason is Reason.BAD_SIGNATURE


def test_a_license_signed_by_another_key_is_rejected(licensed_env):
    attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    doc = make_license(attacker)  # signed with the wrong (attacker) key
    status = verify_license_data(doc, _pubkey(), check_clock=False)
    assert not status.valid and status.reason is Reason.BAD_SIGNATURE


def test_expired_and_not_yet_valid(licensed_env):
    past = _iso(datetime.now(timezone.utc) - timedelta(days=2))
    doc = make_license(licensed_env, expires_at=past)
    assert (
        verify_license_data(doc, _pubkey(), check_clock=False).reason is Reason.EXPIRED
    )

    future = _iso(datetime.now(timezone.utc) + timedelta(days=30))
    doc = make_license(licensed_env, issued_at=future, expires_at=None)
    assert (
        verify_license_data(doc, _pubkey(), check_clock=False).reason
        is Reason.NOT_YET_VALID
    )


def test_machine_binding(licensed_env):
    from src.licensing.fingerprint import machine_fingerprint

    doc = make_license(licensed_env, machine="not-this-machine")
    assert (
        verify_license_data(doc, _pubkey(), check_clock=False).reason
        is Reason.WRONG_MACHINE
    )

    doc = make_license(licensed_env, machine=machine_fingerprint())
    assert verify_license_data(doc, _pubkey(), check_clock=False).valid


def test_feature_gate(licensed_env):
    doc = make_license(licensed_env, features=["core"])
    assert verify_license_data(doc, _pubkey(), feature="core", check_clock=False).valid
    missing = verify_license_data(doc, _pubkey(), feature="ocr", check_clock=False)
    assert not missing.valid and missing.reason is Reason.FEATURE_MISSING


def test_malformed_file_is_reported(licensed_env, tmp_path):
    bad = tmp_path / "bad.omrlic"
    bad.write_text("this is not json")
    assert verify_license_file(bad).reason is Reason.MALFORMED


# -------------------------------------------------------------------- gate + install
def test_install_then_gate_passes(licensed_env):
    from src.licensing.fingerprint import machine_fingerprint

    doc = make_license(
        licensed_env, machine=machine_fingerprint(), features=["core", "ocr"]
    )
    assert not gate.is_licensed()  # nothing installed yet
    status = gate.install_license_bytes(json.dumps(doc).encode())
    assert status.valid
    assert gate.require_license().valid
    assert gate.is_licensed("ocr") and not gate.is_licensed("vlm")


def test_install_rejects_an_invalid_file_without_writing(licensed_env):
    attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    doc = make_license(attacker)
    status = gate.install_license_bytes(json.dumps(doc).encode())
    assert not status.valid
    assert gate.installed_license_path() is None  # nothing was written


def test_clock_rollback_is_detected(licensed_env):
    import time as _time

    from src.licensing import paths, verify

    # Record a "last seen" far in the future, then verify with the real clock.
    state = paths.state_file_path()
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"last_seen": _time.time() + 10 * 24 * 3600}))
    doc = make_license(licensed_env)
    status = verify.verify_license_data(doc, _pubkey(), check_clock=True)
    assert status.reason is Reason.CLOCK_ROLLBACK


# ----------------------------------------------------------------------------- API
def test_api_gates_then_activates(licensed_env, tmp_path):
    from fastapi.testclient import TestClient

    from src.api.app import create_app
    from src.licensing.fingerprint import machine_fingerprint

    client = TestClient(create_app(tmp_path / "data"))
    assert client.get("/health").status_code == 200
    assert client.get("/license/status").json()["valid"] is False
    assert "fingerprint" in client.get("/license/request").json()
    # A real endpoint is blocked until activation.
    assert client.get("/templates").status_code == 402

    doc = make_license(licensed_env, machine=machine_fingerprint(), features=["core"])
    resp = client.post(
        "/license/activate",
        files={
            "file": ("x.omrlic", json.dumps(doc).encode(), "application/octet-stream")
        },
    )
    assert resp.status_code == 200 and resp.json()["valid"] is True
    assert client.get("/templates").status_code == 200

    bad = client.post(
        "/license/activate",
        files={"file": ("x", b"garbage", "text/plain")},
    )
    assert bad.status_code == 400


def _pubkey():
    from src.licensing.verify import load_public_key

    return load_public_key()


# ----------------------------------------------------------- gate activation policy
def test_gate_is_off_by_default_in_a_source_run(monkeypatch):
    """A plain source/server run is not license-gated: licensing protects the
    packaged desktop build, not the bare API."""
    monkeypatch.delenv("OMR_LICENSE_DISABLE", raising=False)
    monkeypatch.delenv("OMR_LICENSE_ENFORCE", raising=False)
    gate.clear_cache()
    assert gate.licensing_active() is False
    # With the gate inactive, status is valid even with no license installed.
    assert gate.current_status().valid


def test_enforce_env_turns_the_gate_on(monkeypatch):
    monkeypatch.delenv("OMR_LICENSE_DISABLE", raising=False)
    monkeypatch.setenv("OMR_LICENSE_ENFORCE", "1")
    gate.clear_cache()
    assert gate.licensing_active() is True


def test_disable_env_wins_over_enforce(monkeypatch):
    monkeypatch.setenv("OMR_LICENSE_ENFORCE", "1")
    monkeypatch.setenv("OMR_LICENSE_DISABLE", "1")
    gate.clear_cache()
    assert gate.licensing_active() is False


# -------------------------------------------------------- online module is dormant
def test_online_module_is_dormant_without_a_server(monkeypatch):
    from src.licensing import online

    monkeypatch.delenv("OMR_LICENSE_SERVER", raising=False)
    assert online.is_configured() is False
    # apply_revocation is a no-op on a valid status when nothing is configured.
    from src.licensing.verify import LicenseStatus, Reason

    status = LicenseStatus(True, Reason.VALID, make_payload_obj())
    assert online.apply_revocation(status) is status


def make_payload_obj():
    from datetime import datetime, timezone

    return LicensePayload(
        license_id="x",
        customer="c",
        issued_at=datetime.now(timezone.utc).isoformat(),
    )


# ------------------------------------------------------------- vendor_lib round-trip
def test_vendor_lib_issue_then_verify(monkeypatch, tmp_path):
    """The vendor library the CLI and web admin share issues a license the app
    verifies."""
    from tools import vendor_lib as vl

    priv, pub = vl.generate_keypair(tmp_path / "keys", bits=2048)
    monkeypatch.setenv("OMR_LICENSE_PUBKEY", str(pub))
    key = vl.load_private_key(priv)
    doc = vl.issue_license(key, customer="Acme", floating=True, features="core,ocr")
    status = verify_license_data(doc, _pubkey(), check_clock=False)
    assert status.valid and "ocr" in status.payload.features

    # Inspect reports a good signature, and a bad one after tampering.
    _, _, ok = vl.inspect_license(doc, pub)
    assert ok is True
    doc["payload"]["features"].append("vlm")
    _, _, ok = vl.inspect_license(doc, pub)
    assert ok is False


def test_revocation_list_roundtrip(tmp_path):
    from tools import vendor_lib as vl

    rev = tmp_path / "revocations.json"
    assert vl.is_revoked(rev, "abc") is False
    vl.revoke(rev, "abc", "refunded")
    assert vl.is_revoked(rev, "abc") is True
    vl.unrevoke(rev, "abc")
    assert vl.is_revoked(rev, "abc") is False


# ------------------------------------------------------- License Issuer (admin app)
def test_issuer_offers_an_encrypted_key_and_keeps_issued_copies(tmp_path):
    from fastapi.testclient import TestClient

    from tools import vendor_lib as vl
    from tools.license_admin import server

    private, _ = vl.generate_keypair(tmp_path / "keys", bits=2048, passphrase="pw")
    saved = dict(server._state)
    try:
        server.configure(key=private, home=tmp_path / "home")
        client = TestClient(server.create_app())
        state = client.get("/api/state").json()
        assert state["offered_key"] == str(private) and not state["key_loaded"]
        assert client.post("/api/load-key", json={"path": str(private)}).status_code == 400
        assert client.post("/api/load-key", json={"path": str(private), "passphrase": "pw"}).status_code == 200
        issued = client.post("/api/issue", json={"customer": "Acme", "floating": True}).json()
        assert Path(issued["saved_to"]).read_text(encoding="utf-8") == issued["text"]
        checked = client.post("/api/inspect", json={"text": issued["text"]}).json()
        assert checked["signature_ok"] is True
        assert (tmp_path / "home" / "issued").is_dir()
    finally:
        server._state.clear()
        server._state.update(saved)


# ------------------------------------------------------------- packaged builds
def test_a_packaged_build_ignores_the_disable_flag_and_swapped_keys(licensed_env, keys, monkeypatch):
    import sys
    import types

    from src.licensing import paths

    private_key, pub_path = keys
    monkeypatch.setattr(paths, "_frozen", lambda: True)
    # No key built in: the key file named by OMR_LICENSE_PUBKEY is not trusted
    monkeypatch.setitem(sys.modules, "src.licensing.embedded_key", types.ModuleType("embedded_key"))
    gate.install_license_bytes(json.dumps(make_license(private_key)).encode(), require_valid=False)
    gate.clear_cache()
    assert gate.current_status().reason == Reason.NO_PUBLIC_KEY
    # Nor does the disable flag turn the gate off
    monkeypatch.setenv("OMR_LICENSE_DISABLE", "1")
    monkeypatch.delenv("OMR_LICENSE_ENFORCE")
    gate.clear_cache()
    assert gate.licensing_active() and not gate.current_status().valid
    # The key built in is the one used
    embedded = types.ModuleType("embedded_key")
    embedded.PUBLIC_KEY_PEM = pub_path.read_bytes()
    monkeypatch.setitem(sys.modules, "src.licensing.embedded_key", embedded)
    gate.clear_cache()
    assert gate.current_status().valid


def test_embed_public_key_writes_and_clears_the_module(tmp_path, keys):
    import importlib.util

    def load(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    # packaging/ is not importable as a package (pip's "packaging" wins)
    embed = load("embed_public_key", Path(__file__).resolve().parents[2] / "packaging" / "embed_public_key.py").embed
    _, pub_path = keys
    module = tmp_path / "embedded_key.py"
    assert embed(pub_path, module)
    assert load("embedded_key", module).PUBLIC_KEY_PEM == pub_path.read_bytes().strip()
    assert not embed(tmp_path / "missing.pem", module)
    assert not module.exists()
