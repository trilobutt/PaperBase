"""Tests for paperbase.core.taxonomy."""

from pathlib import Path

import pytest

from paperbase.core.taxonomy import (
    MAX_NAME,
    Label,
    TaxonomyError,
    load_taxonomy,
    parse_taxonomy,
    save_taxonomy,
)


def test_parse_basic() -> None:
    text = "Ecology\nGenetics: the study of genes\n"
    labels = parse_taxonomy(text)
    assert labels == [
        Label(name="Ecology", description=""),
        Label(name="Genetics", description="the study of genes"),
    ]
    assert labels[0].text == "Ecology"
    assert labels[1].text == "Genetics. the study of genes"


def test_comments_and_blanks_ignored() -> None:
    text = "# a comment\n\nEcology\n   \n# another\nGenetics\n"
    labels = parse_taxonomy(text)
    assert [label.name for label in labels] == ["Ecology", "Genetics"]


def test_only_first_colon_splits() -> None:
    labels = parse_taxonomy("Isotopes: C, N: ratios\n")
    assert len(labels) == 1
    assert labels[0].name == "Isotopes"
    assert labels[0].description == "C, N: ratios"


def test_duplicates_dropped_case_insensitively() -> None:
    labels = parse_taxonomy("Ecology\nECOLOGY\n")
    assert len(labels) == 1
    assert labels[0].name == "Ecology"


def test_invalid_lines_skipped() -> None:
    overlong = "x" * (MAX_NAME + 1)
    text = f"Ecology\n{overlong}\n: no name\nGenetics\n"
    labels = parse_taxonomy(text)
    assert [label.name for label in labels] == ["Ecology", "Genetics"]


def test_no_label_cap() -> None:
    text = "\n".join(f"Label{i}" for i in range(2500))
    labels = parse_taxonomy(text)
    assert len(labels) == 2500


def test_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "taxonomy.txt"
    labels = [
        Label(name="Ecology", description=""),
        Label(name="Genetics", description="the study of genes"),
    ]
    save_taxonomy(path, labels)
    reloaded = load_taxonomy(path)
    assert reloaded == labels


def test_missing_file_returns_empty(tmp_path: Path) -> None:
    assert load_taxonomy(None) == []
    assert load_taxonomy(tmp_path / "does_not_exist.txt") == []


def test_directory_raises(tmp_path: Path) -> None:
    with pytest.raises(TaxonomyError):
        load_taxonomy(tmp_path)


def test_hierarchy_parsed() -> None:
    text = (
        "Palaeontology: fossils\n"
        "Palaeontology > Taphonomy: decay\n"
        "palaeontology > Ichnology\n"
    )
    labels = parse_taxonomy(text)
    assert [label.path for label in labels] == [
        ("Palaeontology",),
        ("Palaeontology", "Taphonomy"),
        ("Palaeontology", "Ichnology"),
    ]
    assert labels[1].description == "decay"


def test_child_before_parent_skipped() -> None:
    labels = parse_taxonomy("Evolution > Stasis\nEvolution\n")
    assert [label.path for label in labels] == [("Evolution",)]


def test_same_leaf_under_different_parents() -> None:
    labels = parse_taxonomy("A\nB\nA > Methods\nB > Methods\n")
    assert [label.path for label in labels] == [
        ("A",), ("B",), ("A", "Methods"), ("B", "Methods"),
    ]


def test_hierarchy_round_trip(tmp_path: Path) -> None:
    labels = [
        Label(name="Evolution", description="theory"),
        Label(name="Stasis", parents=("Evolution",)),
    ]
    path = tmp_path / "taxonomy.txt"
    save_taxonomy(path, labels)
    assert load_taxonomy(path) == labels
