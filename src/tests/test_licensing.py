"""
RSA license: signing/verification, machine binding, expiry, tamper and
forgery resistance, and the API activation flow.

Licensing is disabled for the rest of the suite (conftest). These tests
re-enable it with their own ephemeral key pair and a temp license dir, so they
exercise the real gate without touching a developer's installed license.
"""

import base64
import json
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
