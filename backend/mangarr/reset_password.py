"""Interactive local recovery for the app-managed browser login."""

import argparse
import asyncio
import sys
import warnings
from getpass import GetPassWarning, getpass

from sqlalchemy.exc import SQLAlchemyError

from .browser_setup import load_browser_login, replace_browser_password, valid_password
from .config import config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m mangarr.reset_password", description="Reset the existing Mangarr browser password.",
    )
    _, extra = parser.parse_known_args(argv)
    if extra:
        parser.error("Enter the password when prompted in an interactive terminal.")

    if config.auth_username:
        print("This login is managed by the environment. Update MANGARR_AUTH_PASSWORD and restart Mangarr.", file=sys.stderr)
        return 1

    try:
        login = load_browser_login()
    except (OSError, KeyError, ValueError, TypeError):
        print("Could not read the browser login in the configured data directory.", file=sys.stderr)
        return 1
    if login is None:
        print("There is no browser login in the configured data directory. Complete setup in the browser first.", file=sys.stderr)
        return 1
    if not sys.stdin.isatty():
        print("Run the password reset in an interactive terminal.", file=sys.stderr)
        return 1

    print(f"Resetting the password for {login.username!r}.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", GetPassWarning)
            password = getpass("New Mangarr password: ")
            confirmation = getpass("Confirm new password: ")
    except (EOFError, GetPassWarning, KeyboardInterrupt):
        print("\nPassword reset canceled.", file=sys.stderr)
        return 1
    if not valid_password(password, confirmation):
        print("Choose a password of 12 to 128 characters and enter it twice.", file=sys.stderr)
        return 1

    try:
        changed = asyncio.run(replace_browser_password(login, password))
    except (OSError, KeyError, ValueError, TypeError, SQLAlchemyError):
        print("Could not update the browser login. Check the configured data directory and file permissions.", file=sys.stderr)
        return 1
    if not changed:
        print("The login changed while you were entering the password. Run the reset command again.", file=sys.stderr)
        return 1
    print(f"Mangarr password reset. Sign in as {login.username!r} with your new password.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
