from __future__ import annotations

import yaml
from toy import Widget, widget_from_dict, widget_to_dict

from gitops_engine.manifest import MANIFEST, manifest_edits
from gitops_engine.writers import (
    load_record_dir,
    slugify,
    unique_slugs,
    write_files,
    write_record_dir,
)


def _w(name: str, **over) -> Widget:
    return Widget(**{"name": name, "status": "active", "ids": [name.lower()], **over})


def _write(rows, path, **kw):
    write_record_dir(rows, path, slug=lambda w: slugify(w.name), to_dict=widget_to_dict, **kw)


def test_slugify():
    assert slugify("Stark Fabrication LLC (The Forge)") == "stark-fabrication-llc-the-forge"
    assert slugify("  --Ünï  x__y-- ") == "n-x-y"
    assert slugify("!!!") == ""


def test_unique_slugs_suffixes_repeats_in_order():
    assert unique_slugs(["a", "b", "a", "a", "b"]) == ["a", "b", "a-2", "a-3", "b-2"]
    assert unique_slugs([]) == []


def test_write_record_dir_one_yaml_per_row_and_round_trips(tmp_path):
    d = tmp_path / "w"
    rows = [_w("Gear", tags=["x"], size=2), _w("Cog")]
    _write(rows, d)
    assert sorted(p.name for p in d.glob("*.yaml")) == ["cog.yaml", "gear.yaml"]
    assert yaml.safe_load((d / "gear.yaml").read_text()) == widget_to_dict(rows[0])
    assert [w.name for w in load_record_dir(d, widget_from_dict)] == ["Cog", "Gear"]


def test_write_record_dir_is_byte_identical_on_rerun(tmp_path):
    d = tmp_path / "w"
    _write([_w("Gear")], d)
    first = (d / "gear.yaml").read_bytes()
    _write([_w("Gear")], d)
    assert (d / "gear.yaml").read_bytes() == first
    assert first.decode() == yaml.safe_dump(
        widget_to_dict(_w("Gear")), sort_keys=False, default_flow_style=False, allow_unicode=True
    )


def test_write_record_dir_removes_stale_files_but_not_the_manifest(tmp_path):
    d = tmp_path / "w"
    _write([_w("Gear"), _w("Cog")], d, generator="gen")
    assert (d / MANIFEST).exists() and manifest_edits(d) == []
    _write([_w("Gear")], d, generator="gen")
    assert sorted(p.name for p in d.glob("*.yaml")) == [".manifest.yaml", "gear.yaml"]
    assert manifest_edits(d) == []  # the manifest follows the rewrite


def test_write_record_dir_without_generator_writes_no_manifest(tmp_path):
    d = tmp_path / "w"
    _write([_w("Gear")], d)
    assert not (d / MANIFEST).exists()


def test_write_record_dir_dedups_duplicate_slugs(tmp_path):
    d = tmp_path / "w"
    _write([_w("Dup"), _w("Dup", size=9)], d)
    assert sorted(p.name for p in d.glob("*.yaml")) == ["dup-2.yaml", "dup.yaml"]


def test_write_files_uses_the_given_stems(tmp_path):
    d = tmp_path / "w"
    write_files([("id-1", _w("Gear")), ("id-2", _w("Cog"))], d, to_dict=widget_to_dict)
    assert sorted(p.stem for p in d.glob("*.yaml")) == ["id-1", "id-2"]


def test_write_leaves_files_outside_the_directory_alone(tmp_path):
    curated = tmp_path / "curated.yaml"
    curated.write_text("keep: me\n")
    _write([_w("Gear")], tmp_path / "w")
    assert curated.read_text() == "keep: me\n"


def test_load_record_dir_of_a_missing_directory_is_empty(tmp_path):
    assert load_record_dir(tmp_path / "nope", widget_from_dict) == []
