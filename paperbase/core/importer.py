"""
ImportWorker: QThread that handles all three import modes.

Signals emitted on the Qt main thread via Qt signal machinery:
  item_started(label: str)
  item_finished(label: str, success: bool, needs_review: bool)
  item_failed(label: str, reason: str)
  progress(done: int, total: int, succeeded: int, needs_review: int, failed: int)
  log_message(text: str)
  finished()
"""
import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from PyQt6.QtCore import QThread, pyqtSignal

from paperbase.core.categoriser import EmbeddingCategoriser
from paperbase.core.db import Database
from paperbase.core.downloader import download_pdf_direct, download_via_unpaywall
from paperbase.core.indexer import Indexer
from paperbase.core.metadata import (
    PdfText,
    RateLimiter,
    extract_doi,
    extract_isbn,
    guess_metadata,
    read_pdf,
    resolve_book_metadata,
    resolve_metadata,
)
from paperbase.core.organiser import DEFAULT_PATTERN, copy_to_secondary, place_file
from paperbase.core.scraper import ScrapeResult, classify_url, scrape_landing_page
from paperbase.core.vectors import VectorStore
from paperbase.models.paper import Paper

logger = logging.getLogger(__name__)

# 50 rather than 200: an incremental run is about 200 papers, and at 200 it would flush once,
# at the very end, so a crash would cost the whole run's indexing.
INDEX_COMMIT_INTERVAL = 50
_MAX_CONCURRENT_ITEMS = 4                # measured 56ms/DOI against Crossref; 1 gives 152ms
_PENDING_TEXT_LIMIT = 8 * 1024 * 1024    # flush early rather than hold this much text


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ImportWorker(QThread):
    item_started   = pyqtSignal(str)
    item_finished  = pyqtSignal(str, bool, bool)   # label, success, needs_review
    item_failed    = pyqtSignal(str, str)           # label, reason
    progress       = pyqtSignal(int, int, int, int, int, int)  # done,total,ok,review,fail,dupes
    log_message    = pyqtSignal(str)
    finished_all   = pyqtSignal()

    def __init__(
        self,
        mode: str,              # "pdfs" | "dois" | "urls"
        items: list[str],       # file paths / DOI strings / URL strings
        db: Database,
        indexer: Indexer,
        library_root: Path,
        user_email: str,
        state_file: Optional[Path] = None,
        folder_pattern: str = DEFAULT_PATTERN,
        secondary_dest: Optional[Path] = None,
        categoriser: Optional[EmbeddingCategoriser] = None,
        vector_store: Optional[VectorStore] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._mode = mode
        self._items = items
        self._db = db
        self._indexer = indexer
        self._library_root = library_root
        self._user_email = user_email
        self._state_file = state_file
        self._folder_pattern = folder_pattern
        self._secondary_dest = secondary_dest
        self._categoriser = categoriser
        self._vector_store = vector_store
        self._pause_requested = False
        self._stop_requested = False
        self._tmp_dir = library_root / "tmp"
        self._pending: list[tuple[Paper, str]] = []
        self._pending_state: list[str] = []
        self._pending_bytes = 0
        # Identity keys claimed by items already in flight this run. Bounded by the item
        # count and discarded with the worker.
        self._claimed: dict[str, set[str]] = {"hash": set(), "doi": set(), "isbn": set()}
        self._total = 0
        self._done = 0
        self._skipped = 0
        self._imported = 0
        self._review_count = 0
        self._failed = 0
        self._dupes = 0
        self._worked = 0
        self._start_time = 0.0

    def _is_known(self, kind: str, value: Optional[str]) -> bool:
        """Cheap pre-check against the library only. Not authoritative.

        Saves a Crossref round trip or a download for something obviously already held.
        Another in-flight item may be about to insert the same key, which is why
        `_claim_paper` and not this is what decides.
        """
        if not value:
            return False
        if kind == "doi":
            return self._db.paper_exists_by_doi(value)
        if kind == "isbn":
            return self._db.paper_exists_by_isbn(value)
        if kind == "hash":
            return self._db.paper_exists_by_hash(value)
        raise ValueError(f"Unknown dedupe key: {kind}")

    def _claim_paper(self, paper: Paper, pdf: PdfText) -> bool:
        """The one authority on whether this paper is a duplicate. Claims its keys if not.

        Called immediately before place_file, with no await between here and insert_paper, so
        the event loop cannot interleave another item inside the window. That is what makes
        the guarantee hold with four items in flight; the database checks alone cannot,
        because the gap between a check and its insert spans an await.

        Keys are checked hash first (definitive: the same bytes are the same file), then DOI
        and ISBN (the same paper arriving as a different file). The claimed sets are bounded
        by the run's item count and die with the worker.
        """
        paper.content_hash = pdf.sha256 or None
        for kind, value in (("hash", pdf.sha256), ("doi", paper.doi), ("isbn", paper.isbn)):
            if not value:
                continue
            if value in self._claimed[kind] or self._is_known(kind, value):
                return False
        for kind, value in (("hash", pdf.sha256), ("doi", paper.doi), ("isbn", paper.isbn)):
            if value:
                self._claimed[kind].add(value)
        return True

    def _buffer(self, paper: Paper, fulltext: str) -> None:
        """Queue a placed paper for the next categorise-index-record flush."""
        self._pending.append((paper, fulltext))
        self._pending_bytes += len(fulltext)

    def _flush_pending(self) -> None:
        """Categorise, index and record everything buffered since the last flush.

        One flush point rather than per-paper work: a single encode over 50 documents is
        several times faster on CPU than 50 calls. Synchronous on purpose, and it contains no
        await, so the four workers cannot interleave inside it and no lock is needed. The
        model pass does stall them for its duration, which is the trade being made.
        """
        if self._pending:
            batch = self._pending
            self._pending = []
            self._pending_bytes = 0
            self._apply_categorisation_batch([p for p, _ in batch])
            for paper, fulltext in batch:
                self._indexer.add_document(paper, fulltext)
            self._indexer.commit()
            if self._vector_store is not None:
                self._vector_store.flush()

        # State is written last, after the index commit: an item counts as processed once its
        # row, its index entry and its tags are all durable. A crash before this costs a redo
        # of the batch, which the DOI duplicate check absorbs.
        state = self._pending_state
        self._pending_state = []
        self._append_state(state)

    def _apply_categorisation_batch(self, papers: list[Paper]) -> None:
        """Merge auto-categorisation onto a batch, and store each paper's vector. No-op if no
        categoriser is configured.

        Without a loaded model categorise_papers still returns keyword tags and taxa, which
        need none, so those land at import; collections wait for a categorisation run.
        With the model, categorise_papers does the encoding (it already batches it) and,
        given vector_store, persists each vector in the same pass, so re-running
        categorisation over the library never re-embeds.
        """
        if self._categoriser is None:
            return
        try:
            results = self._categoriser.categorise_papers(
                papers, self._db, vector_store=self._vector_store
            )
        except Exception as e:
            logger.warning("Auto-categorisation failed for a batch of %d: %s", len(papers), e)
            return
        for paper, (col_ids, tags, taxa) in zip(papers, results):
            new_col_ids = sorted(set(paper.collection_ids) | set(col_ids))
            new_tags = sorted(set(paper.tags) | set(tags))
            new_taxa = paper.taxa if paper.taxa_locked else taxa
            if (
                new_col_ids != paper.collection_ids
                or new_tags != paper.tags
                or new_taxa != paper.taxa
            ):
                paper.collection_ids = new_col_ids
                paper.tags = new_tags
                paper.taxa = new_taxa
                try:
                    self._db.update_paper(paper)
                except Exception as e:
                    logger.warning("Writing categorisation for paper %s failed: %s",
                                   paper.id, e)

    def request_pause(self) -> None:
        self._pause_requested = True

    def request_resume(self) -> None:
        self._pause_requested = False

    def request_stop(self) -> None:
        self._stop_requested = True
        self._pause_requested = False

    # ------------------------------------------------------------------

    def run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._run_async())
        finally:
            loop.close()
        self.finished_all.emit()

    async def _run_async(self) -> None:
        rate_limiter = RateLimiter()
        processed = self._load_state()
        if processed:
            # Collapse a legacy whole-set state file into the append-only format once.
            self._rewrite_state(processed)

        # Identical entries in one list are dropped before anything looks at them. Without
        # this, four workers can pull the same path or DOI concurrently and race each other
        # to the claim, which wastes a download to discover what the list already said.
        # dict.fromkeys preserves the user's ordering; a set would not.
        self._items = list(dict.fromkeys(self._items))

        self._total = len(self._items)
        # Invariant held from here on: done == imported + dupes + failed + skipped.
        self._done = 0
        self._skipped = 0
        self._imported = 0
        self._review_count = 0
        self._failed = 0
        self._dupes = 0
        self._worked = 0
        self._start_time = time.monotonic()

        # Resume skips are settled before any worker starts, so the queue holds only real work.
        queue: asyncio.Queue[str] = asyncio.Queue()
        for item in self._items:
            if item in processed:
                self._skipped += 1
                self._done += 1
            else:
                queue.put_nowait(item)
        self._emit_progress()

        # One client for the whole run. A fresh AsyncClient per request pays a TLS handshake
        # every time: measured against Crossref, 627ms per DOI against 152ms reused.
        limits = httpx.Limits(
            max_connections=_MAX_CONCURRENT_ITEMS * 2,
            max_keepalive_connections=_MAX_CONCURRENT_ITEMS * 2,
        )
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=30.0, limits=limits
        ) as client:
            workers = [
                asyncio.create_task(self._worker(queue, client, rate_limiter))
                for _ in range(_MAX_CONCURRENT_ITEMS)
            ]
            try:
                await asyncio.gather(*workers)
            finally:
                self._flush_pending()

        if self._skipped:
            self.log_message.emit(
                f"Skipped {self._skipped} already-processed item(s) from a previous run."
            )

    async def _worker(
        self, queue: "asyncio.Queue[str]", client: httpx.AsyncClient, rl: RateLimiter
    ) -> None:
        """One of _MAX_CONCURRENT_ITEMS pullers. Items overlap; one item's own steps do not."""
        while True:
            if self._stop_requested:
                return

            while self._pause_requested:
                await asyncio.sleep(0.5)
                if self._stop_requested:
                    return

            try:
                item = queue.get_nowait()
            except asyncio.QueueEmpty:
                return

            await self._process_item(item, client, rl)

    async def _process_item(
        self, item: str, client: httpx.AsyncClient, rl: RateLimiter
    ) -> None:
        self.item_started.emit(item)

        try:
            if self._mode == "pdfs":
                path = Path(item)
                # Off the event loop: PyMuPDF releases the GIL, so the other in-flight
                # items keep their network calls moving during the read.
                pdf = await asyncio.to_thread(read_pdf, path)
                ok, nr, is_dupe = await self._import_pdf(path, rl, client, pdf)
            elif self._mode == "dois":
                ok, nr, is_dupe = await self._import_doi(item, rl, client)
            else:  # urls
                ok, nr, is_dupe = await self._import_url(item, rl, client)
        except Exception as e:
            logger.exception("Unexpected error importing %s", item)
            self.item_failed.emit(item, str(e))
            ok, nr, is_dupe = False, False, False

        if ok and is_dupe:
            self._dupes += 1
        elif ok:
            self._imported += 1
            if nr:
                self._review_count += 1
        else:
            self._failed += 1

        self._pending_state.append(item)
        self._done += 1
        self._worked += 1

        if (len(self._pending) >= INDEX_COMMIT_INTERVAL
                or self._pending_bytes >= _PENDING_TEXT_LIMIT):
            self._flush_pending()

        self._emit_progress()
        self.log_message.emit(
            f"[{self._done}/{self._total}] "
            f"{Path(item).name if self._mode == 'pdfs' else item}{self._eta()}"
        )

    def _emit_progress(self) -> None:
        self.progress.emit(self._done, self._total, self._imported,
                           self._review_count, self._failed, self._dupes)

    def _eta(self) -> str:
        """Rate over items this run did real work for; items skipped on resume complete
        instantly and would otherwise collapse the estimate."""
        if self._worked <= 0:
            return ""
        avg = (time.monotonic() - self._start_time) / self._worked
        remaining = avg * max(self._total - self._done, 0)
        if remaining <= 0:
            return ""
        m, s = divmod(int(remaining), 60)
        return f" | ETA {m}m {s}s"

    # ------------------------------------------------------------------
    # Mode 1: local PDFs
    # ------------------------------------------------------------------

    async def _import_pdf(
        self, path: Path, rl: RateLimiter, client: httpx.AsyncClient, pdf: PdfText
    ) -> tuple[bool, bool, bool]:
        """`pdf` is read by the caller, off the event loop, before this is entered."""
        # The content hash is checked first and costs nothing extra: reading the file to
        # extract its text already computed it. This is the check that replaces the old
        # source-path lookup, deleted in Phase 2, which never matched because
        # place_file(move=False) stores the destination path while that lookup compared
        # the source.
        if pdf.sha256 and self._is_known("hash", pdf.sha256):
            self.log_message.emit(f"Skipped (identical file already in library): {path.name}")
            self.item_finished.emit(str(path), True, False)
            return True, False, True

        doi = extract_doi(pdf)
        if doi and self._is_known("doi", doi):
            self.log_message.emit(f"Skipped (already in library, DOI {doi}): {path.name}")
            self.item_finished.emit(str(path), True, False)
            return True, False, True

        paper = None
        if doi:
            paper = await resolve_metadata(doi, self._user_email, rl, client)

        # No DOI or Crossref returned nothing — try ISBN (book)
        if paper is None:
            isbn = extract_isbn(pdf)
            if isbn:
                if self._is_known("isbn", isbn):
                    self.log_message.emit(
                        f"Skipped (already in library, ISBN {isbn}): {path.name}"
                    )
                    self.item_finished.emit(str(path), True, False)
                    return True, False, True
                paper = await resolve_book_metadata(isbn, rl, client)

        if paper is None:
            paper = await guess_metadata(path, pdf, self._user_email, rl, client)

        if not self._claim_paper(paper, pdf):
            self.log_message.emit(f"Skipped (already in library): {path.name}")
            self.item_finished.emit(str(path), True, False)
            return True, False, True

        # Nothing between here and _buffer yields control back to the event loop, so it
        # cannot interleave another item between the claim, the destination name and the
        # inserted row.
        place_file(path, paper, self._library_root, move=False, folder_pattern=self._folder_pattern)
        if self._secondary_dest:
            copy_to_secondary(Path(paper.file_path), self._library_root, self._secondary_dest)
        paper_id = self._db.insert_paper(paper)
        paper.id = paper_id
        self._buffer(paper, pdf.fulltext)

        self.item_finished.emit(str(path), True, paper.needs_review)
        return True, paper.needs_review, False

    # ------------------------------------------------------------------
    # Mode 2: DOI strings
    # ------------------------------------------------------------------

    async def _import_doi(
        self, doi: str, rl: RateLimiter, client: httpx.AsyncClient
    ) -> tuple[bool, bool, bool]:
        doi = doi.strip()
        if self._is_known("doi", doi):
            self.item_finished.emit(doi, True, False)
            return True, False, True

        result = await download_via_unpaywall(doi, self._user_email, self._tmp_dir, rl, client)
        if not result.success:
            self.item_failed.emit(doi, result.reason)
            return False, False, False

        pdf = await asyncio.to_thread(read_pdf, result.tmp_path)  # type: ignore[arg-type]
        if pdf.sha256 and self._is_known("hash", pdf.sha256):
            result.tmp_path.unlink(missing_ok=True)  # type: ignore[union-attr]
            self.log_message.emit(f"Skipped (identical file already in library): {doi}")
            self.item_finished.emit(doi, True, False)
            return True, False, True
        paper = await resolve_metadata(doi, self._user_email, rl, client)
        if paper is None:
            paper = await guess_metadata(result.tmp_path, pdf, self._user_email, rl, client)  # type: ignore[arg-type]
        paper.open_access = True

        if not self._claim_paper(paper, pdf):
            result.tmp_path.unlink(missing_ok=True)  # type: ignore[union-attr]
            self.log_message.emit(f"Skipped (already in library): {doi}")
            self.item_finished.emit(doi, True, False)
            return True, False, True
        place_file(result.tmp_path, paper, self._library_root, move=True, folder_pattern=self._folder_pattern)  # type: ignore[arg-type]
        if self._secondary_dest:
            copy_to_secondary(Path(paper.file_path), self._library_root, self._secondary_dest)
        paper_id = self._db.insert_paper(paper)
        paper.id = paper_id
        self._buffer(paper, pdf.fulltext)

        self.item_finished.emit(doi, True, paper.needs_review)
        return True, paper.needs_review, False

    # ------------------------------------------------------------------
    # Mode 3: URLs
    # ------------------------------------------------------------------

    async def _import_url(
        self, url: str, rl: RateLimiter, client: httpx.AsyncClient
    ) -> tuple[bool, bool, bool]:
        url_type = await classify_url(url, client)

        if url_type == "pdf":
            return await self._import_direct_pdf_url(url, rl, client)
        else:
            return await self._import_landing_page(url, rl, client)

    async def _import_direct_pdf_url(
        self, url: str, rl: RateLimiter, client: httpx.AsyncClient
    ) -> tuple[bool, bool, bool]:
        result = await download_pdf_direct(url, None, self._tmp_dir, client)
        if not result.success:
            self.item_failed.emit(url, result.reason)
            return False, False, False

        tmp = result.tmp_path
        pdf = await asyncio.to_thread(read_pdf, tmp)  # type: ignore[arg-type]
        if pdf.sha256 and self._is_known("hash", pdf.sha256):
            tmp.unlink(missing_ok=True)  # type: ignore[union-attr]
            self.log_message.emit(f"Skipped (identical file already in library): {url}")
            self.item_finished.emit(url, True, False)
            return True, False, True
        doi = extract_doi(pdf)

        if doi and self._is_known("doi", doi):
            tmp.unlink(missing_ok=True)  # type: ignore[union-attr]
            self.log_message.emit(f"Skipped (already in library, DOI {doi}): {url}")
            self.item_finished.emit(url, True, False)
            return True, False, True

        paper = None
        if doi:
            paper = await resolve_metadata(doi, self._user_email, rl, client)
        if paper is None:
            isbn = extract_isbn(pdf)
            if isbn:
                paper = await resolve_book_metadata(isbn, rl, client)
        if paper is None:
            paper = await guess_metadata(tmp, pdf, self._user_email, rl, client)  # type: ignore[arg-type]
        paper.open_access = False

        if not self._claim_paper(paper, pdf):
            tmp.unlink(missing_ok=True)  # type: ignore[union-attr]
            self.log_message.emit(f"Skipped (already in library): {url}")
            self.item_finished.emit(url, True, False)
            return True, False, True
        place_file(tmp, paper, self._library_root, move=True, folder_pattern=self._folder_pattern)  # type: ignore[arg-type]
        if self._secondary_dest:
            copy_to_secondary(Path(paper.file_path), self._library_root, self._secondary_dest)
        paper_id = self._db.insert_paper(paper)
        paper.id = paper_id
        self._buffer(paper, pdf.fulltext)

        self.item_finished.emit(url, True, paper.needs_review)
        return True, paper.needs_review, False

    async def _import_landing_page(
        self, url: str, rl: RateLimiter, client: httpx.AsyncClient
    ) -> tuple[bool, bool, bool]:
        try:
            scrape: ScrapeResult = await scrape_landing_page(url, client)
        except ValueError as e:
            if str(e) == "direct_pdf":
                return await self._import_direct_pdf_url(url, rl, client)
            self.item_failed.emit(url, str(e))
            return False, False, False
        except Exception as e:
            self.item_failed.emit(url, str(e))
            return False, False, False

        if scrape.doi and self._is_known("doi", scrape.doi):
            self.log_message.emit(f"Skipped (already in library, DOI {scrape.doi}): {url}")
            self.item_finished.emit(url, True, False)
            return True, False, True

        # Try direct PDF from scrape result
        if scrape.pdf_url:
            dl = await download_pdf_direct(scrape.pdf_url, scrape.doi, self._tmp_dir, client)
            if dl.success:
                tmp = dl.tmp_path
                pdf = await asyncio.to_thread(read_pdf, tmp)  # type: ignore[arg-type]
                if pdf.sha256 and self._is_known("hash", pdf.sha256):
                    tmp.unlink(missing_ok=True)  # type: ignore[union-attr]
                    self.log_message.emit(
                        f"Skipped (identical file already in library): {url}"
                    )
                    self.item_finished.emit(url, True, False)
                    return True, False, True
                doi = scrape.doi or extract_doi(pdf)
                if doi and self._is_known("doi", doi):
                    tmp.unlink(missing_ok=True)  # type: ignore[union-attr]
                    self.log_message.emit(f"Skipped (already in library, DOI {doi}): {url}")
                    self.item_finished.emit(url, True, False)
                    return True, False, True
                if doi:
                    paper = await resolve_metadata(doi, self._user_email, rl, client)
                else:
                    paper = None
                if paper is None:
                    paper = scrape.metadata or await guess_metadata(tmp, pdf, self._user_email, rl, client)  # type: ignore[arg-type]
                paper.open_access = scrape.is_open_access
                if not self._claim_paper(paper, pdf):
                    tmp.unlink(missing_ok=True)  # type: ignore[union-attr]
                    self.log_message.emit(f"Skipped (already in library): {url}")
                    self.item_finished.emit(url, True, False)
                    return True, False, True
                place_file(tmp, paper, self._library_root, move=True, folder_pattern=self._folder_pattern)  # type: ignore[arg-type]
                if self._secondary_dest:
                    copy_to_secondary(Path(paper.file_path), self._library_root, self._secondary_dest)
                paper_id = self._db.insert_paper(paper)
                paper.id = paper_id
                self._buffer(paper, pdf.fulltext)
                self.item_finished.emit(url, True, paper.needs_review)
                return True, paper.needs_review, False
            # Fall through to Unpaywall

        # Try Unpaywall
        if scrape.doi:
            dl = await download_via_unpaywall(
                scrape.doi, self._user_email, self._tmp_dir, rl, client
            )
            if dl.success:
                pdf = await asyncio.to_thread(read_pdf, dl.tmp_path)  # type: ignore[arg-type]
                if pdf.sha256 and self._is_known("hash", pdf.sha256):
                    dl.tmp_path.unlink(missing_ok=True)  # type: ignore[union-attr]
                    self.log_message.emit(
                        f"Skipped (identical file already in library): {url}"
                    )
                    self.item_finished.emit(url, True, False)
                    return True, False, True
                paper = await resolve_metadata(scrape.doi, self._user_email, rl, client)
                if paper is None:
                    paper = await guess_metadata(dl.tmp_path, pdf, self._user_email, rl, client)  # type: ignore[arg-type]
                paper.open_access = True
                if not self._claim_paper(paper, pdf):
                    dl.tmp_path.unlink(missing_ok=True)  # type: ignore[union-attr]
                    self.log_message.emit(f"Skipped (already in library): {url}")
                    self.item_finished.emit(url, True, False)
                    return True, False, True
                place_file(dl.tmp_path, paper, self._library_root, move=True, folder_pattern=self._folder_pattern)  # type: ignore[arg-type]
                if self._secondary_dest:
                    copy_to_secondary(Path(paper.file_path), self._library_root, self._secondary_dest)
                paper_id = self._db.insert_paper(paper)
                paper.id = paper_id
                self._buffer(paper, pdf.fulltext)
                self.item_finished.emit(url, True, paper.needs_review)
                return True, paper.needs_review, False

            self.item_failed.emit(url, "no_oa_pdf")
            return False, False, False

        self.item_failed.emit(url, "no_doi_no_pdf")
        return False, False, False

    # ------------------------------------------------------------------
    # State persistence (for resumable 130k import)
    # ------------------------------------------------------------------

    def _load_state(self) -> set[str]:
        """Read the processed set from the append-only state file.

        Accepts the legacy {"processed": [...]} JSON object written by earlier versions so
        an interrupted import started on the old format still resumes.
        """
        if not self._state_file or not self._state_file.exists():
            return set()
        try:
            raw = self._state_file.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning("Failed to read import state: %s", e)
            return set()

        stripped = raw.lstrip()
        if stripped.startswith("{"):
            try:
                return set(json.loads(raw).get("processed", []))
            except (ValueError, AttributeError):
                logger.warning("Import state file is corrupt; starting from empty")
                return set()

        processed: set[str] = set()
        for line in raw.splitlines():
            if line:
                try:
                    processed.add(json.loads(line))
                except ValueError:
                    continue  # torn final line from a hard kill
        return processed

    def _append_state(self, items: list[str]) -> None:
        """Append a flush's worth of processed items in one open. Never rewrites the set."""
        if not self._state_file or not items:
            return
        try:
            with self._state_file.open("a", encoding="utf-8") as fh:
                fh.writelines(json.dumps(i, ensure_ascii=False) + "\n" for i in items)
        except OSError as e:
            logger.warning("Failed to append import state: %s", e)

    def _rewrite_state(self, processed: set[str]) -> None:
        """Rewrite the state file from scratch. Called once when an old-format file is
        loaded, so the append path is safe from then on."""
        if not self._state_file:
            return
        try:
            self._state_file.write_text(
                "".join(json.dumps(i, ensure_ascii=False) + "\n" for i in sorted(processed)),
                encoding="utf-8",
            )
        except OSError as e:
            logger.warning("Failed to rewrite import state: %s", e)
