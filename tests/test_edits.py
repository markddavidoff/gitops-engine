"""Destination edits on the toy schema: pairing, detection and recording."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from toy import TOY_OVERRIDES as FMT
from toy import LiveWidget, OwnedToySchema, Widget

from gitops_engine.edits import Edit, OldPair, detect_edits, pair_old_state, record_edits
from gitops_engine.overrides import OverridesStore
from gitops_engine.schema import SyncMark

SCHEMA = OwnedToySchema()


def old(*, name="Gear", status="ok", tags=(), size=None, ids=("w1",), live_id="L1") -> Widget:
    """A state row: what the mirror recorded for live row `live_id`."""
    return Widget(
        name, status, ids=list(ids), tags=list(tags), size=size, mark=SyncMark(live_id, "v1", "h")
    )


def live(*, name="Gear", status="ok", tags=(), size=None, ids=("w1",), live_id="L1") -> LiveWidget:
    return LiveWidget(live_id, name, status, list(ids), list(tags), size, "v2")


def desired(*, name="Gear", status="ok", tags=(), size=None, cid="w1", owned=False) -> Widget:
    return Widget(name, status, ids=[cid], tags=list(tags), size=size, owned=owned)


def detect(o: Widget, lv: LiveWidget, d: Widget, **kw: Any) -> list[Edit]:
    return detect_edits([o], [lv], [d], schema=SCHEMA, **kw)


def test_cleared_optional_is_absorbed() -> None:
    got = detect(old(size=5), live(size=None), desired(size=5))
    assert got == [Edit("w1", "size", "clear", None)]


def test_removed_list_value_is_absorbed() -> None:
    got = detect(old(tags=["a", "b"]), live(tags=["a"]), desired(tags=["a", "b"]))
    assert got == [Edit("w1", "tags", "remove", "b")]


def test_changed_scalar_is_pending() -> None:
    got = detect(old(size=5), live(size=6), desired(size=5))
    assert got == [Edit("w1", "size", "pending", 6)]


def test_added_list_value_is_pending() -> None:
    got = detect(old(tags=["a"]), live(tags=["a", "c"]), desired(tags=["a"]))
    assert got == [Edit("w1", "tags", "pending", "c")]


def test_status_change_pending_and_status_cleared_written_back() -> None:
    changed = detect(old(status="ok"), live(status="broken"), desired(status="ok"))
    assert changed == [Edit("w1", "status", "pending", "broken")]
    # `status` is required, so it cannot clear: the next deploy writes it back.
    assert detect(old(status="ok"), live(status=""), desired(status="ok")) == []


def test_live_equal_desired_is_not_an_edit() -> None:
    # A deploy's own write seen against a stale mirror.
    assert detect(old(size=5), live(size=7), desired(size=7)) == []
    assert detect(old(tags=["a"]), live(tags=["a", "b"]), desired(tags=["a", "b"])) == []
    assert detect(old(tags=["a", "b"]), live(tags=["a"]), desired(tags=["a"])) == []


def test_case_only_list_change_is_not_an_edit() -> None:
    # A value re-cased at the destination: no remove, no pending add.
    got = detect(old(tags=["Acme.test"]), live(tags=["acme.test"]), desired(tags=["Acme.test"]))
    assert got == []


def test_base_hook() -> None:
    def base(field: str, value: Any) -> Any:
        if field == "name" and isinstance(value, str):
            return re.sub(r" \[[xy]\]$", "", value)
        return value

    def name_edits(live_name: str) -> list[Edit]:
        return detect(old(name="A [x]"), live(name=live_name), desired(name="A [x]"), base=base)

    assert name_edits("A [y]") == []  # only the suffix changed
    assert name_edits("A") == []  # the suffix was deleted
    assert name_edits("B [x]") == [Edit("w1", "name", "pending", "B")]


def test_id_field_stub_and_unpaired_skipped() -> None:
    # A changed id field is never an edit: the next deploy writes it back.
    assert detect(old(ids=["w1"]), live(ids=["w1", "zz"]), desired()) == []
    # A destination-owned (stub) row is skipped.
    assert detect(old(size=5), live(size=6), desired(size=5, owned=True)) == []
    # No live row for the mark, or no desired row for the canonical id.
    assert detect(old(size=5), live(size=6, live_id="L9"), desired(size=5)) == []
    assert detect(old(size=5), live(size=6), desired(size=5, cid="w2")) == []


def test_edits_ordered_by_id_then_field_then_value() -> None:
    olds = [old(ids=["w2"], live_id="L2", size=1), old(tags=["a"], size=1)]
    lives = [live(ids=["w2"], live_id="L2", size=2), live(tags=["c", "b"], size=None)]
    desireds = [desired(cid="w2", size=1), desired(tags=["a"], size=1)]
    got = detect_edits(olds, lives, desireds, schema=SCHEMA)
    assert got == [
        Edit("w1", "tags", "remove", "a"),
        Edit("w1", "tags", "pending", "b"),
        Edit("w1", "tags", "pending", "c"),
        Edit("w1", "size", "clear", None),
        Edit("w2", "size", "pending", 2),
    ]


def test_pair_old_state_skips_unpaired_and_owned() -> None:
    o1 = old(ids=["w1"], live_id="L1")
    o2 = old(ids=["x", "w2"], live_id="L2")  # pairs by any of its ids
    o3 = old(ids=["w3"], live_id="L3")  # desired row is a stub
    o4 = old(ids=["w4"], live_id="L4")  # no desired row
    lv1 = live(live_id="L1")
    d1, d2, d3 = desired(cid="w1"), desired(cid="w2"), desired(cid="w3", owned=True)
    got = pair_old_state([o1, o2, o3, o4], [lv1], [d1, d2, d3], schema=SCHEMA)
    assert got == [OldPair(o1, d1, "L1", lv1), OldPair(o2, d2, "L2", None)]


def test_record_edits(tmp_path: Path) -> None:
    path = tmp_path / FMT.file
    path.write_text("widgets:\n  w1:\n    tags: {add: [b]}\n    pending: {size: 9}\n")
    store = OverridesStore(path, FMT)
    first = [Edit("w1", "size", "clear", None), Edit("w1", "tags", "remove", "b")]
    assert record_edits(store, first) == 2
    entry = store.overrides.entries["w1"]
    assert entry.pin == {"size": None}
    assert entry.pending == {}
    assert entry.add == {} and entry.remove == {}  # the value came from `add`

    more = [
        Edit("w1", "size", "clear", None),  # already recorded
        Edit("w1", "tags", "remove", "z"),
        Edit("w1", "name", "pending", "Cog"),
        Edit("w1", "tags", "pending", "d"),
    ]
    assert record_edits(store, more) == 3
    entry = store.overrides.entries["w1"]
    assert entry.remove == {"tags": ["z"]}
    assert entry.pending == {"name": "Cog", "tags_add": ["d"]}
    assert record_edits(store, more) == 0
