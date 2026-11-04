"""Browser access and DNS-rebinding protection, outside the API-key boundary."""

import base64
import binascii
import ipaddress
import secrets
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse

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
    # The mounted API independently validates every X-Api-Key. Neither arbitrary
    # headers nor browser credentials bypass that dependency.
    is_api = path == "/api/v1" or path.startswith("/api/v1/")
    if path != "/ping" and not is_api:
        if not config.auth_username and not config.allow_unauthenticated_browser:
            return JSONResponse(
                {"detail": "Set MANGARR_AUTH_USERNAME and MANGARR_AUTH_PASSWORD to enable browser access."},
                status_code=403,
                headers={"Cache-Control": "no-store"},
            )
        if config.auth_username and not browser_authenticated(request):
            return JSONResponse(
                {"detail": "Browser login required"}, status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Mangarr", charset="UTF-8"',
                         "Cache-Control": "no-store"},
            )
    response = await call_next(request)
    if path == "/initialize.json":
        response.headers["Cache-Control"] = "no-store"
    return response
