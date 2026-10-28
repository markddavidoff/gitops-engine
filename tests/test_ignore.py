"""The ignore list on the toy adapter: matching, scope exemption, validation, conflicts."""

from __future__ import annotations

import pytest
from toy import TOY_IDENTITY_SPEC as SPEC
from toy import LiveWidget, ToySchema, Widget

from gitops_engine.datasets import DatasetRecord
from gitops_engine.identity import Identity, IdentityTable, ItemKeys
from gitops_engine.ignore import (
    BUILTIN_PREFIX,
    IgnoreEntry,
    IgnoreFormat,
    IgnoreList,
    IgnorePrefix,
    append_ignore,
    ignore_conflicts,
    ignore_errors,
    ignored_lines,
    load_ignore,
    partition_live,
    stale_ignore,
)
from gitops_engine.schema import SyncMark

SCHEMA = ToySchema()
FMT = IgnoreFormat("ignore.yaml", "store_id", "# Widgets the sync never touches.\n", "widgets")
FOUND = "found"

OWN_FILE = """\
# Widgets the sync never touches.
widgets:
  - store_id: L_own
    name: Gear Works      # informational; matching is by store_id
    reason: our own widget
name_prefixes:
  - prefix: "test-"
    reason: manual experiments
"""


def live(live_id: str, name: str, *, tags=(), ids=()) -> LiveWidget:
    return LiveWidget(live_id, name, "ok", list(ids), list(tags), None, "v1")


def _ignore(tmp_path, text=OWN_FILE) -> IgnoreList:
    path = tmp_path / FMT.file
    path.write_text(text)
    return load_ignore(path, FMT)


def test_builtin_prefix_only_unscoped(tmp_path):
    ign = _ignore(tmp_path)
    name = f"{BUILTIN_PREFIX}abc-Gear"
    assert [p.line is None for p in ign.active_prefixes(None)] == [True, False]
    assert ign.match("L1", name, scope=None) is not None
    # a run scoped to the built-in prefix may write its own records
    scope = f"{BUILTIN_PREFIX}1a2b-"
    assert [p.line is None for p in ign.active_prefixes(scope)] == [False]
    assert ign.match("L1", f"{scope}Gear", scope=scope) is None
    # a scope that does not start with it leaves the built-in prefix in force
    assert ign.match("L1", name, scope="other") is not None
    # a file prefix applies under every scope
    assert ign.match("L2", "test-x", scope=scope) == IgnorePrefix("test-", "manual experiments", 7)
    # ids still match under a scope
    assert ign.match("L_own", "Gear Works", scope=scope) is not None


def test_match_by_id_survives_rename_and_prefix_is_startswith(tmp_path):
    ign = _ignore(tmp_path)
    assert ign.match("L_own", "Renamed", scope=None) == IgnoreEntry(
        "L_own", "Gear Works", "our own widget", 3
    )
    assert ign.match("L_other", "Gear Works", scope=None) is None
    assert ign.match("L3", "Acme test-", scope=None) is None


def test_a_hand_built_list_names_its_file():
    ign = IgnoreList("ignore.yaml", entries=[IgnoreEntry("L1", None, "r", 3)])
    assert ign.where(ign.entries[0]) == "ignore.yaml:3"
    assert stale_ignore(ign, [], SCHEMA) == [
        "stale ignore entry: ignore.yaml:3: L1 is not in store"
    ]


def test_load_missing_file_has_builtin_prefix_only(tmp_path):
    ign = load_ignore(tmp_path / "missing.yaml", FMT)
    assert ign.entries == [] and [p.line for p in ign.prefixes] == [None]
    assert ign.match("x", f"{BUILTIN_PREFIX}x", scope=None) is not None


def test_partition_live_keeps_order(tmp_path):
    ign = _ignore(tmp_path)
    rows = [live("L_own", "Gear Works"), live("L1", "Cog"), live("L2", "test-x")]
    managed, ignored = partition_live(rows, ign, SCHEMA, scope=None)
    assert [lv.live_id for lv in managed] == ["L1"]
    assert [lv.live_id for lv in ignored] == ["L_own", "L2"]


def test_validation_missing_reason_short_prefix_duplicate_id(tmp_path):
    path = tmp_path / FMT.file
    path.write_text(
        "widgets:\n"
        "  - store_id: a\n"
        "    reason: r\n"
        "  - store_id: b\n"
        "  - store_id: a\n"
        "    reason: again\n"
        "name_prefixes:\n"
        "  - prefix: zz\n"
        "    reason: too short\n"
    )
    assert ignore_errors(path, FMT) == [
        "ignore.yaml:4: missing reason",
        "ignore.yaml:5: store_id a listed twice",
        "ignore.yaml:8: prefix 'zz' is shorter than 4 characters",
    ]
    assert ignore_errors(tmp_path / "missing.yaml", FMT) == []
    path.write_text(OWN_FILE)
    assert ignore_errors(path, FMT) == []
    path.write_text("- a\n- list\n")
    assert ignore_errors(path, FMT) == ["ignore.yaml: top level must be a mapping"]


def test_stale_entry_warns_with_the_destination_noun(tmp_path):
    ign = _ignore(tmp_path)
    assert stale_ignore(ign, [live("L_x", "Cog")], SCHEMA) == [
        "stale ignore entry: ignore.yaml:3: L_own is not in store"
    ]
    assert stale_ignore(ign, [live("L_own", "Gear Works")], SCHEMA) == []


def test_ignored_lines_list_rows_then_active_prefixes(tmp_path):
    ign = _ignore(tmp_path)
    _, ignored = partition_live([live("L_own", "Gear Works")], ign, SCHEMA, scope=None)
    assert ignored_lines(ignored, ign, SCHEMA, scope=None) == [
        "IGNORED: 'Gear Works' (L_own): our own widget",
        f"IGNORED PREFIX: {BUILTIN_PREFIX} (built-in)",
        "IGNORED PREFIX: test- (ignore.yaml:7): manual experiments",
    ]


def _record_keys(e: DatasetRecord[Widget]) -> ItemKeys:
    w = e.record
    sources = ((SPEC.destination_source, w.mark.live_id if w.mark else None),)
    return ItemKeys(
        label=w.name, ids=(e.source_id,), sources=sources, values=(("tags", tuple(w.tags)),)
    )


def _implicit(w: Widget, taken: set[str]) -> Identity:
    cid = w.ids[0] if w.ids else w.name.lower()
    return Identity(cid, values={"names": [w.name], "tags": list(w.tags)}, sources={})


def _conflicts(ignored, ign, entries, desired, table=None):
    table = table or IdentityTable()
    return ignore_conflicts(
        ignored,
        ign,
        entries,
        desired,
        table,
        schema=SCHEMA,
        spec=SPEC,
        found_dataset=FOUND,
        record_keys=_record_keys,
        record_live_id=lambda w: w.mark.live_id if w.mark else None,
        fallback_id=lambda e: e.source_id,
        implicit=_implicit,
        scope=None,
    )


def test_conflict_message_names_the_shared_key_and_where(tmp_path):
    ign = _ignore(tmp_path)
    desired = [Widget(name="Gear Co", status="ok", ids=["gear"], tags=["red"])]
    own = live("L_own", "Gear Works", tags=["red"])
    failures, reports = _conflicts([own], ign, [], desired)
    assert failures == [
        (
            "desired record 'Gear Co' shares tag 'red' with ignored store widget 'Gear Works' "
            "(ignore.yaml:3). Remove that entry or change Gear Co."
        )
    ]
    assert reports == []
    builtin = live("L_e2e", f"{BUILTIN_PREFIX}x", tags=["red"])
    failures, _ = _conflicts([builtin], ign, [], desired)
    assert failures[0].endswith("(built-in). Remove that entry or change Gear Co.")
    # an ignored row that resolves to no desired row is no conflict
    assert _conflicts([live("L_own", "Gear Works", tags=["blue"])], ign, [], desired) == ([], [])


def test_ignoring_a_stubbed_row_is_reported_not_failed(tmp_path):
    ign = _ignore(tmp_path, "widgets:\n  - store_id: L_umb\n    reason: a test\n")
    stub = Widget(name="Umbrella", status="ok", tags=["u"], mark=SyncMark("L_umb", "v1", "h"))
    stub_row = Widget(
        name="Umbrella", status="ok", ids=["umbrella"], tags=["u"], owned=True,
        mark=SyncMark("L_umb", "v1", "h"),
    )  # fmt: skip
    entries = [DatasetRecord(FOUND, "umbrella", stub)]
    umb = live("L_umb", "Umbrella", tags=["u"], ids=["umbrella"])
    failures, reports = _conflicts([umb], ign, entries, [stub_row])
    assert failures == []
    assert reports == [
        "stub 'Umbrella' is for ignored store widget 'Umbrella': the next refresh drops it"
    ]
    # a different row on that stub's identity is still a conflict
    ign2 = _ignore(tmp_path, "widgets:\n  - store_id: L_umb2\n    reason: dup\n")
    other = live("L_umb2", "Umbrella EU", tags=["u"])
    failures, _ = _conflicts([other], ign2, entries, [stub_row])
    assert len(failures) == 1 and "'Umbrella EU' (ignore.yaml:2)" in failures[0]


def test_append_ignore_creates_file_with_header_and_builtin_prefix(tmp_path):
    path = tmp_path / FMT.file
    assert append_ignore(path, FMT, live_id="L_x", name="Cog", reason=" a test ") is True
    assert path.read_text().startswith("# Widgets the sync never touches.\n")
    assert ignore_errors(path, FMT) == []
    ign = load_ignore(path, FMT)
    assert ign.entries == [IgnoreEntry("L_x", "Cog", "a test", ign.entries[0].line)]
    assert [p.line is None for p in ign.prefixes] == [True]  # the file's own built-in entry


def test_append_ignore_already_listed_returns_false(tmp_path):
    path = tmp_path / FMT.file
    path.write_text(OWN_FILE)
    assert append_ignore(path, FMT, live_id="L_own", name="Gear Works", reason="again") is False
    assert path.read_text() == OWN_FILE  # untouched, bytes and all
    assert append_ignore(path, FMT, live_id="L_new", name="Cog", reason="a test") is True
    assert "# informational; matching is by store_id" in path.read_text()
    assert [e.live_id for e in load_ignore(path, FMT).entries] == ["L_own", "L_new"]


def test_append_ignore_rejects_empty_id_or_reason_and_bad_shape(tmp_path):
    path = tmp_path / FMT.file
    for kw in ({"live_id": " ", "reason": "r"}, {"live_id": "L_x", "reason": " "}):
        with pytest.raises(ValueError, match="must not be empty"):
            append_ignore(path, FMT, name="x", **kw)
    assert not path.exists()
    path.write_text("- just\n- a list\n")
    with pytest.raises(ValueError, match="top level must be a mapping"):
        append_ignore(path, FMT, live_id="L_x", name="x", reason="r")
