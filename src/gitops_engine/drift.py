"""Detect whether the recorded state still matches live, by content hash.

Three drift classes:

- **changed**: a tracked state row whose live row's hash no longer matches the mark;
- **added**: a live row no tracked state row points at;
- **deleted**: a tracked state row whose live row is gone.

A state row with no mark is untracked and is evidence of nothing. A content change
on a destination-owned row (`owned_ids`) is not drift: the destination owns its fields.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field

from .schema import RecordSchema


@dataclass
class DriftReport[D, L]:
    """Drift between recorded state and live. `D` is the state row type."""

    changed: list[tuple[D, L]] = field(default_factory=list)
    added: list[L] = field(default_factory=list)
    deleted: list[D] = field(default_factory=list)

    def any(self) -> bool:
        return bool(self.changed or self.added or self.deleted)


def detect_drift[D, L](
    state: list[D],
    live: list[L],
    *,
    schema: RecordSchema[D, L],
    owned_ids: Collection[str] = frozenset(),
) -> DriftReport[D, L]:
    """Compare each state row's mark (`schema.mark`) with its live row
    (`schema.live_hash`). `owned_ids` are the live ids of destination-owned rows."""
    live_by_id = {schema.live_id(lv): lv for lv in live}
    rep: DriftReport[D, L] = DriftReport()
    tracked: set[str] = set()
    for s in state:
        mark = schema.mark(s)
        if mark is None:
            continue  # untracked row — not evidence either way
        tracked.add(mark.live_id)
        lv = live_by_id.get(mark.live_id)
        if lv is None:
            rep.deleted.append(s)
            continue
        if schema.live_hash(lv) != mark.content_hash and mark.live_id not in owned_ids:
            rep.changed.append((s, lv))
    rep.added = [lv for lv in live if schema.live_id(lv) not in tracked]
    return rep
