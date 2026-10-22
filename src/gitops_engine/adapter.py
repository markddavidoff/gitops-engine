"""The contract between the engine and one external system.

The engine knows nothing about any specific system — matching, hashing, identity,
reads, and writes all go through an `Adapter`. Three record shapes flow through it:

- **D (desired)**: the declarative spec, the source of truth in git.
- **L (live)**: a record as the external system reports it, with an identity and a
  `version` token for optimistic concurrency.
- **state**: a desired record that also carries a `SyncMark` — the exact live state
  it was last reconciled against. The engine reads a state row only through
  `state_mark` / `mark_for`, so the state record type is the adapter's business.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, TypeVar

if TYPE_CHECKING:
    from .plan import Plan

D = TypeVar("D")
L = TypeVar("L")
S = TypeVar("S")


@dataclass(frozen=True)
class SyncMark:
    """The live state a desired row was last reconciled against.

    `version` is the optimistic-concurrency token (an updatedAt, etag, revision…);
    `content_hash` is the adapter's hash of the deployable fields at that point.
    Together they let the engine tell a benign metadata bump (version moved, hash
    unchanged) from real drift (both moved).
    """

    live_id: str
    version: str
    content_hash: str


class Adapter(Protocol[D, L]):
    """What a system integration supplies. Reads and identity are pure; writes
    (`preflight`, `create`, `update`) touch the external system."""

    def match(self, d: D, live: list[L]) -> L | None:
        """The live row this desired row corresponds to, or None (a create)."""
        ...

    def desired_hash(self, d: D) -> str:
        """Hash of the deployable fields of a desired row."""
        ...

    def live_hash(self, lv: L) -> str:
        """Hash of the deployable fields of a live row, comparable to desired_hash."""
        ...

    def live_id(self, lv: L) -> str:
        """Stable identity of a live row."""
        ...

    def live_version(self, lv: L) -> str:
        """Optimistic-concurrency token of a live row."""
        ...

    def mark_for(self, d: D, lv: L, state: list[S]) -> SyncMark | None:
        """The mark recorded for this desired/live pair, or None if untracked.

        The engine does not know how state rows are keyed; the adapter locates the
        state row (by its own identity rules) and returns its mark.
        """
        ...

    def state_mark(self, s: S) -> SyncMark | None:
        """The mark on a state row, or None if the row is untracked."""
        ...

    def preflight(self, plan: Plan[D, L]) -> None:
        """Validate the plan before any write. Raise to abort the whole apply."""
        ...

    def get(self, live_id: str) -> L | None:
        """Re-read a live row at write time, or None if it is gone."""
        ...

    def create(self, d: D) -> L:
        """Create the row for a desired record; return the live row written."""
        ...

    def update(self, live_id: str, d: D) -> L:
        """Update an existing row to a desired record; return the live row written."""
        ...
