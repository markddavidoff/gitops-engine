"""Choosing one live row for an identity that several live rows share.

Of several live rows on one identity, the primary is the one carrying the canonical
id, else the one whose id the identity records as its destination source. Two or more
carrying the canonical id is an error, never a pick by listing order.
"""

from __future__ import annotations

from typing import Any

from .identity import IdentitySpec, IdentityTable
from .schema import RecordSchema


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
