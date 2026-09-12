"""Behavioural check for Phase 7 of the importer.py rewrite: the concurrent run loop and the
batched flush (categorise -> index -> commit -> state, in that order).

Rebinds resolve_metadata/resolve_book_metadata/guess_metadata on paperbase.core.importer to
async stubs (matching the client-carrying signatures Phase 3/5 introduced) so the run is
network-free, and lowers INDEX_COMMIT_INTERVAL so more than one flush happens over 24 items.
_MAX_CONCURRENT_ITEMS is left at 4 so the dedupe cases below race for real, with the stubs'
10ms sleep standing in for network latency.
"""
import asyncio
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fitz  # noqa: E402

import paperbase.core.importer as imp  # noqa: E402
from paperbase.core.db import Database  # noqa: E402
from paperbase.core.indexer import Indexer  # noqa: E402
from paperbase.models.paper import Paper  # noqa: E402

EMAIL = "flush-check@example.invalid"
NOW = datetime.now(timezone.utc).isoformat()

FILLER_WORDS = [
    "mycorrhizal", "phylogenetic", "sediment", "isotope", "predation", "symbiosis",
    "stratigraphy", "genome", "morphology", "biogeography", "taphonomy", "speciation",
]


def _filler_lines(seed: int, n: int = 30) -> list[str]:
    return [
        " ".join(FILLER_WORDS[(seed + i + j) % len(FILLER_WORDS)] for j in range(6))
        for i in range(n)
    ]


def _build_pdf(path: Path, lines: list[str]) -> None:
    doc = fitz.open()
    page = doc.new_page()
    y = 72.0
    for line in lines:
        page.insert_text((72, y), line, fontsize=9)
        y += 14
    doc.save(str(path))
    doc.close()


def _canned_paper(doi: Optional[str], isbn: Optional[str], title: str, source: str) -> Paper:
    return Paper(
        id=None, doi=doi, title=title, authors=["Flush, A."], journal="Journal of Flush",
        year=2024, volume="1", issue="1", pages="1-1",
        abstract="An abstract long enough to be worth embedding.",
        keywords=[], tags=[], collection_ids=[], file_path="",
        date_added=NOW, date_modified=NOW, metadata_source=source,
        needs_review=False, open_access=False, isbn=isbn,
        document_type="book" if isbn else "article",
    )


async def _stub_resolve_metadata(doi, user_email, rate_limiter, client):
    await asyncio.sleep(0.01)
    return _canned_paper(doi, None, f"Flushbenchmark paper {doi}", "crossref")


async def _stub_resolve_book_metadata(isbn, rate_limiter, client):
    await asyncio.sleep(0.01)
    return _canned_paper(None, isbn, f"Flushbenchmark book {isbn}", "openlibrary")


async def _stub_guess_metadata(path, pdf, user_email, rate_limiter, client):
    await asyncio.sleep(0.01)
    # No DOI/ISBN was found in the (byte-identical) text, so this stub fabricates a doi from
    # the path — distinct per file even though the bytes, and therefore the content hash, are
    # not. That is what proves the hash claim, not the DOI claim, is what catches this case.
    doi = f"10.7777/hashdupe.{Path(path).stem}"
    return _canned_paper(doi, None, f"Flushbenchmark hashdupe {Path(path).stem}", "filename")


def _fresh_env(base: Path, name: str) -> tuple[Database, Indexer, Path, Path, Path]:
    root = base / name
    root.mkdir(parents=True)
    db_path = root / "paperbase.db"
    db = Database(db_path)
    db.open()
    indexer = Indexer(root / "index")
    indexer.open()
    library_root = root / "lib"
    state_file = root / "import_state.json"
    return db, indexer, library_root, state_file, db_path


def _content_hashes(db_path: Path) -> list[Optional[str]]:
    conn = sqlite3.connect(str(db_path))
    try:
        return [row[0] for row in conn.execute("SELECT content_hash FROM papers")]
    finally:
        conn.close()


def _run(mode: str, items: list[str], db: Database, indexer: Indexer, library_root: Path,
          state_file: Path) -> "imp.ImportWorker":
    worker = imp.ImportWorker(
        mode=mode, items=items, db=db, indexer=indexer, library_root=library_root,
        user_email=EMAIL, state_file=state_file, categoriser=None,
    )
    worker.run()
    return worker


def main() -> int:
    imp.resolve_metadata = _stub_resolve_metadata
    imp.resolve_book_metadata = _stub_resolve_book_metadata
    imp.guess_metadata = _stub_guess_metadata
    imp.INDEX_COMMIT_INTERVAL = 5  # forces at least 4 flushes over the 24-item batch below

    tmp = Path(tempfile.mkdtemp(prefix="pb_flush_"))
    try:
        # ------------------------------------------------------------
        # 24 PDFs, each with a distinct DOI: commit, resume, content_hash
        # ------------------------------------------------------------
        pdf_dir = tmp / "pdfs"
        pdf_dir.mkdir()
        pdfs = []
        for i in range(24):
            p = pdf_dir / f"flush_{i:03d}.pdf"
            lines = _filler_lines(i)
            lines[2] = f"doi:10.1234/flush.{i}"
            _build_pdf(p, lines)
            pdfs.append(p)

        db, indexer, library_root, state_file, db_path = _fresh_env(tmp, "main")
        try:
            _run("pdfs", [str(p) for p in pdfs], db, indexer, library_root, state_file)

            count = db.get_paper_count()
            assert count == 24, f"expected 24 papers after first run, got {count}"

            state_lines = [l for l in state_file.read_text(encoding="utf-8").splitlines() if l]
            assert len(state_lines) == 24, f"expected 24 state lines, got {len(state_lines)}"

            results = indexer.search("flushbenchmark")
            assert len(results) == 24, (
                f"expected 24 indexed+committed hits, got {len(results)}"
            )

            hashes = _content_hashes(db_path)
            assert len(hashes) == 24
            for h in hashes:
                assert h is not None and len(h) == 64, f"bad content_hash: {h!r}"

            # Resume: same items, same state file -> everything is skipped, count unchanged.
            _run("pdfs", [str(p) for p in pdfs], db, indexer, library_root, state_file)
            count_after_resume = db.get_paper_count()
            assert count_after_resume == 24, (
                f"resume run should not add rows, got {count_after_resume}"
            )
        finally:
            indexer.close()
            db.close()

        # ------------------------------------------------------------
        # Dedupe case 1: byte-identical copies, only the content hash can catch it
        # ------------------------------------------------------------
        case1_src = tmp / "case1_src"
        case1_src.mkdir()
        base_pdf = case1_src / "base.pdf"
        _build_pdf(base_pdf, _filler_lines(100))  # no DOI/ISBN text at all
        case1_paths = []
        for i in range(6):
            p = case1_src / f"copy_{i}.pdf"
            shutil.copy2(base_pdf, p)
            case1_paths.append(p)

        db1, idx1, lib1, state1, _ = _fresh_env(tmp, "case1")
        w1 = _run("pdfs", [str(p) for p in case1_paths], db1, idx1, lib1, state1)
        count1 = db1.get_paper_count()
        assert count1 == 1, f"case1: expected exactly 1 row, got {count1}"
        assert w1._imported == 1 and w1._dupes == 5 and w1._failed == 0, (
            f"case1 counts: imported={w1._imported} dupes={w1._dupes} failed={w1._failed}"
        )
        # Exactly one file in the library, which is the precise statement of "no
        # place_file collision companion was written". Globbing for *_2.pdf is not:
        # an unsorted paper keeps its source name, so copy_2.pdf matches that pattern
        # whenever the race is won by that copy.
        placed = sorted(lib1.rglob("*.pdf"))
        assert len(placed) == 1, f"case1: expected one placed file, got {placed}"

        # Re-run the same case with a FRESH state file against the SAME db/library: the claim
        # must hold against what is already in the library, not only against this run's items.
        state1_rerun = tmp / "case1" / "import_state_rerun.json"
        w1b = _run("pdfs", [str(p) for p in case1_paths], db1, idx1, lib1, state1_rerun)
        count1_rerun = db1.get_paper_count()
        assert count1_rerun == 1, (
            f"case1 rerun against an existing library should add nothing, got {count1_rerun}"
        )
        assert w1b._dupes == 6 and w1b._imported == 0 and w1b._failed == 0, (
            f"case1 rerun counts: imported={w1b._imported} dupes={w1b._dupes} "
            f"failed={w1b._failed}"
        )
        idx1.close()
        db1.close()

        # ------------------------------------------------------------
        # Dedupe case 2: different bytes, same DOI
        # ------------------------------------------------------------
        case2_src = tmp / "case2_src"
        case2_src.mkdir()
        case2_paths = []
        for i in range(6):
            p = case2_src / f"same_doi_{i}.pdf"
            lines = _filler_lines(200 + i * 7)
            lines[2] = "doi:10.1234/samedoi.case2"
            _build_pdf(p, lines)
            case2_paths.append(p)

        db2, idx2, lib2, state2, _ = _fresh_env(tmp, "case2")
        try:
            w2 = _run("pdfs", [str(p) for p in case2_paths], db2, idx2, lib2, state2)
            count2 = db2.get_paper_count()
            assert count2 == 1, f"case2: expected exactly 1 row, got {count2}"
            assert w2._imported == 1 and w2._dupes == 5 and w2._failed == 0, (
                f"case2 counts: imported={w2._imported} dupes={w2._dupes} failed={w2._failed}"
            )
        finally:
            idx2.close()
            db2.close()

        # ------------------------------------------------------------
        # Dedupe case 3: different bytes, no DOI, same ISBN
        # ------------------------------------------------------------
        case3_src = tmp / "case3_src"
        case3_src.mkdir()
        case3_paths = []
        for i in range(6):
            p = case3_src / f"same_isbn_{i}.pdf"
            lines = _filler_lines(300 + i * 11)
            lines[5] = "ISBN: 9780306406157"
            _build_pdf(p, lines)
            case3_paths.append(p)

        db3, idx3, lib3, state3, _ = _fresh_env(tmp, "case3")
        try:
            w3 = _run("pdfs", [str(p) for p in case3_paths], db3, idx3, lib3, state3)
            count3 = db3.get_paper_count()
            assert count3 == 1, f"case3: expected exactly 1 row, got {count3}"
            assert w3._imported == 1 and w3._dupes == 5 and w3._failed == 0, (
                f"case3 counts: imported={w3._imported} dupes={w3._dupes} failed={w3._failed}"
            )
        finally:
            idx3.close()
            db3.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("flush OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
