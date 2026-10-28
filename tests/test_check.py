"""run_check and the offline checks against toy widgets.

A check fails (EXIT_DRIFT) on drift, plan-time aborts, planner errors, unacknowledged
destructive changes, stale retire entries, ignore conflicts, a live row on two
identities and orphans. It only reports untracked values, known duplicates, unused
identities, records with no identity and stale ignore entries.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from toy import TOY_IDENTITY_SPEC as SPEC
from toy import LiveWidget, ToySchema, Widget

import gitops_engine
from gitops_engine import deploy
from gitops_engine.check import (
    CheckResult,
    DatasetHooks,
    identity_offline_errors,
    manifest_errors,
    orphans,
    run_check,
    unused_identities,
)
from gitops_engine.datasets import DatasetRecord
from gitops_engine.exitcodes import EXIT_DRIFT, EXIT_OK, EXIT_OPERROR
from gitops_engine.fieldplan import FieldChange
from gitops_engine.identity import Identity, IdentityTable, ItemKeys
from gitops_engine.ignore import IgnoreFormat, IgnoreList, load_ignore
from gitops_engine.manifest import write_manifest

SCHEMA = ToySchema()
PRIMARY, FOUND = "origin", "found"
SRC, DEST = SPEC.sources
FMT = IgnoreFormat("ignore.yaml", "store_id", "# Widgets the sync never touches.\n", "widgets")
OWN = "widgets:\n  - store_id: L_own\n    name: Gear Works\n    reason: our own widget\n"


def want(name="Gear", *, ids=("gear",), tags=("red",), size=None, owned=False) -> Widget:
    return Widget(name=name, status="ok", ids=list(ids), tags=list(tags), size=size, owned=owned)


def live(
    name="Gear", *, live_id="L1", ids=("gear",), tags=("red",), size=None, version="v1"
) -> LiveWidget:
    return LiveWidget(live_id, name, "ok", list(ids), list(tags), size, version)


def recorded(lv: LiveWidget) -> Widget:
    return SCHEMA.mirror_row(lv)


def implicit(w: Widget, taken: set[str]) -> Identity:
    cid = w.ids[0] if w.ids else w.name.lower()
    return Identity(cid, values={"names": [w.name], "tags": list(w.tags)}, sources={})


def record_keys(e: DatasetRecord[Widget]) -> ItemKeys:
    w = e.record
    values = (("tags", tuple(w.tags)), ("names", (w.name,)))
    if e.dataset == FOUND:
        live_id = w.mark.live_id if w.mark else None
        return ItemKeys(label=w.name, ids=(e.source_id,), sources=((DEST, live_id),), values=values)
    return ItemKeys(label=w.name, sources=((SRC, e.source_id),), values=values)


def record_implicit(e: DatasetRecord[Widget], taken: set[str]) -> Identity:
    cid, n = e.source_id, 1
    while cid in taken:
        n += 1
        cid = f"{e.source_id}-{n}"
    return Identity(cid, values={"names": [e.record.name], "tags": list(e.record.tags)})


HOOKS = DatasetHooks(
    keys=record_keys,
    record_id=lambda e: e.source_id,
    implicit=record_implicit,
    live_id=lambda w: w.mark.live_id if w.mark else None,
    found_dataset=FOUND,
    held_key="tags",
    unidentified_hint="run the import",
)


def record(source_id="gear-file", name="Gear", *, tags=("red",), dataset=PRIMARY):
    return DatasetRecord(dataset, source_id, Widget(name=name, status="ok", tags=list(tags)))


def table(**identities: dict) -> IdentityTable:
    return IdentityTable(
        entries={
            cid: Identity(cid, values=kw.get("values", {}), sources=kw.get("sources", {}))
            for cid, kw in identities.items()
        }
    )


def ignore_list(tmp_path, text=OWN) -> IgnoreList:
    (tmp_path / FMT.file).write_text(text)
    return load_ignore(tmp_path / FMT.file, FMT)


def check(desired, state, live_rows, tbl=None, **kw) -> CheckResult:
    kw.setdefault("ignore", IgnoreList(FMT.file))
    return run_check(
        desired,
        state,
        live_rows,
        tbl or IdentityTable(),
        schema=SCHEMA,
        spec=SPEC,
        implicit=implicit,
        hooks=HOOKS,
        **kw,
    )


# ---- exit codes ----


def test_exit_codes_keep_their_values():
    assert (EXIT_OK, EXIT_OPERROR, EXIT_DRIFT) == (0, 2, 3)
    assert gitops_engine.__version__ == "0.2.0"
    assert gitops_engine.EXIT_DRIFT == EXIT_DRIFT and gitops_engine.run_check is run_check


def test_in_sync_is_exit_ok():
    lv = live()
    res = check([want()], [recorded(lv)], [lv])
    assert res.code == EXIT_OK and not res.drift.any() and res.plan.unchanged
    assert res.untracked == [] and res.stale == [] and res.failures == [] and res.orphans == []


def test_content_drift_is_exit_drift():
    lv = live()
    res = check([want()], [recorded(lv)], [live(size=9)])
    assert res.code == EXIT_DRIFT and res.drift.changed


def test_check_exit_drift_on_abort():
    then = live(size=1)
    now = live(size=99, version="v2")  # version and content moved since the state mark
    res = check([want(size=5)], [recorded(then)], [now])
    assert [(d.name, why) for d, _, why in res.plan.aborts] == [
        ("Gear", "drift: live version v2 != pre-state v1 (content changed)")
    ]
    assert res.code == EXIT_DRIFT


def test_check_owned_rows_not_drift():
    then = live(size=1)
    now = live("Gear Inc", size=40, tags=("red", "blue"), version="v2")
    res = check([want(owned=True)], [recorded(then)], [now])
    assert not res.drift.changed and not res.plan.aborts and res.untracked == []
    assert res.code == EXIT_OK
    # control: a row the plan owns, edited the same way, is drift
    res = check([want()], [recorded(then)], [now])
    assert res.drift.changed and res.plan.aborts and res.code == EXIT_DRIFT


def test_check_untracked_reported_not_failed():
    lv = live(tags=("red", "Blue"), size=7)
    res = check([want(tags=("red",))], [recorded(lv)], [lv])
    assert res.code == EXIT_OK and not res.drift.any() and not res.plan.aborts
    assert [(u.field, u.value) for u in res.untracked] == [("tags", "Blue"), ("size", 7)]


def test_stale_retire_fails():
    lv = live()
    res = check([want()], [recorded(lv)], [lv], retire={"gear": {"tags": ["gone"]}})
    assert res.stale == ["gear: tags gone not live"] and res.code == EXIT_DRIFT
    assert not res.drift.any() and not res.untracked


def test_unacknowledged_destructive_fails(monkeypatch):
    lv = live(size=1)
    bad = FieldChange("size", 1, None, "clear")  # retire=False: a planner bug
    monkeypatch.setattr(deploy, "plan_row", lambda d, live, retire, **_: (d, [bad]))
    res = check([want()], [recorded(lv)], [lv])
    assert res.plan.unacked == ["Gear: size clear without retire"] and res.code == EXIT_DRIFT


def test_planner_error_fails():
    tbl = table(gear={"values": {"tags": ["red"]}})
    a, b = live(live_id="La", ids=()), live(live_id="Lb", ids=())
    res = check([want()], [recorded(a), recorded(b)], [a, b], tbl)
    assert res.plan.errors and res.code == EXIT_DRIFT
    assert not res.drift.any()


def test_known_duplicate_reported_and_passes():
    main, twin = live(), live("Gear Twin", live_id="L2", ids=())
    res = check([want()], [recorded(main), recorded(twin)], [main, twin])
    assert res.code == EXIT_OK
    assert [(cid, lv.live_id) for cid, lv in res.plan.known_duplicates] == [("gear", "L2")]


# ---- the one managed / ignored partition ----


def test_ignored_not_in_drift_or_plan(tmp_path):
    ign = ignore_list(tmp_path)
    gear = live()
    own_then = live("Gear Works", live_id="L_own", ids=(), tags=("own",))
    own_now = replace(own_then, name="Gear Works Ltd", size=7, version="v5")
    state = [recorded(gear), recorded(own_then)]
    res = check([want()], state, [gear, own_now], ignore=ign)
    assert not res.drift.any() and res.code == EXIT_OK
    assert [lv.live_id for lv in res.ignored] == ["L_own"]
    assert res.plan.orphans == [] and [d.name for d in res.plan.unchanged] == ["Gear"]
    # an ignored row gone from the store is not deleted drift either
    assert not check([want()], state, [gear], ignore=ign).drift.any()
    # control: without the entry the same edit is drift and the row an orphan
    res = check([want()], state, [gear, own_now])
    assert res.drift.changed and res.plan.orphans == [own_now]


def test_ignored_row_on_a_desired_identity_is_a_conflict_not_untracked(tmp_path):
    ign = ignore_list(tmp_path)
    gear = live()
    own = live("Gear Works", live_id="L_own", tags=("red", "own"))  # carries "gear" too
    res = check([want()], [], [own, gear], ignore=ign)
    assert res.untracked == [] and [d.name for d in res.plan.unchanged] == ["Gear"]
    assert res.failures == [
        (
            "desired record 'Gear' shares source id 'gear' with ignored store widget "
            "'Gear Works' (ignore.yaml:2). Remove that entry or change Gear."
        )
    ]
    assert res.code == EXIT_DRIFT


def test_ignored_state_rows_are_dropped_by_id_and_by_prefix(tmp_path):
    ign = ignore_list(tmp_path, OWN + "name_prefixes:\n  - prefix: test-\n    reason: scratch\n")
    gear = live()
    scratch = live("test-1", live_id="L_t", ids=(), tags=("t",))
    state = [recorded(gear), recorded(scratch)]
    res = check([want()], state, [gear], ignore=ign)  # scratch row gone from the store
    assert not res.drift.any() and res.code == EXIT_OK


# ---- orphans, unused identities, records with no identity ----


def test_orphan_fails():
    spare = live("Spare", live_id="L9", ids=(), tags=("spare",))
    res = check([], [recorded(spare)], [spare], entries=[])
    assert res.orphans == [spare] and res.code == EXIT_DRIFT
    assert not res.drift.any()
    # without entries the orphan check does not run
    assert check([], [recorded(spare)], [spare]).orphans == []


def test_ignored_is_not_an_orphan(tmp_path):
    own = live("Gear Works", live_id="L_own", ids=(), tags=("own",))
    res = check([], [], [own], ignore=ignore_list(tmp_path), entries=[])
    assert res.orphans == [] and res.code == EXIT_OK


def test_orphan_is_a_live_row_on_an_identity_no_record_resolves_to():
    tbl = table(
        cog={"values": {"tags": ["blue"]}, "sources": {DEST: "L_cog"}},
        gear={"values": {"tags": ["red"]}, "sources": {SRC: "gear-file"}},
    )
    cog = live("Cog", live_id="L_cog", ids=("cog",), tags=("blue",))
    gear = live()
    nobody = live("Newco", live_id="L_new", ids=(), tags=("new",))
    got = orphans([cog, gear, nobody], tbl, [record()], schema=SCHEMA, spec=SPEC, hooks=HOOKS)
    assert got == [cog, nobody]


def test_record_with_no_identity_makes_its_live_row_no_orphan():
    got = orphans(
        [live(ids=())], IdentityTable(), [record()], schema=SCHEMA, spec=SPEC, hooks=HOOKS
    )
    assert got == []


def test_unused_identity_and_value_reported(tmp_path):
    tbl = table(
        gear={"values": {"tags": ["red", "OLD"]}, "sources": {SRC: "gear-file"}},
        ghost={"values": {"tags": ["boo"]}},
    )
    entries = [record()]
    held = live(tags=("red", "old"))  # still holds OLD, case-insensitively
    kw = {"schema": SCHEMA, "spec": SPEC, "hooks": HOOKS}
    assert unused_identities(tbl, entries, [held], **kw) == ["unused identity: ghost"]
    assert unused_identities(tbl, entries, [live()], **kw) == [
        "unused identity tag: gear: OLD",
        "unused identity: ghost",
    ]
    res = check([want()], [recorded(live())], [live()], tbl, entries=entries)
    assert res.code == EXIT_OK
    assert res.reports == ["unused identity tag: gear: OLD", "unused identity: ghost"]


def test_record_with_no_identity_is_reported_and_passes():
    lv = live()
    res = check([want()], [recorded(lv)], [lv], entries=[record()])
    assert res.code == EXIT_OK and res.orphans == []
    assert res.reports == ["no identity: Gear: run the import"]


def test_scoped_check_skips_stale_ignore_and_unused(tmp_path):
    ign = ignore_list(tmp_path, OWN.replace("L_own", "L_gone"))
    tbl = table(
        gear={"values": {"tags": ["red"]}, "sources": {SRC: "gear-file"}},
        ghost={"values": {"tags": ["boo"]}},
    )
    lv = live()
    args = ([want()], [recorded(lv)], [lv], tbl)
    res = check(*args, ignore=ign, entries=[record()])
    assert res.reports == [
        "stale ignore entry: ignore.yaml:2: L_gone is not in store",
        "unused identity: ghost",
    ]
    assert res.code == EXIT_OK
    scoped = check(*args, ignore=ign, entries=[record()], scope="Gear")
    assert scoped.reports == [] and scoped.code == EXIT_OK


def test_live_row_on_two_identities_fails():
    tbl = table(gear={"values": {"tags": ["red"]}}, cog={"values": {"tags": ["blue"]}})
    lv = live(ids=(), tags=("red", "blue"))
    res = check([want()], [recorded(lv)], [lv], tbl)
    assert res.failures == [
        (
            "Gear resolves to two identities: gear (tag 'red') and cog (tag 'blue') "
            "(store id L1): merge the two identities in identities.yaml, or remove the "
            "value that points at the wrong one"
        )
    ]
    assert res.code == EXIT_DRIFT


def test_hooks_are_required_with_entries():
    with pytest.raises(ValueError, match="hooks"):
        run_check(
            [], [], [], IdentityTable(), schema=SCHEMA, spec=SPEC, implicit=implicit, entries=[]
        )


# ---- offline checks ----


def test_identity_offline_errors():
    tbl = table(a={"values": {"tags": ["x"]}}, b={"values": {"tags": ["y", "X"]}})
    kw = {"spec": SPEC, "record_keys": record_keys, "record_id": lambda e: e.source_id}
    assert identity_offline_errors([], tbl, **kw) == ["tag 'x' is on two identities: a, b"]
    clean = table(a={"values": {"tags": ["x"]}}, b={"values": {"tags": ["y"]}})
    entries = [record("r1", "Both", tags=("x", "y"))]
    assert identity_offline_errors(entries, clean, **kw) == [
        "Both resolves to two identities: a (tag 'x') and b (tag 'y')"
    ]


def test_two_records_in_one_dataset_on_one_identity_fail():
    tbl = table(gear={"values": {"tags": ["red"]}})
    entries = [record("b"), record("a"), record("c", dataset=FOUND)]
    got = identity_offline_errors(
        entries,
        tbl,
        spec=SPEC,
        record_keys=record_keys,
        record_id=lambda e: "gear",
    )
    assert got == ["two records in datasets/origin resolve to gear: a.yaml, b.yaml"]


def test_manifest_errors(tmp_path):
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "a.yaml").write_text("a: 1\n")
    write_manifest(tmp_path / "out", "build")
    (tmp_path / "out" / "a.yaml").write_text("a: 2\n")
    (tmp_path / "bare").mkdir()
    assert manifest_errors(tmp_path, {"out": "build", "bare": "import"}) == [
        "hand-edited generated file: out/a.yaml: run build",
        "missing manifest: bare",
    ]
    write_manifest(tmp_path / "bare", "import")
    (tmp_path / "out" / "a.yaml").write_text("a: 1\n")
    assert manifest_errors(tmp_path, {"out": "build", "bare": "import"}) == []
