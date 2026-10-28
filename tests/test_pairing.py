"""Pairing desired rows with live rows by identity, and picking one of several."""

from __future__ import annotations

from toy import TOY_IDENTITY_SPEC as SPEC
from toy import LiveWidget, ToySchema, Widget

from gitops_engine.identity import Identity, IdentityTable
from gitops_engine.pairing import Pairing, pair_rows, pick_primary, stamped_conflict

SCHEMA = ToySchema()
DEST = SPEC.destination_source


def want(name: str, *, ids=(), tags=()) -> Widget:
    return Widget(name=name, status="ok", ids=list(ids), tags=list(tags))


def live(live_id: str, name: str, *, ids=(), tags=()) -> LiveWidget:
    return LiveWidget(live_id, name, "ok", list(ids), list(tags), None, "v1")


def implicit(w: Widget, taken: set[str]) -> Identity:
    cid = w.ids[0] if w.ids else w.name.lower()
    return Identity(cid, values={"names": [w.name], "tags": list(w.tags)}, sources={})


def table(**identities: dict) -> IdentityTable:
    return IdentityTable(
        {
            cid: Identity(cid, values=kw.get("values", {}), sources=kw.get("sources", {}))
            for cid, kw in identities.items()
        }
    )


def pair(desired, live_rows, t=None) -> Pairing:
    return pair_rows(
        desired, live_rows, t or IdentityTable(), schema=SCHEMA, spec=SPEC, implicit=implicit
    )


def test_pick_primary_stamped_then_source():
    t = table(gear={"values": {"tags": ["red"]}, "sources": {DEST: "L2"}})
    a, b = live("L1", "Gear", tags=["red"]), live("L2", "Gear", tags=["red"])
    # the recorded destination id decides when no row carries the canonical id
    assert pick_primary("gear", [a, b], t, schema=SCHEMA, spec=SPEC) is b
    # a row carrying the canonical id wins over the recorded destination id
    stamped = live("L1", "Gear", ids=["gear"], tags=["red"])
    assert pick_primary("gear", [stamped, b], t, schema=SCHEMA, spec=SPEC) is stamped
    # neither id on any row: no pick
    bare = table(gear={"values": {"tags": ["red"]}})
    assert pick_primary("gear", [a, b], bare, schema=SCHEMA, spec=SPEC) is None
    # two carrying the canonical id: no pick, whatever is recorded
    two = [live("L1", "A", ids=["gear"]), live("L2", "B", ids=["gear"])]
    assert pick_primary("gear", two, t, schema=SCHEMA, spec=SPEC) is None


def test_stamped_conflict_message_uses_nouns():
    two = [live("L2", "Gear B", ids=["gear"]), live("L1", "Gear A", ids=["gear"])]
    assert stamped_conflict("gear", two, schema=SCHEMA) == (
        "two or more store widgets carry the canonical id gear: 'Gear A' (L1), "
        "'Gear B' (L2): merge them in store or remove the id from all but one"
    )
    assert stamped_conflict("gear", two[:1], schema=SCHEMA) is None


def test_pair_rows_pairs_by_identity_and_reports_orphans():
    t = table(gear={"values": {"tags": ["red"]}})
    d = want("Gear", ids=["gear"], tags=["red"])
    lv = live("L1", "Gear Works", tags=["RED"])
    other = live("L9", "Stranger", tags=["blue"])
    got = pair([d], [lv, other], t)
    assert got.pairs == [(d, lv)]
    assert got.unmatched == [other]
    assert got.errors == [] and got.duplicates == []


def test_pair_rows_gives_an_unresolved_row_an_implicit_identity():
    d = want("Cog", ids=["cog"])
    got = pair([d], [live("L1", "Cog", ids=["cog"])])
    assert [(x.name, lv.live_id) for x, lv in got.pairs] == [("Cog", "L1")]
    got = pair([d], [])
    assert got.pairs == [(d, None)]


def test_pair_rows_second_desired_row_on_one_identity_is_an_error():
    t = table(gear={"values": {"tags": ["red"]}})
    a, b = want("Gear", tags=["red"]), want("Gear Two", tags=["red"])
    got = pair([a, b], [], t)
    assert got.pairs == [(a, None)]
    assert got.errors == ["two desired rows resolve to gear: 'Gear' and 'Gear Two'"]


def test_pair_rows_duplicates_with_a_primary():
    t = table(gear={"values": {"tags": ["red"]}})
    d = want("Gear", tags=["red"])
    a, b = live("L1", "Gear", ids=["gear"], tags=["red"]), live("L2", "Gear", tags=["red"])
    got = pair([d], [b, a], t)
    assert got.pairs == [(d, a)]
    assert got.duplicates == [("gear", b)]
    assert got.errors == []


def test_pair_rows_duplicates_with_no_primary_is_an_error():
    t = table(gear={"values": {"tags": ["red"]}})
    d = want("Gear", tags=["red"])
    a, b = live("L1", "Gear", tags=["red"]), live("L2", "Gear Inc", tags=["red"])
    got = pair([d], [a, b], t)
    assert got.pairs == [] and got.duplicates == []
    assert got.errors == [
        (
            "known duplicates for gear with no canonical or recorded store id: "
            f"'Gear' (L1), 'Gear Inc' (L2): merge them in store or record one in {SPEC.file}"
        )
    ]


def test_pair_rows_stamped_conflict_is_an_error_in_either_order():
    t = table(gear={"values": {"tags": ["red"]}})
    d = want("Gear", tags=["red"])
    a, b = live("L1", "A", ids=["gear"]), live("L2", "B", ids=["gear"])
    for rows in ([a, b], [b, a]):
        got = pair([d], rows, t)
        assert got.pairs == [] and got.duplicates == []
        assert got.errors == [stamped_conflict("gear", rows, schema=SCHEMA)]
