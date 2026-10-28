"""The state mirror: one row per live row, sorted by label."""

from __future__ import annotations

from toy import LiveWidget, ToySchema

from gitops_engine.mirror import reconcile_mirror

SCHEMA = ToySchema()


def live(live_id: str, name: str, **kw) -> LiveWidget:
    base = {"status": "ok", "ids": [], "tags": [], "size": None, "version": "v1"}
    return LiveWidget(live_id, name, **{**base, **kw})


def test_mirror_sorted_by_label_casefold():
    rows = [live("1", "beta"), live("2", "Alpha"), live("3", "Gamma"), live("4", "alpha2")]
    assert [w.name for w in reconcile_mirror(rows, SCHEMA)] == ["Alpha", "alpha2", "beta", "Gamma"]


def test_mirror_rows_carry_their_live_mark():
    [row] = reconcile_mirror([live("L1", "Gear", tags=["red"], size=3, version="v9")], SCHEMA)
    assert row.mark is not None
    assert (row.mark.live_id, row.mark.version) == ("L1", "v9")
    assert row.mark.content_hash == SCHEMA.live_hash(live("L1", "Gear", tags=["red"], size=3))


def test_mirror_is_idempotent_and_keeps_equal_labels_in_live_order():
    rows = [live("2", "Gear"), live("1", "gear"), live("3", "Cog")]
    first = reconcile_mirror(rows, SCHEMA)
    assert [w.mark.live_id for w in first] == ["3", "2", "1"]
    assert reconcile_mirror(rows, SCHEMA) == first
    assert reconcile_mirror([], SCHEMA) == []
