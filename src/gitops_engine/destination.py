"""The destination: where planned writes land.

A consumer implements `Destination` over its own client. Values travel as dicts keyed
by the schema's field names; the destination maps them onto its own write calls.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


def is_empty(value: Any) -> bool:
    return value is None or value == "" or value == []


def refused_empty(values: dict[str, Any], *, retire: bool) -> str | None:
    """The never-null guard: the first field holding `None`, `""` or `[]` in a write
    that is not a retire, else None. Apply checks it before the destination is called,
    so a sync never clears a value by accident."""
    if retire:
        return None
    return next((k for k, v in values.items() if is_empty(v)), None)


class DestinationError(RuntimeError):
    """A write the destination refused or failed. Apply and restore stop at the first."""


@runtime_checkable
class Destination[L](Protocol):
    def list_live(self) -> list[L]: ...

    def get(self, live_id: str) -> L | None:
        """The row as it is now, or None when it is gone."""
        ...

    def preflight(self, writes: list[dict[str, Any]], *, restoring: bool = False) -> None:
        """Raise `ValueError` when any write would fail to resolve, before any write.

        `writes` holds every value of each create and the values of each update."""
        ...

    def create(self, values: dict[str, Any]) -> L: ...

    def update(self, live_id: str, values: dict[str, Any], *, retire: bool) -> L:
        """Write exactly `values`. `retire` allows an empty value to clear a field."""
        ...
