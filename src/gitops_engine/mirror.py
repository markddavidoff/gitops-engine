"""The state mirror: the live rows, as the repo records them.

Every field of a mirror row comes from its live row and nothing else: the old state row
for the same destination id has nothing to carry over, and a state row is never matched
by a key or a name (that would hand one row's canonical id to a second row sharing its
key). Each row carries its mark, which `schema.mirror_row` builds. Deterministic and
idempotent.
"""

from __future__ import annotations

from .schema import RecordSchema


def reconcile_mirror[D, L](live: list[L], schema: RecordSchema[D, L]) -> list[D]:
    """The mirror rows of `live`, sorted by lower-cased label (equal labels keep live order)."""
    out = [schema.mirror_row(lv) for lv in live]
    out.sort(key=lambda d: schema.label(d).lower())
    return out
