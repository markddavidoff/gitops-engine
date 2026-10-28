"""Deploy: plan desired rows against live, then apply the plan under per-row
optimistic concurrency.

`plan_deploy` pairs desired rows with live rows by identity (`pairing.pair_rows`) and
runs the field planner (`fieldplan.plan_row`) on each pair, sorting rows into creates,
updates, aborts, orphans and unchanged. `apply_deploy` performs creates and updates
only: it snapshots the live rows first, re-reads each row at write time and aborts on
drift (check-and-set) rather than clobber it, sends only the planned fields, refuses
an empty value outside a retire, and verifies each written row. Orphans are reported,
never deleted.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .destination import Destination, DestinationError, is_empty, refused_empty
from .fieldplan import DESTRUCTIVE, FieldChange, plan_row, unacknowledged_destructive
from .identity import Identity, IdentitySpec, IdentityTable
from .ignore import IgnoreList
from .pairing import pair_rows
from .retire import RetireEntry
from .schema import Always, FieldSpec, RecordSchema, Union
from .snapshot import record_written, write_snapshot

NOT_ATTEMPTED = "not attempted: apply stopped at an earlier failure"


@dataclass
class DeployPlan[D, L]:
    creates: list[D] = field(default_factory=list)
    updates: list[tuple[D, L]] = field(default_factory=list)  # (target, live)
    aborts: list[tuple[D, L, str]] = field(default_factory=list)
    orphans: list[L] = field(default_factory=list)
    unchanged: list[D] = field(default_factory=list)
    # per paired (non-aborted) live id: the field changes plan_row computed
    changes: dict[str, list[FieldChange]] = field(default_factory=dict)
    # one message per destructive change not backed by a retire entry
    unacked: list[str] = field(default_factory=list)
    # planner failures: a check fails and a deploy refuses on any
    errors: list[str] = field(default_factory=list)
    # (canonical id, live row) for each extra live row on one identity: never written
    known_duplicates: list[tuple[str, L]] = field(default_factory=list)
    # one message per exclusive value not written because another live row holds it
    withheld: list[str] = field(default_factory=list)
    # report-only lines (a destination-owned row with no live row)
    notes: list[str] = field(default_factory=list)


def _exclusive(schema: RecordSchema[Any, Any]) -> list[tuple[FieldSpec, Union]]:
    return [
        (f, f.policy) for f in schema.fields if isinstance(f.policy, Union) and f.policy.exclusive
    ]


def _withhold_shared[D, L](
    d: D,
    lv: L | None,
    holders: list[L],
    plan: DeployPlan[D, L],
    schema: RecordSchema[D, L],
) -> D:
    """`d` without the exclusive values another live row holds (shared values are
    unverified). A value the paired row already holds is kept: writing it changes
    nothing."""
    nouns = schema.nouns
    paired_id = schema.live_id(lv) if lv is not None else None
    for f, policy in _exclusive(schema):
        key = policy.key
        values = list(schema.desired_value(d, f.name) or [])
        own = {key(x) for x in schema.live_value(lv, f.name)} if lv is not None else set()
        keep: list[Any] = []
        for v in values:
            k = key(v)
            other = (
                None
                if k in own
                else next(
                    (
                        h
                        for h in holders
                        if (paired_id is None or schema.live_id(h) != paired_id)
                        and k in {key(x) for x in schema.live_value(h, f.name)}
                    ),
                    None,
                )
            )
            if other is None:
                keep.append(v)
                continue
            plan.withheld.append(
                f"withheld {policy.item} '{v}' for '{schema.label(d)}': {nouns.destination} "
                f"{nouns.record} '{schema.live_label(other)}' already holds it "
                f"(shared {f.name} are unverified)"
            )
        if keep != values:
            d = schema.with_values(d, {f.name: keep})
    return d


def plan_deploy[D, L](
    desired: list[D],
    live: list[L],
    table: IdentityTable | None = None,
    *,
    schema: RecordSchema[D, L],
    spec: IdentitySpec,
    implicit: Callable[[D, set[str]], Identity],
    state: list[D] | None = None,
    retire: dict[str, RetireEntry] | None = None,
    other_live: list[L] | None = None,
) -> DeployPlan[D, L]:
    """Diff desired against live, paired by identity.

    The optimistic-concurrency pre-state comes from the `state` mirror, matched to the
    desired row by its ids, falling back to the live row's id. A live row whose version
    moved but whose content hash did not (a metadata-only edit) is not drift. A
    destination-owned row is never drift.

    `updates` holds `(target, live)`, where target is the effective row to write.
    `retire` is keyed by the desired row's first id. `other_live` are live rows the
    plan never writes whose exclusive values still count as held."""
    nouns = schema.nouns
    plan: DeployPlan[D, L] = DeployPlan()
    state_by_id: dict[str, D] = {}
    state_by_live: dict[str, D] = {}
    for s in state or []:
        for x in schema.desired_value(s, schema.id_field) or []:
            state_by_id.setdefault(x, s)
        mark = schema.mark(s)
        if mark is not None:
            state_by_live.setdefault(mark.live_id, s)

    pairing = pair_rows(
        desired, live, table or IdentityTable(), schema=schema, spec=spec, implicit=implicit
    )
    plan.errors = list(pairing.errors)
    plan.known_duplicates = list(pairing.duplicates)
    plan.orphans = list(pairing.unmatched)
    holders = [*live, *(other_live or [])]
    for d, lv in pairing.pairs:
        owned = schema.destination_owned(d)
        if lv is None:
            if owned:
                plan.notes.append(
                    f"stub '{schema.label(d)}' has no {nouns.destination} {nouns.record}: "
                    "run the refresh"
                )
                continue
            plan.creates.append(_withhold_shared(d, None, holders, plan, schema))
            continue
        ids = schema.desired_value(d, schema.id_field) or []
        st = next((state_by_id[x] for x in ids if x in state_by_id), None)
        if st is None:
            st = state_by_live.get(schema.live_id(lv))
        mark = schema.mark(st) if st is not None else None
        version = schema.live_version(lv)
        # a destination-owned row: an edit to it at the destination is not drift
        if (
            mark is not None
            and not owned
            and version != mark.version
            and schema.live_hash(lv) != mark.content_hash
        ):
            plan.aborts.append(
                (
                    d,
                    lv,
                    (
                        f"drift: live {nouns.version} {version} != pre-state {mark.version} "
                        "(content changed)"
                    ),
                )
            )
            continue
        if not owned:
            d = _withhold_shared(d, lv, holders, plan, schema)
        entry = (retire or {}).get(ids[0]) if ids else None
        target, changes = plan_row(d, lv, entry, schema=schema)
        plan.changes[schema.live_id(lv)] = changes
        plan.unacked += [
            f"{schema.label(d)}: {c.field} {c.kind} without retire"
            for c in unacknowledged_destructive(changes)
        ]
        if not changes:
            plan.unchanged.append(target)
            continue
        plan.updates.append((target, lv))
    return plan


@dataclass
class ApplyResult[D]:
    persisted: list[D]  # state-mirror rows built from what the destination returned
    snapshot: Path
    created: list[str]  # live ids
    failure: str | None = None


def _all_values[D](schema: RecordSchema[D, Any], d: D) -> dict[str, Any]:
    return {f.name: schema.desired_value(d, f.name) for f in schema.fields}


def _planned_values[D](
    schema: RecordSchema[D, Any], target: D, changes: list[FieldChange]
) -> dict[str, Any]:
    """The target's value for exactly the fields `changes` names, in change order."""
    out: dict[str, Any] = {}
    for c in changes:
        value = schema.desired_value(target, c.field)
        out[c.field] = list(value) if isinstance(value, list) else value
    return out


def _equal(f: FieldSpec, want: Any, have: Any) -> bool:
    policy = f.policy
    if isinstance(policy, Union):
        return sorted(policy.key(x) for x in want) == sorted(policy.key(x) for x in have)
    if isinstance(policy, Always) and policy.unordered:
        return sorted(want) == sorted(have)
    return want == have


def _mismatch[D, L](
    schema: RecordSchema[D, L],
    target: D,
    got: L,
    before: L,
    changes: list[FieldChange],
) -> str | None:
    """The first field, in schema order, where the returned row differs from the
    target under the field's policy.

    A `required` field with no planned change expects its value from before the write:
    the planner may fill it from the desired row (a destination-owned row whose live
    value is empty) without writing it. Every other field's target already equals live
    when it is not planned, so it expects the target."""
    planned = {c.field for c in changes}
    for f in schema.fields:
        want = schema.desired_value(target, f.name)
        if f.required and f.name not in planned:
            want = schema.live_value(before, f.name)
        have = schema.live_value(got, f.name)
        if not _equal(f, want, have):
            return f"{schema.label(target)}: {f.name} expected {want} got {have}"
    return None


def apply_deploy[D, L](
    plan: DeployPlan[D, L],
    dest: Destination[L],
    *,
    schema: RecordSchema[D, L],
    snapshot_dir: Path,
    ignore: IgnoreList | None = None,
    scope: str | None = None,
) -> ApplyResult[D]:
    """Execute creates and updates.

    Order: refuse a plan with errors or unacknowledged destructive changes ->
    `dest.preflight` -> snapshot of every live row in `plan.updates` (an `OSError`
    propagates before any write) -> creates (non-empty values only) -> per update:
    check-and-set re-read, ignore match on the current row, write-time drift abort,
    the never-null guard, `dest.update` with only the planned fields (`retire=True`
    when a planned change is destructive), record the written version in the
    snapshot, then verify the returned row.

    A destination error, a guard refusal or a mismatch sets `failure` and stops: the
    later updates are recorded in `plan.aborts` as not attempted. A row the current
    `ignore` list matches (default: the built-in prefix) is aborted, never written."""
    if plan.errors:
        raise ValueError(f"planner error(s), nothing written: {plan.errors}")
    if plan.unacked or any(unacknowledged_destructive(cs) for cs in plan.changes.values()):
        raise ValueError(f"unacknowledged destructive change(s), nothing written: {plan.unacked}")
    create_values = [_all_values(schema, d) for d in plan.creates]
    update_values = [
        _planned_values(schema, d, plan.changes.get(schema.live_id(lv), []))
        for d, lv in plan.updates
    ]
    dest.preflight([*create_values, *update_values])

    ignore = ignore if ignore is not None else IgnoreList("")
    snapshot = write_snapshot([lv for _, lv in plan.updates], snapshot_dir, schema=schema)
    result: ApplyResult[D] = ApplyResult(persisted=[], snapshot=snapshot, created=[])

    for d, values in zip(plan.creates, create_values, strict=True):
        try:
            lv = dest.create({k: v for k, v in values.items() if not is_empty(v)})
        except DestinationError as e:
            result.failure = f"{schema.label(d)}: create failed: {e}"
            break
        result.created.append(schema.live_id(lv))
        result.persisted.append(schema.mirror_row(lv))

    for i, ((d, lv0), values) in enumerate(zip(plan.updates, update_values, strict=True)):
        if result.failure is not None:
            plan.aborts += [(t, lv, NOT_ATTEMPTED) for t, lv in plan.updates[i:]]
            break
        live_id = schema.live_id(lv0)
        changes = plan.changes.get(live_id, [])
        if not changes:
            continue
        cur = dest.get(live_id)  # write-time re-read (check-and-set)
        if cur is None:
            plan.aborts.append((d, lv0, "gone at write time"))
            continue
        ignored = ignore.match(schema.live_id(cur), schema.live_label(cur), scope=scope)
        if ignored is not None:
            plan.aborts.append((d, lv0, f"ignored at write time: {ignored.reason}"))
            continue
        # Guard every update, first-link rows included: if the row changed since plan
        # time, abort rather than clobber a concurrent edit.
        now, then = schema.live_version(cur), schema.live_version(lv0)
        if now != then:
            plan.aborts.append((d, lv0, f"write-time drift {now} != {then}"))
            continue
        retire = any(c.kind in DESTRUCTIVE for c in changes)
        empty = refused_empty(values, retire=retire)
        if empty is not None:
            result.failure = f"{schema.label(d)}: update refused: empty {empty} without retire"
            continue
        try:
            lv = dest.update(schema.live_id(cur), values, retire=retire)
        except DestinationError as e:
            result.failure = f"{schema.label(d)}: update failed: {e}"
            continue
        result.persisted.append(schema.mirror_row(lv))
        try:
            record_written(snapshot, schema.live_id(lv), schema.live_version(lv), schema=schema)
        except OSError as e:
            result.failure = (
                f"{schema.label(d)}: written, but recording it in {snapshot} failed: {e}"
            )
            continue
        result.failure = _mismatch(schema, d, lv, cur, changes)

    return result
