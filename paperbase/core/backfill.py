"""
HashBackfillWorker: fills papers.content_hash for rows imported before the column existed.

Resumable with no state file: get_papers_missing_hash() stops returning a row the moment its
hash is written, so a stopped, closed or crashed run resumes simply by starting again.
"""
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QThread, pyqtSignal

from paperbase.core.db import Database
from paperbase.core.metadata import sha256_file

logger = logging.getLogger(__name__)

_HASH_WORKERS = 8   # file reads, not computation: hashlib releases the GIL and the disk
                    # services several small reads better than one serial stream
_CHUNK = 200        # rows per submitted batch, which is also the granularity of Stop


def _hash_one(row: tuple[int, str]) -> tuple[int, str]:
    """(paper_id, digest). An empty digest means the file is gone or unreadable; that row
    keeps its NULL hash and is picked up again on a later run if the file comes back."""
    paper_id, file_path = row
    return paper_id, sha256_file(Path(file_path))


class HashBackfillWorker(QThread):
    progress = pyqtSignal(int, int, int)    # done, total, unreadable
    log_message = pyqtSignal(str)
    finished_all = pyqtSignal(int, int)     # hashed, unreadable

    def __init__(self, db: Database, parent: Optional[object] = None) -> None:
        super().__init__(parent)
        self._db = db
        self._stop_requested = False

    def request_stop(self) -> None:
        self._stop_requested = True

    def run(self) -> None:
        rows = self._db.get_papers_missing_hash()
        total = len(rows)
        if not total:
            self.log_message.emit("Every paper already carries a fingerprint.")
            self.finished_all.emit(0, 0)
            return

        self.log_message.emit(f"Fingerprinting {total:,} paper(s).")
        hashed = 0
        unreadable = 0
        done = 0

        with ThreadPoolExecutor(max_workers=_HASH_WORKERS) as pool:
            for start in range(0, total, _CHUNK):
                if self._stop_requested:
                    self.log_message.emit(
                        f"Stopped. {hashed:,} fingerprint(s) written and kept; "
                        f"starting again resumes from here."
                    )
                    break

                chunk = rows[start : start + _CHUNK]
                results = list(pool.map(_hash_one, chunk))

                # Writes happen on this thread only. The sqlite3 connection is shared with
                # the UI thread, and writing it from the pool's threads as well would be a
                # third writer on one connection.
                for paper_id, digest in results:
                    if digest:
                        self._db.update_paper_field(paper_id, "content_hash", digest)
                        hashed += 1
                    else:
                        unreadable += 1
                        logger.warning("No fingerprint for paper %s: file unreadable", paper_id)
                    done += 1

                self.progress.emit(done, total, unreadable)

        if unreadable:
            self.log_message.emit(
                f"{unreadable:,} file(s) could not be read and have no fingerprint."
            )
        self.finished_all.emit(hashed, unreadable)
