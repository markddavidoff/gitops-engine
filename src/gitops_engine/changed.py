"""Did a push touch a deploy input? Backs a tool's ``changed --since SHA`` command.

A reusable workflow runs this first on a push and ends the run early when nothing
relevant changed. It replaces a hard-coded ``on.push.paths`` list, which GitHub cannot
read from a configuration file.

The caller supplies the deploy inputs as repository-relative globs: ``<dir>/**`` for a
directory, anything else for one exact file. Files outside them, such as a state mirror
or snapshots that a workflow's own PR touches, do not count.

Any doubt reports "changed": an unknown or unreachable sha (first push, force-push,
shallow clone) or a git failure. A spurious run waits at an approval gate; a skipped
one would leave the destination behind silently.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


class _Unknown(Exception):
    """git could not answer; the caller treats it as "changed"."""


def _git(repo: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as e:
        raise _Unknown(str(e)) from None
    if proc.returncode != 0:
        raise _Unknown(proc.stderr.strip())
    return proc.stdout


def git_toplevel(repo: Path) -> Path:
    """The root of the repository that holds ``repo``. Raises ``ValueError`` outside one."""
    try:
        return Path(_git(Path(repo), "rev-parse", "--show-toplevel").strip()).resolve()
    except _Unknown as e:
        raise ValueError(f"not a git repository: {e}") from None


def repo_relative(path: Path, top: Path) -> str | None:
    """``path`` as a repository-relative POSIX path, or None when it is outside ``top``."""
    try:
        return Path(os.path.realpath(path)).relative_to(top).as_posix()
    except ValueError:
        return None


def _matches(path: str, globs: list[str]) -> bool:
    for pattern in globs:
        if pattern.endswith("/**"):
            if path.startswith(pattern[: -len("**")]):
                return True
        elif path == pattern:
            return True
    return False


def changed_since(repo: Path, sha: str, globs: list[str]) -> bool:
    """True when a path matching ``globs`` differs between ``sha`` and HEAD, or when unsure.

    ``repo`` is any path inside the repository. An unknown or unreachable ``sha`` counts
    as changed.
    """
    try:
        top = git_toplevel(repo)
        # `^{commit}` with --verify refuses anything that is not a commit in this clone,
        # including option-shaped input, before it reaches `git diff`.
        base = _git(
            top, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{sha}^{{commit}}"
        ).strip()
        names = _git(top, "diff", "--no-renames", "--name-only", "-z", base, "HEAD")
    except (_Unknown, ValueError):
        return True
    return any(_matches(name, globs) for name in names.split("\0") if name)
