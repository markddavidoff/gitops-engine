"""The field planner: per-field ownership policies and the destructive guard."""

from __future__ import annotations

import itertools

from toy import TOY_IDENTITY_SPEC as SPEC
from toy import LiveWidget, ToySchema, Widget

from gitops_engine.fieldplan import (
    DESTRUCTIVE,
    FieldChange,
    Untracked,
    plan_row,
    stale_retire,
    unacknowledged_destructive,
    untracked_values,
)
from gitops_engine.identity import Identity, IdentityTable
from gitops_engine.schema import SyncMark

SCHEMA = ToySchema()  # name, status (required), ids (unordered), tags (Union), size (KeepIfUnset)


def want(*, name="Gear", status="ok", ids=("gear",), tags=(), size=None, owned=False) -> Widget:
    return Widget(name=name, status=status, ids=list(ids), tags=list(tags), size=size, owned=owned)


def live(*, name="Gear", status="ok", ids=("gear",), tags=(), size=None, live_id="L1"):
    return LiveWidget(live_id, name, status, list(ids), list(tags), size, "v1")


def plan(d, lv, retire=None):
    return plan_row(d, lv, retire, schema=SCHEMA)


def implicit(w: Widget, taken: set[str]) -> Identity:
    cid = w.ids[0] if w.ids else w.name.lower()
    return Identity(cid, values={"names": [w.name], "tags": list(w.tags)}, sources={})


def untracked(desired, live_rows, retire):
    return untracked_values(
        desired, live_rows, IdentityTable(), retire, schema=SCHEMA, spec=SPEC, implicit=implicit
    )


def stale(desired, live_rows, retire):
    return stale_retire(
        desired, live_rows, IdentityTable(), retire, schema=SCHEMA, spec=SPEC, implicit=implicit
    )


# --- plan_row ---------------------------------------------------------------------


def test_union_keeps_live_casing():
    target, changes = plan(want(tags=["A.example"]), live(tags=["a.example"]), {"tags": []})
    assert changes == []
    assert target.tags == ["a.example"]
    # a retire entry matches whatever its case
    target, changes = plan(want(), live(tags=["a.example"]), {"tags": ["A.EXAMPLE"]})
    assert target.tags == []
    assert changes == [FieldChange("tags", "a.example", None, "remove", retire=True)]


def test_keep_if_unset_retire_clears_only_exact_live():
    target, changes = plan(want(), live(size=300), {"size": 300})
    assert target.size is None
    assert changes == [FieldChange("size", 300, None, "clear", retire=True)]
    target, changes = plan(want(), live(size=250), {"size": 300})
    assert target.size == 250
    assert changes == []
    # a retire entry that matches live clears it even when desired sets a value
    target, changes = plan(want(size=9), live(size=300), {"size": 300})
    assert target.size is None
    assert changes == [FieldChange("size", 300, None, "clear", retire=True)]


def test_owned_row_required_field_falls_back():
    d = want(status="open", ids=["gear"], owned=True)
    lv = live(status="", ids=[])
    target, changes = plan(d, lv)
    assert target.status == "open"
    assert changes == [FieldChange("ids", [], ["gear"], "change")]


def test_owned_row_takes_live_values_and_ignores_retire():
    d = want(name="Gear", ids=["gear"], tags=["red"], size=1, owned=True)
    lv = live(name="Gear Systems", status="closed", ids=[], tags=["red", "blue"], size=7)
    target, changes = plan(d, lv)
    assert changes == [FieldChange("ids", [], ["gear"], "change")]
    assert (target.name, target.status, target.ids, target.tags, target.size, target.owned) == (
        "Gear Systems",
        "closed",
        ["gear"],
        ["red", "blue"],
        7,
        True,
    )
    _, changes = plan(d, lv, {"tags": ["blue"], "size": 7})
    assert changes == [FieldChange("ids", [], ["gear"], "change")]


def test_owned_row_with_matching_ids_plans_nothing():
    d = want(ids=["b", "a"], owned=True)
    target, changes = plan(d, live(ids=["a", "b"], status=""))
    assert changes == []
    assert target.ids == ["b", "a"]


def test_unordered_always_change_kind():
    target, changes = plan(want(ids=["x"]), live(ids=[]))
    assert changes == [FieldChange("ids", [], ["x"], "change")]
    assert target.ids == ["x"]
    # order alone is not a change
    _, changes = plan(want(ids=["a", "b"]), live(ids=["b", "a"]))
    assert changes == []


def test_unordered_always_change_is_not_destructive():
    target, changes = plan(want(ids=["gear"]), live(ids=["gear-old"]))
    assert target.ids == ["gear"]
    [c] = changes
    assert (c.field, c.old, c.new, c.kind) == ("ids", ["gear-old"], ["gear"], "change")
    assert c.kind not in DESTRUCTIVE
    assert unacknowledged_destructive(changes) == []


def test_always_set_then_change():
    _, changes = plan(want(status="ok"), live(status=""))
    assert changes == [FieldChange("status", "", "ok", "set")]
    _, changes = plan(want(name="New"), live(name="Old"))
    assert changes == [FieldChange("name", "Old", "New", "change")]


def test_changes_follow_schema_field_order():
    d = want(name="N", status="s2", ids=["gear"], tags=["new.tag"], size=5)
    lv = live(name="O", status="s1", ids=["old"], tags=["gone.tag"], size=4)
    _, changes = plan(d, lv, {"tags": ["gone.tag"]})
    assert [(c.field, c.kind) for c in changes] == [
        ("name", "change"),
        ("status", "change"),
        ("ids", "change"),
        ("tags", "add"),
        ("tags", "remove"),
        ("size", "change"),
    ]


def test_empty_union_keeps_live():
    target, changes = plan(want(tags=[]), live(tags=["x.tag"]))
    assert target.tags == ["x.tag"]
    assert changes == []


def test_keep_if_unset_unset_keeps_live():
    target, changes = plan(want(size=None), live(size=300))
    assert target.size == 300
    assert changes == []


def test_keep_if_unset_set_overrides_live():
    target, changes = plan(want(size=500), live(size=550))
    assert target.size == 500
    assert changes == [FieldChange("size", 550, 500, "change")]
    assert unacknowledged_destructive(changes) == []
    _, changes = plan(want(size=500), live(size=None))
    assert changes == [FieldChange("size", None, 500, "set")]


def test_union_add_and_keep_live():
    target, changes = plan(want(tags=["a.com"]), live(tags=["b.io"]))
    assert target.tags == ["a.com", "b.io"]
    assert changes == [FieldChange("tags", None, "a.com", "add")]


def test_union_drops_desired_duplicates_and_retired_desired_values():
    target, changes = plan(want(tags=["a.com", "A.COM", "x.io"]), live(), {"tags": ["X.IO"]})
    assert target.tags == ["a.com"]
    assert changes == [FieldChange("tags", None, "a.com", "add")]


def test_retire_union_removes():
    target, changes = plan(want(tags=["a.com"]), live(tags=["a.com", "b.io"]), {"tags": ["b.io"]})
    assert target.tags == ["a.com"]
    [c] = changes
    assert (c.field, c.old, c.new, c.kind, c.retire) == ("tags", "b.io", None, "remove", True)
    assert unacknowledged_destructive(changes) == []


def test_target_keeps_the_desired_mark():
    mark = SyncMark("L1", "v0", "h")
    d = Widget(name="Gear", status="ok", ids=["gear"], mark=mark)
    target, _ = plan(d, live())
    assert target.mark is mark


def test_property_no_destructive_without_retire():
    tag_pool = [[], ["a.com"], ["a.com", "b.io"]]
    size_pool = [None, 1, 2]
    for dt, lt, ds, ls in itertools.product(tag_pool, tag_pool, size_pool, size_pool):
        _, changes = plan(want(tags=dt, size=ds), live(tags=lt, size=ls))
        assert unacknowledged_destructive(changes) == [], (dt, lt, ds, ls)
        assert not [c for c in changes if c.kind in DESTRUCTIVE], (dt, lt, ds, ls)


def test_property_destructive_only_from_matching_retire():
    """Every remove/clear is backed by a retire entry whose value is live right now; a
    retired value never survives in the target; no unretired live value is lost."""
    tag_pool = [[], ["a.com"], ["a.com", "b.io"]]
    size_pool = [None, 1, 2]
    retire_pool = [
        None,
        {"tags": ["b.io"]},
        {"tags": ["B.IO"]},
        {"size": 1},
        {"tags": ["a.com"], "size": 2},
    ]
    for dt, lt, ds, ls, rt in itertools.product(
        tag_pool, tag_pool, size_pool, size_pool, retire_pool
    ):
        case = (dt, lt, ds, ls, rt)
        target, changes = plan(want(tags=dt, size=ds), live(tags=lt, size=ls), rt)
        assert unacknowledged_destructive(changes) == [], case
        retired = {t.lower() for t in (rt or {}).get("tags", [])}
        live_tags = {t.lower() for t in lt}
        for c in changes:
            if c.kind == "remove":
                assert c.retire and c.old.lower() in retired & live_tags, case
            if c.kind == "clear":
                assert c.retire and (rt or {}).get(c.field) == c.old == ls, case
        target_tags = {t.lower() for t in target.tags}
        assert not (retired & target_tags), case
        assert live_tags - retired <= target_tags, case
        if ls is not None and (rt or {}).get("size") != ls:
            assert target.size is not None, case


def test_unacknowledged_destructive_flags_retire_false():
    bad = FieldChange("size", 1, None, "clear")
    ok = FieldChange("size", 1, None, "clear", retire=True)
    assert unacknowledged_destructive([bad, ok, FieldChange("name", "a", "b", "change")]) == [bad]


# --- untracked_values -------------------------------------------------------------


def test_untracked_reports_live_only_values():
    got = untracked([want(tags=["a.com"])], [live(tags=["a.com", "x.io", "X.IO"], size=300)], {})
    assert got == [
        Untracked("gear", "Gear", "tags", "x.io"),
        Untracked("gear", "Gear", "size", 300),
    ]


def test_untracked_ignores_case_and_carried_values():
    assert untracked([want(tags=["gear.com"])], [live(tags=["GEAR.com"])], {}) == []
    assert untracked([want(size=5)], [live(size=300)], {}) == []


def test_untracked_excludes_retired():
    retire = {"gear": {"tags": ["OLD.io"], "size": 300}}
    assert untracked([want()], [live(tags=["old.io"], size=300)], retire) == []
    # a retire entry for a different size does not hide the live one
    got = untracked([want()], [live(size=250)], retire)
    assert [(u.field, u.value) for u in got] == [("size", 250)]


def test_untracked_ignores_orphans_and_owned_rows():
    other = live(name="Other", ids=["other"], tags=["x.com"], live_id="L9")
    assert untracked([want()], [other], {}) == []
    assert untracked([want(owned=True)], [live(tags=["x.com"], size=7)], {}) == []


# --- stale_retire -----------------------------------------------------------------


def test_stale_retire_unknown_id_and_absent_value():
    retire = {
        "ghost": {"tags": ["a.com"]},
        "nolive": {"size": 1},
        "gear": {"tags": ["gone.io", "b.io"], "size": 7},
    }
    desired = [want(), want(name="Nolive", ids=["nolive"])]
    assert stale(desired, [live(tags=["b.io"], size=300)], retire) == [
        "ghost: no git row",
        "nolive: no live match",
        "gear: tags gone.io not live",
        "gear: size 7 not live",
    ]


def test_stale_retire_clean_entry_is_silent():
    retire = {"gear": {"tags": ["B.io"], "size": 300}}
    assert stale([want()], [live(tags=["b.io"], size=300)], retire) == []
    assert stale([want()], [live()], {}) == []


def test_stale_retire_conflicts_with_desired():
    retire = {"gear": {"tags": ["A.com"], "size": 5}}
    assert stale([want(tags=["a.com"], size=5)], [live(tags=["a.com"], size=5)], retire) == [
        "gear: tags A.com conflicts with git",
        "gear: size 5 conflicts with git",
    ]
    # desired owns the field whenever it sets one, even a different value
    retire = {"gear": {"size": 300}}
    assert stale([want(size=250)], [live(size=300)], retire) == [
        "gear: size 300 conflicts with git"
    ]


def test_stale_retire_owned_row_uses_destination_noun():
    retire = {"gear": {"size": 7}}
    assert stale([want(owned=True)], [live(size=7)], retire) == [
        "gear: store-owned stub (retire does not apply)"
    ]
