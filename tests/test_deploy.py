"""plan_deploy: pairing, plan-time drift, shared-value withholding, and the field plan."""

from __future__ import annotations

from toy import TOY_IDENTITY_SPEC as SPEC
from toy import LiveWidget, ToySchema, Widget

from gitops_engine.deploy import DeployPlan, plan_deploy
from gitops_engine.fieldplan import FieldChange
from gitops_engine.identity import Identity, IdentityTable

SCHEMA = ToySchema()


def want(name="Gear", *, status="ok", ids=("gear",), tags=(), size=None, owned=False) -> Widget:
    return Widget(name=name, status=status, ids=list(ids), tags=list(tags), size=size, owned=owned)


def live(
    name="Gear", *, live_id="L1", status="ok", ids=("gear",), tags=(), size=None, version="v1"
):
    return LiveWidget(live_id, name, status, list(ids), list(tags), size, version)


def implicit(w: Widget, taken: set[str]) -> Identity:
    cid = w.ids[0] if w.ids else w.name.lower()
    return Identity(cid, values={"names": [w.name], "tags": list(w.tags)}, sources={})


def plan(desired, live_rows, **kw) -> DeployPlan:
    return plan_deploy(
        desired, live_rows, IdentityTable(), schema=SCHEMA, spec=SPEC, implicit=implicit, **kw
    )


def recorded(lv: LiveWidget) -> Widget:
    return SCHEMA.mirror_row(lv)


def test_no_live_match_is_a_create():
    p = plan([want()], [])
    assert [d.name for d in p.creates] == ["Gear"]
    assert not p.updates and not p.unchanged


def test_changed_field_is_an_update_with_target_and_changes():
    p = plan([want(size=5)], [live(size=1)])
    [(target, lv)] = p.updates
    assert target.size == 5 and lv.live_id == "L1"
    assert p.changes["L1"] == [FieldChange("size", 1, 5, "change")]


def test_unchanged_row():
    p = plan([want()], [live()])
    assert [d.name for d in p.unchanged] == ["Gear"] and not p.updates
    assert p.changes["L1"] == []


def test_live_with_no_desired_row_is_an_orphan():
    p = plan([], [live("Spare", ids=("spare",))])
    assert [lv.name for lv in p.orphans] == ["Spare"]


def test_plan_time_drift_is_an_abort():
    then = live(size=1, version="v1")
    now = live(size=2, version="v2")
    p = plan([want(size=5)], [now], state=[recorded(then)])
    assert not p.updates
    [(_, lv, why)] = p.aborts
    assert lv is now
    assert why == "drift: live version v2 != pre-state v1 (content changed)"


def test_metadata_only_version_bump_is_not_drift():
    then = live(size=1, version="v1")
    now = live(size=1, version="v2")
    p = plan([want(size=5)], [now], state=[recorded(then)])
    assert not p.aborts and len(p.updates) == 1


def test_state_matched_by_live_id_when_ids_differ():
    then = live(size=1, version="v1", ids=("old",))
    now = live(size=2, version="v2", ids=("gear",))
    p = plan([want(size=5)], [now], state=[recorded(then)])
    assert len(p.aborts) == 1


def test_owned_row_edit_is_not_drift():
    then = live(size=1, version="v1")
    now = live(size=2, version="v2")
    p = plan([want(owned=True)], [now], state=[recorded(then)])
    assert not p.aborts


def test_owned_row_without_live_is_a_note_not_a_create():
    p = plan([want(owned=True)], [])
    assert not p.creates
    assert p.notes == ["stub 'Gear' has no store widget: run the refresh"]


def test_exclusive_value_held_by_another_live_row_is_withheld():
    other = live("Cog", live_id="L2", ids=("cog",), tags=("Shared.example",))
    p = plan([want(tags=("shared.example", "own.example"))], [live(), other])
    [(target, _)] = p.updates
    assert target.tags == ["own.example"]
    assert p.withheld == [
        (
            "withheld tag 'shared.example' for 'Gear': store widget 'Cog' already holds it "
            "(shared tags are unverified)"
        )
    ]


def test_exclusive_value_the_paired_row_holds_is_kept():
    other = live("Cog", live_id="L2", ids=("cog",), tags=("shared.example",))
    p = plan([want(tags=("shared.example",))], [live(tags=("shared.example",)), other])
    assert p.withheld == [] and [d.name for d in p.unchanged] == ["Gear"]


def test_exclusive_value_on_a_create_and_on_other_live():
    other = live("Cog", live_id="L9", ids=("cog",), tags=("shared.example",))
    p = plan([want(tags=("shared.example",))], [], other_live=[other])
    [created] = p.creates
    assert created.tags == [] and len(p.withheld) == 1


def test_owned_row_is_not_withheld():
    other = live("Cog", live_id="L2", ids=("cog",), tags=("shared.example",))
    p = plan([want(tags=("shared.example",), owned=True)], [live(), other])
    assert p.withheld == []


def test_retire_entry_keyed_by_first_id_acknowledges_destructive():
    p = plan([want()], [live(size=3)], retire={"gear": {"size": 3}})
    [(target, _)] = p.updates
    assert target.size is None
    assert p.changes["L1"] == [FieldChange("size", 3, None, "clear", retire=True)]
    assert p.unacked == []


def test_pairing_errors_and_duplicates_are_carried():
    a = live("Gear", live_id="L1", ids=("gear",))
    b = live("Gear", live_id="L2", ids=("gear",))
    p = plan([want()], [a, b])
    assert p.errors and "two or more store widgets carry the canonical id gear" in p.errors[0]
