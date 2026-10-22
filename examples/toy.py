"""Walk the reconcile lifecycle end to end against the in-memory toy adapter.

Run from a clone:  uv run python examples/toy.py
"""

from __future__ import annotations

from toy_adapter import LiveWidget, ToyAdapter, Widget, state_row

from gitops_engine import EXIT_DRIFT, EXIT_OK, apply, run_check


def main() -> None:
    # The external system already holds one widget; git wants two.
    adapter = ToyAdapter([LiveWidget("id1", "gear", "Gear", 1, "v1")])
    desired = [Widget("gear", "Gear", 5), Widget("cog", "Cog", 2)]

    # 1. Observe the external system and record a state row (with a SyncMark) per
    #    live row. Unrecorded live rows would otherwise show up as "added" drift.
    live = list(adapter.rows.values())
    state = [state_row(lv) for lv in live]

    # 2. Check: state matches live, so no drift; the plan holds one update, one create.
    code, drift, plan = run_check(desired, state, live, adapter)
    print(
        "first check:",
        code,
        "| creates",
        [d.key for d in plan.creates],
        "| updates",
        [d.key for d, _ in plan.updates],
    )
    assert code == EXIT_OK and not plan.aborts

    # 3. Apply, then 4. re-record state from the rows apply returned.
    written = apply(plan, adapter)
    assert not plan.aborts  # apply appends write-time aborts to the plan
    state = [state_row(lv) for lv in written]

    # Re-observe and check again: everything reconciles.
    live = list(adapter.rows.values())
    code, drift, plan = run_check(desired, state, live, adapter)
    print("after apply:", code, "| unchanged", [d.key for d in plan.unchanged])
    assert code == EXIT_OK

    # Someone edits "gear" in the external system, out of band.
    adapter.rows["id1"] = LiveWidget("id1", "gear", "Gear", 9, "v9")
    live = list(adapter.rows.values())
    code, drift, plan = run_check(desired, state, live, adapter)
    print(
        "after out-of-band edit:",
        code,
        "| changed",
        [s.key for s, _ in drift.changed],
        "| aborts",
        [(d.key, why) for d, _, why in plan.aborts],
    )
    assert code == EXIT_DRIFT


if __name__ == "__main__":
    main()
