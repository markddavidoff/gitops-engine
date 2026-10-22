"""make_plan's decision table against toy widgets."""

from __future__ import annotations

from toy_adapter import LiveWidget, ToyAdapter, Widget, content_hash, state_row

from gitops_engine.adapter import SyncMark
from gitops_engine.plan import make_plan


def test_unmatched_desired_is_create():
    adapter = ToyAdapter()
    plan = make_plan([Widget("new", "New", 1)], [], [], adapter)
    assert [c.key for c in plan.creates] == ["new"]
    assert not plan.updates and not plan.aborts and not plan.orphans and not plan.unchanged


def test_matched_changed_content_is_update():
    live = [LiveWidget("w1", "widget", "Widget", 1, "v1")]
    state = [state_row(live[0])]  # mark version matches live => no abort
    d = Widget("widget", "Widget", 999)  # content differs
    plan = make_plan([d], live, state, adapter=ToyAdapter())
    assert len(plan.updates) == 1 and not plan.aborts and not plan.creates


def test_equal_hashes_is_unchanged():
    live = [LiveWidget("w1", "widget", "Widget", 1, "v1")]
    d = Widget("widget", "Widget", 1)  # identical deployable content
    plan = make_plan([d], live, [state_row(live[0])], adapter=ToyAdapter())
    assert d in plan.unchanged and not plan.updates and not plan.aborts


def test_metadata_only_version_change_is_not_abort():
    recorded = LiveWidget("w1", "widget", "Widget", 1, "v1")
    live_now = LiveWidget("w1", "widget", "Widget", 1, "v2")  # version moved, content same
    d = Widget("widget", "Widget", 5)
    plan = make_plan([d], [live_now], [state_row(recorded)], adapter=ToyAdapter())
    assert not plan.aborts and len(plan.updates) == 1


def test_content_change_with_version_change_aborts():
    recorded = LiveWidget("w1", "widget", "Widget", 1, "v1")
    live_now = LiveWidget("w1", "widget", "Widget", 99, "v2")  # version AND content moved
    d = Widget("widget", "Widget", 5)
    plan = make_plan([d], [live_now], [state_row(recorded)], adapter=ToyAdapter())
    assert len(plan.aborts) == 1 and not plan.updates


def test_plan_without_state_never_aborts():
    live_now = LiveWidget("w1", "widget", "Widget", 1, "v2")
    d = Widget("widget", "Widget", 5)
    plan = make_plan([d], [live_now], [], adapter=ToyAdapter())  # first link: no mark
    assert not plan.aborts and len(plan.updates) == 1


def test_unmatched_live_is_orphan():
    live = [LiveWidget("w1", "orphan", "Orphan", 1, "v1")]
    plan = make_plan([], live, [], adapter=ToyAdapter())
    assert [lv.live_id for lv in plan.orphans] == ["w1"]


def test_two_live_rows_match_one_desired_row_plans_once():
    # Two live rows share the key. First match wins; the loser becomes an orphan.
    first = LiveWidget("w1", "dup", "Dup", 1, "v1")
    second = LiveWidget("w2", "dup", "Dup", 1, "v1")
    d = Widget("dup", "Dup", 2)  # content differs => update
    plan = make_plan([d], [first, second], [], adapter=ToyAdapter())
    assert not plan.creates
    assert len(plan.updates) == 1 and plan.updates[0][1] is first
    assert plan.orphans == [second]


def test_empty_desired_and_live_yields_empty_plan():
    plan = make_plan([], [], [], adapter=ToyAdapter())
    assert not (plan.creates or plan.updates or plan.aborts or plan.orphans or plan.unchanged)


def test_mark_found_via_adapter_not_engine():
    # The engine never looks up external ids; it asks the adapter for the mark.
    # Here the state row carries no matching key, only a matching live_id, so the
    # adapter's fallback is what links it.
    recorded = LiveWidget("w1", "widget", "Widget", 1, "v1")
    st = state_row(recorded)
    st.key = "different-key"  # key no longer matches desired; live_id still does
    assert st.mark is not None and st.mark.live_id == "w1"
    live_now = LiveWidget("w1", "widget", "Widget", 99, "v2")  # content + version moved
    d = Widget("widget", "Widget", 5)
    plan = make_plan([d], [live_now], [st], adapter=ToyAdapter())
    assert len(plan.aborts) == 1  # fallback mark found => drift detected


def test_mark_version_matches_but_hash_differs_is_not_abort():
    # Both conditions must hold to abort: version differs AND live hash differs from
    # the mark's hash. Same version => proceed even if the live content moved.
    mark = SyncMark("w1", "v1", content_hash("widget", "Widget", 1))
    live_now = LiveWidget("w1", "widget", "Widget", 99, "v1")  # same version, diff content
    d = Widget("widget", "Widget", 5, mark=mark)
    plan = make_plan([d], [live_now], [d], adapter=ToyAdapter())
    assert not plan.aborts and len(plan.updates) == 1
