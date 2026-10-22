"""Diff desired against live into a plan of creates, updates, aborts, orphans,
and unchanged rows. The decision table is system-agnostic; the adapter supplies
matching, hashing, and identity."""

from __future__ import annotations

from dataclasses import dataclass, field

from .adapter import Adapter


@dataclass
class Plan[D, L]:
    creates: list[D] = field(default_factory=list)
    updates: list[tuple[D, L]] = field(default_factory=list)
    aborts: list[tuple[D, L, str]] = field(default_factory=list)
    orphans: list[L] = field(default_factory=list)
    unchanged: list[D] = field(default_factory=list)


def make_plan[D, L, S](
    desired: list[D],
    live: list[L],
    state: list[S],
    adapter: Adapter[D, L],
) -> Plan[D, L]:
    """Decide what reconciling `desired` onto `live` would do:

    - unmatched desired -> create;
    - matched, and a mark exists whose version differs from live AND whose recorded
      hash differs from the live hash -> abort (real drift since we last looked);
    - matched and the desired hash equals the live hash -> unchanged;
    - otherwise -> update;
    - any live row no desired row matched -> orphan (reported, never deleted).
    """
    plan: Plan[D, L] = Plan()
    matched: set[str] = set()
    for d in desired:
        lv = adapter.match(d, live)
        if lv is None:
            plan.creates.append(d)
            continue
        matched.add(adapter.live_id(lv))
        mark = adapter.mark_for(d, lv, state)
        live_hash = adapter.live_hash(lv)
        if (
            mark is not None
            and adapter.live_version(lv) != mark.version
            and live_hash != mark.content_hash
        ):
            reason = (
                f"drift: live version {adapter.live_version(lv)} != "
                f"state version {mark.version} (content changed)"
            )
            plan.aborts.append((d, lv, reason))
            continue
        if adapter.desired_hash(d) == live_hash:
            plan.unchanged.append(d)
            continue
        plan.updates.append((d, lv))
    plan.orphans = [lv for lv in live if adapter.live_id(lv) not in matched]
    return plan
