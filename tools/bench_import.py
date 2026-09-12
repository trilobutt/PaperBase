"""Benchmark the local import pipeline with the network stubbed out.

    py -3.12 tools/bench_import.py --papers 60 [--pages 12] [--latency 0.05] [--out DIR]

Isolates PDF reading, placement, DB insertion and Tantivy indexing by rebinding the
network-facing calls on paperbase.core.importer (the module that resolves those names at
call time) before driving a real ImportWorker.run() with no QApplication. The stubs match
the *current* resolve_metadata/resolve_book_metadata/guess_metadata_from_text signatures;
later phases add a client parameter and rename guess_metadata_from_text, at which point
only the stub bodies below need to change.
"""
import argparse
import asyncio
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fitz

import paperbase.core.importer as imp
from paperbase.core.db import Database
from paperbase.core.indexer import Indexer
from paperbase.models.paper import Paper

DEFAULT_EMAIL = "bench@example.invalid"

FILLER_WORDS = [
    "mycorrhizal", "phylogenetic", "sediment", "isotope", "predation", "symbiosis",
    "stratigraphy", "genome", "morphology", "biogeography", "taphonomy", "speciation",
]


def _filler_line(line_num: int) -> str:
    return " ".join(FILLER_WORDS[(line_num + j) % len(FILLER_WORDS)] for j in range(6))


def _build_pdf(path: Path, pages: int, doi_line: Optional[str]) -> None:
    doc = fitz.open()
    for page_num in range(pages):
        page = doc.new_page()
        y = 72.0
        for line_num in range(40):
            if page_num == 0 and line_num == 2 and doi_line is not None:
                text = doi_line
            else:
                text = _filler_line(line_num)
            page.insert_text((72, y), text, fontsize=9)
            y += 14
    doc.save(str(path))
    doc.close()


def _make_stubs(latency: float, now: str):
    async def _fake_resolve(doi, user_email, rl, client):
        await asyncio.sleep(latency)
        return Paper(id=None, doi=doi, title=f"Benchmark paper {doi}", authors=["Bench, A."],
                     journal="Journal of Benchmarks", year=2024, volume="1", issue="1",
                     pages="1-10", abstract="An abstract long enough to be worth embedding.",
                     keywords=[], tags=[], collection_ids=[], file_path="",
                     date_added=now, date_modified=now, metadata_source="crossref",
                     needs_review=False, open_access=False)

    async def _fake_resolve_book(isbn, rl, client):
        await asyncio.sleep(latency)
        return Paper(id=None, doi=None, title=f"Benchmark book {isbn}", authors=["Bench, A."],
                     journal="Journal of Benchmarks", year=2024, volume="1", issue="1",
                     pages="1-10", abstract="An abstract long enough to be worth embedding.",
                     keywords=[], tags=[], collection_ids=[], file_path="",
                     date_added=now, date_modified=now, metadata_source="crossref",
                     needs_review=False, open_access=False, isbn=isbn, document_type="book")

    async def _fake_guess(path, pdf, user_email, rl, client):
        await asyncio.sleep(latency)
        return Paper(id=None, doi=None, title=f"Guessed paper {Path(path).name}",
                     authors=["Bench, A."], journal="Journal of Benchmarks", year=2024,
                     volume="1", issue="1", pages="1-10",
                     abstract="An abstract long enough to be worth embedding.",
                     keywords=[], tags=[], collection_ids=[], file_path="",
                     date_added=now, date_modified=now, metadata_source="filename",
                     needs_review=True, open_access=False)

    return _fake_resolve, _fake_resolve_book, _fake_guess


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--papers", type=int, default=60)
    ap.add_argument("--pages", type=int, default=12)
    ap.add_argument("--latency", type=float, default=0.05)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    made_tmp = args.out is None
    out: Path = args.out.expanduser() if args.out else Path(tempfile.mkdtemp(prefix="pb_bench_"))
    out.mkdir(parents=True, exist_ok=True)

    pdf_dir = out / "pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    pdfs: list[Path] = []
    for i in range(args.papers):
        doi_line = f"doi:10.1234/bench.{i}" if i % 2 == 0 else None
        pdf_path = pdf_dir / f"bench_{i:05d}.pdf"
        _build_pdf(pdf_path, args.pages, doi_line)
        pdfs.append(pdf_path)

    data_dir = out / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(data_dir / "paperbase.db")
    db.open()
    indexer = Indexer(data_dir / "index")
    indexer.open()

    now = datetime.now(timezone.utc).isoformat()
    fake_resolve, fake_resolve_book, fake_guess = _make_stubs(args.latency, now)
    imp.resolve_metadata = fake_resolve
    imp.resolve_book_metadata = fake_resolve_book
    imp.guess_metadata = fake_guess

    library_root = out / "lib"
    worker = imp.ImportWorker(
        mode="pdfs",
        items=[str(p) for p in pdfs],
        db=db,
        indexer=indexer,
        library_root=library_root,
        user_email=DEFAULT_EMAIL,
        state_file=None,
        categoriser=None,
    )

    try:
        start = time.perf_counter()
        worker.run()
        elapsed = time.perf_counter() - start
    finally:
        indexer.close()
        db.close()
        if made_tmp:
            shutil.rmtree(out, ignore_errors=True)

    per_paper_ms = (elapsed / args.papers * 1000) if args.papers else 0.0
    print(f"import papers={args.papers} pages={args.pages} latency={args.latency} "
          f"elapsed={elapsed:.2f}s per_paper={per_paper_ms:.1f}ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
