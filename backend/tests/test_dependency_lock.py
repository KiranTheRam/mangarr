"""Verify backend dependency lock file integrity and completeness."""

from pathlib import Path
import re
import tomllib

import pytest
from packaging.requirements import Requirement


def normalize_name(name: str) -> str:
    """PEP 503-normalise a distribution name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_lock_file(content: str):
    """Parse requirements.txt into header comments and backslash-continued requirement blocks."""
    lines = content.splitlines()
    header_lines = []
    blocks = []
    current_block = []
    in_continuation = False
    saw_requirement = False

    for raw_line in lines:
        line = raw_line.strip()
        if not in_continuation:
            if not line or line.startswith("#"):
                if not saw_requirement:
                    header_lines.append(line)
                continue
            saw_requirement = True
            current_block = [line]
            if line.endswith("\\"):
                in_continuation = True
            else:
                blocks.append(current_block)
                current_block = []
        else:
            current_block.append(line)
            if not line.endswith("\\"):
                in_continuation = False
                blocks.append(current_block)
                current_block = []

    if current_block:
        blocks.append(current_block)

    return "\n".join(header_lines), blocks


def test_dependency_lock():
    repo_root = Path(__file__).resolve().parents[2]
    pyproject_path = repo_root / "backend" / "pyproject.toml"
    lock_path = repo_root / "backend" / "requirements.txt"

    if not lock_path.exists():
        pytest.fail(f"backend/requirements.txt does not exist; run scripts/lock-backend.sh to generate it")

    with open(pyproject_path, "rb") as f:
        pyproject = tomllib.load(f)

    lock_content = lock_path.read_text(encoding="utf-8")
    header, blocks = parse_lock_file(lock_content)

    # 1. Lock was generated from pyproject.toml
    assert "pyproject.toml" in header, (
        f"Lock file header in {lock_path.name} does not mention 'pyproject.toml'"
    )

    # 2. Parse pinned requirements and verify each is followed by at least one sha256 hash
    locked_packages = {}
    for block in blocks:
        first_line = block[0].rstrip("\\").strip()
        match = re.match(r"^([a-zA-Z0-9][a-zA-Z0-9_.-]*)\s*==\s*([^\s\\;#]+)", first_line)
        if match:
            pkg_name = normalize_name(match.group(1))
            version = match.group(2)
            locked_packages[pkg_name] = version

            hashes = [line for line in block[1:] if re.match(r"^--hash=sha256:[0-9a-fA-F]{64}\b", line)]
            assert len(hashes) >= 1, (
                f"Pinned requirement '{pkg_name}=={version}' in {lock_path.name} "
                f"is not followed by at least one --hash=sha256: line"
            )

    # The Docker build installs this lock before installing Mangarr with
    # --no-deps and --no-build-isolation, so CI also verifies the pinned versions.
    project_deps = pyproject.get("project", {}).get("dependencies", [])
    build_deps = pyproject.get("build-system", {}).get("requires", [])
    for spec in [*project_deps, *build_deps]:
        requirement = Requirement(spec)
        name = normalize_name(requirement.name)
        assert name in locked_packages, f"{requirement} is not pinned in {lock_path.name}"
        pinned = locked_packages[name]
        assert pinned in requirement.specifier, (
            f"{name}=={pinned} in {lock_path.name} does not satisfy {requirement}"
        )

    # Dev-only dependencies are not required and not in the lock
    dev_deps = pyproject.get("project", {}).get("optional-dependencies", {}).get("dev", [])
    for dev_dep in dev_deps:
        dev_name = normalize_name(Requirement(dev_dep).name)
        assert dev_name not in locked_packages, (
            f"Dev-only dependency '{dev_name}' should not be in {lock_path.name}"
        )
