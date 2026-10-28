from __future__ import annotations

from dataclasses import replace

from toy import TOY_IDENTITY_SPEC as SPEC
from toy import Widget

from gitops_engine.datasets import DatasetRecord
from gitops_engine.identity import Identity, IdentityTable, ItemKeys, resolve
from gitops_engine.identity_store import IdentityStore
from gitops_engine.merge import group_by_identity, merge_by_identity


def table_of(tmp_path, **identities: dict) -> IdentityTable:
    store = IdentityStore(tmp_path / SPEC.file, SPEC)
    for cid, kw in identities.items():
        store.add_identity(cid, **kw)
    return store.table


def record_id_for(table: IdentityTable):
    """The consumer's rule: the identity a record resolves to, else its own file stem."""

    def record_id(entry: DatasetRecord[Widget]) -> str:
        keys = ItemKeys(
            label=entry.record.name,
            sources=((SPEC.sources[0], entry.source_id),),
            values=(("tags", tuple(entry.record.tags)), ("names", (entry.record.name,))),
        )
        hit = resolve(keys, table, SPEC)
        return hit.canonical_id if hit else entry.source_id

    return record_id


def rec(dataset: str, stem: str, name: str, **kw) -> DatasetRecord[Widget]:
    kw.setdefault("status", "ok")
    return DatasetRecord(dataset, stem, Widget(name=name, **kw))


def test_earliest_dataset_wins_whole(tmp_path):
    table = table_of(tmp_path, gear={"names": ["Gear"], "tags": ["red"]})
    entries = [
        rec("beta", "g1", "Gear", tags=["red"], size=9, status="late"),
        rec("alpha", "g2", "Gear Co", tags=["red"], status="early"),  # no size
    ]
    [(cid, winner)] = merge_by_identity(
        entries,
        table,
        ["alpha", "beta"],
        record_id=record_id_for(table),
        gather=lambda r, ident: r,
    )
    assert cid == "gear"
    assert (winner.dataset, winner.source_id) == ("alpha", "g2")
    # Whole record: nothing is mixed in from the losing dataset.
    assert winner.record == Widget(name="Gear Co", status="early", tags=["red"], size=None)


def test_ties_break_by_source_id_and_unlisted_datasets_rank_last(tmp_path):
    table = table_of(tmp_path, gear={"names": ["Gear"]})
    entries = [
        rec("other", "a", "Gear"),
        rec("alpha", "z", "Gear"),
        rec("alpha", "b", "Gear"),
    ]
    groups = group_by_identity(entries, table, ["alpha"], record_id=record_id_for(table))
    assert [(e.dataset, e.source_id) for e in groups["gear"]] == [
        ("alpha", "b"),
        ("alpha", "z"),
        ("other", "a"),
    ]


def test_winners_come_out_in_group_order(tmp_path):
    table = table_of(tmp_path)
    entries = [rec("alpha", "q", "Q"), rec("alpha", "p", "P"), rec("beta", "q", "Q2")]
    got = merge_by_identity(
        entries, table, ["alpha", "beta"], record_id=record_id_for(table), gather=lambda r, i: r
    )
    assert [cid for cid, _ in got] == ["q", "p"]


def test_gather_adds_identity_values(tmp_path):
    table = table_of(tmp_path, gear={"names": ["Gear"], "tags": ["red", "blue"]})
    seen: list[Identity | None] = []

    def gather(record: Widget, ident: Identity | None) -> Widget:
        seen.append(ident)
        extra = [t for t in (ident.values["tags"] if ident else []) if t not in record.tags]
        return replace(record, tags=[*record.tags, *extra])

    entries = [rec("alpha", "g", "Gear", tags=["red"]), rec("alpha", "loose", "Loose")]
    got = merge_by_identity(
        entries, table, ["alpha"], record_id=record_id_for(table), gather=gather
    )
    by_id = {cid: w.record for cid, w in got}
    assert by_id["gear"].tags == ["red", "blue"]
    assert by_id["loose"].tags == []
    assert seen == [table.entries["gear"], None]  # an id with no table entry gets None
