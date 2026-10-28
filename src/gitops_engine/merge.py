"""Merge the datasets by identity: one winner per real-world thing.

Every dataset record resolves to a canonical id (`record_id`, the consumer's rule).
Records that share an id form a group, ordered best first: the dataset that comes
earliest in priority order, then the file name. The best record wins whole; no field
is mixed in from the others. `gather` is the one place a consumer adds what the
identity knows (extra match values, say) to the winner.
"""

from __future__ import annotations

from collections.abc import Callable

from .datasets import DatasetRecord
from .identity import Identity, IdentityTable


def group_by_identity[R](
    entries: list[DatasetRecord[R]],
    table: IdentityTable,
    priority: list[str],
    *,
    record_id: Callable[[DatasetRecord[R]], str],
) -> dict[str, list[DatasetRecord[R]]]:
    """Canonical id -> its entries, best first: dataset priority, then file name.

    `table` is the identity table `record_id` reads; the groups come out in the order
    each id first appears in `entries`. A dataset missing from `priority` ranks last."""
    rank = {name: i for i, name in enumerate(priority)}
    groups: dict[str, list[DatasetRecord[R]]] = {}
    for entry in entries:
        groups.setdefault(record_id(entry), []).append(entry)
    for group in groups.values():
        group.sort(key=lambda e: (rank.get(e.dataset, len(rank)), e.source_id))
    return groups


def merge_by_identity[R](
    entries: list[DatasetRecord[R]],
    table: IdentityTable,
    priority: list[str],
    *,
    record_id: Callable[[DatasetRecord[R]], str],
    gather: Callable[[R, Identity | None], R],
) -> list[tuple[str, DatasetRecord[R]]]:
    """One (canonical id, winner) per identity, in group order.

    The winner is the best record of its group, whole, with `gather(record, identity)`
    as its record; `identity` is None when the table has no entry for the id."""
    out: list[tuple[str, DatasetRecord[R]]] = []
    for cid, group in group_by_identity(entries, table, priority, record_id=record_id).items():
        best = group[0]
        record = gather(best.record, table.entries.get(cid))
        out.append(
            (cid, DatasetRecord(dataset=best.dataset, source_id=best.source_id, record=record))
        )
    return out
