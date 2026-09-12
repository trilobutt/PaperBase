"""Verify the content_hash schema, its queries, and the pre-content_hash migration path.

    py -3.12 tools/check_hash_schema.py

Prints "hash schema OK" and exits 0 on success; raises AssertionError on the first
failing check otherwise.
"""
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from paperbase.core.db import Database
from paperbase.models.paper import Paper

# The papers table exactly as it existed before content_hash was added, pasted verbatim
# so the migration test exercises a real pre-upgrade database rather than a paraphrase.
PRE_CONTENT_HASH_SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    doi             TEXT UNIQUE,
    title           TEXT NOT NULL DEFAULT '',
    authors         TEXT NOT NULL DEFAULT '[]',
    journal         TEXT NOT NULL DEFAULT '',
    year            INTEGER,
    volume          TEXT NOT NULL DEFAULT '',
    issue           TEXT NOT NULL DEFAULT '',
    pages           TEXT NOT NULL DEFAULT '',
    abstract        TEXT NOT NULL DEFAULT '',
    keywords        TEXT NOT NULL DEFAULT '[]',
    tags            TEXT NOT NULL DEFAULT '[]',
    collection_ids  TEXT NOT NULL DEFAULT '[]',
    file_path       TEXT NOT NULL UNIQUE,
    date_added      TEXT NOT NULL,
    date_modified   TEXT NOT NULL,
    metadata_source TEXT NOT NULL DEFAULT 'unknown',
    needs_review    INTEGER NOT NULL DEFAULT 0,
    open_access     INTEGER NOT NULL DEFAULT 0,
    isbn            TEXT,
    document_type   TEXT NOT NULL DEFAULT 'article'
);
"""


def _make_paper(
    doi: str,
    file_path: str,
    content_hash: str | None,
    isbn: str | None = None,
) -> Paper:
    return Paper(
        id=None,
        doi=doi,
        title="A Title",
        authors=["Lastname, Firstname"],
        journal="A Journal",
        year=2020,
        volume="1",
        issue="1",
        pages="1-10",
        abstract="",
        keywords=[],
        tags=[],
        collection_ids=[],
        file_path=file_path,
        date_added="",
        date_modified="",
        metadata_source="manual",
        needs_review=False,
        open_access=False,
        isbn=isbn,
        document_type="article",
        content_hash=content_hash,
    )


def check_queries() -> None:
    db_path = Path(tempfile.mkdtemp()) / "paperbase.db"
    db = Database(db_path)
    db.open()

    # Empty-database case must not raise.
    assert db.get_hash_coverage() == (0, 0), "expected (0, 0) coverage on empty database"

    hash_a = "a" * 64
    hash_b = "b" * 64
    id1 = db.insert_paper(
        _make_paper("10.1/one", "C:/lib/one.pdf", hash_a, isbn="9780000000001")
    )
    id2 = db.insert_paper(_make_paper("10.1/two", "C:/lib/two.pdf", hash_a))
    id3 = db.insert_paper(_make_paper("10.1/three", "C:/lib/three.pdf", None))

    assert db.paper_exists_by_hash(hash_a) is True
    assert db.paper_exists_by_hash(hash_b) is False

    assert db.paper_exists_by_isbn("9780000000001") is True
    assert db.paper_exists_by_isbn("9780000000002") is False

    missing = db.get_papers_missing_hash()
    assert missing == [(id3, "C:/lib/three.pdf")], f"unexpected missing-hash rows: {missing}"

    assert db.get_hash_coverage() == (2, 3), f"unexpected coverage: {db.get_hash_coverage()}"

    groups = db.get_duplicate_hash_groups()
    assert len(groups) == 1, f"expected exactly one duplicate group, got {groups}"
    dup_hash, dup_ids = groups[0]
    assert dup_hash == hash_a
    assert sorted(dup_ids) == sorted([id1, id2]), f"unexpected duplicate ids: {dup_ids}"

    # Round-trip via get_paper / update_paper.
    paper = db.get_paper(id3)
    assert paper is not None
    assert paper.content_hash is None
    paper.content_hash = "c" * 64
    db.update_paper(paper)
    reloaded = db.get_paper(id3)
    assert reloaded is not None
    assert reloaded.content_hash == "c" * 64, "content_hash did not survive get_paper/update_paper round-trip"

    # Round-trip via get_papers_slim_by_ids — the _SLIM_COLS regression.
    slim = db.get_papers_slim_by_ids([id3])
    assert len(slim) == 1
    assert slim[0].content_hash == "c" * 64, "content_hash missing from slim row"
    db.update_paper(slim[0])
    reloaded_after_slim = db.get_paper(id3)
    assert reloaded_after_slim is not None
    assert reloaded_after_slim.content_hash == "c" * 64, (
        "content_hash was nulled out by a slim-loaded Paper written back via update_paper"
    )

    db.update_paper_field(id3, "content_hash", "d" * 64)
    after_field_update = db.get_paper(id3)
    assert after_field_update is not None
    assert after_field_update.content_hash == "d" * 64

    assert not hasattr(Database, "paper_exists_by_path"), "paper_exists_by_path was not removed"

    db.close()


def check_migration() -> None:
    db_path = Path(tempfile.mkdtemp()) / "legacy.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(PRE_CONTENT_HASH_SCHEMA)
    conn.commit()
    conn.close()

    db = Database(db_path)
    db.open()  # must not raise against a pre-content_hash database
    db.close()

    check_conn = sqlite3.connect(str(db_path))
    columns = {row[1] for row in check_conn.execute("PRAGMA table_info(papers)")}
    check_conn.close()
    assert "content_hash" in columns, f"content_hash missing after migration: {columns}"

    db.open()
    db.get_papers_missing_hash()  # must run without error against the migrated table
    db.close()


if __name__ == "__main__":
    check_queries()
    check_migration()
    print("hash schema OK")
