import logging
import re
from pathlib import Path
from typing import Callable, Optional

import tantivy

from paperbase.models.paper import Paper
from paperbase.models.search_result import SearchResult

logger = logging.getLogger(__name__)

SNIPPET_MAX_CHARS = 300

# Splits a query into clauses: quoted phrases, "field:[range]" tokens, or bare
# whitespace-separated words — mirrors what Tantivy's own parser treats as one unit.
_QUERY_TOKEN_RE = re.compile(r'"[^"]*"|\S+:\[[^\]]*\]|\S+:\{[^}]*\}|\S+')
_FIELD_PREFIX_RE = re.compile(r"^([A-Za-z_]\w*):(.+)$")


def _glob_to_regex(pattern: str) -> str:
    # regex_query matches the full, lowercased indexed term — not a substring —
    # so no anchors are needed, just escape the literal runs around each '*'.
    return ".*".join(re.escape(part) for part in pattern.lower().split("*"))


def _build_schema() -> tantivy.Schema:
    builder = tantivy.SchemaBuilder()
    builder.add_integer_field("paper_id", stored=True, indexed=True)
    builder.add_text_field("title",    stored=True,  tokenizer_name="en_stem")
    builder.add_text_field("abstract", stored=False, tokenizer_name="en_stem")
    builder.add_text_field("authors",  stored=False, tokenizer_name="en_stem")
    builder.add_text_field("keywords", stored=False, tokenizer_name="en_stem")
    builder.add_text_field("fulltext", stored=False, tokenizer_name="en_stem")
    builder.add_integer_field("year",  stored=True,  indexed=True)
    return builder.build()


class Indexer:
    def __init__(self, index_dir: Path) -> None:
        self._index_dir = index_dir
        self._index: Optional[tantivy.Index] = None
        self._writer: Optional[tantivy.IndexWriter] = None
        self._schema = _build_schema()

    def open(self) -> None:
        self._index_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._index = tantivy.Index(self._schema, str(self._index_dir))
        except Exception:
            # Index may not exist yet — create it
            self._index = tantivy.Index(self._schema, str(self._index_dir), reuse=False)
        # Writer is created lazily on first write to avoid the 128 MB heap
        # allocation and Rust thread startup on every app launch.

    def close(self) -> None:
        if self._writer:
            try:
                self._writer.commit()
            except Exception:
                pass
        self._index = None
        self._writer = None

    def _ensure_open(self) -> None:
        if self._index is None:
            self.open()

    def _ensure_writer(self) -> None:
        if self._writer is None:
            assert self._index is not None
            self._writer = self._index.writer(heap_size=128 * 1024 * 1024)

    def add_document(self, paper: Paper, fulltext: str) -> None:
        self._ensure_open()
        self._ensure_writer()
        assert self._writer is not None
        doc = tantivy.Document()
        doc.add_integer("paper_id", paper.id or 0)
        doc.add_text("title", paper.title or "")
        doc.add_text("abstract", paper.abstract or "")
        doc.add_text("authors", " ".join(paper.authors))
        doc.add_text("keywords", " ".join(paper.keywords))
        doc.add_text("fulltext", fulltext or "")
        if paper.year:
            doc.add_integer("year", paper.year)
        self._writer.add_document(doc)

    def commit(self) -> None:
        self._ensure_writer()
        if self._writer:
            self._writer.commit()

    def delete_document(self, paper_id: int) -> None:
        self._ensure_open()
        self._ensure_writer()
        assert self._writer is not None
        self._writer.delete_documents("paper_id", paper_id)
        self._writer.commit()

    def index_papers_bulk(
        self,
        papers: list[tuple[Paper, str]],   # (paper, fulltext)
        progress_cb: Optional[Callable[[int, int], None]] = None,
    ) -> None:
        """Index a batch of papers. Commits at the end."""
        self._ensure_open()
        self._ensure_writer()
        total = len(papers)
        for i, (paper, fulltext) in enumerate(papers):
            self.add_document(paper, fulltext)
            if progress_cb and (i % 50 == 0 or i == total - 1):
                progress_cb(i + 1, total)
        self.commit()

    def search(self, query_str: str) -> list[SearchResult]:
        self._ensure_open()
        assert self._index is not None
        if not query_str.strip():
            return []

        # Reload searcher so it sees latest committed segments
        self._index.reload()
        searcher = self._index.searcher()

        default_fields = ["title", "abstract", "authors", "keywords", "fulltext"]
        try:
            if "*" in query_str:
                query = self._parse_wildcard_query(query_str, default_fields)
            else:
                query = self._index.parse_query(query_str, default_fields)
        except Exception:
            escaped = query_str.replace('"', '\\"')
            try:
                query = self._index.parse_query(f'"{escaped}"', default_fields)
            except Exception:
                return []

        # num_docs gives all matching documents — no artificial cap
        hits = searcher.search(query, searcher.num_docs).hits

        results: list[SearchResult] = []
        for score, doc_addr in hits:
            doc = searcher.doc(doc_addr)
            paper_id = doc.get_first("paper_id")
            title = doc.get_first("title") or ""
            year_val = doc.get_first("year")

            # Build a simple snippet from the title since fulltext isn't stored
            snippet = title[:SNIPPET_MAX_CHARS]

            results.append(
                SearchResult(
                    paper_id=int(paper_id),
                    title=title,
                    authors=[],     # caller fills from DB
                    journal="",     # caller fills from DB
                    year=int(year_val) if year_val is not None else None,
                    snippet=snippet,
                    score=float(score),
                )
            )
        return results

    def _parse_wildcard_query(self, query_str: str, default_fields: list[str]) -> tantivy.Query:
        """Build a query for strings containing '*', which Tantivy's parser can't handle natively."""
        clauses: list[tuple[tantivy.Occur, tantivy.Query]] = []
        pending_occur = tantivy.Occur.Should
        for raw in _QUERY_TOKEN_RE.findall(query_str):
            upper = raw.upper()
            if upper == "AND":
                pending_occur = tantivy.Occur.Must
                continue
            if upper == "OR":
                pending_occur = tantivy.Occur.Should
                continue
            if upper == "NOT":
                pending_occur = tantivy.Occur.MustNot
                continue

            occur, token = pending_occur, raw
            if token.startswith("+"):
                occur, token = tantivy.Occur.Must, token[1:]
            elif token.startswith("-"):
                occur, token = tantivy.Occur.MustNot, token[1:]
            pending_occur = tantivy.Occur.Should

            subquery = self._build_clause_query(token, default_fields)
            if subquery is not None:
                clauses.append((occur, subquery))

        if not clauses:
            raise ValueError(f"no usable clauses in query: {query_str!r}")
        if len(clauses) == 1 and clauses[0][0] != tantivy.Occur.MustNot:
            return clauses[0][1]
        return tantivy.Query.boolean_query(clauses)

    def _build_clause_query(
        self, token: str, default_fields: list[str]
    ) -> Optional[tantivy.Query]:
        """Parse one whitespace-separated clause: a plain term/range via Tantivy's parser,
        or — if it contains '*' — a glob expanded into a regex over the relevant field(s)."""
        if "*" not in token or token.startswith('"'):
            try:
                return self._index.parse_query(token, default_fields)
            except Exception:
                logger.warning("Failed to parse query clause %r", token)
                return None

        match = _FIELD_PREFIX_RE.match(token)
        if match and match.group(1) in default_fields:
            fields, pattern = [match.group(1)], match.group(2)
        else:
            fields, pattern = default_fields, token

        if not pattern.replace("*", ""):
            logger.warning("Wildcard-only query clause %r is too broad, skipping", token)
            return None

        regex = _glob_to_regex(pattern)
        field_queries = [
            tantivy.Query.regex_query(self._schema, field, regex) for field in fields
        ]
        if len(field_queries) == 1:
            return field_queries[0]
        return tantivy.Query.boolean_query([(tantivy.Occur.Should, q) for q in field_queries])

    def document_count(self) -> int:
        self._ensure_open()
        assert self._index is not None
        self._index.reload()
        return self._index.searcher().num_docs
