"""Browser access and DNS-rebinding protection, outside the API-key boundary."""

import base64
import binascii
import ipaddress
import secrets
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from .browser_setup import (
    SESSION_COOKIE, change_password, load_browser_login, same_origin, setup_page,
    submit_browser_setup, submit_sign_in,
)
from .config import config


def allowed_host(request: Request) -> bool:
    hosts = request.headers.getlist("host")
    if len(hosts) != 1:
        return False
    value = hosts[0]
    if not value or any(c.isspace() or c in "/\\@?#" for c in value):
        return False
    try:
        parsed = urlsplit("//" + value)
        host = parsed.hostname
        # Accessing port checks malformed/out-of-range ports too.
        parsed.port
        if not host:
            return False
        try:
            ipaddress.ip_address(host)
            return True
        except ValueError:
            return host.lower() in {"localhost", *(h.lower() for h in config.allowed_hosts)}
    except ValueError:
        return False


def browser_authenticated(request: Request) -> bool:
    try:
        scheme, encoded = request.headers.get("authorization", "").split(" ", 1)
        if scheme.lower() != "basic":
            return False
        username, password = base64.b64decode(encoded, validate=True).split(b":", 1)
    except (ValueError, binascii.Error):
        return False
    # Compare bytes so non-ASCII credentials are supported, without short-circuit.
    user_ok = secrets.compare_digest(username, config.auth_username.encode())
    password_ok = secrets.compare_digest(password, config.auth_password.get_secret_value().encode())
    return user_ok & password_ok


async def access_boundary(request: Request, call_next):
    if not allowed_host(request):
        return JSONResponse(
            {"detail": "Unrecognized Host. Use an IP address or add this DNS name to MANGARR_ALLOWED_HOSTS."},
            status_code=400,
        )
    # ASGI mounts can prefix scope["path"]. Classify the same path the
    # router sees so a mount named /api/v1 cannot exempt browser routes.
    path = request.scope["path"]
    root_path = request.scope.get("root_path", "")
    if root_path and (path == root_path or path.startswith(root_path + "/")):
        path = path[len(root_path):]
    is_api = path == "/api/v1" or path.startswith("/api/v1/")
    if path == "/ping":
        return await call_next(request)
    # API keys and app-managed browser sessions are independent.
    if is_api and request.headers.get("x-api-key"):
        return await call_next(request)
    login = None if config.auth_username else load_browser_login()
    signed_in = login is not None and login.has_session(request)
    request.state.browser_session = signed_in
    if is_api:
        if signed_in and request.method not in {"GET", "HEAD", "OPTIONS"} and not same_origin(request):
            return JSONResponse({"detail": "Submit the request from Mangarr."}, status_code=403)
    else:
        needs_setup = not config.auth_username and login is None and not config.allow_unauthenticated_browser
        home = f"/{root_path.strip('/')}".rstrip("/") + "/"
        if path == "/setup":
            if not needs_setup:
                return RedirectResponse(home, status_code=303, headers={"Cache-Control": "no-store"})
            if request.method == "POST":
                return await submit_browser_setup(request, home)
            if request.method == "GET":
                return setup_page()
            return JSONResponse({"detail": "Method not allowed"}, status_code=405, headers={"Allow": "GET, POST"})
        if path == "/login":
            if login is None or signed_in:
                return RedirectResponse(home, status_code=303, headers={"Cache-Control": "no-store"})
            if request.method == "POST":
                return await submit_sign_in(request, login, home)
            if request.method == "GET":
                return setup_page(sign_in=True, changed=request.query_params.get("changed") == "1")
            return Response(status_code=405, headers={"Allow": "GET, POST"})
        if path in {"/auth/password", "/auth/logout"}:
            if request.method != "POST":
                return Response(status_code=405, headers={"Allow": "POST"})
            if login is None or not signed_in:
                return JSONResponse({"detail": "Sign in with your Mangarr login."}, status_code=401)
            if not same_origin(request):
                return JSONResponse({"detail": "Submit the request from Mangarr."}, status_code=403)
            response = (await change_password(request, login) if path == "/auth/password"
                        else Response(status_code=204, headers={"Cache-Control": "no-store"}))
            if response.status_code == 204:
                response.delete_cookie(SESSION_COOKIE, path=home, httponly=True, samesite="lax",
                                       secure=request.headers["origin"].startswith("https://"))
            return response
        if needs_setup:
            if request.method == "GET" and path != "/initialize.json":
                return RedirectResponse(f"{home}setup", headers={"Cache-Control": "no-store"})
            return JSONResponse(
                {"detail": "Set up your Mangarr login at /setup first."},
                status_code=403,
                headers={"Cache-Control": "no-store"},
            )
        if login is not None and not signed_in:
            if request.method == "GET" and path != "/initialize.json":
                return RedirectResponse(f"{home}login", headers={"Cache-Control": "no-store"})
            return JSONResponse({"detail": "Sign in to Mangarr."}, status_code=401,
                                headers={"Cache-Control": "no-store"})
        if config.auth_username and not browser_authenticated(request):
            return JSONResponse(
                {"detail": "Browser login required"}, status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Mangarr", charset="UTF-8"',
                         "Cache-Control": "no-store"},
            )
    response = await call_next(request)
    if path == "/initialize.json" or signed_in:
        response.headers["Cache-Control"] = "no-store"
    return response
