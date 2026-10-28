"""A toy record schema, plus a walkthrough of the v0.1.0 reconcile lifecycle.

`Widget` and `LiveWidget` are the smallest records that exercise every field policy;
`ToySchema` is their `RecordSchema`. The walkthrough at the bottom runs against the
in-memory `ToyAdapter` and goes away with the v0.1.0 `Adapter`.

Run from a clone:  uv run python examples/toy.py
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from typing import Any

from gitops_engine.identity import IdentitySpec, ItemKeys, MatchKey
from gitops_engine.schema import (
    Always,
    FieldSpec,
    KeepIfUnset,
    Nouns,
    SyncMark,
    Union,
)


@dataclass
class Widget:
    """Desired spec (and, with `mark` set, a recorded state row)."""

    name: str
    status: str
    ids: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    size: int | None = None
    mark: SyncMark | None = None
    owned: bool = False  # the store owns this row's fields


@dataclass
class LiveWidget:
    """A widget as the store reports it."""

    live_id: str
    name: str
    status: str
    ids: list[str]
    tags: list[str]
    size: int | None
    version: str


def widget_to_dict(w: Widget) -> dict[str, Any]:
    return {"name": w.name, "status": w.status, "ids": w.ids, "tags": w.tags, "size": w.size}


def widget_from_dict(data: dict[str, Any]) -> Widget:
    return Widget(
        name=data["name"],
        status=data["status"],
        ids=list(data.get("ids") or []),
        tags=list(data.get("tags") or []),
        size=data.get("size"),
    )


def _hash(name: str, status: str, ids: list[str], tags: list[str], size: int | None) -> str:
    """Hash the deployable fields only: never `version`, `live_id` or the mark."""
    blob = json.dumps(
        {"name": name, "status": status, "ids": sorted(ids), "tags": sorted(tags), "size": size},
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode()).hexdigest()


def _norm_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())


# Resolution order: canonical id, then source `s1`, then `s2`, then tags, then names.
TOY_IDENTITY_SPEC = IdentitySpec(
    sources=("s1", "s2"),
    match_keys=(
        MatchKey("tags", "tag", str.lower),
        MatchKey("names", "name", _norm_name, skip_empty=True),
    ),
    destination_source="s2",
    file_order=("names", "tags", "sources"),
)


class ToySchema:
    """`RecordSchema[Widget, LiveWidget]`."""

    nouns = Nouns("widget", "widgets", "store")
    fields = (
        FieldSpec("name", Always()),
        FieldSpec("status", Always(), required=True),
        FieldSpec("ids", Always(unordered=True)),
        FieldSpec("tags", Union(exclusive=True)),
        FieldSpec("size", KeepIfUnset()),
    )
    id_field = "ids"
    snapshot_id_key = "live_id"
    snapshot_version_key = "version"

    def desired_value(self, d: Widget, field: str) -> Any:
        return getattr(d, field)

    def live_value(self, lv: LiveWidget, field: str) -> Any:
        return getattr(lv, field)

    def label(self, d: Widget) -> str:
        return d.name

    def live_label(self, lv: LiveWidget) -> str:
        return lv.name

    def live_id(self, lv: LiveWidget) -> str:
        return lv.live_id

    def live_version(self, lv: LiveWidget) -> str:
        return lv.version

    def desired_keys(self, d: Widget) -> ItemKeys:
        return ItemKeys(label=d.name, ids=tuple(d.ids), values=(("tags", tuple(d.tags)),))

    def live_keys(self, lv: LiveWidget) -> ItemKeys:
        return ItemKeys(label=lv.name, ids=tuple(lv.ids), values=(("tags", tuple(lv.tags)),))

    def mark(self, d: Widget) -> SyncMark | None:
        return d.mark

    def destination_owned(self, d: Widget) -> bool:
        return d.owned

    def with_values(self, d: Widget, values: dict[str, Any]) -> Widget:
        return replace(d, **values)

    def mirror_row(self, lv: LiveWidget) -> Widget:
        return Widget(
            name=lv.name,
            status=lv.status,
            ids=list(lv.ids),
            tags=list(lv.tags),
            size=lv.size,
            mark=SyncMark(lv.live_id, lv.version, self.live_hash(lv)),
        )

    def live_hash(self, lv: LiveWidget) -> str:
        return _hash(lv.name, lv.status, lv.ids, lv.tags, lv.size)

    def state_to_live(self, s: Widget) -> LiveWidget:
        assert s.mark is not None, "a state row carries its mark"
        return LiveWidget(
            live_id=s.mark.live_id,
            name=s.name,
            status=s.status,
            ids=list(s.ids),
            tags=list(s.tags),
            size=s.size,
            version=s.mark.version,
        )

    def snapshot_row(self, lv: LiveWidget) -> dict[str, Any]:
        return {
            self.snapshot_id_key: lv.live_id,
            self.snapshot_version_key: lv.version,
            **{f.name: getattr(lv, f.name) for f in self.fields},
        }

    def snapshot_values(self, row: dict[str, Any]) -> dict[str, Any]:
        return {f.name: row[f.name] for f in self.fields}


# --- walkthrough (v0.1.0 `Adapter`; removed with it) ---


def main() -> None:
    from toy_adapter import ToyAdapter, live_widget, state_row, widget

    from gitops_engine import EXIT_DRIFT, EXIT_OK, apply, run_check

    # The external system already holds one widget; git wants two.
    adapter = ToyAdapter([live_widget("id1", "gear", "Gear", 1, "v1")])
    desired = [widget("gear", "Gear", 5), widget("cog", "Cog", 2)]

    # 1. Observe the external system and record a state row (with a SyncMark) per
    #    live row. Unrecorded live rows would otherwise show up as "added" drift.
    live = list(adapter.rows.values())
    state = [state_row(lv) for lv in live]

    # 2. Check: state matches live, so no drift; the plan holds one update, one create.
    code, drift, plan = run_check(desired, state, live, adapter)
    print(
        "first check:",
        code,
        "| creates",
        [d.ids[0] for d in plan.creates],
        "| updates",
        [d.ids[0] for d, _ in plan.updates],
    )
    assert code == EXIT_OK and not plan.aborts

    # 3. Apply, then 4. re-record state from the rows apply returned.
    written = apply(plan, adapter)
    assert not plan.aborts  # apply appends write-time aborts to the plan
    state = [state_row(lv) for lv in written]

    # Re-observe and check again: everything reconciles.
    live = list(adapter.rows.values())
    code, drift, plan = run_check(desired, state, live, adapter)
    print("after apply:", code, "| unchanged", [d.ids[0] for d in plan.unchanged])
    assert code == EXIT_OK

    # Someone edits "gear" in the external system, out of band.
    adapter.rows["id1"] = live_widget("id1", "gear", "Gear", 9, "v9")
    live = list(adapter.rows.values())
    code, drift, plan = run_check(desired, state, live, adapter)
    print(
        "after out-of-band edit:",
        code,
        "| changed",
        [s.ids[0] for s, _ in drift.changed],
        "| aborts",
        [(d.ids[0], why) for d, _, why in plan.aborts],
    )
    assert code == EXIT_DRIFT


if __name__ == "__main__":
    main()
