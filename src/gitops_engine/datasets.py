"""`datasets.yaml` and the per-dataset directories under `datasets/`, both inside `data_dir`.

The file lists dataset names in priority order (earlier wins). Any other key passes
through untouched in `DatasetConfig.extra` for the consumer to read and validate.
Each dataset directory holds one YAML file per record plus a `.manifest.yaml`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .manifest import MANIFEST

DATASETS_FILE = "datasets.yaml"
DATASETS_SUBDIR = "datasets"


@dataclass(frozen=True)
class DatasetConfig:
    names: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)  # every key but `datasets`


@dataclass(frozen=True)
class DatasetRecord[R]:
    dataset: str
    source_id: str  # the file stem inside the dataset directory
    record: R


def _read(data_dir: Path) -> dict | None:
    path = Path(data_dir) / DATASETS_FILE
    if not path.is_file():
        return None
    raw = yaml.safe_load(path.read_text())
    return raw if isinstance(raw, dict) else {}


def load_datasets_config(data_dir: Path, *, default_names: Iterable[str] = ()) -> DatasetConfig:
    """Parse `datasets.yaml`; `default_names` when the file is absent.

    A present file with no `datasets` key yields an empty name list, which
    `datasets_errors` reports. Keys other than `datasets` land in `extra`, unread.
    """
    raw = _read(data_dir)
    if raw is None:
        return DatasetConfig(names=list(default_names))
    names = [str(n) for n in raw.get("datasets") or []]
    extra = {k: v for k, v in raw.items() if k != "datasets"}
    return DatasetConfig(names=names, extra=extra)


def dataset_dir(data_dir: Path, name: str) -> Path:
    return Path(data_dir) / DATASETS_SUBDIR / name


def datasets_errors(data_dir: Path, *, default_names: Iterable[str] = ()) -> list[str]:
    """Structural problems in `datasets.yaml` or the directories it should match; empty
    when clean. Keys the engine does not know are not reported."""
    root = Path(data_dir)
    cfg = load_datasets_config(root, default_names=default_names)
    errors: list[str] = []
    if not cfg.names:
        errors.append(f"{DATASETS_FILE}: empty datasets list")
    seen: set[str] = set()
    for name in cfg.names:
        if name in seen:
            errors.append(f"{DATASETS_FILE}: {name} listed twice")
            continue
        seen.add(name)
        if not dataset_dir(root, name).is_dir():
            errors.append(f"{DATASETS_FILE}: {name} has no directory")
    base = root / DATASETS_SUBDIR
    if base.is_dir():
        errors += [
            f"{DATASETS_FILE}: directory {DATASETS_SUBDIR}/{p.name} is not listed"
            for p in sorted(base.iterdir())
            if p.is_dir() and p.name not in seen
        ]
    return errors


def load_datasets[R](
    data_dir: Path, cfg: DatasetConfig, parse: Callable[[dict], R]
) -> list[DatasetRecord[R]]:
    """Every record of every configured dataset: priority order, then file name."""
    out: list[DatasetRecord[R]] = []
    for name in cfg.names:
        directory = dataset_dir(data_dir, name)
        for path in sorted(directory.glob("*.yaml")):
            if path.name == MANIFEST:
                continue
            record = parse(yaml.safe_load(path.read_text()))
            out.append(DatasetRecord(dataset=name, source_id=path.stem, record=record))
    return out
