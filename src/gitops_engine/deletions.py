"""Deletion confirmation: which managed rows the destination has deleted.

A row the last refresh mirrored can go missing from the live listing for reasons other
than deletion (a paged listing cut short, a filtered view). So a deletion is confirmed
twice before anything records it:

1. the row is absent from the full `list_live` listing, and
2. a direct `dest.get` by its live id returns `None` (not found).

The candidates are the old state rows `pair_old_state` pairs with no live row.
Destination-owned rows never pair, so they are never candidates. A row `get` still
finds is reported as still live. A `DestinationError` from `get` propagates, so a
failed read never confirms a deletion.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .destination import Destination
from .edits import pair_old_state
from .overrides import _canonical_id
from .schema import RecordSchema


@dataclass
class Deletions:
    """Each entry is a (canonical id, live id). `confirmed` rows the destination
    reports not found; `still_live` rows missing from the listing that `get` found."""

    confirmed: list[tuple[str, str]] = field(default_factory=list)
    still_live: list[tuple[str, str]] = field(default_factory=list)


def confirm_deletions[D, L](
    old_state: list[D],
    live: list[L],
    desired: list[D],
    dest: Destination[L],
    *,
    schema: RecordSchema[D, L],
) -> Deletions:
    """The old state rows missing from `live`, split by a direct `dest.get`: `None`
    confirms the deletion, a returned row is still live. `live` is the full
    `list_live` listing. A `DestinationError` from `get` propagates."""
    out = Deletions()
    for pair in pair_old_state(old_state, live, desired, schema=schema):
        if pair.live is not None:
            continue
        entry = (_canonical_id(pair.desired, schema), pair.live_id)
        if dest.get(pair.live_id) is None:
            out.confirmed.append(entry)
        else:
            out.still_live.append(entry)
    return out


__all__ = ["Deletions", "confirm_deletions"]
