"""`run_check`: the plan guard a CI job keys off, and the offline integrity checks.

A check splits live rows once into managed and ignored (`ignore.partition_live`);
drift, the plan, untracked values and stale retire entries see only the managed set.
It fails (`EXIT_DRIFT`) on:

- drift between the state mirror and live (`drift.detect_drift`); an edit at the
  destination to a destination-owned row is not drift;
- a plan-time abort, a planner error or an unacknowledged destructive change
  (`deploy.plan_deploy`, the planner `deploy` runs);
- a retire entry that can no longer do its job (`fieldplan.stale_retire`);
- an ignored row that resolves to a desired row's identity (`ignore.ignore_conflicts`);
- a live row whose keys reach two identities;
- with the dataset records supplied, an orphan: a managed live row that no record
  resolves to.

It only reports untracked values, known duplicates, withheld values, stale ignore
entries, unused identities and records with no identity. A scoped run sees only its
own rows, so it skips the stale-ignore and unused-identity reports, where every
out-of-scope row would look stale or unused.

The offline checks need no destination access: `manifest_errors` (generated files
edited by hand) and `identity_offline_errors` (identity problems that change
generated output).
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .datasets import DATASETS_SUBDIR, DatasetRecord
from .deploy import DeployPlan, plan_deploy
from .drift import DriftReport, detect_drift
from .exitcodes import EXIT_DRIFT, EXIT_OK
from .fieldplan import Untracked, stale_retire, untracked_values
from .identity import (
    Identity,
    IdentitySpec,
    IdentityTable,
    ItemKeys,
    conflict,
    resolve,
    table_errors,
    with_implicit,
)
from .ignore import IgnoreList, ignore_conflicts, partition_live, stale_ignore
from .manifest import manifest_edits
from .merge import group_by_identity
from .pairing import pair_rows
from .retire import RetireEntry
from .schema import RecordSchema


@dataclass(frozen=True)
class DatasetHooks[R]:
    """How a check reads the dataset records behind the desired rows.

    - `keys`: what a record offers to `resolve`.
    - `record_id`: the identity id of a record that resolves to none.
    - `implicit(entry, taken)`: the identity of a record that resolves to none; `taken`
      holds the canonical ids already in use.
    - `live_id`: the destination id a record holds, if any.
    - `found_dataset`: the dataset of destination-only stubs.
    - `held_key`: the match-key field whose values an identity lists but nothing holds
      any more are reported (`unused_identities`).
    - `unidentified_hint`: what to do about a record with no identity, as the report
      line ends with it.
    - `describe`: names a desired row in an ignore-conflict message (default: its label).
    """

    keys: Callable[[DatasetRecord[R]], ItemKeys]
    record_id: Callable[[DatasetRecord[R]], str]
    implicit: Callable[[DatasetRecord[R], set[str]], Identity]
    live_id: Callable[[R], str | None]
    found_dataset: str
    held_key: str
    unidentified_hint: str
    describe: Callable[[Any], str] | None = None


@dataclass
class CheckResult[D, L]:
    code: int
    drift: DriftReport[D, L]
    plan: DeployPlan[D, L]
    untracked: list[Untracked] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    # ignore conflicts, a live row on two identities: fail
    failures: list[str] = field(default_factory=list)
    # stubs of ignored rows, stale ignore entries, unused identities,
    # records with no identity: report and pass
    reports: list[str] = field(default_factory=list)
    ignored: list[L] = field(default_factory=list)
    # managed live rows in no dataset: fail
    orphans: list[L] = field(default_factory=list)


def _record_table[R](
    table: IdentityTable,
    entries: list[DatasetRecord[R]],
    spec: IdentitySpec,
    hooks: DatasetHooks[R],
) -> tuple[IdentityTable, set[str]]:
    """`table` plus an implicit identity for each record that resolves to none, and
    the canonical ids at least one record resolves to."""
    full = with_implicit(table, entries, spec, keys=hooks.keys, implicit=hooks.implicit)
    held: set[str] = set()
    for e in entries:
        hit = resolve(hooks.keys(e), full, spec)
        if hit is not None:
            held.add(hit.canonical_id)
    return full, held


def orphans[L, R](
    managed: list[L],
    table: IdentityTable,
    entries: list[DatasetRecord[R]],
    *,
    schema: RecordSchema[Any, L],
    spec: IdentitySpec,
    hooks: DatasetHooks[R],
) -> list[L]:
    """Managed live rows in no dataset: they resolve to no identity, or to one that no
    record resolves to. A record with no identity counts through its implicit one."""
    full, held = _record_table(table, entries, spec, hooks)
    out: list[L] = []
    for lv in managed:
        hit = resolve(schema.live_keys(lv), full, spec)
        if hit is None or hit.canonical_id not in held:
            out.append(lv)
    return out


def unused_identities[L, R](
    table: IdentityTable,
    entries: list[DatasetRecord[R]],
    live: list[L],
    *,
    schema: RecordSchema[Any, L],
    spec: IdentitySpec,
    hooks: DatasetHooks[R],
) -> list[str]:
    """Report lines for identities no record and no live row resolves to, and for
    `hooks.held_key` values of a used identity that none of them still holds."""
    mk = next(m for m in spec.match_keys if m.field == hooks.held_key)
    held: dict[str, set[str]] = {}
    for keys in (*(hooks.keys(e) for e in entries), *(schema.live_keys(lv) for lv in live)):
        hit = resolve(keys, table, spec)
        if hit is not None:
            values = dict(keys.values).get(mk.field, ())
            held.setdefault(hit.canonical_id, set()).update(mk.norm(v) for v in values)
    out: list[str] = []
    for cid, ident in table.entries.items():
        if cid not in held:
            out.append(f"unused identity: {cid}")
            continue
        out += [
            f"unused identity {mk.label}: {cid}: {v}"
            for v in ident.values.get(mk.field, ())
            if mk.norm(v) not in held[cid]
        ]
    return out


def unidentified_records[R](
    table: IdentityTable,
    entries: list[DatasetRecord[R]],
    *,
    spec: IdentitySpec,
    hooks: DatasetHooks[R],
) -> list[str]:
    """A record that resolves to no identity is reported."""
    out: list[str] = []
    for e in entries:
        keys = hooks.keys(e)
        if resolve(keys, table, spec) is None:
            out.append(f"no identity: {keys.label}: {hooks.unidentified_hint}")
    return out


def live_identity_conflicts[L](
    managed: list[L],
    table: IdentityTable,
    *,
    schema: RecordSchema[Any, L],
    spec: IdentitySpec,
) -> list[str]:
    """A live row whose keys reach two identities fails the check."""
    out: list[str] = []
    for lv in managed:
        msg = conflict(schema.live_keys(lv), table, spec)
        if msg is not None:
            out.append(
                f"{msg} ({schema.nouns.destination} id {schema.live_id(lv)}): merge the two "
                f"identities in {spec.file}, or remove the value that points at the wrong one"
            )
    return out


def _state_ignored[D](
    s: D,
    ignored_ids: set[str],
    ignore: IgnoreList,
    scope: str | None,
    schema: RecordSchema[D, Any],
) -> bool:
    mark = schema.mark(s)
    if mark is None:
        return False
    lid = mark.live_id
    return lid in ignored_ids or ignore.match(lid, schema.label(s), scope=scope) is not None


def _no_hooks() -> DatasetHooks[Any]:
    """Callbacks `ignore_conflicts` never calls when there are no records."""

    def unused(*_: Any) -> Any:
        raise AssertionError("no dataset records")

    return DatasetHooks(unused, unused, unused, unused, "", "", "")


def run_check[D, L, R](
    desired: list[D],
    state: list[D],
    live: list[L],
    table: IdentityTable,
    *,
    schema: RecordSchema[D, L],
    spec: IdentitySpec,
    implicit: Callable[[D, set[str]], Identity],
    retire: dict[str, RetireEntry] | None = None,
    ignore: IgnoreList | None = None,
    scope: str | None = None,
    entries: list[DatasetRecord[R]] | None = None,
    hooks: DatasetHooks[R] | None = None,
    other_live: list[L] | None = None,
) -> CheckResult[D, L]:
    """Drift, the plan and every live check, as one `CheckResult`.

    `ignore=None` is the built-in prefix only. `entries` (the dataset records, read
    through `hooks`) let an ignored row's own stub be reported, not failed; with them
    the check also fails orphans and reports unused identities and records with no
    identity. `entries=None` skips those checks. `hooks` is required with `entries`.
    `other_live` are further live rows outside this run (a scoped run's out-of-scope
    rows) whose exclusive values still count as held."""
    if entries is not None and hooks is None:
        raise ValueError("run_check: entries need hooks to read them")
    ignore = ignore if ignore is not None else IgnoreList("")
    managed, ignored = partition_live(live, ignore, schema, scope=scope)
    ignored_ids = {schema.live_id(lv) for lv in ignored}
    state = [s for s in state if not _state_ignored(s, ignored_ids, ignore, scope, schema)]
    bound = {"schema": schema, "spec": spec, "implicit": implicit}
    owned: Collection[str] = {
        schema.live_id(lv)
        for d, lv in pair_rows(desired, managed, table, **bound).pairs
        if lv is not None and schema.destination_owned(d)
    }
    drift = detect_drift(state, managed, schema=schema, owned_ids=owned)
    plan = plan_deploy(
        desired,
        managed,
        table,
        **bound,
        state=state,
        retire=retire,
        other_live=[*ignored, *(other_live or [])],
    )
    untracked = untracked_values(desired, managed, table, retire, **bound)
    stale = stale_retire(desired, managed, table, retire, **bound)
    h = hooks if hooks is not None else _no_hooks()
    failures, reports = ignore_conflicts(
        ignored,
        ignore,
        entries or [],
        desired,
        table,
        **bound,
        found_dataset=h.found_dataset,
        record_keys=h.keys,
        record_live_id=h.live_id,
        fallback_id=h.record_id,
        describe=h.describe,
        scope=scope,
    )
    failures += live_identity_conflicts(managed, table, schema=schema, spec=spec)
    if scope is None:  # a scoped run sees only its own rows: every entry would look stale
        reports += stale_ignore(ignore, live, schema)
    found: list[L] = []
    if entries is not None and hooks is not None:
        found = orphans(managed, table, entries, schema=schema, spec=spec, hooks=hooks)
        reports += unidentified_records(table, entries, spec=spec, hooks=hooks)
        if scope is None:  # a scoped run sees only its own rows: most would look unused
            reports += unused_identities(
                table, entries, live, schema=schema, spec=spec, hooks=hooks
            )
    # untracked values, known duplicates and withheld values are reported, not failed
    failing = (
        drift.any() or plan.aborts or plan.unacked or plan.errors or stale or failures or found
    )
    return CheckResult(
        EXIT_DRIFT if failing else EXIT_OK,
        drift,
        plan,
        untracked,
        stale,
        failures,
        reports,
        ignored,
        found,
    )


def manifest_errors(data_dir: Path, generated: dict[str, str]) -> list[str]:
    """Generated directories (relative to `data_dir`, each mapped to the command that
    writes it) with no manifest, or with files edited by hand since it was written."""
    errors: list[str] = []
    for sub, generator in generated.items():
        edits = manifest_edits(Path(data_dir) / sub)
        if edits is None:
            errors.append(f"missing manifest: {sub}")
            continue
        errors += [
            f"hand-edited generated file: {sub}/{edit.split(': ', 1)[1]}: run {generator}"
            for edit in edits
        ]
    return errors


def identity_offline_errors[R](
    entries: list[DatasetRecord[R]],
    table: IdentityTable,
    *,
    spec: IdentitySpec,
    record_keys: Callable[[DatasetRecord[R]], ItemKeys],
    record_id: Callable[[DatasetRecord[R]], str],
) -> list[str]:
    """Identity problems that change generated output; empty when clean: a value on two
    identities, a record whose keys reach two identities, and two records of one
    dataset on one identity."""
    errors = table_errors(table, spec)
    for entry in entries:
        msg = conflict(record_keys(entry), table, spec)
        if msg is not None:
            errors.append(msg)
    for cid, group in group_by_identity(entries, table, [], record_id=record_id).items():
        per_dataset: dict[str, list[str]] = {}
        for entry in group:
            per_dataset.setdefault(entry.dataset, []).append(entry.source_id)
        for dataset, stems in per_dataset.items():
            if len(stems) > 1:
                files = ", ".join(f"{s}.yaml" for s in sorted(stems))
                errors.append(
                    f"two records in {DATASETS_SUBDIR}/{dataset} resolve to {cid}: {files}"
                )
    return errors
