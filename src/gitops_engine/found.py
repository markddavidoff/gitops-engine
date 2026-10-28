"""Destination-only records: a stub for each live row the primary dataset lacks.

`build_found` runs offline over the state mirror. Each managed live row resolves
against the identity table. A row that resolves to nothing gets a new identity; a row
whose identity has no record in the primary dataset becomes a stub in the found
dataset. The primary row's own match values and its destination id are appended to its
identity. A stub's file stem is its canonical id, and a stub keeps that stem across
runs, keyed by its destination id, so the id stamped into the destination never drifts
when the row's name changes there.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .datasets import DatasetRecord
from .identity import IdentitySpec, ItemKeys, resolve
from .identity_store import IdentityStore
from .pairing import pick_primary, stamped_conflict
from .schema import RecordSchema


@dataclass
class FoundBuild[R]:
    stubs: list[tuple[str, R]] = field(default_factory=list)  # (canonical id, stub record)
    duplicates: list[str] = field(default_factory=list)
    reports: list[str] = field(default_factory=list)


def build_found[R, L](
    managed: list[L],
    store: IdentityStore,
    entries: list[DatasetRecord[R]],
    *,
    schema: RecordSchema[Any, L],
    spec: IdentitySpec,
    found_dataset: str,
    primary_dataset: str,
    record_keys: Callable[[DatasetRecord[R]], ItemKeys],
    new_id: Callable[[L, str | None], str],
    stub: Callable[[str, L, R | None], R],
    new_values: Callable[[L], dict[str, list[str]]],
    learned_values: Callable[[L], dict[str, list[str]]],
) -> FoundBuild[R]:
    """Stubs for the managed live rows whose identity has no `primary_dataset` record.

    `managed` must already exclude ignored rows. Rows are taken in live-id order. Of
    several on one identity, the primary is the one carrying the canonical id or the
    recorded destination id (as the planner picks it); the others are known duplicates
    and contribute nothing. When neither id is on any of them, nothing is recorded on
    the identity (it is reported), and a stub, if the identity needs one, is built from
    the lowest live id.

    `new_id(row, stem)` is the base id for a row that resolves to nothing (`stem` is
    the file stem of its previous stub, if any). `stub(cid, row, previous)` builds the
    stub record; `previous` is the record the identity's last stub held. `new_values`
    gives a new identity its match values; `learned_values` gives the primary row's
    values appended to an existing identity."""
    out: FoundBuild[R] = FoundBuild()
    old = [e for e in entries if e.dataset == found_dataset]
    stem_by_live: dict[str, str] = {}
    for e in old:
        live_id = dict(record_keys(e).sources).get(spec.destination_source)
        if live_id:
            stem_by_live[live_id] = e.source_id
    previous_by_stem = {e.source_id: e.record for e in old}

    groups: dict[str, list[L]] = {}
    for lv in sorted(managed, key=schema.live_id):
        stem = stem_by_live.get(schema.live_id(lv))
        keys = schema.live_keys(lv)
        if stem is not None:
            keys = dataclasses.replace(keys, ids=(stem, *keys.ids))
        hit = resolve(keys, store.table, spec)
        if hit is not None:
            cid = hit.canonical_id
        else:
            # No destination source yet: a second unresolved row may land on this new
            # identity, and recording the first id would settle the pick by id order. The
            # primary's id is recorded below once the group is known.
            cid = store.add_identity(new_id(lv, stem), sources={}, **new_values(lv))
        groups.setdefault(cid, []).append(lv)

    with_record = set()
    for e in entries:
        if e.dataset != primary_dataset:
            continue
        hit = resolve(record_keys(e), store.table, spec)
        if hit is not None:
            with_record.add(hit.canonical_id)

    n = schema.nouns
    for cid, group in groups.items():
        primary = (
            group[0]
            if len(group) == 1
            else pick_primary(cid, group, store.table, schema=schema, spec=spec)
        )
        if primary is None:
            # Ambiguous: record nothing on the identity, so the planner's error keeps
            # failing `check` until a human records one or merges the rows in the destination.
            conflict = stamped_conflict(cid, group, schema=schema)
            if conflict is not None:
                out.reports.append(f"{found_dataset}: {conflict}; nothing recorded")
            else:
                named = ", ".join(
                    f"'{schema.live_label(lv)}' ({schema.live_id(lv)})" for lv in group
                )
                out.reports.append(
                    f"{found_dataset}: {cid}: no {n.record} carries the canonical or recorded "
                    f"{n.destination} id ({named}); nothing recorded: record one in {spec.file} "
                    f"or merge in {n.destination}"
                )
            primary = group[0]
        else:
            out.duplicates += [
                f"{cid}: '{schema.live_label(lv)}' ({schema.live_id(lv)}) is a known duplicate; "
                f"merge it in {n.destination}"
                for lv in group
                if lv is not primary
            ]
            store.add_values(
                cid,
                sources={spec.destination_source: schema.live_id(primary)},
                **learned_values(primary),
            )
        if cid in with_record:
            continue
        out.stubs.append((cid, stub(cid, primary, previous_by_stem.get(cid))))
    out.stubs.sort(key=lambda p: p[0])
    return out
