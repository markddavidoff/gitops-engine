from __future__ import annotations

import dataclasses

import pytest
from toy import LiveWidget, ToySchema, Widget

from gitops_engine.adapter import SyncMark as AdapterSyncMark
from gitops_engine.schema import (
    Always,
    FieldSpec,
    KeepIfUnset,
    Nouns,
    RecordSchema,
    SyncMark,
    Union,
)


def _live(**over) -> LiveWidget:
    base = {
        "live_id": "w1",
        "name": "Gear",
        "status": "active",
        "ids": ["g-1"],
        "tags": ["a", "b"],
        "size": 3,
        "version": "v1",
    }
    return LiveWidget(**{**base, **over})


def test_toy_schema_is_a_record_schema():
    schema = ToySchema()
    assert isinstance(schema, RecordSchema)
    lv = _live()
    assert schema.live_hash(lv) == schema.mark(schema.mirror_row(lv)).content_hash


def test_mirror_row_carries_the_live_mark():
    schema = ToySchema()
    row = schema.mirror_row(_live())
    assert schema.mark(row) == SyncMark("w1", "v1", schema.live_hash(_live()))
    assert schema.state_to_live(row) == _live()


def test_live_hash_ignores_version_and_id_but_not_content():
    schema = ToySchema()
    base = schema.live_hash(_live())
    assert schema.live_hash(_live(version="v9", live_id="other")) == base
    assert schema.live_hash(_live(size=4)) != base


def test_toy_fields_and_nouns():
    schema = ToySchema()
    assert schema.nouns == Nouns("widget", "widgets", "store", source="git")
    assert Nouns().source == "source"
    by_name = {f.name: f for f in schema.fields}
    assert [f.name for f in schema.fields] == ["name", "status", "ids", "tags", "size"]
    assert by_name["name"].policy == Always()
    assert by_name["status"] == FieldSpec("status", Always(), required=True)
    assert by_name["ids"].policy == Always(unordered=True)
    assert schema.id_field == "ids"
    assert by_name["tags"].policy == Union(exclusive=True)
    assert by_name["size"].policy == KeepIfUnset()


def test_value_accessors_and_with_values():
    schema = ToySchema()
    d = Widget(name="Gear", status="active", ids=["g-1"], tags=[], size=None, owned=True)
    assert schema.desired_value(d, "ids") == ["g-1"]
    assert schema.live_value(_live(), "size") == 3
    assert schema.label(d) == "Gear" and schema.live_label(_live()) == "Gear"
    assert schema.destination_owned(d) is True
    assert schema.live_id(_live()) == "w1" and schema.live_version(_live()) == "v1"
    changed = schema.with_values(d, {"size": 9, "tags": ["x"]})
    assert (changed.size, changed.tags, changed.name) == (9, ["x"], "Gear")
    assert d.size is None  # the input is not mutated


def test_snapshot_round_trip():
    schema = ToySchema()
    lv = _live()
    row = schema.snapshot_row(lv)
    assert row[schema.snapshot_id_key] == "w1" and row[schema.snapshot_version_key] == "v1"
    values = schema.snapshot_values(row)
    assert set(values) == {f.name for f in schema.fields}
    assert all(values[f.name] == schema.live_value(lv, f.name) for f in schema.fields)


def test_nouns_defaults_and_frozen_policies():
    assert Nouns() == Nouns("record", "records", "destination")
    assert Union().key("AbC") == "abc" and Union().exclusive is False
    assert Always().unordered is False
    with pytest.raises(dataclasses.FrozenInstanceError):
        Always().unordered = True  # type: ignore[misc]
    assert FieldSpec("x", KeepIfUnset()).required is False


def test_sync_mark_is_shared_with_the_v010_adapter_module():
    assert AdapterSyncMark is SyncMark


def test_toy_keys_reduce_desired_and_live_rows_to_item_keys():
    from gitops_engine.identity import ItemKeys

    schema = ToySchema()
    want = Widget(name="Gear", status="active", ids=["g-1"], tags=["a", "b"])
    assert schema.desired_keys(want) == ItemKeys(
        label="Gear", ids=("g-1",), values=(("tags", ("a", "b")),)
    )
    assert schema.live_keys(_live()) == ItemKeys(
        label="Gear", ids=("g-1",), values=(("tags", ("a", "b")),)
    )
