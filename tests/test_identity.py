from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from toy import TOY_IDENTITY_SPEC as SPEC

from gitops_engine.identity import (
    Hit,
    Identity,
    IdentitySpec,
    IdentityTable,
    ItemKeys,
    MatchKey,
    conflict,
    load_identities,
    load_text,
    parse_identities,
    resolve,
    resolve_all,
    table_errors,
    with_implicit,
)
from gitops_engine.identity_store import IdentityStore


def table(tmp_path: Path, **identities: dict) -> IdentityTable:
    """A table built the way a consumer's ingest builds one: through the store."""
    store = IdentityStore(tmp_path / SPEC.file, SPEC)
    for cid, kw in identities.items():
        assert store.add_identity(cid, **kw) == cid
    return store.table


def table_from_file(tmp_path: Path, text: str) -> IdentityTable:
    """Hand-written file text, for the shapes the store never writes."""
    p = tmp_path / SPEC.file
    p.write_text(textwrap.dedent(text))
    return IdentityStore(p, SPEC).table


def item(label: str = "Gear", **kw) -> ItemKeys:
    """An item whose `values` and `sources` are given as dicts."""
    return ItemKeys(
        label=label,
        ids=tuple(kw.get("ids", ())),
        sources=tuple(kw.get("sources", {}).items()),
        values=tuple((k, tuple(v)) for k, v in kw.get("values", {}).items()),
    )


def got(hit: Hit | None) -> tuple[str, str, str] | None:
    return None if hit is None else (hit.canonical_id, hit.key, hit.value)


def test_missing_file_is_an_empty_table(tmp_path):
    assert load_identities(tmp_path / SPEC.file, SPEC).entries == {}


def test_unknown_field_raises_naming_the_key(tmp_path):
    with pytest.raises(ValueError, match="aliases"):
        table_from_file(
            tmp_path,
            """
            a:
              names: [A]
              aliases: [AA]
        """,
        )


def test_file_order_is_kept(tmp_path):
    t = table(tmp_path, zeta={"names": ["Zeta"]}, alpha={"names": ["Alpha"]})
    assert list(t.entries) == ["zeta", "alpha"]


def test_parse_rejects_malformed_shapes():
    with pytest.raises(ValueError, match="top level must be a mapping"):
        parse_identities(["a"], "f.yaml", SPEC)
    with pytest.raises(ValueError, match="identity 'a' must be a mapping"):
        parse_identities({"a": ["x"]}, "f.yaml", SPEC)
    with pytest.raises(ValueError, match="tags must be a list of strings"):
        parse_identities({"a": {"tags": "x"}}, "f.yaml", SPEC)
    with pytest.raises(ValueError, match=r"unknown source key\(s\): \['s9'\]"):
        parse_identities({"a": {"sources": {"s9": "x"}}}, "f.yaml", SPEC)
    with pytest.raises(ValueError, match="source s1 must be a non-empty string"):
        parse_identities({"a": {"sources": {"s1": ""}}}, "f.yaml", SPEC)
    with pytest.raises(ValueError, match="canonical id 1 must be a non-empty string"):
        parse_identities({1: {}}, "f.yaml", SPEC)


def test_parse_fills_every_match_key_field():
    t = parse_identities({"a": None, "b": {"tags": ["x"]}}, "f.yaml", SPEC)
    assert t.entries["a"] == Identity("a", {"tags": [], "names": []}, {})
    assert t.entries["b"].values == {"tags": ["x"], "names": []}


def test_duplicate_canonical_id_rejected():
    text = "a:\n  tags: [x]\nb: {}\na:\n  tags: [y]\n"
    with pytest.raises(ValueError, match=r"f.yaml: duplicate key 'a' \(line 4\)"):
        load_text(text, "f.yaml")
    with pytest.raises(ValueError, match="duplicate key 'a'"):
        load_text(text, "f.yaml", round_trip=True)


def test_invalid_yaml_names_the_file():
    with pytest.raises(ValueError, match="f.yaml: not valid YAML"):
        load_text("a: [unclosed\n", "f.yaml")


def test_resolves_by_canonical_id(tmp_path):
    t = table(tmp_path, gear={"names": ["Something Else"]})
    assert got(resolve(item(ids=["gear"]), t, SPEC)) == ("gear", "source id", "gear")


def test_resolves_by_source_id(tmp_path):
    t = table(tmp_path, a={"sources": {"s1": "x-1"}})
    assert got(resolve(item(sources={"s1": "x-1"}), t, SPEC)) == ("a", "source id", "x-1")
    assert resolve(item(sources={"s1": None}), t, SPEC) is None
    assert resolve(item(sources={"s2": "x-1"}), t, SPEC) is None  # wrong source


def test_match_key_normalisers_apply(tmp_path):
    t = table(tmp_path, a={"tags": ["red"], "names": ["Big Gear"]})
    assert got(resolve(item(values={"tags": ["RED"]}), t, SPEC)) == ("a", "tag", "RED")
    assert got(resolve(item(values={"names": ["big-gear!"]}), t, SPEC)) == (
        "a",
        "name",
        "big-gear!",
    )


def _loose_spec(*, skip_empty: bool) -> IdentitySpec:
    """A spec whose `tags` key normalises punctuation away, so "--" becomes ""."""
    strip = lambda v: "".join(c for c in v.lower() if c.isalnum())
    return IdentitySpec(
        sources=("s1",),
        match_keys=(MatchKey("tags", "tag", strip, skip_empty=skip_empty),),
        destination_source="s1",
        file_order=("tags", "sources"),
    )


def test_skip_empty_true_matches_and_reports_nothing_for_empty_values():
    spec = _loose_spec(skip_empty=True)
    t = parse_identities({"a": {"tags": ["--"]}, "b": {"tags": ["!!"]}}, "f.yaml", spec)
    assert resolve(item(values={"tags": ["??"]}), t, spec) is None
    assert table_errors(t, spec) == []


def test_skip_empty_false_treats_the_empty_value_as_a_value():
    spec = _loose_spec(skip_empty=False)
    t = parse_identities({"a": {"tags": ["--"]}, "b": {"tags": ["!!"]}}, "f.yaml", spec)
    assert got(resolve(item(values={"tags": ["??"]}), t, spec)) == ("a", "tag", "??")
    assert table_errors(t, spec) == ["tag '--' is on two identities: a, b"]


def test_skip_empty_defaults_to_false():
    assert MatchKey("tags", "tag", str.lower).skip_empty is False


def test_unknown_item_value_field_is_ignored(tmp_path):
    t = table(tmp_path, a={"tags": ["red"]})
    assert resolve(item(values={"colour": ["red"]}), t, SPEC) is None


def test_resolve_key_order(tmp_path):
    """Review Focus 1: ids reach A, tags reach B. A wins and the conflict names both."""
    t = table(tmp_path, a={"names": ["Alpha"]}, b={"tags": ["red"]})
    it = item(ids=["a"], values={"tags": ["red"]})
    assert got(resolve(it, t, SPEC)) == ("a", "source id", "a")
    assert [got(h) for h in resolve_all(it, t, SPEC)] == [
        ("a", "source id", "a"),
        ("b", "tag", "red"),
    ]
    assert conflict(it, t, SPEC) == (
        "Gear resolves to two identities: a (source id 'a') and b (tag 'red')"
    )


def test_source_order_declared(tmp_path):
    t = table(tmp_path, one={"sources": {"s1": "x"}}, two={"sources": {"s2": "y"}})
    # the item lists s2 first; the spec's order (s1, s2) still decides
    it = item(sources={"s2": "y", "s1": "x"})
    assert [got(h) for h in resolve_all(it, t, SPEC)] == [
        ("one", "source id", "x"),
        ("two", "source id", "y"),
    ]
    flipped = IdentitySpec(
        sources=("s2", "s1"),
        match_keys=SPEC.match_keys,
        destination_source="s2",
        file_order=SPEC.file_order,
    )
    assert got(resolve(it, t, flipped)) == ("two", "source id", "y")


def test_match_key_order_declared(tmp_path):
    t = table(tmp_path, by_tag={"tags": ["red"]}, by_name={"names": ["Gear"]})
    it = item(values={"names": ["Gear"], "tags": ["red"]})
    assert got(resolve(it, t, SPEC)) == ("by_tag", "tag", "red")  # tags are declared first
    flipped = IdentitySpec(
        sources=SPEC.sources,
        match_keys=tuple(reversed(SPEC.match_keys)),
        destination_source=SPEC.destination_source,
        file_order=SPEC.file_order,
    )
    assert got(resolve(it, t, flipped)) == ("by_name", "name", "Gear")


def test_a_source_hit_beats_a_match_key_hit(tmp_path):
    t = table(tmp_path, a={"tags": ["red"]}, b={"sources": {"s2": "y"}})
    it = item(sources={"s2": "y"}, values={"tags": ["red"]})
    assert got(resolve(it, t, SPEC)) == ("b", "source id", "y")


def test_no_conflict_when_every_key_agrees(tmp_path):
    t = table(tmp_path, a={"names": ["Gear"], "tags": ["red"]})
    assert conflict(item(ids=["a"], values={"tags": ["red"]}), t, SPEC) is None


def test_unlisted_item_resolves_to_none(tmp_path):
    t = table(tmp_path, a={"names": ["Gear Works"], "tags": ["red"]})
    it = item(values={"names": ["Gear Wokrs"], "tags": ["blue"]})
    assert resolve(it, t, SPEC) is None
    assert resolve_all(it, t, SPEC) == []
    assert conflict(it, t, SPEC) is None


def test_table_errors_label_from_spec(tmp_path):
    t = table(tmp_path, A={"tags": ["x"]}, B={"tags": ["X"]})
    assert table_errors(t, SPEC) == ["tag 'x' is on two identities: A, B"]


def test_table_errors_duplicate_value_name_and_source(tmp_path):
    t = table(
        tmp_path,
        a={"names": ["Big Gear"], "tags": ["red"], "sources": {"s1": "x", "s2": "y"}},
        b={"names": ["BIG gear."], "tags": ["RED"], "sources": {"s1": "x", "s2": "y"}},
        c={"names": ["Clean"]},
    )
    assert table_errors(t, SPEC) == [
        "tag 'red' is on two identities: a, b",
        "name 'Big Gear' is on two identities: a, b",
        "source s1 'x' is on two identities: a, b",
        "source s2 'y' is on two identities: a, b",
    ]


def test_table_errors_empty_for_a_clean_table(tmp_path):
    t = table(tmp_path, a={"names": ["A"], "tags": ["x"]}, b={"names": ["B"], "tags": ["y"]})
    assert table_errors(t, SPEC) == []


def _keys(row: dict) -> ItemKeys:
    return item(row["name"], ids=row.get("ids", ()), values={"names": [row["name"]]})


def _implicit(row: dict, taken: set[str]) -> Identity:
    base = row["name"].lower().replace(" ", "-")
    cid, n = base, 1
    while cid in taken:
        n += 1
        cid = f"{base}-{n}"
    return Identity(cid, {"names": [row["name"]], "tags": []}, {})


def test_with_implicit_adds_only_unresolved_rows(tmp_path):
    t = table(tmp_path, gear={"names": ["Gear"]})
    rows = [{"name": "Gear"}, {"name": "Cog"}, {"name": "cog!"}, {"name": "Axle"}]
    out = with_implicit(t, rows, SPEC, keys=_keys, implicit=_implicit)
    assert list(out.entries) == ["gear", "cog", "axle"]  # the second cog resolves to the first
    assert out.entries["cog"].values["names"] == ["Cog"]
    assert list(t.entries) == ["gear"]  # input untouched


def test_with_implicit_hands_the_taken_ids_to_the_builder(tmp_path):
    t = table(tmp_path, cog={"names": ["Other"]})
    out = with_implicit(t, [{"name": "Cog"}], SPEC, keys=_keys, implicit=_implicit)
    assert list(out.entries) == ["cog", "cog-2"]


def test_with_implicit_refuses_a_taken_id(tmp_path):
    t = table(tmp_path, cog={"names": ["Other"]})
    clash = lambda row, taken: Identity("cog")
    with pytest.raises(ValueError, match="'cog' is already taken"):
        with_implicit(t, [{"name": "Cog"}], SPEC, keys=_keys, implicit=clash)


def test_spec_validates_its_declarations():
    mk = (MatchKey("tags", "tag", str.lower),)
    with pytest.raises(ValueError, match="file_order must list"):
        IdentitySpec(("s1",), mk, "s1", ("tags",))  # no "sources"
    with pytest.raises(ValueError, match="file_order must list"):
        IdentitySpec(("s1",), mk, "s1", ("tags", "sources", "sources"))
    with pytest.raises(ValueError, match="not a source"):
        IdentitySpec(("s1",), mk, "s9", ("tags", "sources"))
