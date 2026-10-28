"""Deterministic YAML writers for a directory of records, one file per record.

Output is stable across re-runs (the caller's row order, fixed key order, no
timestamps) so unchanged input produces a zero-diff re-run. Only `*.yaml` files in
the target directory are touched, and the `.manifest.yaml` is never deleted.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from .manifest import MANIFEST, write_manifest


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def unique_slugs(labels: list[str]) -> list[str]:
    """One slug per label, in order. A label seen again gets `-2`, `-3`, ... appended."""
    seen: dict[str, int] = {}
    out: list[str] = []
    for base in labels:
        if base in seen:
            seen[base] += 1
            out.append(f"{base}-{seen[base]}")
        else:
            seen[base] = 1
            out.append(base)
    return out


def write_files[R](
    files: list[tuple[str, R]],
    path: Path,
    *,
    to_dict: Callable[[R], dict[str, Any]],
    generator: str | None = None,
) -> None:
    """Write each `(stem, row)` to `<path>/<stem>.yaml` and delete the stale `*.yaml` files.

    With `generator`, also record the directory's manifest under that name."""
    path.mkdir(parents=True, exist_ok=True)
    wanted = {f"{stem}.yaml" for stem, _ in files}
    for existing in path.glob("*.yaml"):
        if existing.name not in wanted and existing.name != MANIFEST:
            existing.unlink()
    for stem, row in files:
        text = yaml.safe_dump(
            to_dict(row), sort_keys=False, default_flow_style=False, allow_unicode=True
        )
        (path / f"{stem}.yaml").write_text(text)
    if generator is not None:
        write_manifest(path, generator)


def write_record_dir[R](
    rows: list[R],
    path: Path,
    *,
    slug: Callable[[R], str],
    to_dict: Callable[[R], dict[str, Any]],
    generator: str | None = None,
) -> None:
    """Write `rows`, already in write order, to `<path>/<slug>.yaml`.

    `slug` gives each row's base slug; repeats are made unique by `unique_slugs`."""
    stems = unique_slugs([slug(r) for r in rows])
    write_files(list(zip(stems, rows, strict=True)), path, to_dict=to_dict, generator=generator)


def load_record_dir[R](path: Path, from_dict: Callable[[dict], R]) -> list[R]:
    """Every `*.yaml` row under `path` in file-name order; the manifest is skipped."""
    out: list[R] = []
    for p in sorted(Path(path).glob("*.yaml")):
        if p.name == MANIFEST:
            continue
        out.append(from_dict(yaml.safe_load(p.read_text())))
    return out
