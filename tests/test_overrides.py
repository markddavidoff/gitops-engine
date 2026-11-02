"""The overrides file on the toy schema: format, loader, validation and store."""

from __future__ import annotations

from pathlib import Path

import pytest
from toy import TOY_OVERRIDES as FMT
from toy import OwnedToySchema, ToySchema

from gitops_engine.ignore import BUILTIN_PREFIX, BUILTIN_REASON
from gitops_engine.overrides import (
    Deleted,
    HiddenPrefix,
    OverrideEntry,
    OverridesStore,
    load_overrides,
    override_errors,
)

FULL = """\
widgets:
  acme:
    pin:                    # these values win over upstream
      status: active
      size: null              # pinned empty
      name: Acme Corp
    tags:
      add: [acme.io]
      remove: [old-acme.com]
    pending:
      size: 150000
      tags_add: [acme.dev]
    reason: optional free text
  globex:
    hidden: {reason: test account}
  initech:
    deleted: {store_id: L_initech, confirmed: 2026-10-28}
hidden_name_prefixes:
  - {prefix: "sandbox-", reason: manual experiments}
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / FMT.file
    path.write_text(text)
    return path


def test_missing_and_comment_only_file_is_empty(tmp_path: Path) -> None:
    missing = load_overrides(tmp_path / FMT.file, FMT)
    comments = load_overrides(_write(tmp_path, "# nothing yet\n"), FMT)
    for ov in (missing, comments):
        assert ov.entries == {}
        assert [p.prefix for p in ov.prefixes] == [BUILTIN_PREFIX]
        assert ov.prefixes[0] == HiddenPrefix(BUILTIN_PREFIX, BUILTIN_REASON, None)


def test_pin_null_differs_from_absent(tmp_path: Path) -> None:
    ov = load_overrides(_write(tmp_path, "widgets:\n  w1:\n    pin: {size: null}\n"), FMT)
    entry = ov.entries["w1"]
    assert entry.pin == {"size": None}
    assert "name" not in entry.pin


def test_full_format_parses(tmp_path: Path) -> None:
    ov = load_overrides(_write(tmp_path, FULL), FMT)
    assert ov.entries == {
        "acme": OverrideEntry(
            pin={"status": "active", "size": None, "name": "Acme Corp"},
            add={"tags": ["acme.io"]},
            remove={"tags": ["old-acme.com"]},
            pending={"size": 150000, "tags_add": ["acme.dev"]},
            reason="optional free text",
            line=2,
        ),
        "globex": OverrideEntry(hidden="test account", line=14),
        "initech": OverrideEntry(deleted=Deleted("L_initech", "2026-10-28"), line=16),
    }
    assert ov.prefixes[1:] == [HiddenPrefix("sandbox-", "manual experiments", 19)]
    assert override_errors(tmp_path / FMT.file, FMT, OwnedToySchema()) == []


def test_malformed_file_names_file_and_line(tmp_path: Path) -> None:
    path = _write(tmp_path, "widgets:\n  w1:\n    pin: [1, 2]\n")
    with pytest.raises(ValueError, match=r"toy-overrides\.yaml:3"):
        load_overrides(path, FMT)
    assert any("toy-overrides.yaml:3" in e for e in override_errors(path, FMT, OwnedToySchema()))


@pytest.mark.parametrize(
    ("text", "substring"),
    [
        ("widgets:\n  w1:\n    pin: {status: null}\n", "w1: status cannot be pinned empty"),
        ("widgets:\n  w1:\n    pin: {ids: [a]}\n", "w1: ids can never be pinned"),
        ("widgets:\n  w1:\n    hidden: {}\n", "w1: hidden needs a reason"),
        ("hidden_name_prefixes:\n  - {prefix: abc, reason: r}\n", "shorter than 4"),
        ("widgets:\n  w1:\n    pinn: {name: x}\n", "unknown key 'pinn'"),
        ("widgets:\n  w1:\n    deleted: {confirmed: 2026-10-28}\n", "deleted needs store_id"),
        ("- a\n", "top level must be a mapping"),
        ("widgets:\n  w1:\n    pin: {owner: x}\n", "owner cannot be pinned"),
        ("widgets:\n  w1:\n    name: {add: [x]}\n", "name is not a list field"),
        ("widgets:\n  w1:\n    tags: {add: [1]}\n", "tags.add values must be strings"),
        ("widgets:\n  w1:\n    pending: {name_add: [x]}\n", "pending key 'name_add'"),
        ("widgets:\n  w1:\n    deleted: {store_id: L1}\n", "deleted needs confirmed"),
        (
            "hidden_name_prefixes:\n  - {prefix: abcd, reason: r}\n  - {prefix: abcd, reason: r}\n",
            "listed twice",
        ),
    ],
)
def test_override_errors(tmp_path: Path, text: str, substring: str) -> None:
    errors = override_errors(_write(tmp_path, text), FMT, OwnedToySchema())
    assert any(substring in e for e in errors), errors


def test_pin_null_on_a_field_that_does_not_clear(tmp_path: Path) -> None:
    path = _write(tmp_path, "widgets:\n  w1:\n    pin: {size: null}\n")
    assert override_errors(path, FMT, OwnedToySchema()) == []
    assert any("size cannot be pinned empty" in e for e in override_errors(path, FMT, ToySchema()))


def test_store_round_trip_keeps_comments(tmp_path: Path) -> None:
    path = _write(tmp_path, "# keep me\nwidgets:\n  w1:\n    reason: hand written  # keep me\n")
    store = OverridesStore(path, FMT)
    assert store.pin("w1", "size", 7)
    assert store.save()
    text = path.read_text()
    assert text.startswith("# keep me\n")
    assert "reason: hand written  # keep me" in text
    assert load_overrides(path, FMT).entries["w1"].pin == {"size": 7}


def test_fresh_file_gets_header(tmp_path: Path) -> None:
    path = tmp_path / FMT.file
    store = OverridesStore(path, FMT)
    assert store.hide("w1", "test widget")
    assert store.pin("w2", "size", None)
    assert store.save()
    assert path.read_text().startswith(FMT.header)
    assert "size: null" in path.read_text()
    assert load_overrides(path, FMT).entries["w1"].hidden == "test widget"


def test_remove_values_drops_from_add_first(tmp_path: Path) -> None:
    path = _write(tmp_path, "widgets:\n  w1:\n    tags:\n      add: [x]\n")
    store = OverridesStore(path, FMT)
    assert store.remove_values("w1", "tags", ["x", "y"])
    entry = store.overrides.entries["w1"]
    assert entry.add == {}
    assert entry.remove == {"tags": ["y"]}


def test_add_values_drops_from_remove_first(tmp_path: Path) -> None:
    store = OverridesStore(_write(tmp_path, "widgets:\n  w1:\n    tags: {remove: [x]}\n"), FMT)
    assert store.add_values("w1", "tags", ["x", "y"])
    entry = store.overrides.entries["w1"]
    assert entry.remove == {}
    assert entry.add == {"tags": ["y"]}
    assert not store.add_values("w1", "tags", ["y"])


def test_save_unchanged_returns_false(tmp_path: Path) -> None:
    path = _write(tmp_path, FULL)
    store = OverridesStore(path, FMT)
    assert not store.pin("acme", "size", None)
    assert not store.pin("acme", "status", "active")
    assert not store.save()
    assert path.read_text() == FULL


def test_hide_keeps_existing_reason(tmp_path: Path) -> None:
    store = OverridesStore(tmp_path / FMT.file, FMT)
    assert store.hide("w1", "first")
    assert not store.hide("w1", "second")
    assert store.overrides.entries["w1"].hidden == "first"


def test_overrides_view_reflects_unsaved_edits(tmp_path: Path) -> None:
    path = tmp_path / FMT.file
    store = OverridesStore(path, FMT)
    store.hide("w1", "test widget")
    assert store.overrides.entries["w1"].hidden == "test widget"
    assert not path.exists()


def test_pending_scalar_and_list(tmp_path: Path) -> None:
    store = OverridesStore(tmp_path / FMT.file, FMT)
    assert store.set_pending("w1", "size", 9)
    assert store.add_pending("w1", "tags", ["a", "b"])
    assert not store.add_pending("w1", "tags", ["a"])
    assert store.overrides.entries["w1"].pending == {"size": 9, "tags_add": ["a", "b"]}
    assert store.drop_pending("w1", "tags")
    assert store.drop_pending("w1", "size")
    assert not store.drop_pending("w1", "size")
    assert "w1" not in store.overrides.entries


def test_mark_deleted_and_prefix_reload(tmp_path: Path) -> None:
    path = tmp_path / FMT.file
    store = OverridesStore(path, FMT)
    assert store.mark_deleted("w1", "L1", "2026-10-28")
    assert not store.mark_deleted("w1", "L1", "2026-10-28")
    assert store.add_hidden_prefix("sandbox-", "manual experiments")
    assert not store.add_hidden_prefix("sandbox-", "again")
    assert not store.add_hidden_prefix(BUILTIN_PREFIX, "built in")
    assert store.save()
    ov = load_overrides(path, FMT)
    assert ov.entries["w1"].deleted == Deleted("L1", "2026-10-28")
    assert [p.prefix for p in ov.prefixes] == [BUILTIN_PREFIX, "sandbox-"]
    assert override_errors(path, FMT, OwnedToySchema()) == []


def test_store_rejects_unknown_fields(tmp_path: Path) -> None:
    store = OverridesStore(tmp_path / FMT.file, FMT)
    with pytest.raises(ValueError, match="ids"):
        store.pin("w1", "ids", ["a"])
    with pytest.raises(ValueError, match="name"):
        store.add_values("w1", "name", ["a"])
    with pytest.raises(ValueError, match="reason"):
        store.hide("w1", " ")
