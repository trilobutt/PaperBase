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

from PyQt6.QtCore import QThread, pyqtSignal

from paperbase.core.db import Database
from paperbase.core.downloader import download_pdf_direct, download_via_unpaywall
from paperbase.core.indexer import Indexer
from paperbase.core.metadata import (
    RateLimiter,
    extract_doi_from_pdf,
    extract_fulltext,
    extract_isbn_from_pdf,
    guess_metadata_from_text,
    resolve_book_metadata,
    resolve_metadata,
)
from paperbase.core.organiser import place_file
from paperbase.core.scraper import ScrapeResult, classify_url, scrape_landing_page
from paperbase.models.paper import Paper

logger = logging.getLogger(__name__)

STATE_SAVE_INTERVAL = 100   # save progress every N papers


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ImportWorker(QThread):
    item_started   = pyqtSignal(str)
    item_finished  = pyqtSignal(str, bool, bool)   # label, success, needs_review
    item_failed    = pyqtSignal(str, str)           # label, reason
    progress       = pyqtSignal(int, int, int, int, int)  # done,total,ok,review,fail
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
        self._pause_requested = False
        self._stop_requested = False
        self._tmp_dir = library_root / "tmp"

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

        total = len(self._items)
        done = len(processed)
        succeeded = 0
        review_count = 0
        failed = 0
        start_time = time.monotonic()

        for idx, item in enumerate(self._items):
            if self._stop_requested:
                break

            while self._pause_requested:
                await asyncio.sleep(0.5)

            if item in processed:
                done += 1
                continue

            self.item_started.emit(item)

            try:
                if self._mode == "pdfs":
                    ok, nr = await self._import_pdf(Path(item), rate_limiter)
                elif self._mode == "dois":
                    ok, nr = await self._import_doi(item, rate_limiter)
                else:  # urls
                    ok, nr = await self._import_url(item, rate_limiter)
            except Exception as e:
                logger.exception("Unexpected error importing %s", item)
                self.item_failed.emit(item, str(e))
                failed += 1
                ok, nr = False, False

            if ok:
                succeeded += 1
                if nr:
                    review_count += 1
            else:
                failed += 1

            processed.add(item)
            done += 1

            elapsed = time.monotonic() - start_time
            eta_str = ""
            if done > 0:
                avg = elapsed / done
                remaining = avg * (total - done)
                m, s = divmod(int(remaining), 60)
                eta_str = f" | ETA {m}m {s}s" if remaining > 0 else ""

            self.progress.emit(done, total, succeeded, review_count, failed)
            self.log_message.emit(
                f"[{done}/{total}] {Path(item).name if self._mode == 'pdfs' else item}"
                f"{eta_str}"
            )

            if done % STATE_SAVE_INTERVAL == 0:
                self._save_state(processed)

        self._save_state(processed)

    # ------------------------------------------------------------------
    # Mode 1: local PDFs
    # ------------------------------------------------------------------

    async def _import_pdf(self, path: Path, rl: RateLimiter) -> tuple[bool, bool]:
        if self._db.paper_exists_by_path(str(path)):
            self.item_finished.emit(str(path), True, False)
            return True, False

        doi = extract_doi_from_pdf(path)
        if doi and self._db.paper_exists_by_doi(doi):
            self.item_finished.emit(str(path), True, False)
            return True, False

        paper = None
        if doi:
            paper = await resolve_metadata(doi, self._user_email, rl)

        # No DOI or Crossref returned nothing — try ISBN (book)
        if paper is None:
            isbn = extract_isbn_from_pdf(path)
            if isbn:
                paper = await resolve_book_metadata(isbn, self._user_email, rl)

        if paper is None:
            paper = await guess_metadata_from_text(path, self._user_email, rl)

        place_file(path, paper, self._library_root, move=False)
        fulltext = extract_fulltext(path)
        paper_id = self._db.insert_paper(paper)
        paper.id = paper_id
        self._indexer.add_document(paper, fulltext)
        self._indexer.commit()

        self.item_finished.emit(str(path), True, paper.needs_review)
        return True, paper.needs_review

    # ------------------------------------------------------------------
    # Mode 2: DOI strings
    # ------------------------------------------------------------------

    async def _import_doi(self, doi: str, rl: RateLimiter) -> tuple[bool, bool]:
        doi = doi.strip()
        if self._db.paper_exists_by_doi(doi):
            self.item_finished.emit(doi, True, False)
            return True, False

        from paperbase.core.downloader import download_via_unpaywall
        result = await download_via_unpaywall(doi, self._user_email, self._tmp_dir, rl)
        if not result.success:
            self.item_failed.emit(doi, result.reason)
            return False, False

        paper = await resolve_metadata(doi, self._user_email, rl)
        if paper is None:
            paper = await guess_metadata_from_text(result.tmp_path, self._user_email, rl)  # type: ignore[arg-type]
        paper.open_access = True

        place_file(result.tmp_path, paper, self._library_root, move=True)  # type: ignore[arg-type]
        fulltext = extract_fulltext(Path(paper.file_path))
        paper_id = self._db.insert_paper(paper)
        paper.id = paper_id
        self._indexer.add_document(paper, fulltext)
        self._indexer.commit()

        self.item_finished.emit(doi, True, paper.needs_review)
        return True, paper.needs_review

    # ------------------------------------------------------------------
    # Mode 3: URLs
    # ------------------------------------------------------------------

    async def _import_url(self, url: str, rl: RateLimiter) -> tuple[bool, bool]:
        url_type = await classify_url(url)

        if url_type == "pdf":
            return await self._import_direct_pdf_url(url, rl)
        else:
            return await self._import_landing_page(url, rl)

    async def _import_direct_pdf_url(self, url: str, rl: RateLimiter) -> tuple[bool, bool]:
        result = await download_pdf_direct(url, None, self._tmp_dir)
        if not result.success:
            self.item_failed.emit(url, result.reason)
            return False, False

        tmp = result.tmp_path
        doi = extract_doi_from_pdf(tmp)  # type: ignore[arg-type]

        paper = None
        if doi:
            paper = await resolve_metadata(doi, self._user_email, rl)
        if paper is None:
            isbn = extract_isbn_from_pdf(tmp)  # type: ignore[arg-type]
            if isbn:
                paper = await resolve_book_metadata(isbn, self._user_email, rl)
        if paper is None:
            paper = await guess_metadata_from_text(tmp, self._user_email, rl)  # type: ignore[arg-type]
        paper.open_access = False

        place_file(tmp, paper, self._library_root, move=True)  # type: ignore[arg-type]
        fulltext = extract_fulltext(Path(paper.file_path))
        paper_id = self._db.insert_paper(paper)
        paper.id = paper_id
        self._indexer.add_document(paper, fulltext)
        self._indexer.commit()

        self.item_finished.emit(url, True, paper.needs_review)
        return True, paper.needs_review

    async def _import_landing_page(self, url: str, rl: RateLimiter) -> tuple[bool, bool]:
        try:
            scrape: ScrapeResult = await scrape_landing_page(url)
        except ValueError as e:
            if str(e) == "direct_pdf":
                return await self._import_direct_pdf_url(url, rl)
            self.item_failed.emit(url, str(e))
            return False, False
        except Exception as e:
            self.item_failed.emit(url, str(e))
            return False, False

        # Try direct PDF from scrape result
        if scrape.pdf_url:
            dl = await download_pdf_direct(scrape.pdf_url, scrape.doi, self._tmp_dir)
            if dl.success:
                tmp = dl.tmp_path
                doi = scrape.doi or extract_doi_from_pdf(tmp)  # type: ignore[arg-type]
                if doi:
                    paper = await resolve_metadata(doi, self._user_email, rl)
                else:
                    paper = None
                if paper is None:
                    paper = scrape.metadata or await guess_metadata_from_text(tmp, self._user_email, rl)  # type: ignore[arg-type]
                paper.open_access = scrape.is_open_access
                place_file(tmp, paper, self._library_root, move=True)  # type: ignore[arg-type]
                fulltext = extract_fulltext(Path(paper.file_path))
                paper_id = self._db.insert_paper(paper)
                paper.id = paper_id
                self._indexer.add_document(paper, fulltext)
                self._indexer.commit()
                self.item_finished.emit(url, True, paper.needs_review)
                return True, paper.needs_review
            # Fall through to Unpaywall

        # Try Unpaywall
        if scrape.doi:
            dl = await download_via_unpaywall(scrape.doi, self._user_email, self._tmp_dir, rl)
            if dl.success:
                paper = await resolve_metadata(scrape.doi, self._user_email, rl)
                if paper is None:
                    paper = await guess_metadata_from_text(dl.tmp_path, self._user_email, rl)  # type: ignore[arg-type]
                paper.open_access = True
                place_file(dl.tmp_path, paper, self._library_root, move=True)  # type: ignore[arg-type]
                fulltext = extract_fulltext(Path(paper.file_path))
                paper_id = self._db.insert_paper(paper)
                paper.id = paper_id
                self._indexer.add_document(paper, fulltext)
                self._indexer.commit()
                self.item_finished.emit(url, True, paper.needs_review)
                return True, paper.needs_review

            self.item_failed.emit(url, "no_oa_pdf")
            return False, False

        self.item_failed.emit(url, "no_doi_no_pdf")
        return False, False

    # ------------------------------------------------------------------
    # State persistence (for resumable 130k import)
    # ------------------------------------------------------------------

    def _load_state(self) -> set[str]:
        if self._state_file and self._state_file.exists():
            try:
                data = json.loads(self._state_file.read_text(encoding="utf-8"))
                return set(data.get("processed", []))
            except Exception:
                pass
        return set()

    def _save_state(self, processed: set[str]) -> None:
        if self._state_file:
            try:
                self._state_file.write_text(
                    json.dumps({"processed": list(processed)}, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except Exception as e:
                logger.warning("Failed to save import state: %s", e)
