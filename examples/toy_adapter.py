"""Toy destinations over an in-memory dict of "widget" rows.

`ToyDestination` is the `Destination[LiveWidget]` the v0.2 apply and restore write
through. `ToyAdapter` below it is the v0.1.0 adapter, removed with that API.

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
from dataclasses import replace
from typing import Any

from toy import LiveWidget, Widget

from gitops_engine.destination import DestinationError
from gitops_engine.schema import SyncMark


class ToyDestination:
    """An in-memory widget store. Every call lands in `calls`, in order.

    `statuses` limits the statuses `preflight` resolves (None resolves all). `fail`
    maps a live id (update) or a name (create) to the error message the write raises.
    `tamper` maps a live id to field values the store returns instead of the written
    ones, standing in for a destination that does not apply what it was sent."""

    def __init__(
        self,
        rows: list[LiveWidget] | None = None,
        *,
        statuses: set[str] | None = None,
        fail: dict[str, str] | None = None,
        tamper: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.rows: dict[str, LiveWidget] = {lv.live_id: lv for lv in rows or []}
        self.calls: list[tuple[Any, ...]] = []
        self._statuses = statuses
        self._fail = fail or {}
        self._tamper = tamper or {}
        self._n = 0

    def _version(self) -> str:
        self._n += 1
        return f"w{self._n}"

    def list_live(self) -> list[LiveWidget]:
        return list(self.rows.values())

    def get(self, live_id: str) -> LiveWidget | None:
        self.calls.append(("get", live_id))
        return self.rows.get(live_id)

    def preflight(self, writes: list[dict[str, Any]], *, restoring: bool = False) -> None:
        self.calls.append(("preflight", [dict(w) for w in writes], restoring))
        if self._statuses is None:
            return
        missing = sorted(
            {w["status"] for w in writes if w.get("status") and w["status"] not in self._statuses}
        )
        if missing:
            nothing = "nothing restored" if restoring else "nothing written"
            raise ValueError(f"unresolvable status(es), {nothing}: {missing}")

    def create(self, values: dict[str, Any]) -> LiveWidget:
        self.calls.append(("create", dict(values)))
        if values.get("name") in self._fail:
            raise DestinationError(self._fail[values["name"]])
        version = self._version()
        lv = LiveWidget(
            live_id=f"new-{self._n}",
            name=values.get("name", ""),
            status=values.get("status", ""),
            ids=list(values.get("ids", [])),
            tags=list(values.get("tags", [])),
            size=values.get("size"),
            version=version,
        )
        self.rows[lv.live_id] = lv
        return lv

    def update(self, live_id: str, values: dict[str, Any], *, retire: bool) -> LiveWidget:
        self.calls.append(("update", live_id, dict(values), retire))
        if live_id in self._fail:
            raise DestinationError(self._fail[live_id])
        sent = {k: list(v) if isinstance(v, list) else v for k, v in values.items()}
        new = replace(self.rows[live_id], **sent, version=self._version())
        if live_id in self._tamper:
            new = replace(new, **self._tamper[live_id])
        self.rows[live_id] = new
        return new


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
    """The v0.1.0 in-memory widget store (removed with the v0.1.0 `Adapter`)."""

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
