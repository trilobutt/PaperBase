"""Tests for paperbase.core.taxonomy."""

from pathlib import Path

import pytest

from paperbase.core.taxonomy import (
    MAX_LABELS,
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


def test_cap_enforced() -> None:
    text = "\n".join(f"Label{i}" for i in range(2500))
    labels = parse_taxonomy(text)
    assert len(labels) == MAX_LABELS


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
