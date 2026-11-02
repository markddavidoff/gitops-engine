"""Destination edits: what changed at the destination since the last mirror, and how
the overrides file records it.

The old state mirror is the live state the last refresh recorded. `pair_old_state`
pairs each old state row with its desired row and its current live row, and
`detect_edits` compares old against live field by field. A field that differs was
edited at the destination; when its live value also differs from desired, the edit
needs recording:

- an optional value cleared, on a field whose policy clears: `clear` (a pin to empty);
- a list value removed that desired still carries, on a clearing list: `remove`;
- any other change, or an added list value: `pending`, with the destination's value;
- a field that cannot clear, cleared: nothing, so the next deploy writes it back.

A live value equal to desired is never an edit: it is the deploy's own write seen
against a mirror that has not caught up. `record_edits` writes edits into an
`OverridesStore`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from .overrides import OverridesStore, _canonical_id, _is_list, _list_key
from .schema import FieldSpec, KeepIfUnset, RecordSchema, Union, clears

EditKind = Literal["clear", "remove", "pending"]


@dataclass(frozen=True)
class Edit:
    """One destination edit to record. `field` is the field's own name, a list field's
    included (`tags`, never its pending key). `value` is `None` for a `clear`, one list
    value for a `remove` or a pending add, and the live value for a pending scalar."""

    canonical_id: str
    field: str
    kind: EditKind
    value: Any


@dataclass(frozen=True)
class OldPair[D, L]:
    """An old state row, its desired row, and the live row its mark names (`None` when
    that live row is gone)."""

    old: D
    desired: D
    live_id: str
    live: L | None


def pair_old_state[D, L](
    old_state: list[D], live: list[L], desired: list[D], *, schema: RecordSchema[D, L]
) -> list[OldPair[D, L]]:
    """Each old state row with the desired row whose canonical id is among its id
    values and the live row of its mark's live id, in `old_state` order. Rows without a
    mark or a desired row, and rows whose desired row is destination-owned, are dropped."""
    by_cid: dict[str, D] = {}
    for d in desired:
        cid = _canonical_id(d, schema)
        if cid:
            by_cid.setdefault(cid, d)
    live_by_id = {schema.live_id(lv): lv for lv in live}
    out: list[OldPair[D, L]] = []
    for s in old_state:
        mark = schema.mark(s)
        if mark is None:
            continue
        ids = schema.desired_value(s, schema.id_field) or []
        d = next((by_cid[i] for i in ids if i in by_cid), None)
        if d is None or schema.destination_owned(d):
            continue
        out.append(OldPair(s, d, mark.live_id, live_by_id.get(mark.live_id)))
    return out


def _detected(f: FieldSpec, id_field: str) -> bool:
    """Whether destination edits of the field are recorded at all."""
    if f.name == id_field or isinstance(f.policy, KeepIfUnset):
        return False
    return not (isinstance(f.policy, Union) and not f.policy.clears)


def _empty(value: Any) -> bool:
    return value is None or value == "" or value == []


def _scalar_edit(cid: str, f: FieldSpec, old: Any, live: Any, desired: Any) -> Edit | None:
    old, live, desired = (None if _empty(v) else v for v in (old, live, desired))
    if old == live or live == desired:
        return None
    if live is None:
        return Edit(cid, f.name, "clear", None) if clears(f.policy) else None
    return Edit(cid, f.name, "pending", live)


def _list_edits(
    cid: str, f: FieldSpec, key: Callable[[str], str], old: Any, live: Any, desired: Any
) -> list[Edit]:
    def keyed(values: Any) -> dict[str, str]:
        out: dict[str, str] = {}
        for v in values or []:
            out.setdefault(key(v), v)
        return out

    o, lv, d = keyed(old), keyed(live), keyed(desired)
    # A removal is recorded in desired's spelling, so a value that came from the
    # entry's own `add` list is dropped from there.
    out = [
        Edit(cid, f.name, "remove", d[k]) for k in o if k not in lv and k in d and clears(f.policy)
    ]
    out += [Edit(cid, f.name, "pending", v) for k, v in lv.items() if k not in o and k not in d]
    return out


def detect_edits[D, L](
    old_state: list[D],
    live: list[L],
    desired: list[D],
    *,
    schema: RecordSchema[D, L],
    base: Callable[[str, Any], Any] | None = None,
) -> list[Edit]:
    """The destination edits to record, ordered by canonical id, then field order, then
    value.

    Values compare through `base(field, value)` (identity by default), which strips
    what the destination adds to a value, such as a suffix the deploy writes; a pending
    scalar records the based live value. List values compare by the field's item key,
    so a value re-cased at the destination is not an edit. The id field, `KeepIfUnset`
    fields and `Union` fields that do not clear are not recorded."""

    def based(name: str, value: Any) -> Any:
        return base(name, value) if base is not None else value

    order = {f.name: i for i, f in enumerate(schema.fields)}
    fields = [f for f in schema.fields if _detected(f, schema.id_field)]
    edits: list[Edit] = []
    for pair in pair_old_state(old_state, live, desired, schema=schema):
        if pair.live is None:
            continue
        cid = _canonical_id(pair.desired, schema)
        for f in fields:
            o = based(f.name, schema.desired_value(pair.old, f.name))
            lv = based(f.name, schema.live_value(pair.live, f.name))
            d = based(f.name, schema.desired_value(pair.desired, f.name))
            if _is_list(f):
                found = _list_edits(cid, f, _list_key(schema, f.name), o, lv, d)
            else:
                found = [e for e in [_scalar_edit(cid, f, o, lv, d)] if e is not None]
            edits += [e for e in found if e not in edits]  # two old rows may share a cid
    return sorted(edits, key=lambda e: (e.canonical_id, order[e.field], str(e.value)))


def record_edits(store: OverridesStore, edits: list[Edit]) -> int:
    """Record each edit in `store`; returns how many changed it.

    A `clear` pins the field empty and drops its pending value. A `remove` adds the
    value to the field's `remove` list, or drops it from `add` when it came from there.
    A `pending` edit sets the field's pending value, or adds a list value to its
    pending adds."""
    changed = 0
    for e in edits:
        if e.kind == "clear":
            pinned = store.pin(e.canonical_id, e.field, None)
            dropped = store.drop_pending(e.canonical_id, e.field)
            hit = pinned or dropped
        elif e.kind == "remove":
            hit = store.remove_values(e.canonical_id, e.field, [e.value])
        elif e.field in store.fmt.list_fields:
            hit = store.add_pending(e.canonical_id, e.field, [e.value])
        else:
            hit = store.set_pending(e.canonical_id, e.field, e.value)
        changed += hit
    return changed
