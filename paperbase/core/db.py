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
    open_access     INTEGER NOT NULL DEFAULT 0,
    isbn            TEXT,
    document_type   TEXT NOT NULL DEFAULT 'article',
    content_hash    TEXT,
    taxa            TEXT NOT NULL DEFAULT '[]',
    taxa_locked     INTEGER NOT NULL DEFAULT 0
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
-- Sort keys for the listing view. NOCASE variants are required because the listing
-- sorts case-insensitively and a BINARY index cannot serve a NOCASE ORDER BY.
CREATE INDEX IF NOT EXISTS idx_papers_title_nocase   ON papers(title COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_papers_journal_nocase ON papers(journal COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_papers_date_added     ON papers(date_added);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# All columns except abstract and keywords — used for the listing view where
# neither field is displayed. Avoids loading tens of MB of text on startup.
_SLIM_COLS = (
    "id, doi, title, authors, journal, year, volume, issue, pages, "
    "tags, collection_ids, file_path, date_added, date_modified, "
    "metadata_source, needs_review, open_access, isbn, document_type, content_hash, "
    "taxa, taxa_locked"
)


def _paper_from_row(row: sqlite3.Row) -> Paper:
    keys = row.keys()
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
        isbn=row["isbn"] if "isbn" in keys else None,
        document_type=row["document_type"] if "document_type" in keys else "article",
        content_hash=row["content_hash"] if "content_hash" in keys else None,
        taxa=json.loads(row["taxa"]) if "taxa" in keys else [],
        taxa_locked=bool(row["taxa_locked"]) if "taxa_locked" in keys else False,
    )


def _paper_slim_from_row(row: sqlite3.Row) -> Paper:
    keys = row.keys()
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
        abstract="",
        keywords=[],
        tags=json.loads(row["tags"]),
        collection_ids=json.loads(row["collection_ids"]),
        file_path=row["file_path"],
        date_added=row["date_added"],
        date_modified=row["date_modified"],
        metadata_source=row["metadata_source"],
        needs_review=bool(row["needs_review"]),
        open_access=bool(row["open_access"]),
        isbn=row["isbn"] if "isbn" in keys else None,
        document_type=row["document_type"] if "document_type" in keys else "article",
        content_hash=row["content_hash"] if "content_hash" in keys else None,
        taxa=json.loads(row["taxa"]) if "taxa" in keys else [],
        taxa_locked=bool(row["taxa_locked"]) if "taxa_locked" in keys else False,
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
        self._migrate()
        # Reused by search_filter as the candidate-id set. Lives in the temp schema, so it
        # is per-connection and never touches the database file.
        self._conn.execute(
            "CREATE TEMP TABLE IF NOT EXISTS search_ids (id INTEGER PRIMARY KEY, rank INTEGER)"
        )
        self._conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after initial release (ALTER TABLE is idempotent via try/except)."""
        assert self._conn is not None
        for stmt in (
            "ALTER TABLE papers ADD COLUMN isbn TEXT",
            "ALTER TABLE papers ADD COLUMN document_type TEXT NOT NULL DEFAULT 'article'",
            "ALTER TABLE papers ADD COLUMN content_hash TEXT",
            "ALTER TABLE papers ADD COLUMN taxa TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE papers ADD COLUMN taxa_locked INTEGER NOT NULL DEFAULT 0",
        ):
            try:
                self._conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # column already exists
        # Indexes on late-added columns cannot live in SCHEMA_SQL: open() runs that script
        # through executescript before this method, so on a database predating the column the
        # CREATE INDEX would abort open() with "no such column". Here the ALTER above has
        # already run, and IF NOT EXISTS makes the repeat harmless on every later open.
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_papers_content_hash ON papers(content_hash)"
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_papers_isbn ON papers(isbn)")

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
                 date_added, date_modified, metadata_source, needs_review, open_access,
                 isbn, document_type, content_hash, taxa, taxa_locked)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                paper.isbn,
                paper.document_type,
                paper.content_hash,
                json.dumps(paper.taxa),
                int(paper.taxa_locked),
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
                needs_review=?, open_access=?, isbn=?, document_type=?,
                content_hash=?, taxa=?, taxa_locked=?
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
                paper.isbn,
                paper.document_type,
                paper.content_hash,
                json.dumps(paper.taxa),
                int(paper.taxa_locked),
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
            "isbn", "document_type", "content_hash", "taxa", "taxa_locked",
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
        by_id: dict[int, Paper] = {}
        # SQLite SQLITE_MAX_VARIABLE_NUMBER is 999 on many builds; chunk to be safe.
        chunk_size = 900
        for i in range(0, len(ids), chunk_size):
            chunk = ids[i : i + chunk_size]
            placeholders = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT * FROM papers WHERE id IN ({placeholders})", chunk
            ).fetchall()
            by_id.update({r["id"]: _paper_from_row(r) for r in rows})
        # preserve order
        return [by_id[i] for i in ids if i in by_id]

    def get_papers_slim_by_ids(self, ids: list[int]) -> list[Paper]:
        """Like get_papers_by_ids but omits abstract and keywords for fast listing."""
        if not ids:
            return []
        conn = self._conn_required()
        by_id: dict[int, Paper] = {}
        chunk_size = 900
        for i in range(0, len(ids), chunk_size):
            chunk = ids[i : i + chunk_size]
            placeholders = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT {_SLIM_COLS} FROM papers WHERE id IN ({placeholders})", chunk
            ).fetchall()
            by_id.update({r["id"]: _paper_slim_from_row(r) for r in rows})
        return [by_id[i] for i in ids if i in by_id]

    def paper_exists_by_hash(self, content_hash: str) -> bool:
        conn = self._conn_required()
        row = conn.execute(
            "SELECT 1 FROM papers WHERE content_hash=?", (content_hash,)
        ).fetchone()
        return row is not None

    def paper_exists_by_isbn(self, isbn: str) -> bool:
        conn = self._conn_required()
        row = conn.execute("SELECT 1 FROM papers WHERE isbn=?", (isbn,)).fetchone()
        return row is not None

    def get_papers_missing_hash(self) -> list[tuple[int, str]]:
        """(id, file_path) for every row with no content hash yet.

        The backfill tool is resumable purely by re-running this: a row leaves the result set
        as soon as its hash is written.
        """
        conn = self._conn_required()
        rows = conn.execute(
            "SELECT id, file_path FROM papers "
            "WHERE content_hash IS NULL OR content_hash = '' ORDER BY id"
        ).fetchall()
        return [(r["id"], r["file_path"]) for r in rows]

    def get_hash_coverage(self) -> tuple[int, int]:
        """(rows carrying a content hash, rows in total).

        One pass over an indexed column, so the Settings dialog can state plainly how much of
        the library is protected from content duplicates every time it opens.
        """
        conn = self._conn_required()
        row = conn.execute(
            "SELECT COUNT(*) AS total, "
            "COUNT(CASE WHEN content_hash IS NOT NULL AND content_hash != '' "
            "           THEN 1 END) AS hashed "
            "FROM papers"
        ).fetchone()
        return row["hashed"], row["total"]

    def get_duplicate_hash_groups(self) -> list[tuple[str, list[int]]]:
        """Every content hash held by more than one row, with those rows' ids.

        Reporting only. Nothing in the application deletes on the strength of this.
        """
        conn = self._conn_required()
        rows = conn.execute(
            "SELECT content_hash, GROUP_CONCAT(id) AS ids FROM papers "
            "WHERE content_hash IS NOT NULL AND content_hash != '' "
            "GROUP BY content_hash HAVING COUNT(*) > 1 ORDER BY content_hash"
        ).fetchall()
        return [(r["content_hash"], [int(i) for i in r["ids"].split(",")]) for r in rows]

    def delete_paper(self, paper_id: int) -> None:
        conn = self._conn_required()
        conn.execute("DELETE FROM papers WHERE id=?", (paper_id,))
        conn.commit()

    def paper_exists_by_doi(self, doi: str) -> bool:
        conn = self._conn_required()
        row = conn.execute("SELECT 1 FROM papers WHERE doi=?", (doi,)).fetchone()
        return row is not None

    def get_paper_count(self) -> int:
        conn = self._conn_required()
        return conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]

    def get_needs_review_count(self) -> int:
        conn = self._conn_required()
        return conn.execute("SELECT COUNT(*) FROM papers WHERE needs_review=1").fetchone()[0]

    def get_all_tags(self) -> list[str]:
        """Return the sorted unique tag list.

        json_each keeps the scan inside SQLite. Parsing 150k JSON arrays in Python took
        seconds on the startup path.
        """
        conn = self._conn_required()
        rows = conn.execute(
            "SELECT DISTINCT t.value AS tag FROM papers p, json_each(p.tags) t "
            "WHERE p.tags != '[]' ORDER BY tag COLLATE NOCASE"
        ).fetchall()
        return [r["tag"] for r in rows]

    def get_taxon_counts(self) -> dict[str, int]:
        """Papers per stored taxon name.

        Stored names are each paper's most specific taxa; the sidebar resolves ancestors
        from the taxon tree. json_each keeps the scan inside SQLite, as in get_all_tags.
        """
        conn = self._conn_required()
        rows = conn.execute(
            "SELECT t.value AS taxon, COUNT(*) AS n FROM papers p, json_each(p.taxa) t "
            "WHERE p.taxa != '[]' GROUP BY t.value"
        ).fetchall()
        return {r["taxon"]: r["n"] for r in rows}

    def get_all_paper_ids(self) -> list[int]:
        conn = self._conn_required()
        rows = conn.execute("SELECT id FROM papers ORDER BY id").fetchall()
        return [r["id"] for r in rows]

    def get_all_file_paths(self) -> list[str]:
        conn = self._conn_required()
        rows = conn.execute("SELECT file_path FROM papers").fetchall()
        return [r["file_path"] for r in rows]

    def search_filter(
        self,
        paper_ids: Optional[list[int]],
        year_from: Optional[int] = None,
        year_to: Optional[int] = None,
        journal: Optional[str] = None,
        tags: Optional[list[str]] = None,
        taxa: Optional[list[str]] = None,
        collection_id: Optional[int] = None,
        needs_review_only: bool = False,
        document_type: Optional[str] = None,
        sort_column: str = "date_added",
        descending: bool = True,
    ) -> list[int]:
        """Return the ordered paper ids matching every supplied filter.

        paper_ids=None means no full-text pre-filter (consider every paper). An empty list
        means the search matched nothing and is a distinct case from None.

        sort_column is one of: rank, title, authors, journal, year, date_added. "rank"
        preserves the order of paper_ids and is only meaningful when paper_ids is given.
        """
        if paper_ids is not None and not paper_ids:
            return []
        conn = self._conn_required()

        sort_sql = {
            "rank":       "s.rank",
            "title":      "p.title COLLATE NOCASE",
            "authors":    "p.authors COLLATE NOCASE",
            "journal":    "p.journal COLLATE NOCASE",
            "year":       "p.year",
            "date_added": "p.date_added",
        }.get(sort_column)
        if sort_sql is None:
            raise ValueError(f"Unknown sort column: {sort_column}")
        if sort_sql == "s.rank" and paper_ids is None:
            sort_sql, descending = "p.date_added", True

        conditions: list[str] = []
        params: list[object] = []

        if year_from is not None:
            conditions.append("p.year >= ?")
            params.append(year_from)
        if year_to is not None:
            conditions.append("p.year <= ?")
            params.append(year_to)
        if journal:
            conditions.append("p.journal LIKE ?")
            params.append(f"%{journal}%")
        if needs_review_only:
            conditions.append("p.needs_review = 1")
        if document_type is not None:
            conditions.append("p.document_type = ?")
            params.append(document_type)
        if tags:
            placeholders = ",".join("?" * len(tags))
            conditions.append(
                f"EXISTS (SELECT 1 FROM json_each(p.tags) jt WHERE jt.value IN ({placeholders}))"
            )
            params.extend(tags)
        if taxa:
            # One JSON parameter rather than a placeholder per name: a taxon's descendant
            # list runs to hundreds of names, against the 999-variable ceiling.
            conditions.append(
                "EXISTS (SELECT 1 FROM json_each(p.taxa) jx "
                "WHERE jx.value IN (SELECT value FROM json_each(?)))"
            )
            params.append(json.dumps(taxa))
        if collection_id is not None:
            col_ids = sorted(self._ancestor_and_self(collection_id))
            placeholders = ",".join("?" * len(col_ids))
            conditions.append(
                f"EXISTS (SELECT 1 FROM json_each(p.collection_ids) jc "
                f"WHERE jc.value IN ({placeholders}))"
            )
            params.extend(col_ids)

        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        direction = "DESC" if descending else "ASC"

        if paper_ids is None:
            sql = f"SELECT p.id FROM papers p{where} ORDER BY {sort_sql} {direction}"
            return [r["id"] for r in conn.execute(sql, params).fetchall()]

        # A temp table beats a chunked IN clause: it has no variable limit, so the whole
        # candidate set is filtered and ordered in one statement instead of 167 of them.
        # The table is created once in open(); emptying it avoids DDL on every query.
        conn.execute("DELETE FROM temp.search_ids")
        conn.executemany(
            "INSERT OR IGNORE INTO temp.search_ids (id, rank) VALUES (?, ?)",
            [(pid, rank) for rank, pid in enumerate(paper_ids)],
        )
        join_where = where.replace(" WHERE ", " AND ", 1) if where else ""
        order = "s.rank ASC" if sort_sql == "s.rank" else f"{sort_sql} {direction}"
        sql = (
            f"SELECT p.id FROM papers p JOIN temp.search_ids s ON s.id = p.id"
            f"{join_where} ORDER BY {order}"
        )
        return [r["id"] for r in conn.execute(sql, params).fetchall()]

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

    def add_paper_to_collection(self, paper_id: int, collection_id: int) -> None:
        conn = self._conn_required()
        row = conn.execute("SELECT collection_ids FROM papers WHERE id=?", (paper_id,)).fetchone()
        if row is None:
            return
        ids: list[int] = json.loads(row["collection_ids"])
        if collection_id not in ids:
            ids.append(collection_id)
            conn.execute(
                "UPDATE papers SET collection_ids=?, date_modified=? WHERE id=?",
                (json.dumps(ids), _now_iso(), paper_id),
            )
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
