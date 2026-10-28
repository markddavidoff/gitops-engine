from __future__ import annotations

from toy import TOY_IDENTITY_SPEC as SPEC
from toy import LiveWidget, ToySchema, Widget

from gitops_engine.datasets import DatasetRecord
from gitops_engine.found import FoundBuild, build_found
from gitops_engine.identity import ItemKeys
from gitops_engine.identity_store import IdentityStore
from gitops_engine.schema import SyncMark

SCHEMA = ToySchema()
PRIMARY, FOUND = "origin", "found"
SRC, DEST = SPEC.sources  # s1 (the primary dataset's id), s2 (the destination's id)


def live(live_id: str, name: str, *, ids=(), tags=()) -> LiveWidget:
    return LiveWidget(live_id, name, "ok", list(ids), list(tags), None, "v1")


def stub_widget(live_id: str, name: str, **kw) -> Widget:
    return Widget(name=name, status="ok", mark=SyncMark(live_id, "v1", "h"), **kw)


def record_keys(entry: DatasetRecord[Widget]) -> ItemKeys:
    w = entry.record
    values = (("tags", tuple(w.tags)), ("names", (w.name,)))
    if entry.dataset == FOUND:
        live_id = w.mark.live_id if w.mark else None
        return ItemKeys(
            label=w.name, ids=(entry.source_id,), sources=((DEST, live_id),), values=values
        )
    return ItemKeys(label=w.name, sources=((SRC, entry.source_id),), values=values)


def build(managed, store, entries=(), calls=None) -> FoundBuild[Widget]:
    def stub(cid: str, lv: LiveWidget, prev: Widget | None) -> Widget:
        if calls is not None:
            calls.append((cid, lv.live_id, prev))
        return stub_widget(lv.live_id, lv.name, tags=list(lv.tags))

    return build_found(
        managed,
        store,
        list(entries),
        schema=SCHEMA,
        spec=SPEC,
        found_dataset=FOUND,
        primary_dataset=PRIMARY,
        record_keys=record_keys,
        new_id=lambda lv, stem: stem or lv.name.lower().replace(" ", "-"),
        stub=stub,
        new_values=lambda lv: {"names": [lv.name], "tags": list(lv.tags)},
        learned_values=lambda lv: {"tags": list(lv.tags)},
    )


def store_with(tmp_path, **identities: dict) -> IdentityStore:
    store = IdentityStore(tmp_path / SPEC.file, SPEC)
    for cid, kw in identities.items():
        store.add_identity(cid, **kw)
    return store


def test_stub_for_a_row_with_no_primary_record(tmp_path):
    store = store_with(tmp_path)
    got = build([live("L1", "Gear Co", tags=["red"])], store)
    assert [(cid, w.name) for cid, w in got.stubs] == [("gear-co", "Gear Co")]
    assert got.duplicates == [] and got.reports == []
    ident = store.table.entries["gear-co"]
    assert ident.sources == {DEST: "L1"}  # the primary's id is recorded
    assert ident.values == {"names": ["Gear Co"], "tags": ["red"]}


def test_found_skips_identity_with_primary_record(tmp_path):
    store = store_with(tmp_path, gear={"tags": ["red"], "sources": {SRC: "gear-file"}})
    entries = [DatasetRecord(PRIMARY, "gear-file", Widget(name="Gear", status="ok"))]
    got = build([live("L1", "Gear", tags=["red", "blue"])], store, entries)
    assert got.stubs == []
    # The row still teaches the identity what it knows.
    assert store.table.entries["gear"].sources == {SRC: "gear-file", DEST: "L1"}
    assert store.table.entries["gear"].values["tags"] == ["red", "blue"]
    # An existing identity learns only `learned_values`: the row's name is not appended.
    assert "Gear" not in store.table.entries["gear"].values.get("names", [])


def test_found_ambiguous_records_nothing(tmp_path):
    store = store_with(tmp_path, gear={"tags": ["red"]})
    store.save()
    before = {
        k: (dict(v.sources), {f: list(x) for f, x in v.values.items()})
        for k, v in store.table.entries.items()
    }
    managed = [live("L2", "Gear", tags=["red"]), live("L1", "Gear", tags=["red"])]
    got = build(managed, store)
    after = {
        k: (dict(v.sources), {f: list(x) for f, x in v.values.items()})
        for k, v in store.table.entries.items()
    }
    assert after == before
    assert store.save() is False  # the file is untouched
    [line] = got.reports
    assert line.startswith(f"{FOUND}: gear: no widget carries the canonical or recorded store id")
    assert "('Gear' (L1), 'Gear' (L2))" in line  # the group is in live-id order
    assert line.endswith(f"nothing recorded: record one in {SPEC.file} or merge in store")
    # The stub, if the identity needs one, comes from the lowest id.
    assert [(cid, w.mark.live_id) for cid, w in got.stubs] == [("gear", "L1")]


def test_found_reports_two_rows_carrying_the_canonical_id(tmp_path):
    store = store_with(tmp_path, gear={"tags": ["red"]})
    managed = [live("L1", "Gear A", ids=["gear"]), live("L2", "Gear B", ids=["gear"])]
    got = build(managed, store)
    [line] = got.reports
    assert line == (
        f"{FOUND}: two or more store widgets carry the canonical id gear: 'Gear A' (L1), "
        "'Gear B' (L2): merge them in store or remove the id from all but one; nothing recorded"
    )
    assert store.table.entries["gear"].sources == {}


def test_found_names_the_known_duplicates(tmp_path):
    store = store_with(tmp_path, gear={"tags": ["red"]})
    managed = [live("L1", "Gear", ids=["gear"], tags=["red"]), live("L2", "Gear", tags=["red"])]
    got = build(managed, store)
    assert got.duplicates == ["gear: 'Gear' (L2) is a known duplicate; merge it in store"]
    assert store.table.entries["gear"].sources == {DEST: "L1"}


def test_found_keeps_previous_stub(tmp_path):
    store = store_with(tmp_path, gear={"tags": ["red"], "sources": {DEST: "L1"}})
    old = stub_widget("L1", "Old Name")
    entries = [DatasetRecord(FOUND, "gear", old)]
    calls: list = []
    got = build([live("L1", "New Name", tags=["red"])], store, entries, calls)
    assert calls == [("gear", "L1", old)]  # the previous record, as stored
    assert [cid for cid, _ in got.stubs] == ["gear"]


def test_found_stem_survives_a_rename_and_a_new_row_gets_no_previous(tmp_path):
    store = store_with(tmp_path)
    entries = [DatasetRecord(FOUND, "kept-stem", stub_widget("L1", "Was Called This"))]
    calls: list = []
    got = build([live("L1", "Renamed"), live("L2", "Other")], store, entries, calls)
    # L1 resolves through its old stem, so a rename does not move its id.
    assert [cid for cid, _ in got.stubs] == ["kept-stem", "other"]
    assert [(c, p is None) for c, _, p in calls] == [("kept-stem", False), ("other", True)]


def test_stubs_sorted_by_canonical_id(tmp_path):
    store = store_with(tmp_path)
    got = build([live("L1", "Zed"), live("L2", "Alpha")], store)
    assert [cid for cid, _ in got.stubs] == ["alpha", "zed"]
