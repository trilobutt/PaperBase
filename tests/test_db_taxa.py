"""papers.taxa and papers.taxa_locked: round trip, migration, filter and counts."""

import sqlite3
from pathlib import Path

from paperbase.core.db import Database
from tests.conftest import make_paper


def test_round_trip(db: Database) -> None:
    pid = db.insert_paper(make_paper(taxa=["Histeridae", "Araneae"], taxa_locked=True))
    paper = db.get_paper(pid)
    assert paper.taxa == ["Histeridae", "Araneae"] and paper.taxa_locked is True

    paper.taxa = ["Coleoptera"]
    paper.taxa_locked = False
    db.update_paper(paper)
    again = db.get_paper(pid)
    assert again.taxa == ["Coleoptera"] and again.taxa_locked is False
    assert db.get_papers_slim_by_ids([pid])[0].taxa == ["Coleoptera"]


def test_defaults(db: Database) -> None:
    paper = db.get_paper(db.insert_paper(make_paper()))
    assert paper.taxa == [] and paper.taxa_locked is False


def test_migration_adds_columns(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    first = Database(path)
    first.open()
    pid = first.insert_paper(make_paper())
    first.close()
    conn = sqlite3.connect(str(path))
    conn.execute("ALTER TABLE papers DROP COLUMN taxa")
    conn.execute("ALTER TABLE papers DROP COLUMN taxa_locked")
    conn.commit()
    conn.close()

    reopened = Database(path)
    reopened.open()
    try:
        paper = reopened.get_paper(pid)
        assert paper.taxa == [] and paper.taxa_locked is False
    finally:
        reopened.close()


def test_filter_and_counts(db: Database) -> None:
    a = db.insert_paper(make_paper(taxa=["Histeridae"]))
    b = db.insert_paper(make_paper(taxa=["Araneae", "Histeridae"]))
    db.insert_paper(make_paper(taxa=["Drosophilidae"]))
    db.insert_paper(make_paper())

    found = db.search_filter(None, taxa=["Histeridae", "Coleoptera"])
    assert sorted(found) == sorted([a, b])
    many = [f"Name{i}" for i in range(1500)] + ["Araneae"]
    assert db.search_filter(None, taxa=many) == [b]
    assert db.get_taxon_counts() == {"Histeridae": 2, "Araneae": 1, "Drosophilidae": 1}
