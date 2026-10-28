"""Pre-apply snapshots and restore."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from toy import TOY_IDENTITY_SPEC as SPEC
from toy import LiveWidget, ToySchema, Widget
from toy_adapter import ToyDestination

from gitops_engine.deploy import apply_deploy, plan_deploy
from gitops_engine.identity import Identity, IdentityTable
from gitops_engine.snapshot import (
    RestoreResult,
    load_snapshot,
    record_written,
    restore,
    write_snapshot,
)

SCHEMA = ToySchema()
FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 9, 3, 12, 0, 0, tzinfo=UTC)


def live(name="Gear", *, live_id="L1", status="ok", ids=None, tags=(), size=None, version="v1"):
    ids = [name.lower()] if ids is None else list(ids)
    return LiveWidget(live_id, name, status, ids, list(tags), size, version)


def implicit(w: Widget, taken: set[str]) -> Identity:
    return Identity(w.ids[0], values={"names": [w.name], "tags": list(w.tags)}, sources={})


def updates(dest: ToyDestination) -> list[tuple]:
    return [c for c in dest.calls if c[0] == "update"]


def test_write_load_and_record_round_trip(tmp_path):
    rows = [live(tags=("a.example",), size=3), live("Cog", live_id="L2", status="")]
    path = write_snapshot(rows, tmp_path / "s", schema=SCHEMA, now=NOW)
    assert path == tmp_path / "s" / "20260903T120000Z.yaml"
    raw = yaml.safe_load(path.read_text())
    assert raw["taken_at"] == "2026-09-03T12:00:00Z"
    assert list(raw["rows"][0]) == [
        "live_id",
        "version",
        "name",
        "status",
        "ids",
        "tags",
        "size",
        "written_version",
    ]
    assert raw["rows"][0]["written_version"] is None
    record_written(path, "L2", "v9", schema=SCHEMA)
    loaded = load_snapshot(path, schema=SCHEMA)
    assert [r["written_version"] for r in loaded] == [None, "v9"]
    assert loaded[0]["tags"] == ["a.example"] and loaded[1]["status"] == ""


def test_second_snapshot_in_same_second_does_not_overwrite(tmp_path):
    first = write_snapshot([], tmp_path, schema=SCHEMA, now=NOW)
    second = write_snapshot([], tmp_path, schema=SCHEMA, now=NOW)
    third = write_snapshot([], tmp_path, schema=SCHEMA, now=NOW)
    assert [p.name for p in (first, second, third)] == [
        "20260903T120000Z.yaml",
        "20260903T120000Z-1.yaml",
        "20260903T120000Z-2.yaml",
    ]


def test_load_rejects_unknown_keys_and_reads_null_lists_as_empty(tmp_path):
    path = tmp_path / "s.yaml"
    row = {"live_id": "L1", "version": "v1", "name": "G", "status": "ok", "size": None}
    path.write_text(yaml.safe_dump({"taken_at": "t", "rows": [{**row, "ids": None, "tags": None}]}))
    [loaded] = load_snapshot(path, schema=SCHEMA)
    assert loaded["ids"] == [] and loaded["tags"] == []
    path.write_text(yaml.safe_dump({"taken_at": "t", "rows": [{**row, "colour": "red"}]}))
    with pytest.raises(ValueError, match=r"unknown snapshot key\(s\): \['colour'\]"):
        load_snapshot(path, schema=SCHEMA)


def test_snapshot_records_written_version(tmp_path):
    lv = live(size=1)
    p = plan_deploy(
        [Widget("Gear", "ok", ["gear"], size=5)],
        [lv],
        IdentityTable(),
        schema=SCHEMA,
        spec=SPEC,
        implicit=implicit,
    )
    dest = ToyDestination([lv])
    res = apply_deploy(p, dest, schema=SCHEMA, snapshot_dir=tmp_path)
    [row] = load_snapshot(res.snapshot, schema=SCHEMA)
    assert row["version"] == "v1" and row["size"] == 1
    assert row["written_version"] == dest.rows["L1"].version


def _snap(tmp_path, rows, written=None):
    path = write_snapshot(rows, tmp_path, schema=SCHEMA, now=NOW)
    for live_id, version in (written or {}).items():
        record_written(path, live_id, version, schema=SCHEMA)
    return path


def test_restore_round_trip(tmp_path):
    before = live(size=1, tags=("a.example",))
    path = _snap(tmp_path, [before], {"L1": "v2"})
    dest = ToyDestination([live(size=5, version="v2")])
    res = restore(path, dest, schema=SCHEMA)
    assert res == RestoreResult(restored=["L1"], skipped=[])
    assert updates(dest) == [
        (
            "update",
            "L1",
            {"name": "Gear", "status": "ok", "ids": ["gear"], "tags": ["a.example"], "size": 1},
            False,
        )
    ]
    assert dest.calls[0] == ("preflight", [updates(dest)[0][2]], True)


def test_restore_sends_empties_with_retire_and_skips_empty_status(tmp_path):
    path = _snap(tmp_path, [live(status="", tags=(), size=None)])
    dest = ToyDestination([live(status="ok", size=4)])
    restore(path, dest, schema=SCHEMA)
    assert updates(dest) == [
        ("update", "L1", {"name": "Gear", "ids": ["gear"], "tags": [], "size": None}, True)
    ]


def test_restore_skips_edited_since(tmp_path):
    path = _snap(tmp_path, [live(live_id="L1"), live("Cog", live_id="L2")], {"L1": "v2"})
    dest = ToyDestination(
        [live(live_id="L1", version="v3"), live("Cog", live_id="L2", version="v7")]
    )
    res = restore(path, dest, schema=SCHEMA)
    assert res.restored == [] and updates(dest) == []
    assert res.skipped == [
        ("L1", "edited since snapshot: version v3 != v2"),
        ("L2", "edited since snapshot: version v7 != v1"),
    ]


def test_restore_skips_row_gone(tmp_path):
    path = _snap(tmp_path, [live()])
    res = restore(path, ToyDestination([]), schema=SCHEMA)
    assert res.skipped == [("L1", "gone")]


def test_restore_filters_rows_by_id(tmp_path):
    path = _snap(tmp_path, [live(), live("Cog", live_id="L2")])
    dest = ToyDestination([live(), live("Cog", live_id="L2")])
    res = restore(path, dest, schema=SCHEMA, rows=["cog"])
    assert res.restored == ["L2"]


def test_restore_preflight_failure_restores_nothing(tmp_path):
    path = _snap(tmp_path, [live(status="weird")])
    dest = ToyDestination([live()], statuses={"ok"})
    with pytest.raises(ValueError, match="nothing restored"):
        restore(path, dest, schema=SCHEMA)
    assert updates(dest) == []


def test_restore_stops_at_first_destination_error(tmp_path):
    path = _snap(
        tmp_path, [live(live_id="L1"), live("Cog", live_id="L2"), live("Bolt", live_id="L3")]
    )
    rows = [live(live_id="L1"), live("Cog", live_id="L2"), live("Bolt", live_id="L3")]
    dest = ToyDestination(rows, fail={"L2": "rate limited"})
    res = restore(path, dest, schema=SCHEMA)
    assert res.restored == ["L1"] and res.failure == "L2: rate limited"
    assert [c[1] for c in updates(dest)] == ["L1", "L2"]


def test_restore_v010_snapshot(tmp_path):
    # Written by v0.1.0's write_snapshot + record_written, keys renamed to the toy's.
    # v0.1.0 restored it with exactly these writes (its tier argument has no toy field).
    path = FIXTURES / "snapshot-v010.yaml"
    dest = ToyDestination(
        [
            live("x", live_id="lin_a", version="2026-09-03T12:00:05.000Z"),
            live("y", live_id="lin_b", version="2026-09-02T11:00:00.000Z"),
        ]
    )
    res = restore(path, dest, schema=SCHEMA)
    assert res == RestoreResult(restored=["lin_a", "lin_b"], skipped=[])
    assert updates(dest) == [
        (
            "update",
            "lin_a",
            {
                "name": "Gadget Works",
                "tags": ["gadget.example.test", "Gadget.example.org"],
                "ids": ["gadget"],
                "size": 1200,
                "status": "Active",
            },
            False,
        ),
        (
            "update",
            "lin_b",
            {"name": "Sprocket Ltd", "tags": [], "ids": ["sprocket", "sprk"], "size": None},
            True,
        ),
    ]


def test_record_written_keeps_v010_bytes(tmp_path):
    src = (FIXTURES / "snapshot-v010.yaml").read_text()
    path = tmp_path / "s.yaml"
    path.write_text(src)
    record_written(path, "lin_b", None, schema=SCHEMA)  # rewrite with no change
    assert path.read_text() == src
