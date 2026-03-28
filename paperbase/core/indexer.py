import logging
from pathlib import Path
from typing import Callable, Optional

import tantivy

from paperbase.models.paper import Paper
from paperbase.models.search_result import SearchResult

logger = logging.getLogger(__name__)

SNIPPET_MAX_CHARS = 300


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
        self._writer = self._index.writer(heap_size=128 * 1024 * 1024)

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

    def add_document(self, paper: Paper, fulltext: str) -> None:
        self._ensure_open()
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
        if self._writer:
            self._writer.commit()

    def delete_document(self, paper_id: int) -> None:
        self._ensure_open()
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
        total = len(papers)
        for i, (paper, fulltext) in enumerate(papers):
            self.add_document(paper, fulltext)
            if progress_cb and (i % 50 == 0 or i == total - 1):
                progress_cb(i + 1, total)
        self.commit()

    def search(self, query_str: str, limit: int = 50) -> list[SearchResult]:
        self._ensure_open()
        assert self._index is not None
        if not query_str.strip():
            return []

        # Reload searcher so it sees latest committed segments
        self._index.reload()
        searcher = self._index.searcher()

        default_fields = ["title", "abstract", "authors", "keywords", "fulltext"]
        try:
            query = self._index.parse_query(query_str, default_fields)
        except Exception:
            escaped = query_str.replace('"', '\\"')
            try:
                query = self._index.parse_query(f'"{escaped}"', default_fields)
            except Exception:
                return []

        hits = searcher.search(query, limit).hits

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

    def document_count(self) -> int:
        self._ensure_open()
        assert self._index is not None
        self._index.reload()
        return self._index.searcher().num_docs
