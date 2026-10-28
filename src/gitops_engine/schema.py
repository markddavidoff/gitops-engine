"""The record schema: how the engine reads one kind of record.

A consumer describes its records once, as a `RecordSchema`: the fields the engine
plans and verifies, the policy each field follows, and the accessors that turn the
consumer's own desired (D) and live (L) types into values the engine can compare.
The engine holds no record type of its own.

`SyncMark` is the live state a desired row was last reconciled against.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from .identity import ItemKeys


@dataclass(frozen=True)
class SyncMark:
    """The live state a desired row was last reconciled against.

    `version` is the optimistic-concurrency token (an updatedAt, etag, revision…);
    `content_hash` is the schema's hash of the deployable fields at that point.
    Together they let the engine tell a benign metadata bump (version moved, hash
    unchanged) from real drift (both moved).
    """

    live_id: str
    version: str
    content_hash: str


@dataclass(frozen=True)
class Nouns:
    """The words the engine prints for the consumer's records, their destination, the
    source the desired rows come from, and the destination's version token."""

    record: str = "record"
    records: str = "records"
    destination: str = "destination"
    source: str = "source"
    version: str = "version"


@dataclass(frozen=True)
class Always:
    """A scalar or list field the plan owns outright: desired replaces live.

    With `unordered`, values compare sorted and every difference is kind `"change"`.
    """

    unordered: bool = False


@dataclass(frozen=True)
class Union:
    """A list field the plan adds to and the destination may add to as well.

    `key` normalises a value before values compare (lower-case by default).
    `exclusive` marks a field whose values belong to one record at a time: the plan
    withholds a value another live record already holds. `item` names one value in
    the messages that report it.
    """

    key: Callable[[str], str] = str.lower
    exclusive: bool = False
    item: str = "value"


@dataclass(frozen=True)
class KeepIfUnset:
    """A scalar field the destination owns once it has a value."""


Policy = Always | Union | KeepIfUnset


@dataclass(frozen=True)
class FieldSpec:
    """One planned field. A `required` field falls back to the desired value on a
    destination-owned row whose live value is empty.

    `required` also sets what post-write verification expects of the field when the
    plan has no change for it: the value from before the write, where any other
    unplanned field is expected to equal the plan's target (`deploy.apply_deploy`)."""

    name: str
    policy: Policy
    required: bool = False


@runtime_checkable
class RecordSchema[D, L](Protocol):
    """What a consumer supplies so the engine can plan, verify and mirror its records."""

    nouns: Nouns
    fields: tuple[FieldSpec, ...]  # in plan and verify order
    id_field: str  # the `Always(unordered=True)` field that carries canonical ids
    snapshot_id_key: str
    snapshot_version_key: str

    def desired_value(self, d: D, field: str) -> Any: ...

    def live_value(self, lv: L, field: str) -> Any: ...

    def label(self, d: D) -> str: ...

    def live_label(self, lv: L) -> str: ...

    def live_id(self, lv: L) -> str: ...

    def live_version(self, lv: L) -> str: ...

    def desired_keys(self, d: D) -> ItemKeys: ...

    def live_keys(self, lv: L) -> ItemKeys: ...

    def mark(self, d: D) -> SyncMark | None: ...

    def destination_owned(self, d: D) -> bool: ...

    def with_values(self, d: D, values: dict[str, Any]) -> D: ...

    def mirror_row(self, lv: L) -> D:
        """The live row as the state mirror holds it, with its mark."""
        ...

    def live_hash(self, lv: L) -> str:
        """Equal to `mark(mirror_row(lv)).content_hash`."""
        ...

    def state_to_live(self, s: D) -> L: ...

    def snapshot_row(self, lv: L) -> dict[str, Any]: ...

    def snapshot_values(self, row: dict[str, Any]) -> dict[str, Any]: ...
