"""Smoke test proving the pytest scaffolding can import the package and hit SQLite."""

from paperbase.core.db import Database

from tests.conftest import make_paper


def test_insert_and_round_trip(db: Database) -> None:
    paper_one = make_paper(title="First paper")
    paper_two = make_paper(title="Second paper")

    pid = db.insert_paper(paper_one)
    db.insert_paper(paper_two)

    assert db.get_paper_count() == 2
    fetched = db.get_paper(pid)
    assert fetched is not None
    assert fetched.title == "First paper"
