"""A toy destination over an in-memory dict of "widget" rows.

`ToyDestination` is the `Destination[LiveWidget]` that apply and restore write through.
The engine is domain-free: it reads records through a `RecordSchema` (`toy.ToySchema`)
and writes through a `Destination`. A `LiveWidget` is what the store reports, keyed by
`live_id` and carrying an opaque `version` token.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from toy import LiveWidget

from gitops_engine.destination import DestinationError


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
