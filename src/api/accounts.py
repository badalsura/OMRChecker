"""
Station accounts: registration, login and sessions for the web GUI.

Login is off until the first account is registered; that account is the
administrator, and from then on every request needs a signed-in user (or the
API key, for programs). Later registrations wait for an administrator's
approval unless registration is set to "open" (or "closed": only an
administrator adds accounts).

Server folders: administrators browse and scan any folder on the server.
Every other account may use only the folders an administrator allows it
(users.folders, a JSON list); with none it can only upload files.

Passwords are stored as PBKDF2-SHA256 hashes with a random salt; sessions are
random tokens sent back in an HttpOnly cookie (or an Authorization: Bearer
header). Everything lives in <data_dir>/accounts.sqlite3. Python 3.8
compatible, standard library only.
"""

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path

COOKIE = "omr_session"
SESSION_DAYS = 30
ITERATIONS = 200_000
ROLES = ("admin", "reviewer")
REGISTRATION = ("approval", "open", "closed")
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{1,39}$")
MIN_PASSWORD = 8
# Failed logins per user name before a short lock
MAX_FAILURES = 10
LOCK_SECONDS = 300

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    name TEXT PRIMARY KEY COLLATE NOCASE,
    display TEXT,
    role TEXT NOT NULL,
    active INTEGER NOT NULL,
    salt TEXT NOT NULL,
    hash TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_login REAL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    name TEXT NOT NULL COLLATE NOCASE,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


class AccountError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _hash_password(password, salt):
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), ITERATIONS
    )
    return digest.hex()


def _token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _public(row):
    return {
        "name": row["name"],
        "display": row["display"] or row["name"],
        "role": row["role"],
        "active": bool(row["active"]),
        "created_at": row["created_at"],
        "last_login": row["last_login"],
        "folders": _folders(row),
    }


def _folders(row):
    try:
        folders = json.loads(row["folders"] or "[]")
    except (IndexError, KeyError, ValueError):
        return []
    return [str(f) for f in folders] if isinstance(folders, list) else []


def clean_folders(folders):
    """Absolute, de-duplicated folder paths, or AccountError."""
    if not isinstance(folders, list):
        raise AccountError("Folders must be a list of paths", 422)
    out = []
    for folder in folders:
        folder = str(folder or "").strip()
        if not folder:
            continue
        path = Path(folder).expanduser()
        if not path.is_absolute():
            raise AccountError(f"'{folder}' is not a full folder path", 422)
        try:
            folder = str(path.resolve())
        except (OSError, RuntimeError):
            raise AccountError(f"'{folder}' is not a valid folder path", 422) from None
        if folder not in out:
            out.append(folder)
    if len(out) > 100:
        raise AccountError("Allow at most 100 folders per user", 422)
    return out


class Accounts:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.failures = {}
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.executescript(SCHEMA)
            columns = [r["name"] for r in self.conn.execute("PRAGMA table_info(users)")]
            if "folders" not in columns:  # stations made before per-user folders
                self.conn.execute("ALTER TABLE users ADD COLUMN folders TEXT")
            self.conn.commit()
        self._any = None

    def close(self):
        with self.lock:
            self.conn.close()

    # ---- state ---------------------------------------------------------
    def enabled(self):
        """True once any account exists: from then on login is required."""
        if self._any is None:
            with self.lock:
                row = self.conn.execute("SELECT 1 FROM users LIMIT 1").fetchone()
            self._any = row is not None
        return self._any

    def registration(self):
        with self.lock:
            row = self.conn.execute(
                "SELECT value FROM settings WHERE key = 'registration'"
            ).fetchone()
        return row["value"] if row and row["value"] in REGISTRATION else "approval"

    def set_registration(self, mode):
        if mode not in REGISTRATION:
            raise AccountError(f"Registration must be one of {', '.join(REGISTRATION)}", 422)
        with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('registration', ?)",
                (mode,),
            )
            self.conn.commit()

    # ---- users ---------------------------------------------------------
    def get(self, name):
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM users WHERE name = ?", (name,)
            ).fetchone()
        return row

    def users(self):
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM users ORDER BY active, created_at"
            ).fetchall()
        return [_public(r) for r in rows]

    @staticmethod
    def check_password(password):
        if not isinstance(password, str) or len(password) < MIN_PASSWORD:
            raise AccountError(f"Use a password of at least {MIN_PASSWORD} characters", 422)
        if len(password) > 200:
            raise AccountError("That password is too long", 422)

    def create(self, name, password, display=None, role="reviewer", active=True):
        name = (name or "").strip()
        if not NAME.match(name):
            raise AccountError(
                "User names are 2-40 letters, digits, '.', '_', '-' or '@'", 422
            )
        if role not in ROLES:
            raise AccountError(f"Role must be one of {', '.join(ROLES)}", 422)
        self.check_password(password)
        salt = secrets.token_hex(16)
        try:
            with self.lock:
                self.conn.execute(
                    "INSERT INTO users (name, display, role, active, salt, hash, created_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (
                        name,
                        (display or "").strip()[:80] or None,
                        role,
                        1 if active else 0,
                        salt,
                        _hash_password(password, salt),
                        time.time(),
                    ),
                )
                self.conn.commit()
        except sqlite3.IntegrityError:
            raise AccountError(f"The user name '{name}' is taken", 409) from None
        self._any = True
        return _public(self.get(name))

    def register(self, name, password, display=None):
        """Self-registration: the first account is an active administrator."""
        if not self.enabled():
            return self.create(name, password, display, role="admin", active=True)
        mode = self.registration()
        if mode == "closed":
            raise AccountError("Registration is closed; ask an administrator", 403)
        return self.create(name, password, display, active=(mode == "open"))

    def update(
        self, name, role=None, active=None, password=None, display=None, folders=None
    ):
        row = self.get(name)
        if row is None:
            raise AccountError(f"No user '{name}'", 404)
        if role is not None and role not in ROLES:
            raise AccountError(f"Role must be one of {', '.join(ROLES)}", 422)
        losing_admin = row["role"] == "admin" and row["active"] and (
            (role is not None and role != "admin") or active is False
        )
        if losing_admin and self._admins() <= 1:
            raise AccountError("The station needs at least one active administrator", 409)
        sets, values = [], []
        if role is not None:
            sets.append("role = ?")
            values.append(role)
        if active is not None:
            sets.append("active = ?")
            values.append(1 if active else 0)
        if display is not None:
            sets.append("display = ?")
            values.append(display.strip()[:80] or None)
        if folders is not None:
            sets.append("folders = ?")
            values.append(json.dumps(clean_folders(folders)))
        if password is not None:
            self.check_password(password)
            salt = secrets.token_hex(16)
            sets += ["salt = ?", "hash = ?"]
            values += [salt, _hash_password(password, salt)]
        if sets:
            with self.lock:
                self.conn.execute(
                    f"UPDATE users SET {', '.join(sets)} WHERE name = ?",
                    (*values, row["name"]),
                )
                if active is False or password is not None:
                    # Signed out everywhere
                    self.conn.execute(
                        "DELETE FROM sessions WHERE name = ?", (row["name"],)
                    )
                self.conn.commit()
        return _public(self.get(name))

    def delete(self, name):
        row = self.get(name)
        if row is None:
            raise AccountError(f"No user '{name}'", 404)
        if row["role"] == "admin" and row["active"] and self._admins() <= 1:
            raise AccountError("The station needs at least one active administrator", 409)
        with self.lock:
            self.conn.execute("DELETE FROM users WHERE name = ?", (row["name"],))
            self.conn.execute("DELETE FROM sessions WHERE name = ?", (row["name"],))
            self.conn.commit()

    def _admins(self):
        with self.lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND active = 1"
            ).fetchone()
        return row["n"]

    # ---- sessions ------------------------------------------------------
    def login(self, name, password):
        """(token, user) for a right password; the same error for every failure."""
        key = (name or "").strip().lower()
        now = time.time()
        failed, until = self.failures.get(key, (0, 0))
        if until > now:
            raise AccountError("Too many failed attempts; try again in a few minutes", 429)
        row = self.get(key) if key else None
        ok = False
        if row is not None and isinstance(password, str):
            ok = hmac.compare_digest(_hash_password(password, row["salt"]), row["hash"])
        elif isinstance(password, str):
            # Same work for an unknown name, so timing doesn't reveal accounts
            _hash_password(password, "00" * 16)
        if not ok:
            failed += 1
            self.failures[key] = (failed, now + LOCK_SECONDS if failed >= MAX_FAILURES else 0)
            raise AccountError("Wrong user name or password", 401)
        self.failures.pop(key, None)
        if not row["active"]:
            raise AccountError("This account is waiting for an administrator's approval", 403)
        token = secrets.token_urlsafe(32)
        with self.lock:
            self.conn.execute(
                "INSERT INTO sessions (token_hash, name, created_at, expires_at) VALUES (?,?,?,?)",
                (_token_hash(token), row["name"], now, now + SESSION_DAYS * 86400),
            )
            self.conn.execute(
                "UPDATE users SET last_login = ? WHERE name = ?", (now, row["name"])
            )
            self.conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
            self.conn.commit()
        return token, _public(self.get(row["name"]))

    def session_user(self, token):
        """The signed-in, active user of a session token, or None."""
        if not token:
            return None
        with self.lock:
            row = self.conn.execute(
                "SELECT u.* FROM sessions s JOIN users u ON u.name = s.name"
                " WHERE s.token_hash = ? AND s.expires_at > ? AND u.active = 1",
                (_token_hash(token), time.time()),
            ).fetchone()
        return _public(row) if row else None

    def logout(self, token):
        if not token:
            return
        with self.lock:
            self.conn.execute(
                "DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),)
            )
            self.conn.commit()

    def change_password(self, name, old, new):
        row = self.get(name)
        if row is None or not hmac.compare_digest(
            _hash_password(old or "", row["salt"]), row["hash"]
        ):
            raise AccountError("The current password is wrong", 403)
        return self.update(name, password=new)


def request_user(request, explicit=None, default="local"):
    """The name recorded with a change: the signed-in user (cannot be set by
    the client), else the X-User header, else the name in the body."""
    signed_in = getattr(request.state, "user", None)
    if signed_in:
        return signed_in["name"]
    return request.headers.get("x-user") or explicit or default
