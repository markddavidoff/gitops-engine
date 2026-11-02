"""Hidden rows: live rows the overrides file takes out of the sync on both sides.

A live row is hidden by the canonical id its keys resolve to (an override entry with
`hidden`), else by an active name prefix (`startswith` on the raw live label). The
prefixes follow the ignore file's scope rule (`ignore.active_prefixes`): the built-in
prefix is exempt under a scope that starts with it; a file prefix applies under every
scope.

`partition_hidden` is the one split of live rows into managed and hidden, in place of
`ignore.partition_live` for a consumer that keys exclusions by canonical id.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import ignore
from .datasets import DatasetRecord
from .identity import Identity, IdentitySpec, IdentityTable, ItemKeys, resolve
from .overrides import HiddenPrefix, Overrides
from .schema import RecordSchema


@dataclass
class Hidden:
    file: str  # the file name messages cite
    ids: dict[str, str]  # canonical id -> reason
    prefixes: list[HiddenPrefix]

    def active_prefixes(self, scope: str | None) -> list[HiddenPrefix]:
        """The prefixes in force for a run (`ignore.active_prefixes`)."""
        return ignore.active_prefixes(self.prefixes, scope)

    def where(self, p: HiddenPrefix) -> str:
        return "built-in" if p.line is None else f"{self.file}:{p.line}"

    def prefix_for(self, label: str, *, scope: str | None) -> HiddenPrefix | None:
        """The first active prefix `label` starts with."""
        for p in self.active_prefixes(scope):
            if label.startswith(p.prefix):
                return p
        return None

    def match(self, canonical_id: str | None, label: str, *, scope: str | None) -> str | None:
        """The reason a row is hidden: by canonical id, else by an active prefix."""
        if canonical_id is not None and canonical_id in self.ids:
            return self.ids[canonical_id]
        p = self.prefix_for(label, scope=scope)
        return p.reason if p is not None else None


def hidden_from(ov: Overrides) -> Hidden:
    """The hidden entries and prefixes of a loaded overrides file."""
    ids = {cid: e.hidden for cid, e in ov.entries.items() if e.hidden is not None}
    return Hidden(ov.file, ids, list(ov.prefixes))


def _canonical_id[L](
    lv: L, table: IdentityTable, schema: RecordSchema[Any, L], spec: IdentitySpec
) -> str | None:
    hit = resolve(schema.live_keys(lv), table, spec)
    return hit.canonical_id if hit is not None else None


def live_reason[L](
    lv: L,
    hidden: Hidden,
    table: IdentityTable,
    *,
    schema: RecordSchema[Any, L],
    spec: IdentitySpec,
    scope: str | None,
) -> str | None:
    """The reason this live row is hidden, or `None`. Bound to one run, it is the
    `exclude` hook `apply_deploy` takes."""
    cid = _canonical_id(lv, table, schema, spec)
    return hidden.match(cid, schema.live_label(lv), scope=scope)


def partition_hidden[L](
    live: list[L],
    hidden: Hidden,
    table: IdentityTable,
    *,
    schema: RecordSchema[Any, L],
    spec: IdentitySpec,
    scope: str | None,
) -> tuple[list[L], list[L]]:
    """The one split of live rows: (managed, hidden), order kept."""
    managed: list[L] = []
    out: list[L] = []
    for lv in live:
        hit = live_reason(lv, hidden, table, schema=schema, spec=spec, scope=scope)
        (out if hit is not None else managed).append(lv)
    return managed, out


def hidden_conflicts[D, L, R](
    hidden_live: list[L],
    hidden: Hidden,
    entries: list[DatasetRecord[R]],
    desired: list[D],
    table: IdentityTable,
    *,
    schema: RecordSchema[D, L],
    spec: IdentitySpec,
    found_dataset: str,
    record_keys: Callable[[DatasetRecord[R]], ItemKeys],
    record_live_id: Callable[[R], str | None],
    fallback_id: Callable[[DatasetRecord[R]], str],
    implicit: Callable[[D, set[str]], Identity],
    scope: str | None,
    describe: Callable[[D], str] | None = None,
) -> tuple[list[str], list[str]]:
    """(failures, reports), with `ignore.ignore_conflicts`'s hooks: a hidden row that
    resolves to an identity with a desired row is a failure naming the entry or prefix
    and `hidden.file`, unless that row is the `found_dataset` stub of the same
    destination id, which is reported: the next refresh drops it."""
    describe = describe or schema.label
    n = schema.nouns
    failures: list[str] = []
    reports: list[str] = []
    for c in ignore.excluded_collisions(
        hidden_live,
        entries,
        desired,
        table,
        schema=schema,
        spec=spec,
        found_dataset=found_dataset,
        record_keys=record_keys,
        record_live_id=record_live_id,
        fallback_id=fallback_id,
        implicit=implicit,
    ):
        name = describe(c.desired)
        label = schema.live_label(c.live)
        if c.own_stub:
            reports.append(
                f"stub '{name}' is for hidden {n.destination} {n.record} '{label}': "
                "the next refresh drops it"
            )
            continue
        cid = _canonical_id(c.live, table, schema, spec)
        p = hidden.prefix_for(label, scope=scope)
        if cid is not None and cid in hidden.ids:
            why, what = f"entry {cid}, {hidden.file}", "entry"
        elif p is not None:
            why, what = f"prefix '{p.prefix}', {hidden.where(p)}", "prefix"
        else:  # not hidden under this scope; still name the file
            why, what = hidden.file, "entry"
        failures.append(
            f"desired record '{name}' shares {c.hit.key} '{c.hit.value}' with hidden "
            f"{n.destination} {n.record} '{label}' ({why}). "
            f"Remove that {what} from {hidden.file} or change {name}."
        )
    return failures, reports


def hidden_lines[L](
    hidden_live: list[L],
    hidden: Hidden,
    table: IdentityTable,
    *,
    schema: RecordSchema[Any, L],
    spec: IdentitySpec,
    scope: str | None,
) -> list[str]:
    """A check's visibility lines: each hidden row, then each active prefix."""
    lines: list[str] = []
    for lv in hidden_live:
        reason = live_reason(lv, hidden, table, schema=schema, spec=spec, scope=scope) or ""
        lines.append(f"HIDDEN: '{schema.live_label(lv)}' ({schema.live_id(lv)}): {reason}")
    for p in hidden.active_prefixes(scope):
        if p.line is None:
            lines.append(f"HIDDEN PREFIX: {p.prefix} (built-in)")
        else:
            lines.append(f"HIDDEN PREFIX: {p.prefix} ({hidden.where(p)}): {p.reason}")
    return lines
