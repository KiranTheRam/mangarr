"""First-run browser login setup and persisted credentials."""

import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import parse_qs, urlsplit

from anyio import CapacityLimiter, to_thread
from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .config import config

LOGIN_FILE = "browser_login.json"
MAX_SETUP_BYTES = 4096
_password_workers = CapacityLimiter(2)


def _create_private_file(path: Path, value: str) -> None:
    # Publish a complete 0600 file without replacing an existing one, even
    # when two workers submit the setup form at the same time.
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False)
    try:
        with temp:
            temp.write(value)
            temp.flush()
            os.fsync(temp.fileno())
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


def _password_digest(password: bytes, salt: bytes) -> bytes:
    return hashlib.scrypt(password, salt=salt, n=2**15, r=8, p=1, maxmem=64 * 1024**2)


def load_browser_login() -> BrowserLogin | None:
    try:
        data = json.loads((config.data_dir / LOGIN_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    return BrowserLogin(data["username"], bytes.fromhex(data["salt"]), bytes.fromhex(data["digest"]))


def setup_page(message: str = "", status_code: int = 200) -> HTMLResponse:
    # Messages are fixed server strings. Submitted values never appear in HTML.
    error = f'<p role="alert" class="error">{message}</p>' if message else ""
    return HTMLResponse(
        f"""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Set up your Mangarr login</title>
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
  <h1>Set up your Mangarr login</h1>
  <p>Create a login to finish installing or upgrading Mangarr. Your library, settings,
     and existing API clients stay available.</p>
  <p>Complete setup on a trusted local network. The first person to submit this form
     creates the login.</p>
  {error}
  <form method="post">
    <label>Username<input name="username" required maxlength="64" autocomplete="username"></label>
    <label>Password<input name="password" type="password" required minlength="12" maxlength="128" autocomplete="new-password"></label>
    <label>Confirm password<input name="confirmation" type="password" required maxlength="128" autocomplete="new-password"></label>
    <button>Create login</button>
  </form>
</main>
</html>""",
        status_code=status_code,
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'",
        },
    )


async def submit_browser_setup(request: Request, home: str) -> Response:
    # Prevent another site from claiming a LAN install through a visitor's
    # browser. Match the visible authority even when a proxy terminates TLS.
    try:
        origin = urlsplit(request.headers.get("origin", ""))
    except ValueError:
        return setup_page("Submit the setup form from this page.", 403)
    if (request.headers.get("sec-fetch-site", "same-origin") != "same-origin"
            or origin.scheme not in {"http", "https"}
            or origin.netloc.lower() != request.headers["host"].lower()):
        return setup_page("Submit the setup form from this page.", 403)
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/x-www-form-urlencoded":
        return setup_page("Submit the setup form from this page.", 415)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_SETUP_BYTES:
            return setup_page("The setup form is too large.", 413)
        body.extend(chunk)
    try:
        fields = parse_qs(body.decode(), max_num_fields=3)
        username, password, confirmation = (
            fields[name][0] for name in ("username", "password", "confirmation")
        )
    except (KeyError, ValueError):
        return setup_page("Fill in all three fields.", 400)

    if not 1 <= len(username) <= 64 or username != username.strip() or not username.isprintable() or ":" in username:
        return setup_page("Choose a username of up to 64 characters without a colon or surrounding spaces.", 400)
    if not 12 <= len(password) <= 128 or not password.isprintable() or password != confirmation:
        return setup_page("Choose a password of 12 to 128 characters and enter it twice.", 400)

    salt = secrets.token_bytes(16)
    digest = await to_thread.run_sync(_password_digest, password.encode(), salt, limiter=_password_workers)
    data = json.dumps({"username": username, "salt": salt.hex(), "digest": digest.hex()})
    try:
        _create_private_file(config.data_dir / LOGIN_FILE, data)
    except FileExistsError:
        return setup_page("A login has already been created. Open Mangarr to sign in.", 409)
    return RedirectResponse(home, status_code=303, headers={"Cache-Control": "no-store"})
