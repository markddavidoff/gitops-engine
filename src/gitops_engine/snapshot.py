"""Pre-apply snapshots of live rows, and `restore` from them.

`apply_deploy` writes every live row it is about to update to
`<snapshot_dir>/<UTC timestamp>.yaml` before its first write, then records each row's
post-write version as `written_<version key>`. `restore` writes the snapshot values
back with check-and-set: a row whose current version is not the one the apply left
behind (or, for an unwritten row, the one the snapshot saw) was edited since, and is
skipped and reported.

A snapshot row is the schema's `snapshot_row(live)`, keys in that order, followed by
the written-version key. Rows load as plain dicts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from .destination import Destination, DestinationError, is_empty
from .schema import Always, RecordSchema, Union


@dataclass
class RestoreResult:
    restored: list[str]
    skipped: list[tuple[str, str]]
    # "<live id>: <error>" when a write failed; later rows were not attempted
    failure: str | None = None


def written_key(schema: RecordSchema[Any, Any]) -> str:
    return f"written_{schema.snapshot_version_key}"


def _list_fields(schema: RecordSchema[Any, Any]) -> set[str]:
    return {
        f.name
        for f in schema.fields
        if isinstance(f.policy, Union) or (isinstance(f.policy, Always) and f.policy.unordered)
    }


def _known_keys(schema: RecordSchema[Any, Any]) -> set[str]:
    return {
        schema.snapshot_id_key,
        schema.snapshot_version_key,
        written_key(schema),
        *(f.name for f in schema.fields),
    }


def _dump(path: Path, taken_at: str, rows: list[dict[str, Any]]) -> None:
    body = {"taken_at": taken_at, "rows": rows}
    path.write_text(yaml.safe_dump(body, sort_keys=False, default_flow_style=False))


def write_snapshot[L](
    live: list[L],
    snapshot_dir: Path,
    *,
    schema: RecordSchema[Any, L],
    now: datetime | None = None,
) -> Path:
    """Write `live` to `<snapshot_dir>/<YYYYMMDDTHHMMSSZ>.yaml` and return its path.

    Creates the directory. Never overwrites an earlier snapshot: a second run in the
    same second gets a `-1`, `-2`, … suffix. Raises `OSError` when the file cannot be
    written, which callers treat as "abort before any write"."""
    when = (now or datetime.now(UTC)).astimezone(UTC)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    stem = when.strftime("%Y%m%dT%H%M%SZ")
    path = snapshot_dir / f"{stem}.yaml"
    n = 0
    while path.exists():
        n += 1
        path = snapshot_dir / f"{stem}-{n}.yaml"
    rows = [{**schema.snapshot_row(lv), written_key(schema): None} for lv in live]
    _dump(path, when.strftime("%Y-%m-%dT%H:%M:%SZ"), rows)
    return path


def _load_raw(path: Path, schema: RecordSchema[Any, Any]) -> tuple[str, list[dict[str, Any]]]:
    raw = yaml.safe_load(path.read_text()) or {}
    known = _known_keys(schema)
    lists = _list_fields(schema)
    rows: list[dict[str, Any]] = []
    for r in raw.get("rows") or []:
        unknown = set(r) - known
        if unknown:
            raise ValueError(f"{path}: unknown snapshot key(s): {sorted(unknown)}")
        row = dict(r)
        for name in lists:
            row[name] = list(row.get(name) or [])
        row.setdefault(written_key(schema), None)
        rows.append(row)
    return str(raw.get("taken_at", "")), rows


def load_snapshot(path: Path, *, schema: RecordSchema[Any, Any]) -> list[dict[str, Any]]:
    return _load_raw(path, schema)[1]


def record_written(
    path: Path, live_id: str, version: str | None, *, schema: RecordSchema[Any, Any]
) -> None:
    """Set the written version on the row for `live_id` and rewrite the file."""
    taken_at, rows = _load_raw(path, schema)
    for r in rows:
        if r[schema.snapshot_id_key] == live_id:
            r[written_key(schema)] = version
    _dump(path, taken_at, rows)


def restore[L](
    path: Path,
    dest: Destination[L],
    *,
    schema: RecordSchema[Any, L],
    rows: list[str] | None = None,
) -> RestoreResult:
    """Write snapshot values back, skipping rows edited since.

    `rows` filters by the schema's id field. The schema's `snapshot_values` decides
    what each row sends; a row holding an empty value is sent with `retire=True`. The
    destination's preflight runs before any write. A destination error on a row sets
    `failure` and stops: rows restored so far stay listed, later rows are not
    attempted."""
    snap = load_snapshot(path, schema=schema)
    if rows is not None:
        wanted = set(rows)
        snap = [r for r in snap if wanted & set(r.get(schema.id_field) or [])]
    sends = [schema.snapshot_values(r) for r in snap]
    dest.preflight(sends, restoring=True)

    id_key, version_key = schema.snapshot_id_key, schema.snapshot_version_key
    result = RestoreResult(restored=[], skipped=[])
    for r, send in zip(snap, sends, strict=True):
        live_id = r[id_key]
        cur = dest.get(live_id)
        if cur is None:
            result.skipped.append((live_id, "gone"))
            continue
        expected = r[written_key(schema)] or r[version_key]
        now = schema.live_version(cur)
        if now != expected:
            result.skipped.append(
                (live_id, f"edited since snapshot: {version_key} {now} != {expected}")
            )
            continue
        retire = any(is_empty(v) for v in send.values())
        try:
            dest.update(live_id, send, retire=retire)
        except DestinationError as e:
            result.failure = f"{live_id}: {e}"
            break
        result.restored.append(live_id)
    return result


__all__ = [
    "RestoreResult",
    "load_snapshot",
    "record_written",
    "restore",
    "write_snapshot",
    "written_key",
]
