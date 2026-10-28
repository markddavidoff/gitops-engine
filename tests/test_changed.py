"""`changed_since`: does a push touch a deploy input? Throwaway git repos only."""

import subprocess
from pathlib import Path

import pytest

from gitops_engine.changed import changed_since, git_toplevel, repo_relative

GLOBS = ["data/desired/**", "data/identities.yaml", "data/retire.yaml", "tool.yaml"]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit_all(repo: Path, message: str) -> str:
    _git(repo, "add", "--all")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _edit(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text((path.read_text() if path.exists() else "") + "edit: 1\n")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "caller"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "commit.gpgsign", "false")
    for rel in (
        "tool.yaml",
        "data/identities.yaml",
        "data/retire.yaml",
        "data/desired/acme.yaml",
        "data/state/acme.yaml",
        "data/snapshots/20260101T000000Z.yaml",
        "data/datasets/acme.yaml",
    ):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("seed: 1\n")
    return root, _commit_all(root, "seed")


def test_changed_unknown_sha_is_changed(repo):
    root, _ = repo
    for sha in ("0" * 40, "deadbeef" * 5, "not-a-sha", "", "--output=/dev/null"):
        assert changed_since(root, sha, GLOBS) is True, sha


def test_changed_state_only_is_unchanged(repo):
    root, base = repo
    _edit(root / "data" / "state" / "acme.yaml")
    _edit(root / "data" / "state" / "new.yaml")
    _edit(root / "data" / "snapshots" / "20260102T000000Z.yaml")
    _commit_all(root, "state PR")
    assert changed_since(root, base, GLOBS) is False


@pytest.mark.parametrize(
    "rel",
    [
        "tool.yaml",
        "data/identities.yaml",
        "data/retire.yaml",
        "data/desired/acme.yaml",
        "data/desired/new-row.yaml",
        "data/desired/nested/deep.yaml",
    ],
)
def test_changed_input_edit_is_changed(repo, rel):
    root, base = repo
    _edit(root / rel)
    _commit_all(root, f"edit {rel}")
    assert changed_since(root, base, GLOBS) is True


def test_changed_deleted_input_is_changed(repo):
    root, base = repo
    (root / "data" / "desired" / "acme.yaml").unlink()
    _commit_all(root, "drop a row")
    assert changed_since(root, base, GLOBS) is True


def test_changed_lookalike_paths_are_not_inputs(repo):
    root, base = repo
    # `desired-extra/` shares a prefix with `desired/`; a backup file shares a file name's.
    _edit(root / "data" / "desired-extra" / "acme.yaml")
    _edit(root / "data" / "datasets" / "acme.yaml")
    _edit(root / "data" / "identities.yaml.bak")
    _commit_all(root, "lookalikes")
    assert changed_since(root, base, GLOBS) is False


def test_changed_from_a_subdirectory(repo):
    root, base = repo
    _edit(root / "data" / "retire.yaml")
    _commit_all(root, "edit retire")
    assert changed_since(root / "data", base, GLOBS) is True


def test_changed_outside_a_git_repository_is_changed(tmp_path):
    assert changed_since(tmp_path, "0" * 40, GLOBS) is True


def test_git_toplevel_and_repo_relative(repo, tmp_path):
    root, _ = repo
    top = git_toplevel(root / "data")
    assert top == root.resolve()
    assert repo_relative(root / "data" / "desired", top) == "data/desired"
    assert repo_relative(root, top) == "."
    assert repo_relative(tmp_path / "elsewhere", top) is None
    with pytest.raises(ValueError, match="not a git repository"):
        git_toplevel(tmp_path)
