"""Pairing desired rows with live rows by identity.

`pair_rows` resolves every desired row and every live row to an identity and pairs
them. Of several live rows on one identity, the primary is the one carrying the
canonical id, else the one whose id the identity records as its destination source.
Two or more carrying the canonical id is an error, never a pick by listing order.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .identity import Identity, IdentitySpec, IdentityTable, resolve, with_implicit
from .schema import RecordSchema


@dataclass
class Pairing[D, L]:
    """Desired rows paired with their live row by identity."""

    pairs: list[tuple[D, L | None]] = field(default_factory=list)
    duplicates: list[tuple[str, L]] = field(default_factory=list)  # (canonical id, extra row)
    errors: list[str] = field(default_factory=list)
    unmatched: list[L] = field(default_factory=list)  # live rows with no desired row


def pick_primary[L](
    cid: str,
    group: list[L],
    table: IdentityTable,
    *,
    schema: RecordSchema[Any, L],
    spec: IdentitySpec,
) -> L | None:
    """The live row to treat as the identity's own, or None.

    None when neither id is on any of them, or when two or more carry the canonical
    id (`stamped_conflict` names that error)."""
    stamped = [lv for lv in group if cid in schema.live_keys(lv).ids]
    if len(stamped) > 1:
        return None
    if stamped:
        return stamped[0]
    live_id = table.entries[cid].sources.get(spec.destination_source)
    return next((lv for lv in group if live_id and schema.live_id(lv) == live_id), None)


def stamped_conflict[L](cid: str, group: list[L], *, schema: RecordSchema[Any, L]) -> str | None:
    """The error when two or more live rows carry the canonical id `cid`."""
    stamped = sorted((lv for lv in group if cid in schema.live_keys(lv).ids), key=schema.live_id)
    if len(stamped) < 2:
        return None
    named = ", ".join(f"'{schema.live_label(lv)}' ({schema.live_id(lv)})" for lv in stamped)
    n = schema.nouns
    return (
        f"two or more {n.destination} {n.records} carry the canonical id {cid}: {named}: "
        f"merge them in {n.destination} or remove the id from all but one"
    )


def pair_rows[D, L](
    desired: list[D],
    live: list[L],
    table: IdentityTable,
    *,
    schema: RecordSchema[D, L],
    spec: IdentitySpec,
    implicit: Callable[[D, set[str]], Identity],
) -> Pairing[D, L]:
    """Resolve every desired row and live row to an identity and pair them.

    A desired row that resolves to nothing gets an implicit identity (`implicit`). A
    second desired row on one identity is an error and is not paired. Live rows that
    resolve to no desired row are `unmatched`. With two or more live rows on one
    identity, `pick_primary` chooses the one to write and the rest are duplicates; if
    it finds none, that is an error and the identity is not paired. Two or more
    carrying the canonical id is an error too (`stamped_conflict`)."""
    table = with_implicit(table, desired, spec, keys=schema.desired_keys, implicit=implicit)
    n = schema.nouns
    out: Pairing[D, L] = Pairing()
    by_cid: dict[str, D] = {}
    for d in desired:
        hit = resolve(schema.desired_keys(d), table, spec)
        assert hit is not None  # with_implicit gives every row an identity
        if hit.canonical_id in by_cid:
            out.errors.append(
                f"two desired rows resolve to {hit.canonical_id}: "
                f"'{schema.label(by_cid[hit.canonical_id])}' and '{schema.label(d)}'"
            )
            continue
        by_cid[hit.canonical_id] = d
    groups: dict[str, list[L]] = {}
    for lv in live:
        hit = resolve(schema.live_keys(lv), table, spec)
        if hit is None or hit.canonical_id not in by_cid:
            out.unmatched.append(lv)
            continue
        groups.setdefault(hit.canonical_id, []).append(lv)
    for cid, d in by_cid.items():
        group = groups.get(cid, [])
        if len(group) <= 1:
            out.pairs.append((d, group[0] if group else None))
            continue
        conflict = stamped_conflict(cid, group, schema=schema)
        if conflict is not None:
            out.errors.append(conflict)
            continue
        chosen = pick_primary(cid, group, table, schema=schema, spec=spec)
        if chosen is None:
            named = ", ".join(f"'{schema.live_label(lv)}' ({schema.live_id(lv)})" for lv in group)
            out.errors.append(
                f"known duplicates for {cid} with no canonical or recorded {n.destination} id: "
                f"{named}: merge them in {n.destination} or record one in {spec.file}"
            )
            continue
        out.pairs.append((d, chosen))
        out.duplicates += [(cid, lv) for lv in group if lv is not chosen]
    return out
