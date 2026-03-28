import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from paperbase.models.collection import Collection
from paperbase.models.paper import Paper

SCHEMA_SQL = """
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
    open_access     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS collections (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    name      TEXT NOT NULL,
    parent_id INTEGER REFERENCES collections(id) ON DELETE SET NULL,
    UNIQUE(name, parent_id)
);

CREATE INDEX IF NOT EXISTS idx_papers_doi          ON papers(doi);
CREATE INDEX IF NOT EXISTS idx_papers_year         ON papers(year);
CREATE INDEX IF NOT EXISTS idx_papers_title        ON papers(title);
CREATE INDEX IF NOT EXISTS idx_papers_needs_review ON papers(needs_review);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _paper_from_row(row: sqlite3.Row) -> Paper:
    return Paper(
        id=row["id"],
        doi=row["doi"],
        title=row["title"],
        authors=json.loads(row["authors"]),
        journal=row["journal"],
        year=row["year"],
        volume=row["volume"],
        issue=row["issue"],
        pages=row["pages"],
        abstract=row["abstract"],
        keywords=json.loads(row["keywords"]),
        tags=json.loads(row["tags"]),
        collection_ids=json.loads(row["collection_ids"]),
        file_path=row["file_path"],
        date_added=row["date_added"],
        date_modified=row["date_modified"],
        metadata_source=row["metadata_source"],
        needs_review=bool(row["needs_review"]),
        open_access=bool(row["open_access"]),
    )


def _collection_from_row(row: sqlite3.Row) -> Collection:
    return Collection(id=row["id"], name=row["name"], parent_id=row["parent_id"])


class Database:
    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._conn: Optional[sqlite3.Connection] = None

    def open(self) -> None:
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA_SQL)
        self._conn.commit()

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def _conn_required(self) -> sqlite3.Connection:
        assert self._conn is not None, "Database not open"
        return self._conn

    # ------------------------------------------------------------------
    # Papers
    # ------------------------------------------------------------------

    def insert_paper(self, paper: Paper) -> int:
        conn = self._conn_required()
        now = _now_iso()
        cur = conn.execute(
            """
            INSERT INTO papers
                (doi, title, authors, journal, year, volume, issue, pages,
                 abstract, keywords, tags, collection_ids, file_path,
                 date_added, date_modified, metadata_source, needs_review, open_access)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                paper.doi,
                paper.title,
                json.dumps(paper.authors),
                paper.journal,
                paper.year,
                paper.volume,
                paper.issue,
                paper.pages,
                paper.abstract,
                json.dumps(paper.keywords),
                json.dumps(paper.tags),
                json.dumps(paper.collection_ids),
                paper.file_path,
                paper.date_added or now,
                now,
                paper.metadata_source,
                int(paper.needs_review),
                int(paper.open_access),
            ),
        )
        conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def update_paper(self, paper: Paper) -> None:
        conn = self._conn_required()
        conn.execute(
            """
            UPDATE papers SET
                doi=?, title=?, authors=?, journal=?, year=?, volume=?, issue=?,
                pages=?, abstract=?, keywords=?, tags=?, collection_ids=?,
                file_path=?, date_modified=?, metadata_source=?,
                needs_review=?, open_access=?
            WHERE id=?
            """,
            (
                paper.doi,
                paper.title,
                json.dumps(paper.authors),
                paper.journal,
                paper.year,
                paper.volume,
                paper.issue,
                paper.pages,
                paper.abstract,
                json.dumps(paper.keywords),
                json.dumps(paper.tags),
                json.dumps(paper.collection_ids),
                paper.file_path,
                _now_iso(),
                paper.metadata_source,
                int(paper.needs_review),
                int(paper.open_access),
                paper.id,
            ),
        )
        conn.commit()

    def update_paper_field(self, paper_id: int, field: str, value: object) -> None:
        """Single-field update for immediate inline editing."""
        allowed = {
            "doi", "title", "authors", "journal", "year", "volume", "issue",
            "pages", "abstract", "keywords", "tags", "collection_ids",
            "file_path", "metadata_source", "needs_review", "open_access",
        }
        if field not in allowed:
            raise ValueError(f"Unknown field: {field}")
        conn = self._conn_required()
        conn.execute(
            f"UPDATE papers SET {field}=?, date_modified=? WHERE id=?",
            (value, _now_iso(), paper_id),
        )
        conn.commit()

    def get_paper(self, paper_id: int) -> Optional[Paper]:
        conn = self._conn_required()
        row = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
        return _paper_from_row(row) if row else None

    def get_papers_by_ids(self, ids: list[int]) -> list[Paper]:
        if not ids:
            return []
        conn = self._conn_required()
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT * FROM papers WHERE id IN ({placeholders})", ids
        ).fetchall()
        by_id = {r["id"]: _paper_from_row(r) for r in rows}
        # preserve order
        return [by_id[i] for i in ids if i in by_id]

    def delete_paper(self, paper_id: int) -> None:
        conn = self._conn_required()
        conn.execute("DELETE FROM papers WHERE id=?", (paper_id,))
        conn.commit()

    def paper_exists_by_doi(self, doi: str) -> bool:
        conn = self._conn_required()
        row = conn.execute("SELECT 1 FROM papers WHERE doi=?", (doi,)).fetchone()
        return row is not None

    def paper_exists_by_path(self, file_path: str) -> bool:
        conn = self._conn_required()
        row = conn.execute("SELECT 1 FROM papers WHERE file_path=?", (file_path,)).fetchone()
        return row is not None

    def get_paper_count(self) -> int:
        conn = self._conn_required()
        return conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]

    def get_needs_review_count(self) -> int:
        conn = self._conn_required()
        return conn.execute("SELECT COUNT(*) FROM papers WHERE needs_review=1").fetchone()[0]

    def get_all_tags(self) -> list[str]:
        """Return sorted unique list of all tags across all papers."""
        conn = self._conn_required()
        rows = conn.execute("SELECT tags FROM papers WHERE tags != '[]'").fetchall()
        tag_set: set[str] = set()
        for row in rows:
            tag_set.update(json.loads(row["tags"]))
        return sorted(tag_set)

    def get_all_file_paths(self) -> list[str]:
        conn = self._conn_required()
        rows = conn.execute("SELECT file_path FROM papers").fetchall()
        return [r["file_path"] for r in rows]

    def search_filter(
        self,
        paper_ids: list[int],
        year_from: Optional[int] = None,
        year_to: Optional[int] = None,
        journal: Optional[str] = None,
        tags: Optional[list[str]] = None,
        collection_id: Optional[int] = None,
        needs_review_only: bool = False,
    ) -> list[int]:
        """Post-filter a list of paper_ids from Tantivy using SQLite predicates."""
        if not paper_ids:
            return []
        conn = self._conn_required()
        placeholders = ",".join("?" * len(paper_ids))
        conditions = [f"id IN ({placeholders})"]
        params: list[object] = list(paper_ids)

        if year_from is not None:
            conditions.append("year >= ?")
            params.append(year_from)
        if year_to is not None:
            conditions.append("year <= ?")
            params.append(year_to)
        if journal:
            conditions.append("journal LIKE ?")
            params.append(f"%{journal}%")
        if needs_review_only:
            conditions.append("needs_review = 1")

        where = " AND ".join(conditions)
        rows = conn.execute(f"SELECT id FROM papers WHERE {where}", params).fetchall()
        matched = {r["id"] for r in rows}

        # Tag filter: must hold in Python since tags is a JSON array
        result = [i for i in paper_ids if i in matched]
        if tags:
            filtered = []
            for pid in result:
                paper = self.get_paper(pid)
                if paper and any(t in paper.tags for t in tags):
                    filtered.append(pid)
            result = filtered

        # Collection filter
        if collection_id is not None:
            col_ids = self._ancestor_and_self(collection_id)
            filtered2 = []
            for pid in result:
                paper = self.get_paper(pid)
                if paper and any(c in col_ids for c in paper.collection_ids):
                    filtered2.append(pid)
            result = filtered2

        return result

    def get_all_papers_paginated(self, offset: int, limit: int) -> list[Paper]:
        conn = self._conn_required()
        rows = conn.execute(
            "SELECT * FROM papers ORDER BY date_added DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [_paper_from_row(r) for r in rows]

    # ------------------------------------------------------------------
    # Collections
    # ------------------------------------------------------------------

    def insert_collection(self, collection: Collection) -> int:
        conn = self._conn_required()
        cur = conn.execute(
            "INSERT INTO collections (name, parent_id) VALUES (?, ?)",
            (collection.name, collection.parent_id),
        )
        conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def update_collection(self, collection: Collection) -> None:
        conn = self._conn_required()
        conn.execute(
            "UPDATE collections SET name=?, parent_id=? WHERE id=?",
            (collection.name, collection.parent_id, collection.id),
        )
        conn.commit()

    def delete_collection(self, collection_id: int) -> None:
        """Delete collection and remove its id from all papers' collection_ids arrays."""
        conn = self._conn_required()
        rows = conn.execute(
            "SELECT id, collection_ids FROM papers WHERE collection_ids LIKE ?",
            (f"%{collection_id}%",),
        ).fetchall()
        for row in rows:
            ids: list[int] = json.loads(row["collection_ids"])
            ids = [i for i in ids if i != collection_id]
            conn.execute(
                "UPDATE papers SET collection_ids=? WHERE id=?",
                (json.dumps(ids), row["id"]),
            )
        conn.execute("DELETE FROM collections WHERE id=?", (collection_id,))
        conn.commit()

    def get_collections(self) -> list[Collection]:
        conn = self._conn_required()
        rows = conn.execute("SELECT * FROM collections ORDER BY name").fetchall()
        return [_collection_from_row(r) for r in rows]

    def get_collection(self, collection_id: int) -> Optional[Collection]:
        conn = self._conn_required()
        row = conn.execute("SELECT * FROM collections WHERE id=?", (collection_id,)).fetchone()
        return _collection_from_row(row) if row else None

    def _ancestor_and_self(self, collection_id: int) -> set[int]:
        """Return collection_id plus all descendant IDs (for hierarchy filtering)."""
        conn = self._conn_required()
        all_cols = conn.execute("SELECT id, parent_id FROM collections").fetchall()
        children: dict[Optional[int], list[int]] = {}
        for row in all_cols:
            children.setdefault(row["parent_id"], []).append(row["id"])

        result: set[int] = set()
        stack = [collection_id]
        while stack:
            cid = stack.pop()
            result.add(cid)
            stack.extend(children.get(cid, []))
        return result
