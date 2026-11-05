#!/usr/bin/env python3
"""Resolve deterministic main-build versions or set a new release baseline."""

import argparse
import json
from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = "backend/VERSION"
VERSION_PATTERN = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", re.ASCII)


def parse_version(value: str) -> tuple[int, int, int]:
    match = VERSION_PATTERN.fullmatch(value)
    if not match:
        raise ValueError("Version must be MAJOR.MINOR.PATCH (for example 1.2.0).")
    return tuple(int(part) for part in match.groups())


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def build_version(root: Path, revision: str = "HEAD") -> str:
    """Count main commits after the most recent explicit baseline change.

    First-parent history counts a merged PR once, regardless of its commit count.
    Read both history and VERSION from the revision so a dirty checkout cannot
    change the version assigned to that commit.
    """
    if git(root, "rev-parse", "--is-shallow-repository") == "true":
        raise ValueError("Version resolution needs full Git history (fetch-depth: 0).")
    commit = git(root, "rev-parse", "--verify", f"{revision}^{{commit}}")
    baseline = git(root, "show", f"{commit}:{VERSION_FILE}")
    major, minor, patch = parse_version(baseline)
    baseline_commit = git(root, "log", "--first-parent", "-1", "--format=%H", commit,
                          "--", VERSION_FILE)
    if not baseline_commit:
        raise ValueError("No version baseline found in first-parent history.")
    count = int(git(root, "rev-list", "--first-parent", "--count", f"{baseline_commit}..{commit}"))
    return f"{major}.{minor}.{patch + count}"


def set_version(root: Path, value: str) -> None:
    parse_version(value)
    # Read and validate all files before writing any of them.
    paths = [root / "frontend/package.json", root / "frontend/package-lock.json"]
    manifests = [json.loads(path.read_text()) for path in paths]
    manifests[0]["version"] = value
    manifests[1]["version"] = value
    manifests[1]["packages"][""]["version"] = value
    (root / VERSION_FILE).write_text(value + "\n")
    for path, manifest in zip(paths, manifests):
        path.write_text(json.dumps(manifest, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    show = commands.add_parser("show", help="Print the build version for a committed revision.")
    show.add_argument("--revision", default="HEAD")
    set_command = commands.add_parser("set", help="Set a manual version baseline; commit the resulting files.")
    set_command.add_argument("version")
    args = parser.parse_args()
    try:
        if args.command == "set":
            set_version(ROOT, args.version)
            print(args.version)
        else:
            print(build_version(ROOT, args.revision))
    except (ValueError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
