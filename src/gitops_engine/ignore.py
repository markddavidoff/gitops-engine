"""The ignore file: live rows the sync never touches.

An entry matches a live row by its destination id (stable across renames) or by a name
prefix (`startswith` on the raw live label). `BUILTIN_PREFIX` is built in even when the
file omits it; it applies to unscoped runs only, so a run scoped to it may write its own
throwaway records.

`partition_live` is the one split of live rows into managed and ignored; every consumer
takes the managed half. A missing file means the built-in prefix only. Stale entries
warn, never fail.

The file is a mapping with two lists: the entries, under `IgnoreFormat.list_key`, each
with an id (under `IgnoreFormat.id_key`), an optional name and a reason; and
`name_prefixes`, each with a prefix and a reason.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import YAMLError
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

from .datasets import DatasetRecord
from .identity import (
    Identity,
    IdentitySpec,
    IdentityTable,
    ItemKeys,
    load_text,
    resolve,
    with_implicit,
)
from .schema import RecordSchema

BUILTIN_PREFIX = "zzz-gitops-test-"
_BUILTIN_REASON = "live e2e harness throwaway records (built in; always present)"
_MIN_PREFIX = 4
_PREFIXES_KEY = "name_prefixes"


@dataclass(frozen=True)
class IgnoreFormat:
    """How a consumer's ignore file is laid out: its name, the key that holds a row's
    destination id, the comment header of a new file, and the key of the entry list."""

    file: str
    id_key: str
    header: str
    list_key: str = "entries"


@dataclass(frozen=True)
class IgnoreEntry:
    live_id: str
    name: str | None
    reason: str
    line: int


@dataclass(frozen=True)
class IgnorePrefix:
    prefix: str
    reason: str
    line: int | None  # None = built-in


@dataclass
class IgnoreList:
    file: str  # the file name messages cite
    entries: list[IgnoreEntry] = field(default_factory=list)
    prefixes: list[IgnorePrefix] = field(
        default_factory=lambda: [IgnorePrefix(BUILTIN_PREFIX, _BUILTIN_REASON, None)]
    )

    def where(self, match: IgnoreEntry | IgnorePrefix) -> str:
        return "built-in" if match.line is None else f"{self.file}:{match.line}"

    def active_prefixes(self, scope: str | None) -> list[IgnorePrefix]:
        """The prefixes in force for a run. Only the built-in prefix has a scoped
        exemption: a run whose `scope` starts with it may write the harness's
        records. A file prefix applies under every scope."""
        exempt = bool(scope) and scope.startswith(BUILTIN_PREFIX)
        return [p for p in self.prefixes if not (exempt and p.line is None)]

    def match(
        self,
        live_id: str,
        name: str,
        *,
        scope: str | None,
    ) -> IgnoreEntry | IgnorePrefix | None:
        """The entry ignoring this row: by id, else by an active name prefix
        (`active_prefixes`)."""
        for entry in self.entries:
            if entry.live_id == live_id:
                return entry
        for p in self.active_prefixes(scope):
            if name.startswith(p.prefix):
                return p
        return None


def _shape_error(msg: str) -> ValueError:
    # a malformed file is a ValueError like every other curated-file problem
    return ValueError(msg)


def _load_raw(path: Path, fmt: IgnoreFormat) -> Any:
    try:
        return YAML(typ="rt", pure=True).load(path.read_text())
    except YAMLError as e:
        raise ValueError(f"{fmt.file}: not valid YAML: {e}") from e


def _items(raw: Any, key: str, fmt: IgnoreFormat) -> list[tuple[int, dict]]:
    """(1-based line, mapping) for each item of the `key` list."""
    seq = raw.get(key) if raw is not None else None
    if seq is None:
        return []
    if not isinstance(seq, list):
        raise _shape_error(f"{fmt.file}: '{key}' must be a list")
    out: list[tuple[int, dict]] = []
    for i, item in enumerate(seq):
        line = seq.lc.item(i)[0] + 1
        if not isinstance(item, dict):
            raise _shape_error(f"{fmt.file}:{line}: each '{key}' item must be a mapping")
        out.append((line, item))
    return out


def _parse(path: Path, fmt: IgnoreFormat) -> tuple[list[tuple[int, dict]], list[tuple[int, dict]]]:
    if not path.exists():
        return [], []
    raw = _load_raw(path, fmt)
    if raw is not None and not isinstance(raw, dict):
        raise _shape_error(f"{fmt.file}: top level must be a mapping")
    return _items(raw, fmt.list_key, fmt), _items(raw, _PREFIXES_KEY, fmt)


def _text(item: dict, key: str) -> str:
    value = item.get(key)
    return "" if value is None else str(value).strip()


def load_ignore(path: Path, fmt: IgnoreFormat) -> IgnoreList:
    """The ignore list; the built-in prefix is always present. Raises
    `ValueError` on a file that is not the expected shape (`ignore_errors`
    reports the softer problems: missing reason, short prefix, duplicate id)."""
    entries, prefixes = _parse(path, fmt)
    out = IgnoreList(fmt.file)
    for line, item in entries:
        lid = _text(item, fmt.id_key)
        if lid:
            name = _text(item, "name") or None
            out.entries.append(IgnoreEntry(lid, name, _text(item, "reason"), line))
    for line, item in prefixes:
        prefix = _text(item, "prefix")
        if prefix and prefix != BUILTIN_PREFIX:
            out.prefixes.append(IgnorePrefix(prefix, _text(item, "reason"), line))
    return out


def ignore_errors(path: Path, fmt: IgnoreFormat) -> list[str]:
    """What a check fails on in the ignore file; empty when clean."""
    try:
        entries, prefixes = _parse(path, fmt)
    except ValueError as e:
        return [str(e)]
    errors: list[str] = []
    seen: set[str] = set()
    for line, item in entries:
        where = f"{fmt.file}:{line}"
        lid = _text(item, fmt.id_key)
        if not lid:
            errors.append(f"{where}: missing {fmt.id_key}")
        if not _text(item, "reason"):
            errors.append(f"{where}: missing reason")
        if lid and lid in seen:
            errors.append(f"{where}: {fmt.id_key} {lid} listed twice")
        seen.add(lid)
    for line, item in prefixes:
        where = f"{fmt.file}:{line}"
        prefix = _text(item, "prefix")
        if not _text(item, "reason"):
            errors.append(f"{where}: missing reason")
        if len(prefix) < _MIN_PREFIX:
            errors.append(f"{where}: prefix '{prefix}' is shorter than {_MIN_PREFIX} characters")
    return errors


def partition_live[L](
    live: list[L],
    ignore: IgnoreList,
    schema: RecordSchema[Any, L],
    *,
    scope: str | None,
) -> tuple[list[L], list[L]]:
    """The one split of live rows: (managed, ignored), order kept."""
    managed: list[L] = []
    ignored: list[L] = []
    for lv in live:
        hit = ignore.match(schema.live_id(lv), schema.live_label(lv), scope=scope)
        (ignored if hit else managed).append(lv)
    return managed, ignored


def ignore_conflicts[D, L, R](
    ignored: list[L],
    ignore: IgnoreList,
    entries: list[DatasetRecord[R]],
    desired: list[D],
    table: IdentityTable,
    *,
    schema: RecordSchema[D, L],
    spec: IdentitySpec,
    found_dataset: str,
    record_keys: Callable[[DatasetRecord[R]], ItemKeys],
    record_live_id: Callable[[R], str | None],
    fallback_id: Callable[[DatasetRecord[R]], str],
    implicit: Callable[[D, set[str]], Identity],
    scope: str | None,
    describe: Callable[[D], str] | None = None,
) -> tuple[list[str], list[str]]:
    """(failures, reports): an ignored row that resolves to an identity with a desired
    row is a failure, unless that row is the `found_dataset` stub of the same destination
    id, which is reported: the next refresh drops it.

    `record_live_id` is the destination id a record holds; `fallback_id` is the identity
    id of a record that resolves to none; `implicit` builds an identity for a desired
    row that resolves to none; `describe` names a desired row in a message (default:
    its label)."""
    describe = describe or schema.label
    full = with_implicit(table, desired, spec, keys=schema.desired_keys, implicit=implicit)
    row_for: dict[str, D] = {}
    for d in desired:
        hit = resolve(schema.desired_keys(d), full, spec)
        if hit is not None:
            row_for.setdefault(hit.canonical_id, d)
    stub_ids: dict[str, set[str]] = {}
    for e in entries:
        live_id = record_live_id(e.record) if e.dataset == found_dataset else None
        if not live_id:
            continue
        hit = resolve(record_keys(e), full, spec)
        cid = hit.canonical_id if hit is not None else fallback_id(e)
        stub_ids.setdefault(cid, set()).add(live_id)
    n = schema.nouns
    failures: list[str] = []
    reports: list[str] = []
    for lv in ignored:
        hit = resolve(schema.live_keys(lv), full, spec)
        if hit is None or hit.canonical_id not in row_for:
            continue
        d = row_for[hit.canonical_id]
        name = describe(d)
        label = schema.live_label(lv)
        if schema.destination_owned(d) and schema.live_id(lv) in stub_ids.get(
            hit.canonical_id, set()
        ):
            reports.append(
                f"stub '{name}' is for ignored {n.destination} {n.record} '{label}': "
                "the next refresh drops it"
            )
            continue
        match = ignore.match(schema.live_id(lv), label, scope=scope)
        where = ignore.where(match) if match is not None else ignore.file
        failures.append(
            f"desired record '{name}' shares {hit.key} '{hit.value}' with ignored "
            f"{n.destination} {n.record} '{label}' ({where}). Remove that entry or change {name}."
        )
    return failures, reports


def stale_ignore[L](
    ignore: IgnoreList, live_all: list[L], schema: RecordSchema[Any, L]
) -> list[str]:
    """One warning per id entry no longer live (warn, never fail)."""
    ids = {schema.live_id(lv) for lv in live_all}
    destination = schema.nouns.destination
    return [
        f"stale ignore entry: {ignore.file}:{e.line}: {e.live_id} is not in {destination}"
        for e in ignore.entries
        if e.live_id not in ids
    ]


def ignored_lines[L](
    ignored: list[L],
    ignore: IgnoreList,
    schema: RecordSchema[Any, L],
    *,
    scope: str | None,
) -> list[str]:
    """A check's visibility lines: each ignored row, then each active prefix."""
    lines: list[str] = []
    for lv in ignored:
        match = ignore.match(schema.live_id(lv), schema.live_label(lv), scope=scope)
        reason = match.reason if match is not None else ""
        lines.append(f"IGNORED: '{schema.live_label(lv)}' ({schema.live_id(lv)}): {reason}")
    for p in ignore.active_prefixes(scope):
        if p.line is None:
            lines.append(f"IGNORED PREFIX: {p.prefix} (built-in)")
        else:
            lines.append(f"IGNORED PREFIX: {p.prefix} ({ignore.where(p)}): {p.reason}")
    return lines


def _round_trip_yaml() -> YAML:
    """The identity file's round-trip style (`identity_store._yaml`), so both curated files
    keep one indentation: mappings 2, block sequences 4 with the dash offset 2."""
    yml = YAML(typ="rt")
    yml.indent(mapping=2, sequence=4, offset=2)
    yml.width = 4096
    return yml


def append_ignore(path: Path, fmt: IgnoreFormat, *, live_id: str, name: str, reason: str) -> bool:
    """Append `live_id` to the file's entry list, keeping hand-written comments and quote
    styles (ruamel round trip). Returns False, writing nothing, when the id is already
    listed. A missing file is created with the header and the built-in prefix entry.
    Raises `ValueError` on an empty id or reason, or a file that is not the expected
    shape."""
    live_id, reason = live_id.strip(), reason.strip()
    if not live_id:
        raise ValueError(f"ignore: {fmt.id_key} must not be empty")
    if not reason:
        raise ValueError("ignore: reason must not be empty (each entry needs a reason)")
    prefix_text = ""  # comment-only file content, kept ahead of the entries
    fresh = False
    if path.exists():
        text = path.read_text()
        data = load_text(text, fmt.file, round_trip=True)
        if data is None:  # empty or comment-only
            prefix_text = text if text.strip() else fmt.header
            data, fresh = CommentedMap(), True
    else:
        prefix_text, data, fresh = fmt.header, CommentedMap(), True
    if not isinstance(data, CommentedMap):
        raise _shape_error(f"{fmt.file}: top level must be a mapping")
    if fresh:
        data[fmt.list_key] = CommentedSeq()
        data[_PREFIXES_KEY] = CommentedSeq(
            [
                CommentedMap(
                    prefix=DoubleQuotedScalarString(BUILTIN_PREFIX),
                    reason=_BUILTIN_REASON,
                )
            ]
        )
    entries = data.get(fmt.list_key)
    if entries is None:
        entries = data[fmt.list_key] = CommentedSeq()
    if not isinstance(entries, CommentedSeq):
        raise _shape_error(f"{fmt.file}: '{fmt.list_key}' must be a list")
    for item in entries:
        if isinstance(item, dict) and str(item.get(fmt.id_key, "")).strip() == live_id:
            return False
    entry = CommentedMap()
    entry[fmt.id_key] = live_id
    entry["name"] = name
    entry["reason"] = reason
    entries.append(entry)
    buf = StringIO()
    buf.write(prefix_text)
    _round_trip_yaml().dump(data, buf)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(buf.getvalue())
    os.replace(tmp, path)
    return True
