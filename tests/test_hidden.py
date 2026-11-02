"""Hidden rows on the toy schema: a live row is hidden by the canonical id its keys
resolve to, or by an active name prefix."""

from __future__ import annotations

from toy import TOY_IDENTITY_SPEC as SPEC
from toy import LiveWidget, ToySchema, Widget

from gitops_engine.datasets import DatasetRecord
from gitops_engine.hidden import (
    Hidden,
    hidden_conflicts,
    hidden_from,
    hidden_lines,
    partition_hidden,
)
from gitops_engine.identity import Identity, IdentityTable, ItemKeys
from gitops_engine.ignore import BUILTIN_PREFIX, BUILTIN_REASON
from gitops_engine.overrides import HiddenPrefix, OverrideEntry, Overrides
from gitops_engine.schema import SyncMark

SRC, DEST = SPEC.sources
FOUND = "found"
FILE = "toy-overrides.yaml"


class SourcedSchema(ToySchema):
    """A live widget also offers its store id under the destination source."""

    def live_keys(self, lv: LiveWidget) -> ItemKeys:
        return ItemKeys(
            label=lv.name,
            ids=tuple(lv.ids),
            sources=((DEST, lv.live_id),),
            values=(("tags", tuple(lv.tags)),),
        )


SCHEMA = SourcedSchema()
SANDBOX = HiddenPrefix("sandbox-", "manual experiments", 7)


def live(live_id: str, name: str, *, tags=(), ids=()) -> LiveWidget:
    return LiveWidget(live_id, name, "ok", list(ids), list(tags), None, "v1")


def hidden(ids=None, prefixes=(SANDBOX,)) -> Hidden:
    builtin = HiddenPrefix(BUILTIN_PREFIX, BUILTIN_REASON, None)
    return Hidden(FILE, dict(ids or {}), [builtin, *prefixes])


TABLE = IdentityTable(
    {
        "w-h": Identity("w-h", values={"tags": ["h"]}, sources={DEST: "L_h"}),
        "w-m": Identity("w-m", values={"tags": ["m"]}, sources={DEST: "L_m"}),
    }
)


def test_hidden_from_overrides_keeps_reasons_and_prefixes():
    ov = Overrides(
        FILE,
        entries={
            "w-h": OverrideEntry(hidden="test account", line=3),
            "w-m": OverrideEntry(pin={"size": 1}, line=5),
        },
    )
    ov.prefixes.append(SANDBOX)
    h = hidden_from(ov)
    assert h.file == FILE
    assert h.ids == {"w-h": "test account"}
    assert [p.prefix for p in h.prefixes] == [BUILTIN_PREFIX, "sandbox-"]


def test_partition_by_canonical_id():
    rows = [live("L_m", "Managed"), live("L_h", "Renamed Hidden"), live("L_x", "Other")]
    managed, hid = partition_hidden(
        rows, hidden({"w-h": "test account"}), TABLE, schema=SCHEMA, spec=SPEC, scope=None
    )
    assert [lv.live_id for lv in managed] == ["L_m", "L_x"]
    assert [lv.live_id for lv in hid] == ["L_h"]


def test_match_by_id_else_prefix():
    h = hidden({"w-h": "test account"})
    assert h.match("w-h", "anything", scope=None) == "test account"
    assert h.match(None, "sandbox-1", scope=None) == "manual experiments"
    assert h.match("w-m", "Managed", scope=None) is None


def test_builtin_prefix_scope_exemption():
    h = hidden()
    name = f"{BUILTIN_PREFIX}a"
    rows = [live("L_t", name), live("L_s", "sandbox-x")]
    _, hid = partition_hidden(rows, h, TABLE, schema=SCHEMA, spec=SPEC, scope=None)
    assert [lv.live_id for lv in hid] == ["L_t", "L_s"]
    managed, hid = partition_hidden(rows, h, TABLE, schema=SCHEMA, spec=SPEC, scope=BUILTIN_PREFIX)
    assert [lv.live_id for lv in managed] == ["L_t"]
    # a file prefix applies under every scope
    assert [lv.live_id for lv in hid] == ["L_s"]
    assert h.active_prefixes(BUILTIN_PREFIX) == [SANDBOX]
    assert h.active_prefixes("sandbox-") == h.prefixes


def _record_keys(e: DatasetRecord[Widget]) -> ItemKeys:
    w = e.record
    values = (("tags", tuple(w.tags)),)
    if e.dataset == FOUND:
        lid = w.mark.live_id if w.mark else None
        return ItemKeys(label=w.name, ids=(e.source_id,), sources=((DEST, lid),), values=values)
    return ItemKeys(label=w.name, sources=((SRC, e.source_id),), values=values)


def _implicit(w: Widget, taken: set[str]) -> Identity:
    cid = w.ids[0] if w.ids else w.name.lower()
    return Identity(cid, values={"tags": list(w.tags)}, sources={})


def _conflicts(hidden_live, h, entries, desired, table=None, scope=None):
    return hidden_conflicts(
        hidden_live,
        h,
        entries,
        desired,
        table or IdentityTable(),
        schema=SCHEMA,
        spec=SPEC,
        found_dataset=FOUND,
        record_keys=_record_keys,
        record_live_id=lambda w: w.mark.live_id if w.mark else None,
        fallback_id=lambda e: e.source_id,
        implicit=_implicit,
        scope=scope,
    )


def test_hidden_conflict_prefix_on_desired_identity():
    desired = [Widget(name="Gear Co", status="ok", ids=["gear"], tags=["red"])]
    sand = live("L_s", "sandbox-gear", tags=["red"])
    failures, reports = _conflicts([sand], hidden(), [], desired)
    assert failures == [
        (
            "desired record 'Gear Co' shares tag 'red' with hidden store widget 'sandbox-gear' "
            f"(prefix 'sandbox-', {FILE}:7). Remove that prefix from {FILE} or change Gear Co."
        )
    ]
    assert reports == []
    builtin = live("L_t", f"{BUILTIN_PREFIX}gear", tags=["red"])
    failures, _ = _conflicts([builtin], hidden(), [], desired)
    assert f"(prefix '{BUILTIN_PREFIX}', built-in)" in failures[0]
    assert FILE in failures[0]
    # a hidden row that resolves to no desired row is no conflict
    assert _conflicts([live("L_s", "sandbox-x", tags=["blue"])], hidden(), [], desired) == (
        [],
        [],
    )


def test_hidden_conflict_on_hidden_id_names_the_entry():
    desired = [Widget(name="Hidden Co", status="ok", ids=["w-h"], tags=["h"])]
    row = live("L_h", "Hidden Co", tags=["h"])
    failures, _ = _conflicts([row], hidden({"w-h": "test account"}), [], desired, TABLE)
    assert failures == [
        (
            "desired record 'Hidden Co' shares source id 'L_h' with hidden store widget "
            f"'Hidden Co' (entry w-h, {FILE}). Remove that entry from {FILE} or change Hidden Co."
        )
    ]


def test_hidden_conflict_stub_of_same_row_is_a_report():
    mark = SyncMark("L_s", "v1", "h")
    stub = Widget(name="sandbox-u", status="ok", tags=["u"], mark=mark)
    stub_row = Widget(name="sandbox-u", status="ok", ids=["umb"], tags=["u"], owned=True, mark=mark)
    entries = [DatasetRecord(FOUND, "umb", stub)]
    row = live("L_s", "sandbox-u", tags=["u"], ids=["umb"])
    failures, reports = _conflicts([row], hidden(), entries, [stub_row])
    assert failures == []
    assert reports == [
        "stub 'sandbox-u' is for hidden store widget 'sandbox-u': the next refresh drops it"
    ]
    # a different hidden row on that stub's identity is still a conflict
    other = live("L_s2", "sandbox-u2", tags=["u"])
    failures, _ = _conflicts([other], hidden(), entries, [stub_row])
    assert len(failures) == 1 and "'sandbox-u2'" in failures[0]


def test_hidden_lines_list_rows_then_active_prefixes():
    h = hidden({"w-h": "test account"})
    rows = [live("L_h", "Hidden"), live("L_s", "sandbox-x")]
    lines = hidden_lines(rows, h, TABLE, schema=SCHEMA, spec=SPEC, scope=None)
    assert lines == [
        "HIDDEN: 'Hidden' (L_h): test account",
        "HIDDEN: 'sandbox-x' (L_s): manual experiments",
        f"HIDDEN PREFIX: {BUILTIN_PREFIX} (built-in)",
        f"HIDDEN PREFIX: sandbox- ({FILE}:7): manual experiments",
    ]
    scoped = hidden_lines([], h, TABLE, schema=SCHEMA, spec=SPEC, scope=BUILTIN_PREFIX)
    assert scoped == [f"HIDDEN PREFIX: sandbox- ({FILE}:7): manual experiments"]
