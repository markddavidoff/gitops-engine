"""A toy adapter over an in-memory dict of "widget" rows.

The engine is domain-free: it only talks to an `Adapter`. This toy adapter is the
smallest thing that exercises every engine path. A `Widget` is both the desired
spec and, when it carries a `mark`, a recorded state row (mirroring how a real
record type carries its last-reconciled mark). A `LiveWidget` is what the external
system reports, keyed by `live_id` and carrying an opaque `version` token.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from gitops_engine.adapter import SyncMark


@dataclass
class Widget:
    """Desired spec (and, with `mark` set, a recorded state row)."""

    key: str
    name: str
    value: int
    mark: SyncMark | None = None


@dataclass
class LiveWidget:
    """A widget as the external system reports it."""

    live_id: str
    key: str
    name: str
    value: int
    version: str


def content_hash(key: str, name: str, value: int) -> str:
    """Hash the deployable fields only — never `version`, `live_id`, or the mark."""
    blob = json.dumps({"key": key, "name": name, "value": value}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def state_row(lv: LiveWidget) -> Widget:
    """Build the state row recorded after observing a live row."""
    return Widget(
        key=lv.key,
        name=lv.name,
        value=lv.value,
        mark=SyncMark(lv.live_id, lv.version, content_hash(lv.key, lv.name, lv.value)),
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
            if lv.key == d.key:
                return lv
        return None

    def desired_hash(self, d: Widget) -> str:
        return content_hash(d.key, d.name, d.value)

    def live_hash(self, lv: LiveWidget) -> str:
        return content_hash(lv.key, lv.name, lv.value)

    def live_id(self, lv: LiveWidget) -> str:
        return lv.live_id

    def live_version(self, lv: LiveWidget) -> str:
        return lv.version

    def mark_for(self, d: Widget, lv: LiveWidget, state: list[Widget]) -> SyncMark | None:
        st = next((s for s in state if s.key == d.key), None)
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
        needed = {d.key for d in plan.creates} | {d.key for d, _ in plan.updates}
        missing = sorted(needed - self._resolvable)
        if missing:
            raise ValueError(f"unresolvable widget(s), nothing written: {missing}")

    def get(self, live_id: str) -> LiveWidget | None:
        return self.rows.get(live_id)

    def create(self, d: Widget) -> LiveWidget:
        if d.key == self._fail_create_key:
            raise RuntimeError(f"create failed for {d.key}")
        self.created.append(d)
        lv = LiveWidget(f"new-{d.key}", d.key, d.name, d.value, "v-new")
        self.rows[lv.live_id] = lv
        return lv

    def update(self, live_id: str, d: Widget) -> LiveWidget:
        self.updated.append((live_id, d))
        lv = LiveWidget(live_id, d.key, d.name, d.value, "v-upd")
        self.rows[live_id] = lv
        return lv
