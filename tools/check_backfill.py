"""Check HashBackfillWorker: fingerprints pre-existing rows, is resumable, finds duplicates.

    py -3.12 tools/check_backfill.py
"""
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from paperbase.core.backfill import HashBackfillWorker
from paperbase.core.db import Database
from paperbase.models.paper import Paper


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_paper(file_path: str) -> Paper:
    return Paper(
        id=None,
        doi=None,
        title="Untitled",
        authors=[],
        journal="",
        year=None,
        volume="",
        issue="",
        pages="",
        abstract="",
        keywords=[],
        tags=[],
        collection_ids=[],
        file_path=file_path,
        date_added=_now(),
        date_modified=_now(),
        metadata_source="manual",
        needs_review=False,
        open_access=False,
        content_hash=None,
    )


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        a_path = tmp_path / "a.pdf"
        b_path = tmp_path / "b.pdf"
        c_path = tmp_path / "c.pdf"
        missing_path = tmp_path / "missing.pdf"

        a_path.write_bytes(b"identical content")
        b_path.write_bytes(b"identical content")
        c_path.write_bytes(b"different content")
        # missing_path deliberately not created

        db = Database(tmp_path / "paperbase.db")
        db.open()

        for path in (a_path, b_path, c_path, missing_path):
            db.insert_paper(_make_paper(str(path)))

        recorder = {}

        def _record(hashed: int, unreadable: int) -> None:
            recorder["result"] = (hashed, unreadable)

        worker = HashBackfillWorker(db)
        worker.finished_all.connect(_record)
        worker.run()

        assert recorder["result"] == (3, 1), f"expected (3, 1), got {recorder['result']}"

        papers = {p.file_path: p for p in [db.get_paper(i) for i in (1, 2, 3, 4)]}
        a_paper = papers[str(a_path)]
        b_paper = papers[str(b_path)]
        c_paper = papers[str(c_path)]
        missing_paper = papers[str(missing_path)]

        for p in (a_paper, b_paper, c_paper):
            assert p.content_hash is not None and len(p.content_hash) == 64, (
                f"expected 64-char hash, got {p.content_hash!r}"
            )
        assert a_paper.content_hash == b_paper.content_hash, "a.pdf and b.pdf should match"
        assert a_paper.content_hash != c_paper.content_hash, "c.pdf should differ"
        assert missing_paper.content_hash is None, (
            f"expected None for missing file, got {missing_paper.content_hash!r}"
        )

        groups = db.get_duplicate_hash_groups()
        assert len(groups) == 1, f"expected exactly one duplicate group, got {groups}"
        _digest, ids = groups[0]
        assert len(ids) == 2, f"expected exactly two ids in the group, got {ids}"

        coverage = db.get_hash_coverage()
        assert coverage == (3, 4), f"expected (3, 4), got {coverage}"

        second_recorder = {}

        def _record_second(hashed: int, unreadable: int) -> None:
            second_recorder["result"] = (hashed, unreadable)

        second_worker = HashBackfillWorker(db)
        second_worker.finished_all.connect(_record_second)
        second_worker.run()

        # Not (0, 0): get_papers_missing_hash() returns every row with no hash on every run,
        # by design (a row leaves the set only once a digest is written), so the still-missing
        # file is found, hashed, and counted as unreadable again on this run too. (0, 0) would
        # require the worker to remember a permanently-unreadable row across runs, which
        # contradicts its own documented intent of retrying in case the file comes back.
        assert second_recorder["result"] == (0, 1), (
            f"expected (0, 1) on second run, got {second_recorder['result']}"
        )

        db.close()
        print("backfill OK")


if __name__ == "__main__":
    main()
