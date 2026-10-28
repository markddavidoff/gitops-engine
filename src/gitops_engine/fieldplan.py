"""The field planner: per-field ownership policies for one paired record.

`plan_row` turns a desired row and its live pair into the effective target row plus
a list of `FieldChange`s, one field at a time in `schema.fields` order. Each field
follows the policy the schema declares for it:

- `Always`: desired replaces live. With `unordered`, values compare sorted and any
  difference is one `change` of the whole list.
- `Union`: the desired values unioned with live, minus retired ones. Values compare
  through `Union.key`, and a value live already holds keeps its live spelling. A
  live-only value is dropped only when a retire entry names it.
- `KeepIfUnset`: desired wins when it has a value, else live is kept. A retire entry
  whose value equals live clears it.

A destination-owned row takes every value from live and can change only its
`id_field`; a `required` field whose live value is empty takes the desired value.
Retire entries never apply to it.

`remove` and `clear` are destructive. The planner emits them only from a retire entry
and marks them `retire=True`; `unacknowledged_destructive` finds any that are not,
which callers treat as a planner bug.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from .identity import Identity, IdentitySpec, IdentityTable
from .pairing import pair_rows
from .retire import RetireEntry
from .schema import Always, FieldSpec, KeepIfUnset, RecordSchema, Union

ChangeKind = Literal["set", "change", "add", "remove", "clear"]
DESTRUCTIVE: frozenset[str] = frozenset({"remove", "clear"})


@dataclass(frozen=True)
class FieldChange:
    field: str
    old: Any
    new: Any
    kind: ChangeKind
    retire: bool = False


@dataclass(frozen=True)
class Untracked:
    """A live value the desired row does not carry and no retire entry names."""

    external_id: str
    name: str
    field: str
    value: Any


def _empty(value: Any) -> bool:
    return value is None or value == "" or value == []


def _copy(value: Any) -> Any:
    return list(value) if isinstance(value, list) else value


def _plain(name: str, old: Any, new: Any) -> list[FieldChange]:
    """An owned scalar: written when it differs from live."""
    if old == new:
        return []
    return [FieldChange(name, old, new, "set" if _empty(old) else "change")]


def _unordered(name: str, old: list[Any], new: list[Any]) -> list[FieldChange]:
    """An owned list compared as a set of values: any difference is one `change`."""
    if sorted(old) == sorted(new):
        return []
    return [FieldChange(name, list(old), list(new), "change")]


def _union(
    name: str,
    desired: list[str],
    live: list[str],
    retired: list[str],
    key: Callable[[str], str],
) -> tuple[list[str], list[FieldChange]]:
    live_by_key = {key(v): v for v in live}
    retired_keys = {key(v) for v in retired}
    target: list[str] = []
    seen: set[str] = set()
    changes: list[FieldChange] = []
    for v in desired:
        k = key(v)
        if k in seen or k in retired_keys:
            continue
        seen.add(k)
        if k in live_by_key:
            target.append(live_by_key[k])  # keep the live spelling: not a change
        else:
            target.append(v)
            changes.append(FieldChange(name, None, v, "add"))
    for v in live:
        k = key(v)
        if k in seen:
            continue
        seen.add(k)
        if k in retired_keys:
            changes.append(FieldChange(name, v, None, "remove", retire=True))
        else:
            target.append(v)
    return target, changes


def _keep_if_unset(
    name: str, desired: Any, live: Any, retired: Any
) -> tuple[Any, list[FieldChange]]:
    """Retire (exact match) clears; else desired wins; else live is kept."""
    if retired is not None and retired == live:
        return None, [FieldChange(name, live, None, "clear", retire=True)]
    if desired is None:
        return live, []
    return desired, _plain(name, live, desired)


def _plan_field(
    f: FieldSpec, desired: Any, live: Any, retired: Any
) -> tuple[Any, list[FieldChange]]:
    policy = f.policy
    if isinstance(policy, Always):
        if policy.unordered:
            return list(desired), _unordered(f.name, live, desired)
        return desired, _plain(f.name, live, desired)
    if isinstance(policy, Union):
        return _union(f.name, desired, live, retired or [], policy.key)
    assert isinstance(policy, KeepIfUnset)
    return _keep_if_unset(f.name, desired, live, retired)


def plan_row[D, L](
    d: D, lv: L, retire: RetireEntry | None, *, schema: RecordSchema[D, L]
) -> tuple[D, list[FieldChange]]:
    """The effective target row for `d` against its live pair, and its changes.

    `retire` is the retire entry for `d`, if any; a field it does not name retires
    nothing."""
    owned = schema.destination_owned(d)
    entry = retire or {}
    values: dict[str, Any] = {}
    changes: list[FieldChange] = []
    for f in schema.fields:
        want = schema.desired_value(d, f.name)
        have = schema.live_value(lv, f.name)
        if owned and f.name != schema.id_field:
            values[f.name] = want if f.required and _empty(have) else _copy(have)
            continue
        target, field_changes = _plan_field(f, want, have, None if owned else entry.get(f.name))
        values[f.name] = target
        changes += field_changes
    return schema.with_values(d, values), changes


def unacknowledged_destructive(changes: list[FieldChange]) -> list[FieldChange]:
    """Destructive changes not backed by a retire entry (a planner bug)."""
    return [c for c in changes if c.kind in DESTRUCTIVE and not c.retire]


def _canonical_id[D](d: D, schema: RecordSchema[D, Any]) -> str:
    ids = schema.desired_value(d, schema.id_field)
    return ids[0] if ids else ""


def _paired[D, L](
    desired: list[D],
    live: list[L],
    table: IdentityTable | None,
    schema: RecordSchema[D, L],
    spec: IdentitySpec,
    implicit: Callable[[D, set[str]], Identity],
) -> list[tuple[D, L | None]]:
    return pair_rows(
        desired, live, table or IdentityTable(), schema=schema, spec=spec, implicit=implicit
    ).pairs


def untracked_values[D, L](
    desired: list[D],
    live: list[L],
    table: IdentityTable | None,
    retire: dict[str, RetireEntry] | None,
    *,
    schema: RecordSchema[D, L],
    spec: IdentitySpec,
    implicit: Callable[[D, set[str]], Identity],
) -> list[Untracked]:
    """Live values on paired rows that the desired row does not carry.

    A live `Union` value absent from the desired row (compared through its key), or a
    live `KeepIfUnset` value where the desired row has none, is untracked unless a
    retire entry names it. Unpaired live rows and destination-owned rows are skipped."""
    out: list[Untracked] = []
    for d, lv in _paired(desired, live, table, schema, spec, implicit):
        if lv is None or schema.destination_owned(d):
            continue
        xid = _canonical_id(d, schema)
        entry = (retire or {}).get(xid) or {}
        label = schema.label(d)
        for f in schema.fields:
            want = schema.desired_value(d, f.name)
            have = schema.live_value(lv, f.name)
            if isinstance(f.policy, Union):
                key = f.policy.key
                carried = {key(v) for v in want}
                gone = {key(v) for v in entry.get(f.name) or []}
                seen: set[str] = set()
                for v in have:
                    k = key(v)
                    if k in carried or k in gone or k in seen:
                        continue
                    seen.add(k)
                    out.append(Untracked(xid, label, f.name, v))
            elif isinstance(f.policy, KeepIfUnset):
                if want is None and have is not None and entry.get(f.name) != have:
                    out.append(Untracked(xid, label, f.name, have))
    return out


def stale_retire[D, L](
    desired: list[D],
    live: list[L],
    table: IdentityTable | None,
    retire: dict[str, RetireEntry] | None,
    *,
    schema: RecordSchema[D, L],
    spec: IdentitySpec,
    implicit: Callable[[D, set[str]], Identity],
) -> list[str]:
    """One message per retire entry value that cannot do its job.

    An entry is stale when its canonical id has no desired row, the row is
    destination-owned (retire never applies to one), the row has no live pair, or the
    retired value is not live. It also conflicts when the desired row still carries
    the value: it would be cleared, then set again on alternate runs. A `KeepIfUnset`
    field is the desired row's whenever it sets one (even a different value), so any
    desired value conflicts."""
    if not retire:
        return []
    live_for = {id(d): lv for d, lv in _paired(desired, live, table, schema, spec, implicit)}
    by_xid = {
        _canonical_id(d, schema): d for d in desired if schema.desired_value(d, schema.id_field)
    }
    destination = schema.nouns.destination
    msgs: list[str] = []
    for xid, entry in retire.items():
        d = by_xid.get(xid)
        if d is None:
            msgs.append(f"{xid}: no registry row")
            continue
        if schema.destination_owned(d):
            msgs.append(f"{xid}: {destination}-owned stub (retire does not apply)")
            continue
        lv = live_for.get(id(d))
        if lv is None:
            msgs.append(f"{xid}: no live match")
            continue
        for f in schema.fields:
            want = schema.desired_value(d, f.name)
            have = schema.live_value(lv, f.name)
            if isinstance(f.policy, Union):
                key = f.policy.key
                live_keys = {key(v) for v in have}
                want_keys = {key(v) for v in want}
                for v in entry.get(f.name) or []:
                    if key(v) not in live_keys:
                        msgs.append(f"{xid}: {f.name} {v} not live")
                    if key(v) in want_keys:
                        msgs.append(f"{xid}: {f.name} {v} conflicts with registry")
            elif isinstance(f.policy, KeepIfUnset):
                retired = entry.get(f.name)
                if retired is None:
                    continue
                if retired != have:
                    msgs.append(f"{xid}: {f.name} {retired} not live")
                if want is not None:
                    msgs.append(f"{xid}: {f.name} {retired} conflicts with registry")
    return msgs
