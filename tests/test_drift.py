"""detect_drift against toy widgets: changed, added and deleted, by content hash."""

from __future__ import annotations

from toy import LiveWidget, ToySchema, Widget

from gitops_engine.drift import detect_drift

SCHEMA = ToySchema()


def live(live_id="L1", name="Gear", *, size=1, version="v1") -> LiveWidget:
    return LiveWidget(live_id, name, "ok", [name.lower()], [], size, version)


def recorded(lv: LiveWidget) -> Widget:
    return SCHEMA.mirror_row(lv)


def drift(state, live_rows, owned_ids=frozenset()):
    return detect_drift(state, live_rows, schema=SCHEMA, owned_ids=set(owned_ids))


def test_no_drift_when_state_matches_live():
    lv = live()
    assert not drift([recorded(lv)], [lv]).any()


def test_changed_when_content_differs_even_if_version_same():
    rep = drift([recorded(live())], [live(size=42)])  # same version, content moved
    assert [s.name for s, _ in rep.changed] == ["Gear"] and not rep.added and not rep.deleted


def test_metadata_only_bump_is_not_changed():
    assert not drift([recorded(live())], [live(version="v2")]).any()


def test_deleted_added_changed_classes():
    a, b, c = live("La", "Alpha"), live("Lb", "Beta"), live("Lc", "Gamma")
    rep = drift([recorded(a), recorded(c)], [b, live("Lc", "Gamma", size=99, version="v2")])
    assert [s.name for s in rep.deleted] == ["Alpha"]
    assert [lv.name for lv in rep.added] == ["Beta"]
    assert [s.name for s, _ in rep.changed] == ["Gamma"]


def test_untracked_state_row_is_not_drift():
    untracked = Widget(name="Loose", status="ok")  # no mark
    lv = live()
    rep = drift([untracked, recorded(lv)], [lv])
    assert not rep.changed and not rep.deleted and not rep.added


def test_owned_row_content_change_is_not_drift():
    then, now = live(), live(size=9, version="v2")
    assert not drift([recorded(then)], [now], owned_ids={"L1"}).any()
    assert drift([recorded(then)], [now]).changed  # control: not owned
