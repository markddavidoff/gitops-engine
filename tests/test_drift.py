"""detect_drift and run_check against toy widgets."""

from __future__ import annotations

from toy_adapter import ToyAdapter, live_widget, state_row, widget

from gitops_engine.drift import detect_drift
from gitops_engine.exitcodes import EXIT_DRIFT, EXIT_OK, run_check


def test_no_drift_when_state_matches_live():
    lv = live_widget("w1", "acme", "Acme", 1, "v1")
    assert not detect_drift([state_row(lv)], [lv], ToyAdapter()).any()


def test_changed_when_content_differs_even_if_version_same():
    recorded = live_widget("w1", "acme", "Acme", 1, "v1")
    now = live_widget("w1", "acme", "Acme", 42, "v1")  # same version, content moved
    rep = detect_drift([state_row(recorded)], [now], ToyAdapter())
    assert [s.ids[0] for s, _ in rep.changed] == ["acme"] and not rep.added and not rep.deleted


def test_metadata_only_bump_is_not_changed():
    recorded = live_widget("w1", "acme", "Acme", 1, "v1")
    now = live_widget("w1", "acme", "Acme", 1, "v2")  # version moved, content same
    assert not detect_drift([state_row(recorded)], [now], ToyAdapter()).any()


def test_deleted_added_changed_classes():
    a = live_widget("wa", "alpha", "Alpha", 1, "v1")
    b = live_widget("wb", "beta", "Beta", 1, "v1")
    c = live_widget("wc", "gamma", "Gamma", 1, "v1")
    # a vanished (deleted), b appeared (added), c changed content
    state = [state_row(a), state_row(c)]
    live = [b, live_widget("wc", "gamma", "Gamma", 99, "v2")]
    rep = detect_drift(state, live, ToyAdapter())
    assert [s.ids[0] for s in rep.deleted] == ["alpha"]
    assert [lv.ids[0] for lv in rep.added] == ["beta"]
    assert [s.ids[0] for s, _ in rep.changed] == ["gamma"]


def test_untracked_state_row_is_not_drift():
    untracked = widget("loose", "Loose", 1)  # mark is None
    lv = live_widget("w1", "acme", "Acme", 1, "v1")
    rep = detect_drift([untracked, state_row(lv)], [lv], ToyAdapter())
    assert not rep.changed and not rep.deleted and not rep.added


def test_run_check_exit_codes():
    lv = live_widget("w1", "acme", "Acme", 1, "v1")
    desired = widget("acme", "Acme", 1)
    code, drift, plan = run_check([desired], [state_row(lv)], [lv], ToyAdapter())
    assert code == EXIT_OK and not drift.any() and plan.unchanged

    drifted = live_widget("w1", "acme", "Acme", 9, "v1")  # content moved under the mark
    code, drift, _ = run_check([desired], [state_row(lv)], [drifted], ToyAdapter())
    assert code == EXIT_DRIFT and drift.changed


def test_run_check_exit_drift_on_plan_abort():
    # No content drift reported, but the plan aborts => still EXIT_DRIFT.
    recorded = live_widget("w1", "acme", "Acme", 1, "v1")
    live_now = live_widget("w1", "acme", "Acme", 99, "v2")  # version + content moved
    desired = widget("acme", "Acme", 5)
    code, _drift, plan = run_check([desired], [state_row(recorded)], [live_now], ToyAdapter())
    # the state mark is against v1/content=1; live is content=99 => drift.changed too,
    # but the point here is the plan aborts and the code is EXIT_DRIFT regardless.
    assert plan.aborts and code == EXIT_DRIFT
