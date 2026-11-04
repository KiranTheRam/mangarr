"""One-time browser login setup using a code from the local data directory."""

import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import parse_qs

from anyio import CapacityLimiter, to_thread
from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .config import config

SETUP_CODE_FILE = "browser_setup_code"
LOGIN_FILE = "browser_login.json"
MAX_SETUP_BYTES = 4096
_password_workers = CapacityLimiter(2)


def _create_private_file(path: Path, value: str) -> None:
    # Publish a complete 0600 file without replacing an existing one, even
    # when two workers start or submit the setup form at the same time.
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


def prepare_browser_setup() -> bool:
    """Ensure an unclaimed installation has a persistent, private setup code."""
    if config.auth_username or config.allow_unauthenticated_browser:
        return False
    code_path = config.data_dir / SETUP_CODE_FILE
    if load_browser_login() is not None:
        code_path.unlink(missing_ok=True)
        return False
    try:
        _create_private_file(code_path, secrets.token_hex(16) + "\n")
    except FileExistsError:
        pass
    if load_browser_login() is not None:
        code_path.unlink(missing_ok=True)
        return False
    return True


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
code {{ overflow-wrap: anywhere }}
.error {{ color: #ffb4ab }}
</style>
<main>
  <h1>Set up your Mangarr login</h1>
  <p>Create a login to finish installing or upgrading Mangarr. Your library, settings,
     and existing API clients stay available.</p>
  <p>Get the one-time setup code from the <code>browser_setup_code</code> file in Mangarr’s
     config directory. In Docker Compose, run <code>docker compose exec mangarr cat
     /config/browser_setup_code</code>. On Unraid, open the Mangarr container console
     and run <code>cat /config/browser_setup_code</code>.</p>
  {error}
  <form method="post">
    <label>Setup code<input name="code" required maxlength="64" autocomplete="off" spellcheck="false"></label>
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
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/x-www-form-urlencoded":
        return setup_page("Submit the setup form from this page.", 415)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_SETUP_BYTES:
            return setup_page("The setup form is too large.", 413)
        body.extend(chunk)
    try:
        fields = parse_qs(body.decode(), max_num_fields=4)
        code, username, password, confirmation = (
            fields[name][0] for name in ("code", "username", "password", "confirmation")
        )
    except (KeyError, ValueError):
        return setup_page("Fill in all four fields.", 400)

    code_path = config.data_dir / SETUP_CODE_FILE
    try:
        expected_code = code_path.read_bytes().strip()
    except FileNotFoundError:
        return setup_page("The setup code is unavailable. Reload this page.", 409)
    if not expected_code or not secrets.compare_digest(code.strip().encode(), expected_code):
        return setup_page("The setup code is incorrect.", 403)
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
        return RedirectResponse(home, status_code=303, headers={"Cache-Control": "no-store"})
    code_path.unlink(missing_ok=True)
    return RedirectResponse(home, status_code=303, headers={"Cache-Control": "no-store"})
