"""
DedupeWorker: removes all but one copy of each group of byte-identical PDFs.

Groups come from ``Database.get_duplicate_hash_groups``. The copies in a group hold the same
bytes, so file quality cannot differ; what differs is the row's metadata and where the file
was placed, and that is what picks the keeper. Tags and collections of the removed rows are
merged onto the keeper so no organisation work is lost.

A copy's file is deleted only after it and the keeper are both re-hashed and still match the
group digest: a fingerprint is a record of the file at scan time, and the file may have been
replaced since.
"""
import json
import logging
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QThread, pyqtSignal

from paperbase.core.db import Database
from paperbase.core.indexer import Indexer
from paperbase.core.metadata import sha256_file
from paperbase.models.paper import Paper

logger = logging.getLogger(__name__)

# Lower is better. A Crossref record is the most trustworthy row to keep; a filename guess
# the least.
_SOURCE_RANK = {
    "manual": 0, "crossref": 1, "openlibrary": 2, "googlebooks": 3, "xmp": 4, "filename": 5,
}
_INDEX_BATCH = 200


def _keeper_key(paper: Paper) -> tuple[int, int, int, int]:
    unsorted = Path(paper.file_path).parent.name == "Unsorted"
    return (
        int(paper.needs_review),
        _SOURCE_RANK.get(paper.metadata_source, len(_SOURCE_RANK)),
        int(unsorted),
        paper.id or 0,
    )


def choose_keeper(papers: list[Paper]) -> Paper:
    """The copy to keep: reviewed metadata, best source, filed rather than Unsorted, oldest."""
    return min(papers, key=_keeper_key)


class DedupeWorker(QThread):
    progress = pyqtSignal(int, int)             # groups done, groups total
    finished_all = pyqtSignal(int, int, int)    # rows removed, files deleted, copies skipped

    def __init__(
        self,
        db: Database,
        indexer: Indexer,
        groups: list[tuple[str, list[int]]],
        parent: Optional[object] = None,
    ) -> None:
        super().__init__(parent)
        self._db = db
        self._indexer = indexer
        self._groups = groups

    def run(self) -> None:
        removed = deleted = skipped = 0
        pending_index: list[int] = []
        total = len(self._groups)

        for done, (digest, ids) in enumerate(self._groups, start=1):
            papers = self._db.get_papers_by_ids(ids)
            if len(papers) > 1:
                r, d, s = self._dedupe_group(digest, papers, pending_index)
                removed, deleted, skipped = removed + r, deleted + d, skipped + s
            if len(pending_index) >= _INDEX_BATCH:
                self._indexer.delete_documents(pending_index)
                pending_index.clear()
            self.progress.emit(done, total)

        if pending_index:
            self._indexer.delete_documents(pending_index)
        self.finished_all.emit(removed, deleted, skipped)

    def _dedupe_group(
        self, digest: str, papers: list[Paper], pending_index: list[int]
    ) -> tuple[int, int, int]:
        keeper = choose_keeper(papers)
        if sha256_file(Path(keeper.file_path)) != digest:
            logger.warning("Keeper %s no longer matches its fingerprint; group skipped", keeper.id)
            return 0, 0, len(papers) - 1

        removed = deleted = skipped = 0
        tags = set(keeper.tags)
        collections = set(keeper.collection_ids)
        taxa = set(keeper.taxa)
        taxa_locked = keeper.taxa_locked
        for paper in papers:
            if paper.id == keeper.id:
                continue
            path = Path(paper.file_path)
            if path.exists() and sha256_file(path) != digest:
                logger.warning("Copy %s changed since the scan; left alone", paper.id)
                skipped += 1
                continue
            tags.update(paper.tags)
            collections.update(paper.collection_ids)
            taxa.update(paper.taxa)
            taxa_locked = taxa_locked or paper.taxa_locked
            self._db.delete_paper(paper.id)
            pending_index.append(paper.id)
            removed += 1
            try:
                path.unlink(missing_ok=True)
                deleted += 1
            except OSError as e:
                logger.error("Could not delete %s: %s", path, e)

        if removed:
            self._db.update_paper_field(keeper.id, "tags", json.dumps(sorted(tags)))
            self._db.update_paper_field(
                keeper.id, "collection_ids", json.dumps(sorted(collections))
            )
            self._db.update_paper_field(keeper.id, "taxa", json.dumps(sorted(taxa)))
            self._db.update_paper_field(keeper.id, "taxa_locked", int(taxa_locked))
        return removed, deleted, skipped
