"""Structural check for Phase 5 of the importer.py rewrite.

The module cannot run until Phase 7 lands (it references self._buffer, self._is_known,
self._claim_paper, read_pdf, PdfText and httpx, none of which exist yet). This check only
verifies the source text compiles and has the expected shape; it does not import or run it.
"""
import py_compile
from pathlib import Path

TARGET = Path("paperbase/core/importer.py")


def main() -> None:
    py_compile.compile(str(TARGET), doraise=True)

    text = TARGET.read_text(encoding="utf-8")

    for banned in (
        "extract_fulltext",
        "extract_doi_from_pdf",
        "extract_isbn_from_pdf",
        "guess_metadata_from_text",
    ):
        assert banned not in text, f"{banned!r} still appears in {TARGET}"

    assert "paper_exists_by_path" not in text, "paper_exists_by_path still appears"

    assert text.count("self._buffer(paper, pdf.fulltext)") == 5, (
        "self._buffer(paper, pdf.fulltext) should appear exactly 5 times"
    )

    assert "self._apply_categorisation(paper)" not in text, (
        "self._apply_categorisation(paper) should no longer appear"
    )

    assert text.count("self._claim_paper(paper, pdf)") == 5, (
        "self._claim_paper(paper, pdf) should appear exactly 5 times"
    )

    # 4 at Phase 5 time (once each in _import_doi, _import_direct_pdf_url and the two
    # _import_landing_page success blocks) plus the 5th that Phase 7 adds in _process_item
    # for mode "pdfs", once that method exists. Both phases' done-when may run against a
    # file with either count depending on how far the plan has landed, so accept both.
    read_pdf_calls = text.count("asyncio.to_thread(read_pdf")
    assert read_pdf_calls in (4, 5), (
        "asyncio.to_thread(read_pdf should appear 4 times (pre-Phase-7) or 5 (post-Phase-7), "
        f"found {read_pdf_calls}"
    )

    assert text.count('self._is_known("hash"') == 5, (
        'self._is_known("hash" should appear exactly 5 times'
    )

    assert text.count("self._is_known(") >= 10, (
        "self._is_known( should appear at least 10 times in total"
    )

    # No await may sit between a _claim_paper call and its next insert_paper call: the
    # concurrency guarantee depends on there being no yield point in that window.
    claim_marker = "self._claim_paper("
    insert_marker = "self._db.insert_paper("
    start = 0
    claim_count = 0
    while True:
        claim_idx = text.find(claim_marker, start)
        if claim_idx == -1:
            break
        claim_count += 1
        insert_idx = text.find(insert_marker, claim_idx)
        assert insert_idx != -1, "no insert_paper found after a _claim_paper call"
        slice_ = text[claim_idx:insert_idx]
        assert "await " not in slice_, (
            f"found 'await ' between _claim_paper and insert_paper near offset {claim_idx}"
        )
        start = claim_idx + len(claim_marker)
    assert claim_count == 5, f"expected 5 _claim_paper( occurrences, found {claim_count}"

    print("phase5 OK")


if __name__ == "__main__":
    main()
