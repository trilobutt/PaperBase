"""Verify the PdfText read-once path in paperbase.core.metadata.

    py -3.12 tools/check_metadata_read.py

Builds a one-page PDF with fitz, reads it through read_pdf/extract_doi/extract_isbn,
checks the sha256 helper agrees with the read path, checks hashes distinguish an
identical copy from a modified one, checks the not-ok path on a missing file, and
checks the client-injection refactor removed the internal AsyncClient construction.
"""
import inspect
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fitz

from paperbase.core import metadata
from paperbase.core.metadata import (
    PdfText,
    extract_doi,
    extract_isbn,
    read_pdf,
    resolve_book_metadata,
    resolve_metadata,
    sha256_file,
)

with tempfile.TemporaryDirectory() as tmp:
    tmp_path = Path(tmp)
    original = tmp_path / "original.pdf"

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text(
        (72, 72),
        "Title Here\ndoi:10.1234/test.5678\nISBN 978-0-306-40615-7",
    )
    doc.save(str(original))
    doc.close()

    t = read_pdf(original)
    assert t.ok
    assert len(t.pages) == 1
    assert extract_doi(t) == "10.1234/test.5678"
    assert extract_isbn(t) == "9780306406157"

    assert len(t.sha256) == 64
    assert t.sha256 == sha256_file(original)

    copy_path = tmp_path / "copy.pdf"
    shutil.copyfile(original, copy_path)
    assert read_pdf(copy_path).sha256 == t.sha256

    modified_path = tmp_path / "modified.pdf"
    shutil.copyfile(original, modified_path)
    with modified_path.open("r+b") as fh:
        fh.seek(0)
        byte = fh.read(1)
        fh.seek(0)
        fh.write(bytes([byte[0] ^ 0xFF]))
    assert read_pdf(modified_path).sha256 != t.sha256

    b = read_pdf(Path("nope.pdf"))
    assert not b.ok
    assert b.fulltext == ""
    assert b.sha256 == ""
    assert extract_doi(b) is None

assert not hasattr(metadata, "extract_fulltext")

assert "httpx.AsyncClient(" not in inspect.getsource(metadata)

assert "client" in inspect.signature(resolve_metadata).parameters
assert "client" in inspect.signature(resolve_book_metadata).parameters
assert "user_email" not in inspect.signature(resolve_book_metadata).parameters

print("metadata OK")
