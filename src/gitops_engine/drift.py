"""Detect whether the recorded state still matches live, by content hash.

Three drift classes:

- **changed**: a tracked state row whose live row's hash no longer matches the mark;
- **added**: a live row no tracked state row points at;
- **deleted**: a tracked state row whose live row is gone.

A state row with no mark is untracked and is evidence of nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .adapter import Adapter


@dataclass
class DriftReport[D, L]:
    """Drift between recorded state and live. `D` is the state row type here."""

    changed: list[tuple[D, L]] = field(default_factory=list)
    added: list[L] = field(default_factory=list)
    deleted: list[D] = field(default_factory=list)

    def any(self) -> bool:
        return bool(self.changed or self.added or self.deleted)


def detect_drift[D, L, S](
    state: list[S],
    live: list[L],
    adapter: Adapter[D, L],
) -> DriftReport[S, L]:
    live_by_id = {adapter.live_id(lv): lv for lv in live}
    rep: DriftReport[S, L] = DriftReport()
    tracked: set[str] = set()
    for s in state:
        mark = adapter.state_mark(s)
        if mark is None:
            continue  # untracked row — not evidence either way
        tracked.add(mark.live_id)
        lv = live_by_id.get(mark.live_id)
        if lv is None:
            rep.deleted.append(s)
            continue
        if adapter.live_hash(lv) != mark.content_hash:
            rep.changed.append((s, lv))
    rep.added = [lv for lv in live if adapter.live_id(lv) not in tracked]
    return rep
