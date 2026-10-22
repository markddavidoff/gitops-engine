"""Execute a plan: preflight, then creates, then updates — never deletes.

Each update re-reads its target (check-and-set) and aborts rather than clobber a
row that moved since plan time. apply is NOT transactional: a write that raises
propagates, and rows already written stay written.
"""

from __future__ import annotations

from .adapter import Adapter
from .plan import Plan


def apply[D, L](plan: Plan[D, L], adapter: Adapter[D, L]) -> list[L]:
    """Apply `plan` and return the live rows written. Appends to `plan.aborts` for
    any update whose target is gone or has drifted at write time. Orphans are left
    untouched."""
    adapter.preflight(plan)  # raise here => nothing is written

    written: list[L] = []

    for d in plan.creates:
        written.append(adapter.create(d))

    for d, lv0 in plan.updates:
        cur = adapter.get(adapter.live_id(lv0))  # write-time re-read (check-and-set)
        if cur is None:
            plan.aborts.append((d, lv0, "gone at write time"))
            continue
        if adapter.live_version(cur) != adapter.live_version(lv0):
            reason = f"write-time drift {adapter.live_version(cur)} != {adapter.live_version(lv0)}"
            plan.aborts.append((d, lv0, reason))
            continue
        written.append(adapter.update(adapter.live_id(lv0), d))

    return written
