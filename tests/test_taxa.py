"""Tests for paperbase.core.taxa: the taxa.txt parser and the lexical matcher."""

import logging
from pathlib import Path

import pytest

from paperbase.core.taxa import TaxonTree, load_taxa, parse_taxa, plural_variants
from paperbase.core.taxonomy import TaxonomyError

SAMPLE = """\
# comment
Arthropoda: arthropods
Arthropoda > Araneae: spiders
Arthropoda > Pycnogonida: sea spiders
Arthropoda > Hexapoda
Arthropoda > Hexapoda > Coleoptera: beetles, coleopterans
Arthropoda > Hexapoda > Coleoptera > Histeridae: clown beetles
Arthropoda > Hexapoda > Diptera: true flies
Arthropoda > Hexapoda > Diptera > Drosophilidae: Drosophila
Arthropoda > Crustacea [informal]: crustaceans
Chordata
Chordata > Hominini: hominins, Homo
"""


@pytest.fixture
def tree() -> TaxonTree:
    return parse_taxa(SAMPLE)


def test_structure(tree: TaxonTree) -> None:
    assert len(tree) == 11
    assert tree.children(None) == ["Arthropoda", "Chordata"]
    assert tree.ancestors("Histeridae") == ["Coleoptera", "Hexapoda", "Arthropoda"]
    assert set(tree.descendants_and_self("Hexapoda")) == {
        "Hexapoda", "Coleoptera", "Histeridae", "Diptera", "Drosophilidae",
    }
    assert tree.get("Crustacea").informal is True
    assert tree.get("Coleoptera").informal is False
    assert tree.canonical("  histeridae ") == "Histeridae"
    assert tree.canonical("Histerid") is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("A revision of Operclipygus (Coleoptera: Histeridae)", ["Histeridae"]),
        ("Clown Beetles of Borneo", ["Histeridae"]),
        ("a beetle and a spider", ["Araneae", "Coleoptera"]),
        ("Sea spiders and true spiders", ["Araneae", "Pycnogonida"]),
        ("sea, spiders", ["Araneae"]),
        ("NEW HISTERIDAE FROM BRAZIL", ["Histeridae"]),
        ("Homo sapiens", ["Hominini"]),
        ("HOMO-LUMO gaps", []),
        ("HOMO SAPIENS", []),
        ("Drosophila melanogaster", ["Drosophilidae"]),
        ("drosophila", []),
        ("non-coleopterans", []),
        ("Coleopterans and dipteran larvae", ["Coleoptera"]),
        ("", []),
    ],
)
def test_match(tree: TaxonTree, text: str, expected: list[str]) -> None:
    assert tree.match(text) == expected


def test_bad_lines_and_aliases_are_skipped(caplog: pytest.LogCaptureFixture) -> None:
    text = SAMPLE + (
        "Nowhere > Orphan: orphans\n"
        "Chordata > Coleoptera: dupes\n"
        "Chordata > Aves: birds, beetles, Diptera, birds\n"
        " > Empty\n"
    )
    with caplog.at_level(logging.WARNING, logger="paperbase.core.taxa"):
        tree = parse_taxa(text)
    assert "Orphan" not in tree
    assert tree.get("Coleoptera").parent == "Hexapoda"
    assert tree.get("Aves").aliases == ("birds",)
    assert len(tree) == 12
    assert len(caplog.records) == 5


@pytest.mark.parametrize(
    ("alias", "forms"),
    [
        ("beetles", {"beetle", "beetles"}),
        ("true flies", {"true flies", "true fly"}),
        ("fishes", {"fish", "fishes"}),
        ("mosquitoes", {"mosquito", "mosquitoes"}),
        ("octopuses", {"octopus", "octopuses"}),
    ],
)
def test_plural_variants(alias: str, forms: set[str]) -> None:
    assert plural_variants(alias) == forms


def test_load(tmp_path: Path) -> None:
    assert len(load_taxa(None)) == 0
    assert len(load_taxa(tmp_path / "missing.txt")) == 0
    path = tmp_path / "taxa.txt"
    path.write_text(SAMPLE, encoding="utf-8")
    assert len(load_taxa(path)) == 11
    with pytest.raises(TaxonomyError):
        load_taxa(tmp_path)
