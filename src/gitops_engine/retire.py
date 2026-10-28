"""The retire file: values a reviewed change may remove, keyed by canonical id.

    <canonical id>:
      tags: [a, b]     # a `Union` field: values to retire
      size: 120000     # a `KeepIfUnset` field: the live value to clear

Every key is optional and names a field of the consumer's schema. A field the plan owns
outright (`Always`) cannot be retired and is rejected. The file is read by the planner
and never generated over.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .schema import Always, Policy, RecordSchema, Union

RetireEntry = dict[str, Any]  # field -> list (a `Union` field) or scalar (a `KeepIfUnset` field)


def _retirable(schema: RecordSchema[Any, Any]) -> dict[str, Policy]:
    return {f.name: f.policy for f in schema.fields if not isinstance(f.policy, Always)}


def load_retire(path: Path, schema: RecordSchema[Any, Any]) -> dict[str, RetireEntry]:
    """Read the retire file. A missing or empty file yields `{}`.

    Each entry holds every retirable field of the schema, in schema order: a list for a
    `Union` field (empty when absent), a scalar for any other (None when absent). A key
    naming an `Always` field raises `ValueError`; a key the schema does not name is ignored."""
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text()) or {}
    always = {f.name for f in schema.fields if isinstance(f.policy, Always)}
    retirable = _retirable(schema)
    out: dict[str, RetireEntry] = {}
    for ext_id, body in raw.items():
        body = body or {}
        for name in body:
            if name in always:
                raise ValueError(
                    f"{path.name}: {ext_id}: '{name}' cannot be retired (the plan owns it outright)"
                )
        out[str(ext_id)] = {
            name: (
                [str(v) for v in body.get(name) or []]
                if isinstance(policy, Union)
                else body.get(name)
            )
            for name, policy in retirable.items()
        }
    return out


def _empty(value: Any) -> bool:
    return value is None or (isinstance(value, list) and not value)


def write_retire(entries: dict[str, RetireEntry], path: Path, *, header: str) -> None:
    """Write entries with sorted keys, omitting empty fields and empty entries.

    A field keeps the order the entry gives it (`load_retire` returns schema order).
    `header` is written as a leading `#` comment block."""
    body: dict[str, dict] = {}
    for ext_id in sorted(entries):
        item = {
            name: list(value) if isinstance(value, list) else value
            for name, value in entries[ext_id].items()
            if not _empty(value)
        }
        if item:
            body[ext_id] = item
    comment = "".join(f"# {line}".rstrip() + "\n" for line in header.splitlines())
    text = yaml.safe_dump(body, sort_keys=False, default_flow_style=False) if body else ""
    path.write_text(comment + text)
