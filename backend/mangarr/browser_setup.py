"""First-run browser login setup and persisted credentials."""

import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import parse_qs, urlsplit

from anyio import CapacityLimiter, to_thread
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .config import config
from .db import engine
from .sign_in_throttle import SignInThrottle

LOGIN_FILE = "browser_login.json"
MAX_FORM_BYTES = 8192
SESSION_COOKIE = "mangarr_session"
SESSION_SECONDS = 7 * 24 * 60 * 60
_password_workers = CapacityLimiter(2)
_sign_in_throttle = SignInThrottle()


def _write_private_file(path: Path, value: str, *, replace: bool = False) -> None:
    # Publish a complete 0600 file without replacing an existing one, even
    # when two workers submit the setup form at the same time.
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False)
    try:
        with temp:
            temp.write(value)
            temp.flush()
            os.fsync(temp.fileno())
        if replace:
            os.replace(temp.name, path)
        else:
            os.link(temp.name, path)
    finally:
        Path(temp.name).unlink(missing_ok=True)


@dataclass(frozen=True)
class BrowserLogin:
    username: str
    salt: bytes
    digest: bytes

    async def verify(self, username: bytes, password: bytes) -> bool:
        if len(password) > 512 or not secrets.compare_digest(username, self.username.encode()):
            return False
        digest = await to_thread.run_sync(_password_digest, password, self.salt, limiter=_password_workers)
        return secrets.compare_digest(digest, self.digest)


    def session_token(self) -> str:
        payload = f"{int(time.time()) + SESSION_SECONDS}.{secrets.token_hex(16)}"
        signature = self._session_signature(payload)
        return f"{payload}.{signature}"

    def has_session(self, request: Request) -> bool:
        token = request.cookies.get(SESSION_COOKIE, "")
        if len(token) > 128 or not token.isascii():
            return False
        payload, _, signature = token.rpartition(".")
        expires, _, nonce = payload.partition(".")
        return (expires.isdecimal() and len(nonce) == 32
                and int(expires) > time.time()
                and secrets.compare_digest(signature, self._session_signature(payload)))

    def _session_signature(self, payload: str) -> str:
        return hmac.digest(self.digest, b"mangarr-browser-session:" + payload.encode(), "sha256").hex()


def _password_digest(password: bytes, salt: bytes) -> bytes:
    return hashlib.scrypt(password, salt=salt, n=2**15, r=8, p=1, maxmem=64 * 1024**2)


def load_browser_login() -> BrowserLogin | None:
    try:
        data = json.loads((config.data_dir / LOGIN_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    return BrowserLogin(data["username"], bytes.fromhex(data["salt"]), bytes.fromhex(data["digest"]))


def setup_page(message: str = "", status_code: int = 200, *, sign_in: bool = False, changed: bool = False) -> HTMLResponse:
    # Messages are fixed server strings. Submitted values never appear in HTML.
    error = f'<p role="alert" class="error">{message}</p>' if message else ""
    title = "Sign in to Mangarr" if sign_in else "Set up your Mangarr login"
    intro = (
        '<p role="status">Password changed. Sign in with your new password.</p>'
        if sign_in and changed else ""
    )
    if not sign_in:
        intro = (
            "<p>Create a login to finish installing or upgrading Mangarr. Your library, settings, "
            "and existing API clients stay available.</p><p>Complete setup on a trusted local network. "
            "The first person to submit this form creates the login.</p>"
        )
    # HTML counts UTF-16 code units. Allow 128 characters even when each takes two units.
    confirmation = "" if sign_in else (
        '<label>Confirm password<input name="confirmation" type="password" required maxlength="256" '
        'autocomplete="new-password"></label>'
    )
    autocomplete = "current-password" if sign_in else "new-password"
    button = "Sign in" if sign_in else "Create login"
    return HTMLResponse(
        f"""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root {{ color-scheme: dark; font: 1rem system-ui, sans-serif; background: #111318; color: #e1e2e6 }}
main {{ max-width: 32rem; margin: 6vh auto; padding: 1.5rem }}
p {{ line-height: 1.55 }}
label {{ display: block; margin-top: 1rem }}
input, button {{ box-sizing: border-box; width: 100%; font: inherit; padding: .65rem; margin-top: .35rem }}
button {{ margin-top: 1.25rem; cursor: pointer }}
.error {{ color: #ffb4ab }}
</style>
<main>
  <h1>{title}</h1>
  {intro}
  {error}
  <form method="post">
    <label>Username<input name="username" required maxlength="64" autocomplete="username"></label>
    <label>Password<input name="password" type="password" required maxlength="256" autocomplete="{autocomplete}"></label>
    {confirmation}
    <button>{button}</button>
  </form>
</main>
</html>""",
        status_code=status_code,
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'",
        },
    )


def same_origin(request: Request) -> bool:
    # Match the browser-facing authority even when a proxy terminates TLS.
    try:
        origin = urlsplit(request.headers.get("origin", ""))
    except ValueError:
        return False
    return (request.headers.get("sec-fetch-site", "same-origin") == "same-origin"
            and origin.scheme in {"http", "https"}
            and origin.netloc.lower() == request.headers["host"].lower())


class FormError(ValueError):
    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


async def read_form(request: Request, *names: str) -> tuple[str, ...]:
    if not same_origin(request):
        raise FormError("Submit the form from Mangarr.", 403)
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/x-www-form-urlencoded":
        raise FormError("Submit the form from Mangarr.", 415)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_FORM_BYTES:
            raise FormError("The form is too large.", 413)
        body.extend(chunk)
    try:
        fields = parse_qs(body.decode(), max_num_fields=len(names))
        return tuple(fields[name][0] for name in names)
    except (KeyError, ValueError) as exc:
        raise FormError("Fill in all fields.", 400) from exc


async def encode_login(username: str, password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = await to_thread.run_sync(_password_digest, password.encode(), salt, limiter=_password_workers)
    return json.dumps({"username": username, "salt": salt.hex(), "digest": digest.hex()})


def valid_password(password: str, confirmation: str) -> bool:
    return 12 <= len(password) <= 128 and password.isprintable() and password == confirmation


async def submit_browser_setup(request: Request, home: str) -> Response:
    try:
        username, password, confirmation = await read_form(request, "username", "password", "confirmation")
    except FormError as exc:
        return setup_page(str(exc), exc.status)
    if not 1 <= len(username) <= 64 or username != username.strip() or not username.isprintable() or ":" in username:
        return setup_page("Choose a username of up to 64 characters without a colon or surrounding spaces.", 400)
    if not valid_password(password, confirmation):
        return setup_page("Choose a password of 12 to 128 characters and enter it twice.", 400)

    data = await encode_login(username, password)
    try:
        _write_private_file(config.data_dir / LOGIN_FILE, data)
    except FileExistsError:
        return setup_page("A login has already been created. Open Mangarr to sign in.", 409)
    return RedirectResponse(home, status_code=303, headers={"Cache-Control": "no-store"})


async def submit_sign_in(request: Request, login: BrowserLogin, home: str) -> Response:
    try:
        username, password = await read_form(request, "username", "password")
    except FormError as exc:
        return setup_page(str(exc), exc.status, sign_in=True)
    # The form can arrive after a password reset. Use the current revision
    # for throttling so attempts against the old login cannot lock out the new one.
    if load_browser_login() != login:
        return setup_page("The login changed. Please try again.", 409, sign_in=True)
    host = request.client.host if request.client else None
    attempt = _sign_in_throttle.reserve(host, login.salt)
    if isinstance(attempt, int):
        response = setup_page("Too many sign-in attempts. Wait a moment and try again.", 429, sign_in=True)
        response.headers["Retry-After"] = str(attempt)
        return response
    signed_in = False
    try:
        if not await login.verify(username.encode(), password.encode()):
            return setup_page("The username or password is incorrect.", 401, sign_in=True)
        if load_browser_login() != login:
            return setup_page("The login changed. Please try again.", 409, sign_in=True)
        signed_in = True
    finally:
        _sign_in_throttle.finish(host, attempt, success=signed_in)
    response = RedirectResponse(home, status_code=303, headers={"Cache-Control": "no-store"})
    response.set_cookie(
        SESSION_COOKIE, login.session_token(), max_age=SESSION_SECONDS, path=home,
        httponly=True, samesite="lax", secure=request.headers["origin"].startswith("https://"),
    )
    return response


async def replace_browser_password(login: BrowserLogin, password: str) -> bool:
    data = await encode_login(login.username, password)
    # Serialize the compare-and-replace across server and CLI processes using
    # the SQLite writer lock. The login file remains the credential store.
    async with engine.begin() as connection:
        await connection.exec_driver_sql("BEGIN IMMEDIATE")
        if load_browser_login() != login:
            return False
        _write_private_file(config.data_dir / LOGIN_FILE, data, replace=True)
    return True


async def change_password(request: Request, login: BrowserLogin) -> Response:
    try:
        current, password, confirmation = await read_form(request, "current", "password", "confirmation")
        if not valid_password(password, confirmation):
            raise FormError("Choose a password of 12 to 128 characters and enter it twice.", 400)
        if current == password:
            raise FormError("Choose a different password.", 400)
        if not await login.verify(login.username.encode(), current.encode()):
            raise FormError("The current password is incorrect.", 400)
        if not await replace_browser_password(login, password):
            raise FormError("The login changed. Sign in again before retrying.", 409)
    except FormError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=exc.status, headers={"Cache-Control": "no-store"})
    return Response(status_code=204, headers={"Cache-Control": "no-store"})
