"""apply_deploy: preflight, snapshot, creates, check-and-set updates, the never-null
guard, and post-write verification."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from toy import TOY_IDENTITY_SPEC as SPEC
from toy import LiveWidget, ToySchema, Widget
from toy_adapter import ToyDestination

from gitops_engine.deploy import ApplyResult, DeployPlan, apply_deploy, plan_deploy
from gitops_engine.fieldplan import FieldChange
from gitops_engine.identity import Identity, IdentityTable
from gitops_engine.ignore import IgnoreEntry, IgnoreList

SCHEMA = ToySchema()


def want(name="Gear", *, status="ok", ids=None, tags=(), size=None, owned=False) -> Widget:
    ids = [name.lower()] if ids is None else list(ids)
    return Widget(name=name, status=status, ids=ids, tags=list(tags), size=size, owned=owned)


def live(name="Gear", *, live_id="L1", status="ok", ids=None, tags=(), size=None, version="v1"):
    ids = [name.lower()] if ids is None else list(ids)
    return LiveWidget(live_id, name, status, ids, list(tags), size, version)


def implicit(w: Widget, taken: set[str]) -> Identity:
    cid = w.ids[0] if w.ids else w.name.lower()
    return Identity(cid, values={"names": [w.name], "tags": list(w.tags)}, sources={})


def plan(desired, live_rows, **kw) -> DeployPlan:
    return plan_deploy(
        desired, live_rows, IdentityTable(), schema=SCHEMA, spec=SPEC, implicit=implicit, **kw
    )


def apply(p: DeployPlan, dest: ToyDestination, tmp_path: Path, **kw) -> ApplyResult:
    return apply_deploy(p, dest, schema=SCHEMA, snapshot_dir=tmp_path / "snaps", **kw)


def writes(dest: ToyDestination) -> list[tuple]:
    return [c for c in dest.calls if c[0] in ("create", "update")]


# --- order and check-and-set ------------------------------------------------------


def test_apply_creates_then_updates_and_persists_mirror_rows(tmp_path):
    lv = live(size=1)
    dest = ToyDestination([lv])
    p = plan([want("Cog", size=2), want(size=5)], [lv])
    res = apply(p, dest, tmp_path)
    assert [c[0] for c in dest.calls] == ["preflight", "create", "get", "update"]
    assert res.failure is None and res.created == ["new-1"]
    assert [SCHEMA.mark(r).live_id for r in res.persisted] == ["new-1", "L1"]
    assert res.persisted[1] == SCHEMA.mirror_row(dest.rows["L1"])


def test_apply_check_and_set_aborts_on_write_time_drift(tmp_path):
    p = plan([want(size=5)], [live(size=1, version="v1")])
    dest = ToyDestination([live(size=1, version="v2")])  # moved between plan and apply
    res = apply(p, dest, tmp_path)
    assert writes(dest) == [] and res.persisted == []
    assert [why for _, _, why in p.aborts] == ["write-time drift v2 != v1"]


def test_apply_aborts_row_gone_at_write_time(tmp_path):
    p = plan([want(size=5)], [live(size=1)])
    res = apply(p, ToyDestination([]), tmp_path)
    assert res.failure is None
    assert [why for _, _, why in p.aborts] == ["gone at write time"]


def test_apply_aborts_row_ignored_at_write_time(tmp_path):
    p = plan([want(size=5)], [live(size=1)])
    dest = ToyDestination([live(size=1)])
    ignore = IgnoreList("ignore.yaml", entries=[IgnoreEntry("L1", "Gear", "frozen", 3)])
    apply(p, dest, tmp_path, ignore=ignore)
    assert writes(dest) == []
    assert [why for _, _, why in p.aborts] == ["ignored at write time: frozen"]


def test_apply_default_ignore_is_the_builtin_prefix(tmp_path):
    p = plan([want("zzz-gitops-test-gear", size=5)], [live("zzz-gitops-test-gear", size=1)])
    dest = ToyDestination([live("zzz-gitops-test-gear", size=1)])
    apply(p, dest, tmp_path)
    assert writes(dest) == [] and p.aborts[0][2].startswith("ignored at write time: ")
    p = plan([want("zzz-gitops-test-gear", size=5)], [live("zzz-gitops-test-gear", size=1)])
    apply(p, dest, tmp_path, scope="zzz-gitops-test-run")
    assert len(writes(dest)) == 1


def test_apply_stops_at_first_failure_and_marks_rest_not_attempted(tmp_path):
    rows = [live("A", live_id="LA", size=1), live("B", live_id="LB", size=1)]
    rows.append(live("C", live_id="LC", size=1))
    p = plan([want("A", size=2), want("B", size=2), want("C", size=2)], rows)
    dest = ToyDestination(rows, fail={"LA": "boom"})
    res = apply(p, dest, tmp_path)
    assert res.failure == "A: update failed: boom"
    assert [c[1] for c in writes(dest)] == ["LA"]
    assert [(d.name, why) for d, _, why in p.aborts] == [
        ("B", "not attempted: apply stopped at an earlier failure"),
        ("C", "not attempted: apply stopped at an earlier failure"),
    ]


def test_create_failure_stops_before_any_update(tmp_path):
    lv = live(size=1)
    p = plan([want("Cog"), want("Bolt"), want(size=5)], [lv])
    dest = ToyDestination([lv], fail={"Cog": "no room"})
    res = apply(p, dest, tmp_path)
    assert res.failure == "Cog: create failed: no room"
    assert [c[0] for c in writes(dest)] == ["create"]
    assert [why for _, _, why in p.aborts] == ["not attempted: apply stopped at an earlier failure"]


def test_apply_refuses_planner_errors_and_unacked_before_any_call(tmp_path):
    dest = ToyDestination()
    with pytest.raises(ValueError, match=r"planner error\(s\), nothing written"):
        apply(DeployPlan(errors=["bad"]), dest, tmp_path)
    with pytest.raises(
        ValueError, match=r"unacknowledged destructive change\(s\), nothing written"
    ):
        apply(DeployPlan(unacked=["x"]), dest, tmp_path)
    p = DeployPlan(changes={"L1": [FieldChange("size", 1, None, "clear")]})
    with pytest.raises(ValueError, match="unacknowledged destructive"):
        apply(p, dest, tmp_path)
    assert dest.calls == [] and not (tmp_path / "snaps").exists()


def test_preflight_sees_every_create_value_and_planned_update_values(tmp_path):
    lv = live(size=1, status="ok")
    p = plan([want("Cog", status="new"), want(size=5, status="ok")], [lv])
    dest = ToyDestination([lv])
    apply(p, dest, tmp_path)
    assert dest.calls[0] == (
        "preflight",
        [
            {"name": "Cog", "status": "new", "ids": ["cog"], "tags": [], "size": None},
            {"size": 5},
        ],
        False,
    )


def test_preflight_failure_writes_nothing_and_takes_no_snapshot(tmp_path):
    lv = live(size=1)
    p = plan([want("Cog", status="weird"), want(size=5)], [lv])
    dest = ToyDestination([lv], statuses={"ok"})
    with pytest.raises(ValueError, match="nothing written"):
        apply(p, dest, tmp_path)
    assert writes(dest) == [] and not (tmp_path / "snaps").exists()


def test_apply_never_deletes_orphans(tmp_path):
    orphan = live("Spare")
    p = plan([], [orphan])
    dest = ToyDestination([orphan])
    apply(p, dest, tmp_path)
    assert writes(dest) == [] and "L1" in dest.rows


# --- values sent ------------------------------------------------------------------


def test_creates_send_only_non_empty_values(tmp_path):
    p = plan([want("Cog", tags=(), size=None)], [])
    dest = ToyDestination()
    apply(p, dest, tmp_path)
    assert writes(dest) == [("create", {"name": "Cog", "status": "ok", "ids": ["cog"]})]


def test_update_sends_only_planned_fields(tmp_path):
    lv = live(size=1, tags=("a.example",))
    p = plan([want(size=5, tags=("a.example", "b.example"))], [lv])
    dest = ToyDestination([lv])
    apply(p, dest, tmp_path)
    assert writes(dest) == [
        ("update", "L1", {"tags": ["a.example", "b.example"], "size": 5}, False)
    ]


def test_retire_clear_passes_guard(tmp_path):
    lv = live(size=3)
    p = plan([want()], [lv], retire={"gear": {"size": 3}})
    dest = ToyDestination([lv])
    res = apply(p, dest, tmp_path)
    assert res.failure is None
    assert writes(dest) == [("update", "L1", {"size": None}, True)]


def test_never_null_guard_refuses_before_destination_called(tmp_path):
    lv = live(size=1)
    target = want(name="Gear", size=None)
    p = DeployPlan(updates=[(target, lv)], changes={"L1": [FieldChange("size", 1, None, "change")]})
    later = live("Cog", live_id="L2", size=1)
    p.updates.append((want("Cog", size=2), later))
    p.changes["L2"] = [FieldChange("size", 1, 2, "change")]
    dest = ToyDestination([lv, later])
    res = apply(p, dest, tmp_path)
    assert writes(dest) == []
    assert res.failure == "Gear: update refused: empty size without retire"
    assert res.persisted == []
    assert [why for _, _, why in p.aborts] == ["not attempted: apply stopped at an earlier failure"]


def test_never_null_guard_counts_empty_string_and_list(tmp_path):
    for field, value in (("name", ""), ("ids", [])):
        lv = live(size=1)
        target = replace(want(), **{field: value})
        p = DeployPlan(
            updates=[(target, lv)],
            changes={"L1": [FieldChange(field, getattr(lv, field), value, "change")]},
        )
        dest = ToyDestination([lv])
        res = apply(p, dest, tmp_path)
        assert writes(dest) == []
        assert res.failure == f"{target.name}: update refused: empty {field} without retire"


# --- verification -----------------------------------------------------------------


def test_verify_planned_change_that_did_not_land(tmp_path):
    lv = live(size=1)
    p = plan([want(size=5)], [lv])
    dest = ToyDestination([lv], tamper={"L1": {"size": 1}})
    res = apply(p, dest, tmp_path)
    assert res.failure == "Gear: size expected 5 got 1"
    assert len(res.persisted) == 1  # written rows are still recorded


def test_verify_unplanned_field_change_fails(tmp_path):
    lv = live(size=1, name="Gear")
    p = plan([want(size=5)], [lv])
    dest = ToyDestination([lv], tamper={"L1": {"name": "Other"}})
    res = apply(p, dest, tmp_path)
    assert res.failure == "Gear: name expected Gear got Other"


def test_verify_compares_with_policy_equality(tmp_path):
    lv = live(size=1, ids=("gear", "g2"), tags=("A.example",))
    p = plan([want(size=5, ids=("gear", "g2"), tags=("a.example",))], [lv])
    tamper = {"ids": ["g2", "gear"], "tags": ["a.EXAMPLE"]}
    dest = ToyDestination([lv], tamper={"L1": tamper})
    assert apply(p, dest, tmp_path).failure is None


def test_verify_unplanned_required_field_uses_before_value(tmp_path):
    # An owned row whose live status is empty: the target falls back to the desired
    # status, but status is not planned, so the store keeps "" and that is correct.
    lv = live(status="", ids=("gear",))
    p = plan([want(status="ok", ids=("gear", "g2"), owned=True)], [lv])
    [(target, _)] = p.updates
    assert target.status == "ok"
    assert [c.field for c in p.changes["L1"]] == ["ids"]
    dest = ToyDestination([lv])
    res = apply(p, dest, tmp_path)
    assert writes(dest) == [("update", "L1", {"ids": ["gear", "g2"]}, False)]
    assert res.failure is None


def test_verify_planned_required_field_uses_target(tmp_path):
    lv = live(status="old")
    p = plan([want(status="new")], [lv])
    dest = ToyDestination([lv], tamper={"L1": {"status": "old"}})
    assert apply(p, dest, tmp_path).failure == "Gear: status expected new got old"


def test_verify_failure_stops_later_updates(tmp_path):
    rows = [live("A", live_id="LA", size=1), live("B", live_id="LB", size=1)]
    p = plan([want("A", size=2), want("B", size=2)], rows)
    dest = ToyDestination(rows, tamper={"LA": {"size": 9}})
    res = apply(p, dest, tmp_path)
    assert res.failure == "A: size expected 2 got 9"
    assert [why for _, _, why in p.aborts] == ["not attempted: apply stopped at an earlier failure"]


# --- snapshot ---------------------------------------------------------------------


def test_snapshot_written_before_first_write(tmp_path):
    lv = live(size=1)
    p = plan([want("Cog"), want(size=5)], [lv])
    dest = ToyDestination([lv])
    seen: list[int] = []
    create = dest.create

    def spy(values):
        seen.append(len(list((tmp_path / "snaps").glob("*.yaml"))))
        return create(values)

    dest.create = spy  # type: ignore[method-assign]
    res = apply(p, dest, tmp_path)
    assert seen == [1] and res.snapshot.parent == tmp_path / "snaps"


def test_unwritable_snapshot_dir_aborts_before_any_write(tmp_path):
    lv = live(size=1)
    p = plan([want(size=5)], [lv])
    blocker = tmp_path / "snaps"
    blocker.write_text("not a directory")
    dest = ToyDestination([lv])
    with pytest.raises(OSError):
        apply(p, dest, tmp_path)
    assert writes(dest) == []
