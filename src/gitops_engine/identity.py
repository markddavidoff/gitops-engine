"""The identity table and the single `resolve` that decides "same record".

An identity file holds one entry per real-world thing, keyed by its permanent
canonical id. Every record, desired row and live row is reduced to `ItemKeys`, and
`resolve` matches those keys against the table in a fixed order: canonical id, then
each source id in the order the spec declares, then each match key in the order the
spec declares. The first key that hits decides.

A consumer declares the keys once, as an `IdentitySpec`: its source names, its match
keys (each with the normaliser that makes two spellings equal), and the key order
inside each entry of the file. The engine holds no key of its own.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.constructor import DuplicateKeyError
from ruamel.yaml.error import YAMLError

SOURCE_ID = "source id"  # the `Hit.key` for a canonical id or a source id


@dataclass(frozen=True)
class MatchKey:
    """One list-valued key an entry can be matched on.

    `field` is the key in the file and in `Identity.values`; `label` is the word
    messages use; `norm` makes two spellings of one value equal. With `skip_empty`,
    a value that normalises to "" matches nothing and is never reported as a
    duplicate; without it, the empty string is a value like any other."""

    field: str
    label: str
    norm: Callable[[str], str]
    skip_empty: bool = False


@dataclass(frozen=True)
class IdentitySpec:
    """How a consumer's identity file is laid out and matched.

    `match_keys` is the resolution order. `file_order` is the key order inside each
    entry of the file and names every match-key field plus `"sources"`."""

    sources: tuple[str, ...]
    match_keys: tuple[MatchKey, ...]
    destination_source: str
    file_order: tuple[str, ...]
    file: str = "identities.yaml"

    def __post_init__(self) -> None:
        want = {mk.field for mk in self.match_keys} | {"sources"}
        if len(self.file_order) != len(set(self.file_order)) or set(self.file_order) != want:
            raise ValueError(
                f"file_order must list each of {sorted(want)} exactly once, got "
                f"{list(self.file_order)}"
            )
        if self.destination_source not in self.sources:
            raise ValueError(f"destination_source {self.destination_source!r} is not a source")


@dataclass
class Identity:
    canonical_id: str
    values: dict[str, list[str]] = field(default_factory=dict)  # match-key field -> values
    sources: dict[str, str] = field(default_factory=dict)


@dataclass
class IdentityTable:
    entries: dict[str, Identity] = field(default_factory=dict)


def _invalid(msg: str) -> ValueError:
    """Every malformed-file problem is a ValueError, whatever the cause."""
    return ValueError(msg)


def _str_list(cid: str, key: str, value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"identity {cid!r}: {key} must be a list of strings")
    return list(value)


def _parse_identity(cid: str, data: Any, spec: IdentitySpec) -> Identity:
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise _invalid(f"identity {cid!r} must be a mapping")
    unknown = sorted(set(data) - set(spec.file_order))
    if unknown:
        raise ValueError(f"identity {cid!r}: unknown key(s): {unknown}")
    sources = data.get("sources") or {}
    if not isinstance(sources, dict):
        raise _invalid(f"identity {cid!r}: sources must be a mapping")
    bad = sorted(set(sources) - set(spec.sources))
    if bad:
        raise ValueError(f"identity {cid!r}: unknown source key(s): {bad}")
    for k, v in sources.items():
        if not isinstance(v, str) or not v:
            raise ValueError(f"identity {cid!r}: source {k} must be a non-empty string")
    return Identity(
        canonical_id=cid,
        values={mk.field: _str_list(cid, mk.field, data.get(mk.field)) for mk in spec.match_keys},
        sources=dict(sources),
    )


def parse_identities(raw: Any, name: str, spec: IdentitySpec) -> IdentityTable:
    """Validate a parsed identity mapping into a table. `name` is for errors."""
    if raw is None:
        return IdentityTable()
    if not isinstance(raw, dict):
        raise _invalid(f"{name}: top level must be a mapping of canonical id -> identity")
    entries: dict[str, Identity] = {}
    for cid, data in raw.items():
        if not isinstance(cid, str) or not cid:
            raise ValueError(f"{name}: canonical id {cid!r} must be a non-empty string")
        entries[cid] = _parse_identity(cid, data, spec)
    return IdentityTable(entries=entries)


def load_text(text: str, name: str, *, round_trip: bool = False) -> Any:
    """Parse YAML text with ruamel, which rejects a duplicate key (PyYAML keeps the last).

    A repeated canonical id would otherwise drop an identity from a hand-edited file
    without a word. Any parse failure is a `ValueError` naming the file."""
    yml = YAML(typ="rt" if round_trip else "safe", pure=True)
    if round_trip:
        yml.preserve_quotes = True  # keep hand-written quote styles through a save
    try:
        return yml.load(text)
    except DuplicateKeyError as e:
        m = re.search(r'found duplicate key "([^"]*)"', str(e))
        key = m.group(1) if m else "?"
        line = f" (line {e.problem_mark.line + 1})" if e.problem_mark else ""
        raise ValueError(
            f"{name}: duplicate key {key!r}{line}; each canonical id may appear once"
        ) from e
    except YAMLError as e:
        raise ValueError(f"{name}: not valid YAML: {e}") from e


def load_identities(path: Path, spec: IdentitySpec) -> IdentityTable:
    """Load the identity file. A missing file is an empty table."""
    if not path.exists():
        return IdentityTable()
    return parse_identities(load_text(path.read_text(), path.name), path.name, spec)


@dataclass(frozen=True)
class ItemKeys:
    """What one record / desired row / live row offers to `resolve`.

    `ids` are candidate canonical ids. `sources` pairs a source name with the id the
    item carries in that source. `values` pairs a match-key field with the values the
    item carries for it."""

    label: str
    ids: tuple[str, ...] = ()
    sources: tuple[tuple[str, str | None], ...] = ()
    values: tuple[tuple[str, tuple[str, ...]], ...] = ()


@dataclass(frozen=True)
class Hit:
    canonical_id: str
    key: str  # `SOURCE_ID` or a `MatchKey.label`
    value: str


def _hits(item: ItemKeys, table: IdentityTable, spec: IdentitySpec) -> list[Hit]:
    """Every (identity, key) match in resolution order, duplicates included."""
    out: list[Hit] = []
    for xid in item.ids:
        if xid in table.entries:
            out.append(Hit(xid, SOURCE_ID, xid))
    carried = dict(item.sources)
    for src in spec.sources:
        value = carried.get(src)
        if value:
            out.extend(
                Hit(i.canonical_id, SOURCE_ID, value)
                for i in table.entries.values()
                if i.sources.get(src) == value
            )
    offered = dict(item.values)
    for mk in spec.match_keys:
        for v in offered.get(mk.field, ()):
            nv = mk.norm(v)
            if mk.skip_empty and not nv:
                continue
            out.extend(
                Hit(i.canonical_id, mk.label, v)
                for i in table.entries.values()
                if nv in {mk.norm(x) for x in i.values.get(mk.field, ())}
            )
    return out


def resolve(item: ItemKeys, table: IdentityTable, spec: IdentitySpec) -> Hit | None:
    """The first hit in key order decides: canonical id, source ids, then match keys."""
    hits = _hits(item, table, spec)
    return hits[0] if hits else None


def resolve_all(item: ItemKeys, table: IdentityTable, spec: IdentitySpec) -> list[Hit]:
    """One hit per distinct identity the item reaches, each by its first key."""
    seen: set[str] = set()
    out: list[Hit] = []
    for h in _hits(item, table, spec):
        if h.canonical_id not in seen:
            seen.add(h.canonical_id)
            out.append(h)
    return out


def conflict(item: ItemKeys, table: IdentityTable, spec: IdentitySpec) -> str | None:
    """A message when the later keys pick a different identity than the first."""
    hits = resolve_all(item, table, spec)
    if len(hits) < 2:
        return None
    a, b = hits[0], hits[1]
    return (
        f"{item.label} resolves to two identities: "
        f"{a.canonical_id} ({a.key} '{a.value}') and {b.canonical_id} ({b.key} '{b.value}')"
    )


def _dupes(what: str, pairs: list[tuple[str, str, str]]) -> list[str]:
    """`pairs` = (comparison key, shown value, canonical id), in table order."""
    owners: dict[str, list[str]] = {}
    shown: dict[str, str] = {}
    for k, value, cid in pairs:
        shown.setdefault(k, value)
        ids = owners.setdefault(k, [])
        if cid not in ids:
            ids.append(cid)
    return [
        f"{what} '{shown[k]}' is on two identities: {', '.join(ids)}"
        for k, ids in owners.items()
        if len(ids) > 1
    ]


def table_errors(table: IdentityTable, spec: IdentitySpec) -> list[str]:
    """A match-key value or a source id listed on two identities."""
    ents = list(table.entries.values())
    errors: list[str] = []
    for mk in spec.match_keys:
        errors += _dupes(
            mk.label,
            [
                (mk.norm(v), v, i.canonical_id)
                for i in ents
                for v in i.values.get(mk.field, ())
                if not mk.skip_empty or mk.norm(v)
            ],
        )
    for src in spec.sources:
        errors += _dupes(
            f"source {src}",
            [(i.sources[src], i.sources[src], i.canonical_id) for i in ents if src in i.sources],
        )
    return errors


def with_implicit[D](
    table: IdentityTable,
    rows: list[D],
    spec: IdentitySpec,
    *,
    keys: Callable[[D], ItemKeys],
    implicit: Callable[[D, set[str]], Identity],
) -> IdentityTable:
    """A copy of `table` plus one identity per row that resolves to nothing.

    `implicit(row, taken)` builds the identity for such a row; `taken` holds the
    canonical ids already in use. Rows go in order, so a later row of the same
    thing resolves to the identity an earlier one created."""
    out = IdentityTable(entries=dict(table.entries))
    for row in rows:
        if resolve(keys(row), out, spec) is not None:
            continue
        ident = implicit(row, set(out.entries))
        if ident.canonical_id in out.entries:
            raise ValueError(f"implicit identity {ident.canonical_id!r} is already taken")
        out.entries[ident.canonical_id] = ident
    return out
