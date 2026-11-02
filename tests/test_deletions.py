"""Deletion confirmation on the toy schema: absent from the listing, then not found."""

from __future__ import annotations

import pytest
from toy import LiveWidget, OwnedToySchema, Widget
from toy_adapter import ToyDestination

from gitops_engine.deletions import Deletions, confirm_deletions
from gitops_engine.destination import DestinationError
from gitops_engine.schema import SyncMark

SCHEMA = OwnedToySchema()


def old(cid: str, live_id: str) -> Widget:
    """A state row: what the mirror recorded for live row `live_id`."""
    return Widget("Gear", "ok", ids=[cid], mark=SyncMark(live_id, "v1", "h"))


def live(cid: str, live_id: str) -> LiveWidget:
    return LiveWidget(live_id, "Gear", "ok", [cid], [], None, "v2")


def desired(cid: str, *, owned: bool = False) -> Widget:
    return Widget("Gear", "ok", ids=[cid], owned=owned)


def confirm(dest: ToyDestination, olds: list[Widget], ds: list[Widget]) -> Deletions:
    return confirm_deletions(olds, dest.list_live(), ds, dest, schema=SCHEMA)


def test_confirmed_when_unlisted_and_get_none() -> None:
    dest = ToyDestination([live("w2", "L2")])
    got = confirm(dest, [old("w1", "L1"), old("w2", "L2")], [desired("w1"), desired("w2")])
    assert got == Deletions(confirmed=[("w1", "L1")], still_live=[])
    assert [c for c in dest.calls if c[0] == "get"] == [("get", "L1")]


def test_unlisted_but_get_finds_is_still_live() -> None:
    dest = ToyDestination([live("w1", "L1")], unlisted={"L1"})
    got = confirm(dest, [old("w1", "L1")], [desired("w1")])
    assert got == Deletions(confirmed=[], still_live=[("w1", "L1")])


def test_get_error_propagates() -> None:
    dest = ToyDestination([], get_errors={"L1"})
    with pytest.raises(DestinationError):
        confirm(dest, [old("w1", "L1")], [desired("w1")])


def test_stub_is_never_a_candidate() -> None:
    dest = ToyDestination([])
    got = confirm(dest, [old("w1", "L1")], [desired("w1", owned=True)])
    assert got == Deletions(confirmed=[], still_live=[])
    assert dest.calls == []
