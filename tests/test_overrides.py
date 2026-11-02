"""The overrides file on the toy schema: format, loader, validation and store."""

from __future__ import annotations

from pathlib import Path

import pytest
from toy import TOY_OVERRIDES as FMT
from toy import LiveWidget, OwnedToySchema, ToySchema, Widget

from gitops_engine.identity import Identity, IdentityTable
from gitops_engine.ignore import BUILTIN_PREFIX, BUILTIN_REASON
from gitops_engine.overrides import (
    Deleted,
    HiddenPrefix,
    OverrideEntry,
    OverrideFindings,
    Overrides,
    OverridesStore,
    Pending,
    adopt_entry,
    apply_overrides,
    check_overrides,
    held_rows,
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


def test_drop_pending_values_keeps_the_other_adds(tmp_path: Path) -> None:
    path = tmp_path / FMT.file
    store = OverridesStore(path, FMT)
    store.add_pending("w1", "tags", ["a", "b"])
    store.set_pending("w1", "size", 9)
    assert store.drop_pending_values("w1", "tags", ["a", "zz"])
    assert store.overrides.entries["w1"].pending == {"size": 9, "tags_add": ["b"]}
    assert not store.drop_pending_values("w1", "tags", ["a"])
    assert not store.drop_pending_values("w2", "tags", ["a"])
    assert store.drop_pending_values("w1", "tags", ["b"])
    assert store.overrides.entries["w1"].pending == {"size": 9}
    store.drop_pending("w1", "size")
    assert "w1" not in store.overrides.entries
    with pytest.raises(ValueError, match="size is not a list field"):
        store.drop_pending_values("w1", "size", [9])


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


# --- applying, checking, holding and adopting ---------------------------------------


def widget(cid="w1", *, name="Gear", status="ok", tags=(), size=None, owned=False) -> Widget:
    return Widget(name=name, status=status, ids=[cid], tags=list(tags), size=size, owned=owned)


def live_widget(*, name="Gear", status="ok", tags=(), size=None) -> LiveWidget:
    return LiveWidget("L1", name, status, ["w1"], list(tags), size, "v1")


def ov(**entries: OverrideEntry) -> Overrides:
    return Overrides(FMT.file, entries=dict(entries))


def test_apply_pins_and_lists() -> None:
    rows = [widget(tags=["o", "p"]), widget("w2", tags=["o"])]
    entry = OverrideEntry(pin={"size": 7}, add={"tags": ["n"]}, remove={"tags": ["O"]})
    out = apply_overrides(rows, ov(w1=entry), schema=ToySchema())
    assert (out[0].size, out[0].tags) == (7, ["p", "n"])
    assert out[1] == rows[1]  # no entry: untouched


def test_apply_add_dedupes_by_policy_key() -> None:
    entry = OverrideEntry(add={"tags": ["A", "n", "N"]})
    [out] = apply_overrides([widget(tags=["a"])], ov(w1=entry), schema=ToySchema())
    assert out.tags == ["a", "n"]


def test_apply_hidden_and_deleted_drop_row() -> None:
    rows = [widget("w1"), widget("w2"), widget("w3")]
    entries = ov(w1=OverrideEntry(hidden="test"), w2=OverrideEntry(deleted=Deleted("L2", "x")))
    out = apply_overrides(rows, entries, schema=ToySchema())
    assert [w.ids for w in out] == [["w3"]]


def test_apply_skips_stub_except_hidden() -> None:
    stub = widget(size=3, tags=["a"], owned=True)
    entry = OverrideEntry(pin={"size": 7}, add={"tags": ["n"]}, remove={"tags": ["a"]})
    assert apply_overrides([stub], ov(w1=entry), schema=ToySchema()) == [stub]
    assert apply_overrides([stub], ov(w1=OverrideEntry(hidden="x")), schema=ToySchema()) == []


def test_pending_is_not_applied() -> None:
    row = widget(size=3, tags=["a"])
    entry = OverrideEntry(pending={"size": 9, "tags_add": ["b"]})
    assert apply_overrides([row], ov(w1=entry), schema=ToySchema()) == [row]


def test_apply_uses_pin_callback() -> None:
    calls: list[dict] = []

    def pin(d: Widget, values: dict) -> Widget:
        calls.append(dict(values))
        return ToySchema().with_values(d, {k: f"{v}!" for k, v in values.items()})

    entry = OverrideEntry(pin={"name": "N", "status": "S"})
    [out] = apply_overrides([widget()], ov(w1=entry), schema=ToySchema(), pin=pin)
    assert calls == [{"name": "N", "status": "S"}]
    assert (out.name, out.status) == ("N!", "S!")


def test_held_rows() -> None:
    entries = ov(w1=OverrideEntry(pending={"size": 9}), w2=OverrideEntry(pin={"size": 1}))
    assert held_rows(entries) == {"w1": f"pending decision in {FMT.file}"}


def test_check_overrides_failures_and_reports() -> None:
    table = IdentityTable({"w3": Identity("w3", values={}, sources={})})
    upstream = [widget(tags=["a"]), widget("w2", owned=True)]
    entries = ov(
        w1=OverrideEntry(
            pin={"status": "ok", "size": 7},
            remove={"tags": ["A", "zz"]},
            pending={"size": 9},
        ),
        w2=OverrideEntry(
            pin={"size": 1},
            pending={"size": 2},
            add={"tags": ["n"]},
            remove={"tags": ["m"]},
            hidden="allowed on a stub",
        ),
        w3=OverrideEntry(deleted=Deleted("L3", "2026-10-28")),
        ghost=OverrideEntry(pin={"size": 1}),
    )
    found = check_overrides(entries, upstream, table, schema=ToySchema())
    assert isinstance(found, OverrideFindings)
    assert found.failures == [
        "w1: tags.remove zz: upstream does not carry it",
        "w2: pin does not apply to a store-owned stub",
        "w2: pending does not apply to a store-owned stub",
        "w2: tags.add does not apply to a store-owned stub",
        "w2: tags.remove does not apply to a store-owned stub",
        "ghost: no identity",
    ]
    assert found.reports == [
        "w1: pin status equals upstream (redundant)",
        "w3: deleted at store (L3, 2026-10-28): not recreated",
    ]
    assert found.pending == [Pending("w1", "size", 9), Pending("w2", "size", 2)]


def test_check_overrides_redundant_pin_uses_pin_callback() -> None:
    def pin(d: Widget, values: dict) -> Widget:
        return ToySchema().with_values(d, {k: f"{v} [x]" for k, v in values.items()})

    entries = ov(w1=OverrideEntry(pin={"name": "Gear"}))
    upstream = [widget(name="Gear [x]")]
    found = check_overrides(entries, upstream, IdentityTable(), schema=ToySchema(), pin=pin)
    assert found.reports == ["w1: pin name equals upstream (redundant)"]
    assert not found.failures and not found.pending


def test_adopt_entry() -> None:
    schema = OwnedToySchema()
    entry = adopt_entry(
        widget(size=None, tags=["a"]), live_widget(size=5, tags=["a", "B"]), schema=schema
    )
    assert entry is not None
    assert entry.pin == {"size": 5}
    assert entry.add == {"tags": ["B"]}
    same = adopt_entry(widget(size=5, tags=["A"]), live_widget(size=5, tags=["a"]), schema=schema)
    assert same is None


def test_store_adopt_keeps_existing_pin(tmp_path: Path) -> None:
    store = OverridesStore(tmp_path / FMT.file, FMT)
    store.pin("w1", "size", None)
    entry = OverrideEntry(pin={"size": 5, "status": "S"}, add={"tags": ["B"]})
    assert store.adopt("w1", entry)
    assert not store.adopt("w1", entry)
    got = store.overrides.entries["w1"]
    assert got.pin == {"size": None, "status": "S"}
    assert got.add == {"tags": ["B"]}
