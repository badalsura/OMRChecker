"""
Login and registration for the station (see src/api/accounts.py).

require_access is the dependency every protected endpoint uses: the API key
(programs, the SDK) or, once accounts exist, a signed-in user.
"""

import secrets
from typing import List, Optional

from fastapi import Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from src.api.accounts import COOKIE, SESSION_DAYS, AccountError, Accounts


class Credentials(BaseModel):
    username: str = Field(..., max_length=40)
    password: str = Field(..., max_length=200)
    display: Optional[str] = Field(None, max_length=80, description="Full name")


class PasswordChange(BaseModel):
    old_password: str = Field(..., max_length=200)
    new_password: str = Field(..., max_length=200)


class UserUpdate(BaseModel):
    role: Optional[str] = Field(None, description="admin or reviewer")
    active: Optional[bool] = Field(None, description="false = can't log in")
    password: Optional[str] = Field(None, max_length=200, description="New password")
    display: Optional[str] = Field(None, max_length=80)
    folders: Optional[List[str]] = Field(
        None, description="Server folders a non-admin may browse and scan ([] = none)"
    )


class NewUser(Credentials):
    role: str = Field("reviewer", description="admin or reviewer")


class AuthSettings(BaseModel):
    registration: str = Field(
        ..., description="approval (admin approves sign-ups), open, or closed"
    )


def session_token(request: Request):
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.cookies.get(COOKIE)


def make_access(settings, accounts: Accounts):
    def require_access(request: Request):
        expected = settings.api_key
        provided = request.headers.get("x-api-key") or request.query_params.get(
            "api_key"
        )
        if expected and provided and secrets.compare_digest(provided, expected):
            return
        user = accounts.session_user(session_token(request)) if accounts.enabled() else None
        if user:
            request.state.user = user
            return
        if accounts.enabled():
            raise HTTPException(401, "Log in to use this station")
        if expected:
            raise HTTPException(401, "Missing or invalid API key (X-API-Key header)")

    return require_access


def register(app, ctx):
    accounts = ctx.accounts

    def fail(error):
        raise HTTPException(error.status, str(error)) from None

    def current(request: Request):
        user = accounts.session_user(session_token(request))
        if not user:
            raise HTTPException(401, "Log in first")
        return user

    def admin(request: Request):
        user = current(request)
        if user["role"] != "admin":
            raise HTTPException(403, "Only an administrator can do this")
        return user

    def set_cookie(request, response, token):
        response.set_cookie(
            COOKIE,
            token,
            max_age=SESSION_DAYS * 86400,
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
            path="/",
        )

    @app.get("/auth/status", tags=["auth"])
    def auth_status(request: Request):
        """Whether login is on, who is signed in, and how registration works."""
        user = accounts.session_user(session_token(request))
        return {
            "accounts": accounts.enabled(),
            "registration": accounts.registration() if accounts.enabled() else "first",
            "user": user,
        }

    @app.post("/auth/register", tags=["auth"], status_code=201)
    def auth_register(body: Credentials, request: Request, response: Response):
        """
        Create an account. The first account is the administrator and is
        signed in at once (login is required from then on); later accounts
        wait for an administrator unless registration is open.
        """
        first = not accounts.enabled()
        try:
            user = accounts.register(body.username, body.password, body.display)
            token = None
            if user["active"]:
                token, user = accounts.login(body.username, body.password)
                set_cookie(request, response, token)
        except AccountError as error:
            fail(error)
        return {"user": user, "first": first, "signed_in": bool(token), "token": token}

    @app.post("/auth/login", tags=["auth"])
    def auth_login(body: Credentials, request: Request, response: Response):
        """Sign in; sets the session cookie and returns a token for programs."""
        try:
            token, user = accounts.login(body.username, body.password)
        except AccountError as error:
            fail(error)
        set_cookie(request, response, token)
        return {"user": user, "token": token}

    @app.post("/auth/logout", tags=["auth"])
    def auth_logout(request: Request, response: Response):
        accounts.logout(session_token(request))
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}

    @app.post("/auth/password", tags=["auth"])
    def auth_password(body: PasswordChange, request: Request, response: Response):
        """Change your own password (signs out your other sessions)."""
        user = current(request)
        try:
            accounts.change_password(user["name"], body.old_password, body.new_password)
            token, user = accounts.login(user["name"], body.new_password)
        except AccountError as error:
            fail(error)
        set_cookie(request, response, token)
        return {"user": user}

    # ---- administration ------------------------------------------------
    @app.get("/auth/users", tags=["auth"])
    def auth_users(_: dict = Depends(admin)):
        return {"users": accounts.users(), "registration": accounts.registration()}

    @app.post("/auth/users", tags=["auth"], status_code=201)
    def auth_add_user(body: NewUser, _: dict = Depends(admin)):
        """Add an active account (works when registration is closed)."""
        try:
            return accounts.create(body.username, body.password, body.display, body.role)
        except AccountError as error:
            fail(error)

    @app.patch("/auth/users/{name}", tags=["auth"])
    def auth_update_user(name: str, body: UserUpdate, _: dict = Depends(admin)):
        """Approve or disable an account, change its role, reset its password or
        set the server folders it may use."""
        try:
            return accounts.update(
                name, body.role, body.active, body.password, body.display, body.folders
            )
        except AccountError as error:
            fail(error)

    @app.delete("/auth/users/{name}", tags=["auth"])
    def auth_delete_user(name: str, me: dict = Depends(admin)):
        if name.lower() == me["name"].lower():
            raise HTTPException(409, "You can't delete your own account")
        try:
            accounts.delete(name)
        except AccountError as error:
            fail(error)
        return {"deleted": name}

    @app.patch("/auth/settings", tags=["auth"])
    def auth_settings(body: AuthSettings, _: dict = Depends(admin)):
        try:
            accounts.set_registration(body.registration)
        except AccountError as error:
            fail(error)
        return {"registration": accounts.registration()}
