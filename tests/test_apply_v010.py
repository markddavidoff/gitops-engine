"""v0.1.0 `apply` (removed with the v0.1.0 `Adapter`)."""

from __future__ import annotations

import pytest
from toy_adapter import ToyAdapter, live_widget, widget

from gitops_engine.apply import apply
from gitops_engine.plan import Plan, make_plan


def test_apply_creates_then_updates_and_returns_live_rows():
    create = widget("new", "New", 1)
    update = widget("widget", "Widget", 999)
    live = [live_widget("w1", "widget", "Widget", 1, "v1")]
    adapter = ToyAdapter(live_rows=live)
    plan = make_plan([create, update], live, [], adapter)
    assert len(plan.creates) == 1 and len(plan.updates) == 1

    written = apply(plan, adapter)
    assert [lv.live_id for lv in written] == ["new-new", "w1"]
    assert [w.ids[0] for w in adapter.created] == ["new"]
    assert [live_id for live_id, _ in adapter.updated] == ["w1"]


def test_preflight_failure_writes_nothing():
    ok = widget("a", "A", 1)
    bad = widget("c", "C", 1)
    plan = make_plan([ok, bad], [], [], ToyAdapter())
    assert len(plan.creates) == 2
    adapter = ToyAdapter(resolvable={"a"})  # "c" unresolvable
    with pytest.raises(ValueError, match="unresolvable"):
        apply(plan, adapter)
    assert adapter.created == []  # fail-fast: "a" was resolvable but nothing was written


def test_update_aborts_when_row_gone_at_write_time():
    update = widget("widget", "Widget", 999)
    live = [live_widget("w1", "widget", "Widget", 1, "v1")]
    plan = make_plan([update], live, [], ToyAdapter())
    assert len(plan.updates) == 1
    adapter = ToyAdapter(live_rows=[])  # get("w1") => None
    written = apply(plan, adapter)
    assert written == [] and adapter.updated == []
    assert len(plan.aborts) == 1 and "gone" in plan.aborts[0][2]


def test_update_aborts_on_version_drift_at_write_time():
    update = widget("widget", "Widget", 999)
    live = [live_widget("w1", "widget", "Widget", 1, "v1")]
    plan = make_plan([update], live, [], ToyAdapter())
    assert len(plan.updates) == 1
    # between plan and apply, the row's version moved
    adapter = ToyAdapter(live_rows=[live_widget("w1", "widget", "Widget", 1, "v2")])
    written = apply(plan, adapter)
    assert written == [] and adapter.updated == []  # must not clobber a concurrent edit
    assert len(plan.aborts) == 1


def test_apply_never_deletes_orphans():
    orphan = live_widget("w1", "orphan", "Orphan", 1, "v1")
    plan: Plan = Plan(orphans=[orphan])
    adapter = ToyAdapter()
    assert apply(plan, adapter) == []
    assert adapter.deleted == []


def test_create_failure_mid_batch_propagates():
    # apply is not transactional: a create that raises mid-batch propagates, and
    # whatever was written before it stays written.
    first = widget("a", "A", 1)
    second = widget("boom", "Boom", 1)
    third = widget("c", "C", 1)
    plan = make_plan([first, second, third], [], [], ToyAdapter())
    assert len(plan.creates) == 3
    adapter = ToyAdapter(fail_create_key="boom")
    with pytest.raises(RuntimeError, match="create failed"):
        apply(plan, adapter)
    assert [w.ids[0] for w in adapter.created] == ["a"]  # "a" landed; "boom" raised; "c" never ran
    assert "new-a" in adapter.rows
