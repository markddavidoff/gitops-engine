from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest
from toy import TOY_IDENTITY_SPEC as SPEC

from gitops_engine.identity import IdentitySpec, load_identities
from gitops_engine.identity_store import IdentityStore

HEADER = "# One entry per widget. The key is the canonical id; it never changes.\n"


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / SPEC.file
    p.write_text(textwrap.dedent(text).lstrip("\n"))
    return p


def test_missing_file_starts_with_the_header(tmp_path):
    store = IdentityStore(tmp_path / SPEC.file, SPEC, header=HEADER)
    assert store.table.entries == {}
    assert store.save() is False  # nothing added: nothing written
    assert not (tmp_path / SPEC.file).exists()
    store.add_identity("gear", names=["Gear"], tags=["red"], sources={"s1": "gear"})
    assert store.save() is True
    text = (tmp_path / SPEC.file).read_text()
    assert text.startswith(HEADER)
    assert text.startswith("# One entry per widget.")
    assert text == (HEADER + "gear:\n  names: [Gear]\n  tags: [red]\n  sources:\n    s1: gear\n")
    assert load_identities(tmp_path / SPEC.file, SPEC).entries["gear"].values["tags"] == ["red"]


def test_add_identity_takes_next_suffix_on_collision(tmp_path):
    p = write(
        tmp_path,
        """
        gear:
          names: [Gear]
    """,
    )
    store = IdentityStore(p, SPEC, header=HEADER)
    assert store.add_identity("gear", names=["Gear Two"], tags=[], sources={}) == "gear-2"
    assert store.add_identity("gear", names=["Gear Three"], tags=[], sources={}) == "gear-3"
    assert list(store.table.entries) == ["gear", "gear-2", "gear-3"]
    assert store.save() is True
    assert list(load_identities(p, SPEC).entries) == ["gear", "gear-2", "gear-3"]


def test_add_values_appends_missing_only_and_keeps_hand_edit(tmp_path):
    before = textwrap.dedent("""\
        gear:
          names:
            - Gear
            - Gear Hand Edit
          tags:
            - zed
            - red
          sources:
            s1: gear
    """)
    p = tmp_path / SPEC.file
    p.write_text(before)
    store = IdentityStore(p, SPEC, header=HEADER)
    store.add_values(
        "gear",
        names=["GEAR", "Gear Labs"],
        tags=["RED", "green"],
        sources={"s1": "gear", "s2": "x-1"},
    )
    assert store.reports == []
    assert store.save() is True
    after = p.read_text()
    assert after == textwrap.dedent("""\
        gear:
          names:
            - Gear
            - Gear Hand Edit
            - Gear Labs
          tags:
            - zed
            - red
            - green
          sources:
            s1: gear
            s2: x-1
    """)
    # nothing but added lines differs from the hand-written file
    assert [ln for ln in before.splitlines() if ln not in after.splitlines()] == []
    t = load_identities(p, SPEC)
    assert t.entries["gear"].values["names"] == ["Gear", "Gear Hand Edit", "Gear Labs"]
    assert t.entries["gear"].values["tags"] == ["zed", "red", "green"]


def test_add_values_creates_missing_keys_and_extends_flow_lists(tmp_path):
    p = write(
        tmp_path,
        """
        gear: {names: [Gear]}
        cog:
          names: [Cog]
          tags: [blue]
    """,
    )
    store = IdentityStore(p, SPEC, header=HEADER)
    store.add_values("gear", tags=["red"], sources={"s2": "x-1"})
    store.add_values("cog", names=["Cog Co"])
    assert store.save() is True
    t = load_identities(p, SPEC)
    assert t.entries["gear"].values["tags"] == ["red"]
    assert t.entries["gear"].sources == {"s2": "x-1"}
    assert t.entries["cog"].values["names"] == ["Cog", "Cog Co"]
    assert t.entries["cog"].values["tags"] == ["blue"]


def test_never_appends_value_held_by_another_identity(tmp_path):
    p = write(
        tmp_path,
        """
        gear:
          names: [Gear]
          tags: [red]
        cog:
          names: [Cog Co]
          tags: [blue]
    """,
    )
    original = p.read_text()
    store = IdentityStore(p, SPEC, header=HEADER)
    store.add_values("gear", names=["cog co."], tags=["BLUE"])
    assert store.reports == [
        "identities: not adding tag 'BLUE' to 'gear': already on 'cog'",
        "identities: not adding name 'cog co.' to 'gear': already on 'cog'",
    ]
    assert store.save() is False
    assert p.read_text() == original
    assert store.table.entries["gear"].values["tags"] == ["red"]


def test_source_value_never_overwritten(tmp_path):
    p = write(
        tmp_path,
        """
        gear:
          sources:
            s2: x-old
    """,
    )
    original = p.read_text()
    store = IdentityStore(p, SPEC, header=HEADER)
    store.add_values("gear", sources={"s2": "x-new"})
    assert store.reports == [
        "identities: 'gear' keeps source s2='x-old'; ingest saw 'x-new'",
    ]
    assert store.save() is False
    assert p.read_text() == original
    assert store.table.entries["gear"].sources == {"s2": "x-old"}


def test_comments_and_order_survive_a_save(tmp_path):
    p = write(
        tmp_path,
        """
        # Curated by hand: keep the order.
        zed:
          names: [Zed]
        gear:
          names: [Gear]
          tags: [red]   # was hand-enriched.yaml
          sources:
            s1: gear  # slug at migration
        # trailing note about cog
        cog:
          names: [Cog]
    """,
    )
    store = IdentityStore(p, SPEC, header=HEADER)
    store.add_values("gear", tags=["amber"])
    store.add_identity("axle", names=["Axle"], tags=[], sources={})
    assert store.save() is True
    text = p.read_text()
    assert text.startswith("# Curated by hand: keep the order.\nzed:")
    assert "# was hand-enriched.yaml" in text
    assert "# slug at migration" in text
    assert "# trailing note about cog" in text
    assert list(load_identities(p, SPEC).entries) == ["zed", "gear", "cog", "axle"]
    assert load_identities(p, SPEC).entries["gear"].values["tags"] == ["red", "amber"]


def test_save_without_changes_writes_nothing(tmp_path):
    p = write(
        tmp_path,
        """
        gear:
          names: [Gear]
          tags: [red]
    """,
    )
    os.utime(p, (1_000_000_000, 1_000_000_000))
    before = p.stat().st_mtime_ns
    store = IdentityStore(p, SPEC, header=HEADER)
    store.add_values("gear", names=["gear"], tags=["RED"])  # already present
    assert store.save() is False
    assert p.stat().st_mtime_ns == before


def test_save_twice_writes_once(tmp_path):
    store = IdentityStore(tmp_path / SPEC.file, SPEC, header=HEADER)
    store.add_identity("gear", names=["Gear"], tags=[], sources={})
    assert store.save() is True
    assert store.save() is False


def test_comment_only_file_keeps_its_comments(tmp_path):
    p = tmp_path / SPEC.file
    p.write_text("# nothing yet, add entries below\n")
    store = IdentityStore(p, SPEC, header=HEADER)
    store.add_identity("gear", names=["Gear"], tags=[], sources={})
    assert store.save() is True
    assert p.read_text().startswith("# nothing yet, add entries below\ngear:")


def test_duplicate_canonical_id_is_rejected(tmp_path):
    p = write(
        tmp_path,
        """
        gear:
          names: [Gear]
        cog:
          names: [Cog]
        gear:
          tags: [red]
    """,
    )
    with pytest.raises(ValueError, match="duplicate key 'gear'"):
        IdentityStore(p, SPEC, header=HEADER)
    with pytest.raises(ValueError, match="duplicate key 'gear'"):
        load_identities(p, SPEC)


def test_malformed_file_is_a_value_error(tmp_path):
    p = write(
        tmp_path,
        """
        gear:
          aliases: [GEAR]
    """,
    )
    with pytest.raises(ValueError, match="aliases"):
        IdentityStore(p, SPEC, header=HEADER)
    p.write_text("gear: [unclosed\n")
    with pytest.raises(ValueError, match=SPEC.file):
        IdentityStore(p, SPEC, header=HEADER)


def test_add_values_unknown_identity_or_source_key_raises(tmp_path):
    store = IdentityStore(tmp_path / SPEC.file, SPEC, header=HEADER)
    with pytest.raises(ValueError, match="nope"):
        store.add_values("nope", names=["X"])
    store.add_identity("gear", names=[], tags=[], sources={})
    with pytest.raises(ValueError, match="hubspot"):
        store.add_values("gear", sources={"hubspot": "x"})
    with pytest.raises(ValueError, match="hubspot"):
        store.add_identity("cog", names=[], tags=[], sources={"hubspot": "x"})


def test_sources_round_trip_under_their_declared_key(tmp_path):
    p = tmp_path / SPEC.file
    store = IdentityStore(p, SPEC)
    store.add_identity("gear", names=["Gear"], tags=["red"], sources={"s1": "gear"})
    assert store.save() is True
    assert "    s1: gear\n" in p.read_text()
    assert load_identities(p, SPEC).entries["gear"].sources == {"s1": "gear"}


def test_unknown_value_field_is_a_type_error(tmp_path):
    store = IdentityStore(tmp_path / SPEC.file, SPEC)
    with pytest.raises(TypeError, match="colour"):
        store.add_identity("gear", colour=["red"])
    store.add_identity("gear")
    with pytest.raises(TypeError, match="colour"):
        store.add_values("gear", colour=["red"])


def test_new_entry_keys_follow_the_spec_file_order(tmp_path):
    flipped = IdentitySpec(
        sources=SPEC.sources,
        match_keys=SPEC.match_keys,
        destination_source=SPEC.destination_source,
        file_order=("sources", "tags", "names"),
    )
    store = IdentityStore(tmp_path / SPEC.file, flipped)
    store.add_identity("gear", names=["Gear"], tags=["red"], sources={"s1": "gear"})
    store.save()
    assert (tmp_path / SPEC.file).read_text() == (
        "gear:\n  sources:\n    s1: gear\n  tags: [red]\n  names: [Gear]\n"
    )


def test_store_round_trip_keeps_comments(tmp_path):
    p = write(
        tmp_path,
        """
        # top note
        gear:  # trailing on the key
          names: ["Gear, Big", 'Cog: Two', Plain]   # quoted as written
          tags: [red]
          sources:
            s1: gear  # slug
    """,
    )
    store = IdentityStore(p, SPEC, header=HEADER)
    store.add_values("gear", tags=["blue"])
    assert store.save() is True
    assert p.read_text() == textwrap.dedent("""\
        # top note
        gear:  # trailing on the key
          names: ["Gear, Big", 'Cog: Two', Plain]   # quoted as written
          tags: [red, blue]
          sources:
            s1: gear  # slug
    """)


def test_hand_quoted_values_survive_append(tmp_path):
    p = tmp_path / SPEC.file
    p.write_text(
        textwrap.dedent("""\
        gear:
          names: ["GEAR, Inc.", 'Gear: Two', Plain]
          tags:
            - a.com
    """)
    )
    store = IdentityStore(p, SPEC, header=HEADER)
    store.add_values("gear", names=["Fresh"], tags=["gold"])
    assert store.save() is True
    text = p.read_text()
    assert "names: [\"GEAR, Inc.\", 'Gear: Two', Plain, Fresh]" in text
    assert "- gold" in text
    assert load_identities(p, SPEC).entries["gear"].values["names"] == [
        "GEAR, Inc.",
        "Gear: Two",
        "Plain",
        "Fresh",
    ]
