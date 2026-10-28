"""A toy v0.1.0 adapter over an in-memory dict of "widget" rows.

The engine is domain-free: it only talks to an `Adapter`. This toy adapter is the
smallest thing that exercises every engine path. A `Widget` is both the desired
spec and, when it carries a `mark`, a recorded state row (mirroring how a real
record type carries its last-reconciled mark). A `LiveWidget` is what the external
system reports, keyed by `live_id` and carrying an opaque `version` token.

The adapter matches on a widget's first id (its "key") and hashes its name and size
(its "value"). `widget` and `live_widget` build rows in that v0.1.0 shape.
"""

from __future__ import annotations

import hashlib
import json

from toy import LiveWidget, Widget

from gitops_engine.schema import SyncMark


def widget(key: str, name: str, value: int, mark: SyncMark | None = None) -> Widget:
    return Widget(name=name, status="active", ids=[key], size=value, mark=mark)


def live_widget(live_id: str, key: str, name: str, value: int, version: str) -> LiveWidget:
    return LiveWidget(live_id, name, "active", [key], [], value, version)


def content_hash(key: str, name: str, value: int) -> str:
    """Hash the deployable fields only — never `version`, `live_id`, or the mark."""
    blob = json.dumps({"key": key, "name": name, "value": value}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def state_row(lv: LiveWidget) -> Widget:
    """Build the state row recorded after observing a live row."""
    return widget(
        lv.ids[0],
        lv.name,
        lv.size,
        SyncMark(lv.live_id, lv.version, content_hash(lv.ids[0], lv.name, lv.size)),
    )


class ToyAdapter:
    """In-memory widget store the engine reconciles against."""

    def __init__(
        self,
        live_rows: list[LiveWidget] | None = None,
        *,
        resolvable: set[str] | None = None,
        fail_create_key: str | None = None,
    ) -> None:
        # `rows` backs `get`; it is the write-time view, independent of the `live`
        # list a plan was built from, so a test can simulate drift between the two.
        self.rows: dict[str, LiveWidget] = {lv.live_id: lv for lv in (live_rows or [])}
        self.created: list[Widget] = []
        self.updated: list[tuple[str, Widget]] = []
        self.deleted: list[str] = []
        self._resolvable = resolvable  # None => preflight always passes
        self._fail_create_key = fail_create_key

    # --- reads / identity ---
    def match(self, d: Widget, live: list[LiveWidget]) -> LiveWidget | None:
        for lv in live:  # first match wins
            if lv.ids[0] == d.ids[0]:
                return lv
        return None

    def desired_hash(self, d: Widget) -> str:
        return content_hash(d.ids[0], d.name, d.size)

    def live_hash(self, lv: LiveWidget) -> str:
        return content_hash(lv.ids[0], lv.name, lv.size)

    def live_id(self, lv: LiveWidget) -> str:
        return lv.live_id

    def live_version(self, lv: LiveWidget) -> str:
        return lv.version

    def mark_for(self, d: Widget, lv: LiveWidget, state: list[Widget]) -> SyncMark | None:
        st = next((s for s in state if s.ids[0] == d.ids[0]), None)
        if st is None:
            st = next(
                (s for s in state if s.mark is not None and s.mark.live_id == lv.live_id),
                None,
            )
        return st.mark if st is not None else None

    def state_mark(self, s: Widget) -> SyncMark | None:
        return s.mark

    # --- writes ---
    def preflight(self, plan) -> None:
        if self._resolvable is None:
            return
        needed = {d.ids[0] for d in plan.creates} | {d.ids[0] for d, _ in plan.updates}
        missing = sorted(needed - self._resolvable)
        if missing:
            raise ValueError(f"unresolvable widget(s), nothing written: {missing}")

    def get(self, live_id: str) -> LiveWidget | None:
        return self.rows.get(live_id)

    def create(self, d: Widget) -> LiveWidget:
        if d.ids[0] == self._fail_create_key:
            raise RuntimeError(f"create failed for {d.ids[0]}")
        self.created.append(d)
        lv = live_widget(f"new-{d.ids[0]}", d.ids[0], d.name, d.size, "v-new")
        self.rows[lv.live_id] = lv
        return lv

    def update(self, live_id: str, d: Widget) -> LiveWidget:
        self.updated.append((live_id, d))
        lv = live_widget(live_id, d.ids[0], d.name, d.size, "v-upd")
        self.rows[live_id] = lv
        return lv
