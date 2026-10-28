"""Read and append to an identity file, keeping hand-written comments.

The store only appends: new identities and missing values. It never changes or
removes a value a human wrote. The file is loaded with ruamel.yaml's round-trip
loader, so comments, key order and unchanged lines survive a save. Appends go through
`add_identity` / `add_values`; nothing is written until `save`, and `save` writes only
when something was appended.

Round-trip output uses one indent style (mappings 2, block sequences 4 with the dash
offset 2). A hand-edited file in another sequence style is re-indented on the first save
that appends something; a file with nothing to append is never rewritten.

Values are passed by match-key field name: `add_identity("a", tags=[...], sources={...})`
for a spec whose match keys include a `tags` field.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from io import StringIO
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from .identity import (
    Identity,
    IdentitySpec,
    IdentityTable,
    MatchKey,
    load_text,
    parse_identities,
)


def _yaml() -> YAML:
    yml = YAML(typ="rt")
    yml.indent(mapping=2, sequence=4, offset=2)
    yml.width = 4096  # never wrap a long value list
    return yml


def _flow_list(values: list[str]) -> CommentedSeq:
    seq = CommentedSeq(values)
    seq.fa.set_flow_style()
    return seq


class IdentityStore:
    """The identity file, loaded for appending. Reads see pending appends via `.table`.

    `header` is the comment block a new or comment-free file starts with."""

    def __init__(self, path: Path, spec: IdentitySpec, *, header: str = "") -> None:
        self.path = path
        self.spec = spec
        self.header = header
        self.reports: list[str] = []
        self._dirty = False
        self._prefix = ""  # comment-only file content, kept ahead of the first entry
        self._data: CommentedMap
        if path.exists():
            text = path.read_text()
            loaded = load_text(text, path.name, round_trip=True)
            self.table = parse_identities(loaded, path.name, spec)
            if loaded is None:
                self._data = CommentedMap()
                self._prefix = text if text.strip() else header
            else:
                self._data = loaded
        else:
            self.table = IdentityTable()
            self._data = CommentedMap()
            self._prefix = header

    # --- reads -----------------------------------------------------------------

    def _holder(self, mk: MatchKey, value: str, skip: str) -> str | None:
        """The other identity that already lists this value, if any."""
        want = mk.norm(value)
        for cid, ident in self.table.entries.items():
            if cid != skip and want in {mk.norm(v) for v in ident.values.get(mk.field, ())}:
                return cid
        return None

    def _match_key(self, field_name: str) -> MatchKey:
        for mk in self.spec.match_keys:
            if mk.field == field_name:
                return mk
        raise TypeError(f"identities: unknown value field {field_name!r}")

    # --- appends ---------------------------------------------------------------

    def add_identity(
        self, base_id: str, *, sources: dict[str, str] | None = None, **values: list[str]
    ) -> str:
        """Append a new identity and return its id: `base_id`, else `base_id-2`, `-3`, ..."""
        if not base_id:
            raise ValueError("identities: canonical id must be a non-empty string")
        sources = sources or {}
        for name in values:
            self._match_key(name)
        self._check_source_keys(sources)
        cid, n = base_id, 1
        while cid in self.table.entries:
            n += 1
            cid = f"{base_id}-{n}"
        kept = {mk.field: _unique(values.get(mk.field, []), mk.norm) for mk in self.spec.match_keys}
        entry = CommentedMap()
        for key in self.spec.file_order:
            if key == "sources":
                if sources:
                    entry["sources"] = CommentedMap(sources)
            elif kept[key]:
                entry[key] = _flow_list(kept[key])
        if not entry:
            entry = _empty_map()
        self._data[cid] = entry
        self.table.entries[cid] = Identity(
            canonical_id=cid, values={k: list(v) for k, v in kept.items()}, sources=dict(sources)
        )
        self._dirty = True
        return cid

    def add_values(
        self, canonical_id: str, *, sources: dict[str, str] | None = None, **values: list[str]
    ) -> None:
        """Append the values the identity lacks. Never changes or removes one."""
        ident = self.table.entries.get(canonical_id)
        if ident is None:
            raise ValueError(f"identities: no identity {canonical_id!r}")
        sources = sources or {}
        for name in values:
            self._match_key(name)
        self._check_source_keys(sources)
        entry = self._data[canonical_id]
        if entry is None:
            entry = self._data[canonical_id] = _empty_map()
        for mk in self.spec.match_keys:
            self._append(entry, ident, mk, values.get(mk.field, []))
        for key, value in sources.items():
            old = ident.sources.get(key)
            if old is None:
                if "sources" not in entry or entry["sources"] is None:
                    entry["sources"] = CommentedMap()
                entry["sources"][key] = value
                ident.sources[key] = value
                self._dirty = True
            elif old != value:
                self.reports.append(
                    f"identities: '{canonical_id}' keeps source {key}='{old}'; ingest saw '{value}'"
                )

    def _append(
        self, entry: CommentedMap, ident: Identity, mk: MatchKey, values: list[str]
    ) -> None:
        have = ident.values.setdefault(mk.field, [])
        for value in values:
            if mk.norm(value) == "" or mk.norm(value) in {mk.norm(h) for h in have}:
                continue
            holder = self._holder(mk, value, skip=ident.canonical_id)
            if holder is not None:
                self.reports.append(
                    f"identities: not adding {mk.label} '{value}' to '{ident.canonical_id}': "
                    f"already on '{holder}'"
                )
                continue
            if mk.field not in entry or entry[mk.field] is None:
                entry[mk.field] = _flow_list([])
            entry[mk.field].append(value)
            have.append(value)
            self._dirty = True

    def _check_source_keys(self, sources: dict[str, str]) -> None:
        bad = sorted(set(sources) - set(self.spec.sources))
        if bad:
            raise ValueError(f"identities: unknown source key(s): {bad}")
        for k, v in sources.items():
            if not isinstance(v, str) or not v:
                raise ValueError(f"identities: source {k} must be a non-empty string")

    # --- write -----------------------------------------------------------------

    def save(self) -> bool:
        """Write the file if anything was appended. Returns whether it wrote."""
        if not self._dirty:
            return False
        buf = StringIO()
        buf.write(self._prefix)
        if self._data:
            _yaml().dump(self._data, buf)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(buf.getvalue())
        os.replace(tmp, self.path)
        self._dirty = False
        return True


def _unique(values: list[str], norm: Callable[[str], str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        n = norm(v)
        if n and n not in seen:
            seen.add(n)
            out.append(v)
    return out


def _empty_map() -> CommentedMap:
    m = CommentedMap()
    m.fa.set_flow_style()
    return m
