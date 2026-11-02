"""The overrides file: every curated decision that differs from upstream, by canonical id.

The file is a mapping with two keys: the entries, under `OverridesFormat.records_key`,
keyed by canonical id, and `hidden_name_prefixes`, a list of `{prefix, reason}` items.
Each entry may hold:

- `pin`: field values that win over upstream. `null` pins an empty value, allowed only on
  a field whose policy clears. The schema's id field can never be pinned.
- `<list field>: {add: [...], remove: [...]}`: values added to or removed from upstream.
- `pending`: a destination edit waiting for a decision, under the field's name for a
  scalar and under `<list field>_add` (`PENDING_ADD`) for added list values.
- `hidden: {reason: ...}`: the record is out of the sync on both sides.
- `deleted: {<live id key>: ..., confirmed: <date>}`: the record was deleted in the
  destination and is never recreated.
- `reason`: free text.

`BUILTIN_PREFIX` is always the first hidden prefix, as in the ignore file.

`load_overrides` raises `ValueError` on a file it cannot read as this shape;
`override_errors` reports that and every softer problem without raising.
`OverridesStore` edits the file with ruamel's round trip, keeping comments.

`apply_overrides` turns upstream desired rows into the rows a deploy writes;
`check_overrides` reports what a check fails on or prints about the entries against
those upstream rows; `held_rows` names the rows a deploy must not write while a
decision is pending; `adopt_entry` and `OverridesStore.adopt` record live values the
desired rows would otherwise clear as accepted overrides.
"""

from __future__ import annotations

import datetime
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.representer import RoundTripRepresenter
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

from .destination import is_empty
from .identity import IdentityTable, load_text
from .ignore import BUILTIN_PREFIX, BUILTIN_REASON, round_trip_yaml
from .schema import Always, FieldSpec, RecordSchema, Union, clears

PENDING_ADD = "_add"
PREFIXES_KEY = "hidden_name_prefixes"
_MIN_PREFIX = 4
_LIST_OPS = ("add", "remove")
_ENTRY_KEYS = ("pin", "pending", "hidden", "deleted", "reason")


@dataclass(frozen=True)
class OverridesFormat:
    """How a consumer's overrides file is laid out: its name, the comment header of a new
    file, the key of the entry mapping, the key that holds a deleted row's destination id,
    the fields a pin or pending value may name, and the list fields."""

    file: str
    header: str
    records_key: str
    live_id_key: str
    pinnable: tuple[str, ...]
    list_fields: tuple[str, ...]


@dataclass(frozen=True)
class Deleted:
    live_id: str
    confirmed: str  # ISO date


@dataclass(frozen=True)
class HiddenPrefix:
    prefix: str
    reason: str
    line: int | None  # None = built-in


@dataclass(frozen=True)
class OverrideEntry:
    pin: dict[str, Any] = field(default_factory=dict)
    add: dict[str, list[str]] = field(default_factory=dict)
    remove: dict[str, list[str]] = field(default_factory=dict)
    pending: dict[str, Any] = field(default_factory=dict)
    hidden: str | None = None  # the reason; "" when hidden without one
    deleted: Deleted | None = None
    reason: str | None = None
    line: int = 0


@dataclass
class Overrides:
    file: str  # the file name messages cite
    entries: dict[str, OverrideEntry] = field(default_factory=dict)
    prefixes: list[HiddenPrefix] = field(
        default_factory=lambda: [HiddenPrefix(BUILTIN_PREFIX, BUILTIN_REASON, None)]
    )


# --- reading -------------------------------------------------------------------


def _plain(value: Any) -> Any:
    """A ruamel round-trip value as plain Python: str, int, float, bool, None, list, dict.
    A date becomes its ISO string."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    if isinstance(value, str):
        return str(value)
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    return value


def _key_line(m: Any, key: Any, default: int) -> int:
    try:
        return m.lc.key(key)[0] + 1
    except (AttributeError, KeyError, TypeError):
        return default


def _item_line(seq: Any, i: int) -> int:
    """The 1-based line of a list item; 0 for an item added since the file was read."""
    try:
        return seq.lc.item(i)[0] + 1
    except (AttributeError, KeyError, TypeError):
        return 0


def _text(value: Any) -> str:
    return "" if value is None else str(_plain(value)).strip()


def _is_strings(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


class _Walk:
    """One pass over a loaded file. `shape` holds what stops it being read as overrides
    (a container of the wrong type); `problems` holds every other error. Both cite
    `file:line`. With no schema, the schema checks are skipped."""

    def __init__(self, fmt: OverridesFormat, schema: RecordSchema | None) -> None:
        self.fmt = fmt
        self.schema = schema
        self.shape: list[str] = []
        self.problems: list[str] = []
        self.out = Overrides(fmt.file)

    def run(self, data: Any) -> _Walk:
        if data is None:
            return self
        if not isinstance(data, dict):
            self.shape.append(f"{self.fmt.file}: top level must be a mapping")
            return self
        for key in data:
            if key not in (self.fmt.records_key, PREFIXES_KEY):
                line = _key_line(data, key, 1)
                self.problems.append(f"{self.fmt.file}:{line}: unknown key '{key}'")
        records = data.get(self.fmt.records_key)
        if records is not None:
            if isinstance(records, dict):
                for cid, raw in records.items():
                    self._entry(str(cid), raw, _key_line(records, cid, 1))
            else:
                line = _key_line(data, self.fmt.records_key, 1)
                self.shape.append(
                    f"{self.fmt.file}:{line}: '{self.fmt.records_key}' must be a mapping"
                )
        prefixes = data.get(PREFIXES_KEY)
        if prefixes is not None:
            if isinstance(prefixes, list):
                self._prefixes(prefixes)
            else:
                line = _key_line(data, PREFIXES_KEY, 1)
                self.shape.append(f"{self.fmt.file}:{line}: '{PREFIXES_KEY}' must be a list")
        return self

    def _clears(self, name: str) -> bool:
        assert self.schema is not None
        spec = next((f for f in self.schema.fields if f.name == name), None)
        return spec is not None and clears(spec.policy)

    def _mapping(self, value: Any, where: str, what: str) -> dict | None:
        if value is None:
            return {}
        if isinstance(value, dict):
            return value
        self.shape.append(f"{where}: {what} must be a mapping")
        return None

    def _unknown(self, m: dict, allowed: Iterable[str], where: str, what: str) -> None:
        allowed = set(allowed)
        for key in m:
            if key not in allowed:
                self.problems.append(f"{where}: unknown key '{key}' in {what}")

    def _entry(self, cid: str, raw: Any, line: int) -> None:
        file = self.fmt.file
        if raw is None:
            self.out.entries[cid] = OverrideEntry(line=line)
            return
        if not isinstance(raw, dict):
            self.shape.append(f"{file}:{line}: {cid}: an entry must be a mapping")
            return
        pin: dict[str, Any] = {}
        add: dict[str, list[str]] = {}
        remove: dict[str, list[str]] = {}
        pending: dict[str, Any] = {}
        hidden: str | None = None
        deleted: Deleted | None = None
        reason: str | None = None
        for key, value in raw.items():
            key = str(key)
            where = f"{file}:{_key_line(raw, key, line)}: {cid}"
            if key == "pin":
                m = self._mapping(value, where, "pin")
                if m is not None:
                    pin = {str(k): _plain(v) for k, v in m.items()}
                    self._check_pins(pin, where)
            elif key in self.fmt.list_fields:
                m = self._mapping(value, where, key)
                if m is not None:
                    self._unknown(m, _LIST_OPS, where, key)
                    for op, into in (("add", add), ("remove", remove)):
                        vals = _plain(m.get(op))
                        if vals is None:
                            continue
                        if not _is_strings(vals):
                            self.problems.append(f"{where}: {key}.{op} values must be strings")
                            vals = [str(v) for v in vals] if isinstance(vals, list) else []
                        if vals:
                            into[key] = vals
            elif key == "pending":
                m = self._mapping(value, where, "pending")
                if m is not None:
                    pending = {str(k): _plain(v) for k, v in m.items()}
                    self._check_pending(pending, where)
            elif key == "hidden":
                m = self._mapping(value, where, "hidden")
                if m is not None:
                    self._unknown(m, ("reason",), where, "hidden")
                    hidden = _text(m.get("reason"))
                    if not hidden:
                        self.problems.append(f"{where}: hidden needs a reason")
            elif key == "deleted":
                m = self._mapping(value, where, "deleted")
                if m is not None:
                    id_key = self.fmt.live_id_key
                    self._unknown(m, (id_key, "confirmed"), where, "deleted")
                    deleted = Deleted(_text(m.get(id_key)), _text(m.get("confirmed")))
                    if not deleted.live_id:
                        self.problems.append(f"{where}: deleted needs {id_key}")
                    if not deleted.confirmed:
                        self.problems.append(f"{where}: deleted needs confirmed")
            elif key == "reason":
                reason = _text(value) or None
            elif key in self.fmt.pinnable or (
                isinstance(value, dict) and any(op in value for op in _LIST_OPS)
            ):
                self.problems.append(f"{where}: {key} is not a list field")
            else:
                self.problems.append(f"{where}: unknown key '{key}'")
        self.out.entries[cid] = OverrideEntry(
            pin=pin,
            add=add,
            remove=remove,
            pending=pending,
            hidden=hidden,
            deleted=deleted,
            reason=reason,
            line=line,
        )

    def _check_pins(self, pin: dict[str, Any], where: str) -> None:
        for name, value in pin.items():
            if self.schema is not None and name == self.schema.id_field:
                self.problems.append(f"{where}: {name} can never be pinned")
            elif name not in self.fmt.pinnable:
                self.problems.append(
                    f"{where}: {name} cannot be pinned (pinnable: {', '.join(self.fmt.pinnable)})"
                )
            elif value is None and self.schema is not None and not self._clears(name):
                self.problems.append(f"{where}: {name} cannot be pinned empty")

    def _check_pending(self, pending: dict[str, Any], where: str) -> None:
        list_keys = {f"{f}{PENDING_ADD}" for f in self.fmt.list_fields}
        for key, value in pending.items():
            if key in list_keys:
                if not _is_strings(value):
                    self.problems.append(f"{where}: pending {key} values must be strings")
            elif key not in self.fmt.pinnable:
                self.problems.append(
                    f"{where}: pending key '{key}' is neither a pinnable field "
                    f"nor <list field>{PENDING_ADD}"
                )

    def _prefixes(self, seq: list) -> None:
        file = self.fmt.file
        seen: set[str] = set()
        for i, item in enumerate(seq):
            line = _item_line(seq, i)
            where = f"{file}:{line}"
            if not isinstance(item, dict):
                self.shape.append(f"{where}: each '{PREFIXES_KEY}' item must be a mapping")
                continue
            self._unknown(item, ("prefix", "reason"), where, "a hidden prefix")
            prefix, reason = _text(item.get("prefix")), _text(item.get("reason"))
            if not reason:
                self.problems.append(f"{where}: hidden prefix '{prefix}' needs a reason")
            if len(prefix) < _MIN_PREFIX:
                self.problems.append(
                    f"{where}: prefix '{prefix}' is shorter than {_MIN_PREFIX} characters"
                )
            if prefix in seen:
                self.problems.append(f"{where}: prefix '{prefix}' listed twice")
            seen.add(prefix)
            if prefix and prefix != BUILTIN_PREFIX:
                self.out.prefixes.append(HiddenPrefix(prefix, reason, line))


def _read(path: Path, fmt: OverridesFormat) -> tuple[str, Any]:
    """(text, round-trip data); a missing file is ("", None)."""
    if not path.exists():
        return "", None
    text = path.read_text()
    return text, load_text(text, fmt.file, round_trip=True)


def load_overrides(path: Path, fmt: OverridesFormat) -> Overrides:
    """The overrides; the built-in prefix is always first. A missing, empty or
    comment-only file has no entries. Raises `ValueError` naming the file and line on a
    file that is not the expected shape (`override_errors` reports the rest)."""
    walk = _Walk(fmt, None).run(_read(path, fmt)[1])
    if walk.shape:
        raise ValueError(walk.shape[0])
    return walk.out


def override_errors(path: Path, fmt: OverridesFormat, schema: RecordSchema) -> list[str]:
    """What a check fails on in the overrides file; empty when clean. Never raises."""
    try:
        data = _read(path, fmt)[1]
    except ValueError as e:
        return [str(e)]
    walk = _Walk(fmt, schema).run(data)
    return walk.shape + walk.problems


# --- applying --------------------------------------------------------------------


def _canonical_id(d: Any, schema: RecordSchema[Any, Any]) -> str:
    ids = schema.desired_value(d, schema.id_field)
    return ids[0] if ids else ""


def _spec(schema: RecordSchema[Any, Any], name: str) -> FieldSpec | None:
    return next((f for f in schema.fields if f.name == name), None)


def _is_list(f: FieldSpec) -> bool:
    return isinstance(f.policy, Union) or (isinstance(f.policy, Always) and f.policy.unordered)


def _list_key(schema: RecordSchema[Any, Any], name: str) -> Callable[[str], str]:
    """How a list field's values compare: a `Union` field's key, else exact."""
    spec = _spec(schema, name)
    if spec is not None and isinstance(spec.policy, Union):
        return spec.policy.key
    return str


def _gone(entry: OverrideEntry) -> bool:
    return entry.hidden is not None or entry.deleted is not None


def apply_overrides[D](
    rows: list[D],
    ov: Overrides,
    *,
    schema: RecordSchema[D, Any],
    pin: Callable[[D, dict[str, Any]], D] | None = None,
) -> list[D]:
    """The rows with their overrides applied, in order.

    A `hidden` or `deleted` row is dropped. A destination-owned row is otherwise left
    as it is. Every pin goes through one `pin(row, pins)` call (default
    `schema.with_values`). Each list field then gains its `add` values not already
    present and loses its `remove` values, both compared by the field's key. `pending`
    is never applied."""
    pin = pin or schema.with_values
    out: list[D] = []
    for d in rows:
        entry = ov.entries.get(_canonical_id(d, schema))
        if entry is None:
            out.append(d)
            continue
        if _gone(entry):
            continue
        if schema.destination_owned(d):
            out.append(d)
            continue
        if entry.pin:
            d = pin(d, dict(entry.pin))
        lists: dict[str, list[Any]] = {}
        for name in dict.fromkeys([*entry.add, *entry.remove]):
            key = _list_key(schema, name)
            values = list(schema.desired_value(d, name) or [])
            have = {key(v) for v in values}
            for v in entry.add.get(name, []):
                if key(v) not in have:
                    values.append(v)
                    have.add(key(v))
            drop = {key(v) for v in entry.remove.get(name, [])}
            lists[name] = [v for v in values if key(v) not in drop]
        if lists:
            d = schema.with_values(d, lists)
        out.append(d)
    return out


def held_rows(ov: Overrides) -> dict[str, str]:
    """Canonical id -> why a deploy must not write the row: a pending decision."""
    return {cid: f"pending decision in {ov.file}" for cid, e in ov.entries.items() if e.pending}


# --- checking --------------------------------------------------------------------


@dataclass(frozen=True)
class Pending:
    """One pending value. `field` is a scalar field or `<list field>_add`."""

    canonical_id: str
    field: str
    value: Any


@dataclass
class OverrideFindings:
    failures: list[str]
    reports: list[str]
    pending: list[Pending]  # each a failure the caller renders


def check_overrides[D](
    ov: Overrides,
    upstream: list[D],
    table: IdentityTable,
    *,
    schema: RecordSchema[D, Any],
    pin: Callable[[D, dict[str, Any]], D] | None = None,
) -> OverrideFindings:
    """The entries checked against the upstream rows (before overrides) and the identity
    table. `pin` is the callback `apply_overrides` uses; a pin is redundant when it
    alone leaves the field's desired value unchanged."""
    pin = pin or schema.with_values
    nouns = schema.nouns
    by_cid: dict[str, D] = {}
    for d in upstream:
        by_cid.setdefault(_canonical_id(d, schema), d)
    found = OverrideFindings([], [], [])
    for cid, e in ov.entries.items():
        found.pending += [Pending(cid, name, value) for name, value in e.pending.items()]
        d = by_cid.get(cid)
        if d is None and cid not in table.entries:
            found.failures.append(f"{cid}: no identity")
            continue
        if e.deleted is not None:
            found.reports.append(
                f"{cid}: deleted at {nouns.destination} "
                f"({e.deleted.live_id}, {e.deleted.confirmed}): not recreated"
            )
        if d is not None and schema.destination_owned(d):
            what = [
                *(["pin"] if e.pin else []),
                *(["pending"] if e.pending else []),
                *(f"{name}.add" for name in e.add),
                *(f"{name}.remove" for name in e.remove),
            ]
            found.failures += [
                f"{cid}: {w} does not apply to a {nouns.destination}-owned stub" for w in what
            ]
            continue
        for name, values in e.remove.items():
            key = _list_key(schema, name)
            upstream_values = schema.desired_value(d, name) if d is not None else None
            carried = {key(v) for v in upstream_values or []}
            found.failures += [
                f"{cid}: {name}.remove {v}: upstream does not carry it"
                for v in values
                if key(v) not in carried
            ]
        if d is not None:
            for name, value in e.pin.items():
                if schema.desired_value(pin(d, {name: value}), name) == schema.desired_value(
                    d, name
                ):
                    found.reports.append(f"{cid}: pin {name} equals upstream (redundant)")
    return found


# --- adopting --------------------------------------------------------------------


def adopt_entry[D, L](d: D, lv: L, *, schema: RecordSchema[D, L]) -> OverrideEntry | None:
    """The overrides that keep `lv`'s values a deploy of `d` would otherwise clear: a
    pin of the live value for each clearing scalar field `d` leaves empty, and an `add`
    of each live value of a clearing list field `d` lacks (by the field's key). None
    when there is nothing to keep."""
    pins: dict[str, Any] = {}
    adds: dict[str, list[str]] = {}
    for f in schema.fields:
        if not clears(f.policy):
            continue
        want, have = schema.desired_value(d, f.name), schema.live_value(lv, f.name)
        if _is_list(f):
            key = _list_key(schema, f.name)
            seen = {key(v) for v in want or []}
            missing: list[str] = []
            for v in have or []:
                if key(v) not in seen:
                    missing.append(v)
                    seen.add(key(v))
            if missing:
                adds[f.name] = missing
        elif is_empty(want) and not is_empty(have):
            pins[f.name] = have
    if not pins and not adds:
        return None
    return OverrideEntry(pin=pins, add=adds)


# --- writing -------------------------------------------------------------------


class _NullRepresenter(RoundTripRepresenter):
    """Writes None as `null`, so a pinned empty value reads as one."""


_NullRepresenter.add_representer(
    type(None),
    lambda rep, _data: rep.represent_scalar("tag:yaml.org,2002:null", "null"),
)


def _yaml() -> YAML:
    yml = round_trip_yaml()
    yml.Representer = _NullRepresenter
    return yml


def _flow_list(values: Iterable[str]) -> CommentedSeq:
    seq = CommentedSeq(values)
    seq.fa.set_flow_style()
    return seq


def _flow_map(**items: Any) -> CommentedMap:
    m = CommentedMap(items)
    m.fa.set_flow_style()
    return m


class OverridesStore:
    """The overrides file, loaded for editing. Reads see unsaved edits via `.overrides`.

    Each mutator returns whether it changed anything; nothing is written until `save`.
    Raises `ValueError` on a file `load_overrides` would reject, and on a field the
    format does not allow for the edit."""

    def __init__(self, path: Path, fmt: OverridesFormat) -> None:
        self.path = path
        self.fmt = fmt
        self._dirty = False
        self._prefix = ""  # comment-only file content, kept ahead of the first key
        text, loaded = _read(path, fmt)
        walk = _Walk(fmt, None).run(loaded)
        if walk.shape:
            raise ValueError(walk.shape[0])
        if loaded is None:
            self._data = CommentedMap()
            self._prefix = text if text.strip() else fmt.header
        else:
            self._data = loaded

    @property
    def overrides(self) -> Overrides:
        return _Walk(self.fmt, None).run(self._data).out

    # --- entry plumbing ----------------------------------------------------------

    def _records(self) -> CommentedMap:
        records = self._data.get(self.fmt.records_key)
        if records is None:
            records = self._data[self.fmt.records_key] = CommentedMap()
        return records

    def _find(self, cid: str) -> CommentedMap | None:
        records = self._data.get(self.fmt.records_key)
        return records.get(cid) if records is not None else None

    def _entry(self, cid: str) -> CommentedMap:
        records = self._records()
        entry = records.get(cid)
        if entry is None:
            entry = records[cid] = CommentedMap()
        return entry

    def _sub(self, cid: str, key: str) -> CommentedMap:
        entry = self._entry(cid)
        sub = entry.get(key)
        if sub is None:
            sub = entry[key] = CommentedMap()
        return sub

    def _prune(self, cid: str, *path: str) -> None:
        """Drop the empty containers along `path` inside the entry, then the entry if
        it is empty."""
        entry = self._find(cid)
        if entry is None:
            return
        chain: list[tuple[Any, str]] = []
        node: Any = entry
        for key in path:
            if not isinstance(node, dict) or key not in node:
                break
            chain.append((node, key))
            node = node[key]
        for parent, key in reversed(chain):
            child = parent[key]
            if child is None or (isinstance(child, (dict, list)) and not child):
                del parent[key]
        if not entry:
            del self._data[self.fmt.records_key][cid]

    def _changed(self) -> bool:
        self._dirty = True
        return True

    def _need_pinnable(self, name: str) -> None:
        if name not in self.fmt.pinnable:
            raise ValueError(f"{self.fmt.file}: {name} is not a pinnable field")

    def _need_list(self, name: str) -> None:
        if name not in self.fmt.list_fields:
            raise ValueError(f"{self.fmt.file}: {name} is not a list field")

    def _set_scalar(self, cid: str, key: str, name: str, value: Any) -> bool:
        entry = self._find(cid)
        current = entry.get(key) if entry is not None else None
        if current is not None and name in current and _plain(current[name]) == value:
            return False
        self._sub(cid, key)[name] = value
        return self._changed()

    def _move(self, cid: str, name: str, values: Iterable[str], into: str, out_of: str) -> bool:
        """Add `values` to the entry's `into` list for `name`, except a value already in
        its `out_of` list, which is dropped from there instead."""
        self._need_list(name)
        changed = False
        for value in values:
            entry = self._find(cid)
            ops = entry.get(name) if entry is not None else None
            other = ops.get(out_of) if ops is not None else None
            if other is not None and value in other:
                other.remove(value)
                self._prune(cid, name, out_of)
                changed = True
                continue
            mine = ops.get(into) if ops is not None else None
            if mine is not None and value in mine:
                continue
            ops = self._sub(cid, name)
            if ops.get(into) is None:
                ops[into] = _flow_list([])
            ops[into].append(value)
            changed = True
        return self._changed() if changed else False

    # --- mutators ------------------------------------------------------------------

    def pin(self, cid: str, field: str, value: Any) -> bool:
        self._need_pinnable(field)
        return self._set_scalar(cid, "pin", field, value)

    def add_values(self, cid: str, field: str, values: Iterable[str]) -> bool:
        return self._move(cid, field, values, "add", "remove")

    def remove_values(self, cid: str, field: str, values: Iterable[str]) -> bool:
        return self._move(cid, field, values, "remove", "add")

    def set_pending(self, cid: str, field: str, value: Any) -> bool:
        self._need_pinnable(field)
        return self._set_scalar(cid, "pending", field, value)

    def add_pending(self, cid: str, field: str, values: Iterable[str]) -> bool:
        self._need_list(field)
        key = f"{field}{PENDING_ADD}"
        changed = False
        for value in values:
            entry = self._find(cid)
            pending = entry.get("pending") if entry is not None else None
            have = pending.get(key) if pending is not None else None
            if have is not None and value in have:
                continue
            pending = self._sub(cid, "pending")
            if pending.get(key) is None:
                pending[key] = _flow_list([])
            pending[key].append(value)
            changed = True
        return self._changed() if changed else False

    def drop_pending(self, cid: str, field: str) -> bool:
        if field in self.fmt.list_fields:
            key = f"{field}{PENDING_ADD}"
        else:
            self._need_pinnable(field)
            key = field
        entry = self._find(cid)
        pending = entry.get("pending") if entry is not None else None
        if pending is None or key not in pending:
            return False
        del pending[key]
        self._prune(cid, "pending")
        return self._changed()

    def drop_pending_values(self, cid: str, field: str, values: Iterable[str]) -> bool:
        """Drop each of `values` from the list field's pending adds, keeping the rest;
        the pending key goes once it is empty."""
        self._need_list(field)
        key = f"{field}{PENDING_ADD}"
        entry = self._find(cid)
        pending = entry.get("pending") if entry is not None else None
        have = pending.get(key) if pending is not None else None
        if have is None:
            return False
        drop = [v for v in values if v in have]
        if not drop:
            return False
        for value in drop:
            have.remove(value)
        self._prune(cid, "pending", key)
        return self._changed()

    def hide(self, cid: str, reason: str) -> bool:
        reason = reason.strip()
        if not reason:
            raise ValueError(f"{self.fmt.file}: hidden needs a reason")
        entry = self._find(cid)
        if entry is not None and "hidden" in entry:
            return False
        self._entry(cid)["hidden"] = _flow_map(reason=reason)
        return self._changed()

    def mark_deleted(self, cid: str, live_id: str, confirmed: str) -> bool:
        live_id = live_id.strip()
        if not live_id:
            raise ValueError(f"{self.fmt.file}: deleted needs {self.fmt.live_id_key}")
        day = datetime.date.fromisoformat(confirmed)  # ValueError on a malformed date
        entry = self._find(cid)
        if entry is not None and self.overrides.entries[cid].deleted == Deleted(
            live_id, day.isoformat()
        ):
            return False
        self._entry(cid)["deleted"] = _flow_map(**{self.fmt.live_id_key: live_id, "confirmed": day})
        return self._changed()

    def adopt(self, cid: str, entry: OverrideEntry) -> bool:
        """Merge `entry`'s pins and adds into `cid`'s entry. An existing pin, even an
        empty one, is kept."""
        current = self.overrides.entries.get(cid)
        pinned = current.pin if current is not None else {}
        changed = False
        for name, value in entry.pin.items():
            if name not in pinned:
                changed = self.pin(cid, name, value) or changed
        for name, values in entry.add.items():
            changed = self.add_values(cid, name, values) or changed
        return changed

    def add_hidden_prefix(self, prefix: str, reason: str) -> bool:
        prefix, reason = prefix.strip(), reason.strip()
        if len(prefix) < _MIN_PREFIX:
            raise ValueError(
                f"{self.fmt.file}: prefix '{prefix}' is shorter than {_MIN_PREFIX} characters"
            )
        if not reason:
            raise ValueError(f"{self.fmt.file}: hidden prefix '{prefix}' needs a reason")
        if prefix in {p.prefix for p in self.overrides.prefixes}:
            return False
        seq = self._data.get(PREFIXES_KEY)
        if seq is None:
            seq = self._data[PREFIXES_KEY] = CommentedSeq()
        seq.append(_flow_map(prefix=DoubleQuotedScalarString(prefix), reason=reason))
        return self._changed()

    # --- write -----------------------------------------------------------------------

    def save(self) -> bool:
        """Write the file if anything changed. Returns whether it wrote."""
        if not self._dirty:
            return False
        buf = StringIO()
        buf.write(self._prefix)
        if self._data:
            _yaml().dump(self._data, buf)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(buf.getvalue())
        os.replace(tmp, self.path)
        self._dirty = False
        return True
