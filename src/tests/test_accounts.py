"""Station accounts: first admin, approval, login required, API key, audit name."""

from fastapi.testclient import TestClient

from src.api.app import create_app
from src.tests.test_api import make_sheet, png_bytes, upload_template
from src.synth.render import default_spec


def login(client, name, password="secret123"):
    return client.post("/auth/login", json={"username": name, "password": password})


def test_login_off_until_the_first_account(tmp_path):
    app = create_app(tmp_path / "data", workers=1)
    with TestClient(app) as admin:
        assert admin.get("/auth/status").json()["accounts"] is False
        assert admin.get("/capabilities").status_code == 200

        # The first account is an active administrator, signed in at once
        first = admin.post(
            "/auth/register", json={"username": "badal", "password": "secret123"}
        ).json()
        assert first["first"] and first["signed_in"]
        assert first["user"]["role"] == "admin"
        assert admin.get("/capabilities").status_code == 200

        # From now on a visitor must sign in
        visitor = TestClient(app)
        assert visitor.get("/capabilities").status_code == 401
        assert visitor.get("/health").status_code == 200

        # Later sign-ups wait for approval
        pending = visitor.post(
            "/auth/register", json={"username": "teacher", "password": "secret123"}
        ).json()
        assert pending["user"]["active"] is False and not pending["signed_in"]
        assert login(visitor, "teacher").status_code == 403
        assert visitor.get("/auth/users").status_code == 401
        admin.patch("/auth/users/teacher", json={"active": True})
        assert login(visitor, "teacher").status_code == 200
        assert visitor.get("/capabilities").status_code == 200
        # Reviewers can't manage users
        assert visitor.get("/auth/users").status_code == 403

        # Wrong passwords look the same as unknown names
        assert login(visitor, "teacher", "wrong-one").status_code == 401
        assert login(visitor, "nobody").status_code == 401

        # The last administrator can't be demoted, disabled or deleted
        assert admin.patch("/auth/users/badal", json={"role": "reviewer"}).status_code == 409
        assert admin.delete("/auth/users/badal").status_code == 409

        # Disabling signs the account out everywhere
        admin.patch("/auth/users/teacher", json={"active": False})
        assert visitor.get("/capabilities").status_code == 401

        # Closed registration: only admins add accounts
        admin.patch("/auth/settings", json={"registration": "closed"})
        closed = TestClient(app).post(
            "/auth/register", json={"username": "other", "password": "secret123"}
        )
        assert closed.status_code == 403
        assert admin.post(
            "/auth/users", json={"username": "other", "password": "secret123"}
        ).status_code == 201

        admin.post("/auth/logout")
        assert admin.get("/capabilities").status_code == 401


def test_api_key_still_works_and_corrections_carry_the_signed_in_name(tmp_path):
    spec = default_spec(questions=20, roll_digits=4, with_zones=False)
    app = create_app(tmp_path / "data", workers=1, api_key="k3y")
    with TestClient(app) as client:
        client.post("/auth/register", json={"username": "badal", "password": "secret123"})
        template_id = upload_template(client, spec)
        image, _ = make_sheet(spec, 3)
        scan = client.post(
            "/scans",
            data={"template_id": template_id},
            files=[("files", ("s.png", png_bytes(image), "image/png"))],
        ).json()["scans"][0]
        # The signed-in name wins over a client-chosen X-User
        current = client.get(f"/scans/{scan['scan_id']}").json()["responses"]["q1"]
        client.post(
            f"/scans/{scan['scan_id']}/corrections",
            json={"changes": {"q1": "A" if current != "A" else "B"}},
            headers={"X-User": "someone-else"},
        )
        audit = client.get(f"/scans/{scan['scan_id']}/audit").json()["items"]
        assert audit[0]["user"] == "badal"

        # Programs keep using the API key without an account
        program = TestClient(app)
        assert program.get("/capabilities").status_code == 401
        assert program.get("/capabilities", headers={"X-API-Key": "k3y"}).status_code == 200
        # ... or a bearer token from /auth/login
        token = login(program, "badal").json()["token"]
        bearer = TestClient(app)
        assert bearer.get(
            "/capabilities", headers={"Authorization": f"Bearer {token}"}
        ).status_code == 200
