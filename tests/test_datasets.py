from __future__ import annotations

from toy import Widget, widget_from_dict, widget_to_dict

from gitops_engine.datasets import (
    DATASETS_FILE,
    DATASETS_SUBDIR,
    DatasetConfig,
    DatasetRecord,
    dataset_dir,
    datasets_errors,
    load_datasets,
    load_datasets_config,
)
from gitops_engine.manifest import MANIFEST, write_manifest
from gitops_engine.writers import slugify, write_record_dir


def _widget(name: str) -> Widget:
    return Widget(name=name, status="active", ids=[name.lower()])


def _write_dataset(root, name: str, widgets: list[Widget]) -> None:
    rows = sorted(widgets, key=lambda w: w.name.lower())
    write_record_dir(
        rows, dataset_dir(root, name), slug=lambda w: slugify(w.name), to_dict=widget_to_dict
    )


def test_constants_and_dataset_dir(tmp_path):
    assert DATASETS_FILE == "datasets.yaml" and DATASETS_SUBDIR == "datasets"
    assert dataset_dir(tmp_path, "a") == tmp_path / "datasets" / "a"


def test_unknown_dataset_keys_pass_through(tmp_path):
    (tmp_path / DATASETS_FILE).write_text("datasets: [a]\nother_key: {x: 1}\n")
    (tmp_path / "datasets" / "a").mkdir(parents=True)
    cfg = load_datasets_config(tmp_path)
    assert cfg == DatasetConfig(names=["a"], extra={"other_key": {"x": 1}})
    assert cfg.extra == {"other_key": {"x": 1}}
    assert datasets_errors(tmp_path) == []  # the engine does not judge keys it does not know


def test_config_default_names_when_file_absent(tmp_path):
    assert load_datasets_config(tmp_path) == DatasetConfig(names=[], extra={})
    assert load_datasets_config(tmp_path, default_names=["main"]).names == ["main"]
    # a present file without a `datasets` key is empty, not the default
    (tmp_path / DATASETS_FILE).write_text("other: 1\n")
    assert load_datasets_config(tmp_path, default_names=["main"]).names == []
    (tmp_path / DATASETS_FILE).write_text("- not\n- a mapping\n")
    assert load_datasets_config(tmp_path) == DatasetConfig(names=[], extra={})


def test_errors_unlisted_dir_missing_dir_duplicate_and_empty(tmp_path):
    _write_dataset(tmp_path, "main", [_widget("Acme")])
    assert datasets_errors(tmp_path, default_names=["main"]) == []
    assert datasets_errors(tmp_path) == ["datasets.yaml: empty datasets list"] + [
        "datasets.yaml: directory datasets/main is not listed"
    ]

    (tmp_path / "datasets" / "extra").mkdir()
    assert datasets_errors(tmp_path, default_names=["main"]) == [
        "datasets.yaml: directory datasets/extra is not listed"
    ]

    (tmp_path / DATASETS_FILE).write_text("datasets: [main, extra, gone]\n")
    assert datasets_errors(tmp_path) == ["datasets.yaml: gone has no directory"]

    (tmp_path / DATASETS_FILE).write_text("datasets: [main, main, extra]\n")
    assert datasets_errors(tmp_path) == ["datasets.yaml: main listed twice"]

    (tmp_path / DATASETS_FILE).write_text("datasets: []\n")
    assert "datasets.yaml: empty datasets list" in datasets_errors(tmp_path)


def test_load_datasets_priority_then_stem(tmp_path):
    _write_dataset(tmp_path, "a", [_widget("A1"), _widget("A2")])
    _write_dataset(tmp_path, "b", [_widget("B2"), _widget("B1")])
    for name in ("a", "b"):
        write_manifest(dataset_dir(tmp_path, name), "gen")
    (tmp_path / DATASETS_FILE).write_text("datasets: [b, a]\n")

    loaded = load_datasets(tmp_path, load_datasets_config(tmp_path), widget_from_dict)

    assert [(d.dataset, d.source_id) for d in loaded] == [
        ("b", "b1"),
        ("b", "b2"),
        ("a", "a1"),
        ("a", "a2"),
    ]
    assert [d.record.name for d in loaded] == ["B1", "B2", "A1", "A2"]
    assert all(isinstance(d, DatasetRecord) for d in loaded)
    assert all(d.source_id != MANIFEST.removesuffix(".yaml") for d in loaded)
