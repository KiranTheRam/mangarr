"""Exercise release versioning against real Git merge histories."""

import importlib.util
import json
from pathlib import Path
import subprocess

import pytest


spec = importlib.util.spec_from_file_location(
    "app_version", Path(__file__).resolve().parents[2] / "scripts/version.py"
)
app_version = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app_version)


def git(root, *args):
    return app_version.git(root, *args)


def commit(root, message):
    git(root, "add", ".")
    git(root, "commit", "--quiet", "-m", message)
    return git(root, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "--quiet", "--initial-branch=main")
    git(tmp_path, "config", "user.name", "Version test")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend/VERSION").write_text("0.1.1\n")
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend/package.json").write_text('{"version":"0.1.1","private":true}\n')
    (tmp_path / "frontend/package-lock.json").write_text(
        '{"version":"0.1.1","lockfileVersion":3,"packages":{"":{"version":"0.1.1"}}}\n'
    )
    commit(tmp_path, "Set baseline")
    return tmp_path


def test_each_main_commit_increments_and_reruns_are_stable(repo):
    baseline = git(repo, "rev-parse", "HEAD")
    assert app_version.build_version(repo) == "0.1.1"
    for patch in (2, 3):
        (repo / "change").write_text(str(patch))
        commit(repo, "Change app")
        assert app_version.build_version(repo) == f"0.1.{patch}"
        assert app_version.build_version(repo) == f"0.1.{patch}"
    assert app_version.build_version(repo, baseline) == "0.1.1"
    (repo / "backend/VERSION").write_text("9.0.0\n")
    assert app_version.build_version(repo) == "0.1.3"


def test_merge_counts_once_and_manual_version_resets_on_merge(repo):
    git(repo, "checkout", "--quiet", "-b", "feature")
    for number in (1, 2):
        (repo / "change").write_text(str(number))
        commit(repo, "Feature work")
    git(repo, "checkout", "--quiet", "main")
    git(repo, "merge", "--quiet", "--no-ff", "feature", "-m", "Merge feature")
    assert app_version.build_version(repo) == "0.1.2"

    git(repo, "checkout", "--quiet", "-b", "release")
    app_version.set_version(repo, "1.2.0")
    commit(repo, "Select next version")
    (repo / "more").write_text("More work in release PR")
    commit(repo, "Finish release")
    git(repo, "checkout", "--quiet", "main")
    git(repo, "merge", "--quiet", "--no-ff", "release", "-m", "Merge release")
    assert app_version.build_version(repo) == "1.2.0"
    (repo / "change").write_text("3")
    commit(repo, "Another change")
    assert app_version.build_version(repo) == "1.2.1"


def test_set_updates_manifests_and_preserves_other_fields(repo):
    app_version.set_version(repo, "2.0.0")
    assert (repo / "backend/VERSION").read_text() == "2.0.0\n"
    package = json.loads((repo / "frontend/package.json").read_text())
    lock = json.loads((repo / "frontend/package-lock.json").read_text())
    assert package == {"version": "2.0.0", "private": True}
    assert lock["version"] == lock["packages"][""]["version"] == "2.0.0"
    assert lock["lockfileVersion"] == 3


@pytest.mark.parametrize("value", ["1.2", "v1.2.3", "1.02.3", "-1.2.3", "1.2.3;echo bad", "1.2.3\n"])
def test_invalid_manual_version_changes_nothing(repo, value):
    with pytest.raises(ValueError, match="MAJOR.MINOR.PATCH"):
        app_version.set_version(repo, value)
    assert not git(repo, "status", "--porcelain")


def test_shallow_history_fails_instead_of_reusing_a_version(repo, tmp_path):
    shallow = tmp_path / "shallow"
    subprocess.run(["git", "clone", "--quiet", "--depth=1", repo.as_uri(), str(shallow)], check=True)
    with pytest.raises(ValueError, match="full Git history"):
        app_version.build_version(shallow)


def test_check_versions_passes_on_fixture(repo):
    assert app_version.check_versions(repo) == "0.1.1"


def test_check_versions_fails_on_package_json_version_drift(repo):
    path = repo / "frontend/package.json"
    manifest = json.loads(path.read_text())
    manifest["version"] = "0.9.9"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match=r"frontend/package\.json version") as exc_info:
        app_version.check_versions(repo)
    assert "0.9.9" in str(exc_info.value)
    assert "python3 scripts/version.py set 0.1.1" in str(exc_info.value)


def test_check_versions_fails_on_lock_top_level_version_drift(repo):
    path = repo / "frontend/package-lock.json"
    manifest = json.loads(path.read_text())
    manifest["version"] = "0.9.9"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match=r"frontend/package-lock\.json top-level version") as exc_info:
        app_version.check_versions(repo)
    assert "0.9.9" in str(exc_info.value)
    assert "python3 scripts/version.py set 0.1.1" in str(exc_info.value)


def test_check_versions_fails_on_lock_packages_version_drift(repo):
    path = repo / "frontend/package-lock.json"
    manifest = json.loads(path.read_text())
    manifest["packages"][""]["version"] = "0.9.9"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match=r'frontend/package-lock\.json packages\[""\]\.version') as exc_info:
        app_version.check_versions(repo)
    assert "0.9.9" in str(exc_info.value)
    assert "python3 scripts/version.py set 0.1.1" in str(exc_info.value)


def test_check_versions_fails_cleanly_for_invalid_baseline(repo):
    (repo / "backend/VERSION").write_text("invalid\n")
    with pytest.raises(ValueError, match="MAJOR.MINOR.PATCH"):
        app_version.check_versions(repo)


def test_check_versions_fails_cleanly_for_missing_package_json(repo):
    (repo / "frontend/package.json").unlink()
    with pytest.raises(ValueError, match="frontend/package.json"):
        app_version.check_versions(repo)


def test_check_versions_returns_updated_version_after_set(repo):
    app_version.set_version(repo, "2.0.0")
    assert app_version.check_versions(repo) == "2.0.0"


def test_check_versions_real_repository_is_consistent():
    repo_root = Path(__file__).resolve().parents[2]
    expected = (repo_root / "backend/VERSION").read_text().strip()
    assert app_version.check_versions(repo_root) == expected
