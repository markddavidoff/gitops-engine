"""The retire file: values a reviewed change may remove, keyed by canonical id."""

from __future__ import annotations

import yaml
from toy import ToySchema

from gitops_engine.retire import load_retire, write_retire

SCHEMA = ToySchema()  # tags: Union, size: KeepIfUnset; name, status, ids: Always


def test_retire_round_trip_bytes(tmp_path):
    path = tmp_path / "retire.yaml"
    entries = {
        "zeta": {"tags": ["z.com", "y.io"], "size": 500},
        "alpha": {"size": 100},
        "empty": {},
    }
    write_retire(entries, path, header="hand-curated\nsecond line")
    first = path.read_bytes()
    assert first.startswith(b"# hand-curated\n# second line\n")
    assert list(yaml.safe_load(first)) == ["alpha", "zeta"]  # sorted; the empty entry is dropped
    loaded = load_retire(path, SCHEMA)
    assert loaded["zeta"] == {"tags": ["z.com", "y.io"], "size": 500}
    write_retire(loaded, path, header="hand-curated\nsecond line")
    assert path.read_bytes() == first


def test_retire_file_format_is_pinned(tmp_path):
    path = tmp_path / "retire.yaml"
    write_retire({"b": {"tags": ["x"], "size": 0}, "a": {"size": 2}}, path, header="h")
    assert path.read_text() == "# h\na:\n  size: 2\nb:\n  tags:\n  - x\n  size: 0\n"
    write_retire({}, path, header="only a header")
    assert path.read_text() == "# only a header\n"


def test_retire_ignores_always_field(tmp_path):
    path = tmp_path / "retire.yaml"
    for field in ("name", "status", "ids", "unknown"):
        path.write_text(f"gear:\n  {field}: x\n  size: 3\n")
        got = load_retire(path, SCHEMA)  # nothing raises
        assert got == {"gear": {"tags": [], "size": 3}}
        assert field not in got["gear"]


def test_retire_missing_or_empty_file_is_empty(tmp_path):
    assert load_retire(tmp_path / "missing.yaml", SCHEMA) == {}
    (tmp_path / "e.yaml").write_text("# nothing\n")
    assert load_retire(tmp_path / "e.yaml", SCHEMA) == {}


def test_retire_loads_every_retirable_field_per_entry(tmp_path):
    path = tmp_path / "retire.yaml"
    path.write_text("gear:\n  tags: [1, B]\nbare:\ncog: {}\n")
    got = load_retire(path, SCHEMA)
    # a Union field is a list of strings, a KeepIfUnset field a scalar or None
    assert got == {
        "gear": {"tags": ["1", "B"], "size": None},
        "bare": {"tags": [], "size": None},
        "cog": {"tags": [], "size": None},
    }
