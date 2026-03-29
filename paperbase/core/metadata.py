import asyncio
import difflib
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import fitz  # PyMuPDF
import httpx

from paperbase.models.paper import Paper

logger = logging.getLogger(__name__)

DOI_RE = re.compile(r'\b(10\.\d{4,9}/[^\s"<>{|}\\^[\]`]+)')
JATS_TAG_RE = re.compile(r"<[^>]+>")

# Matches "ISBN", "ISBN-13", "ISBN-10", "ISBN:" etc. followed by the number.
# Captures the raw digit/hyphen string; normalisation is done separately.
ISBN_RE = re.compile(
    r'(?:ISBN(?:-1[03])?[:\s]*)'
    r'((?:97[89][- ]?)?(?:\d[- ]?){9}[\dX])',
    re.IGNORECASE,
)
# Bare ISBN-13 (no prefix) — 978/979 followed by 10 digits
ISBN13_BARE_RE = re.compile(r'\b(97[89]\d{10})\b')

CROSSREF_BASE = "https://api.crossref.org/works"
OPENLIBRARY_BASE = "https://openlibrary.org/api/books"
GOOGLEBOOKS_BASE = "https://www.googleapis.com/books/v1/volumes"

# Crossref type field values that map to book-like documents
_BOOK_TYPES = {"book", "monograph", "reference-book", "edited-book", "book-set"}
_CHAPTER_TYPES = {"book-chapter", "book-section", "book-part", "book-track"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _strip_trailing_punct(doi: str) -> str:
    return doi.rstrip(".,;)>\"'")


def _validate_doi(doi: str) -> bool:
    return bool(re.match(r"^10\.\d{4,9}/", doi))


def extract_doi_from_pdf(path: Path) -> Optional[str]:
    """Extract first DOI from PDF text and XMP metadata."""
    try:
        doc = fitz.open(str(path))
    except Exception as e:
        logger.warning("Cannot open PDF %s: %s", path, e)
        return None

    try:
        # Check first 3 pages
        text_parts: list[str] = []
        for page_num in range(min(3, len(doc))):
            text_parts.append(doc[page_num].get_text())
        full_text = "\n".join(text_parts)

        # Search first 100 lines
        lines = full_text.splitlines()
        for line in lines[:100]:
            m = DOI_RE.search(line)
            if m:
                doi = _strip_trailing_punct(m.group(1))
                if _validate_doi(doi):
                    return doi

        # Try entire first-page text
        if text_parts:
            m = DOI_RE.search(text_parts[0])
            if m:
                doi = _strip_trailing_punct(m.group(1))
                if _validate_doi(doi):
                    return doi

        # Check XMP metadata fields
        meta = doc.metadata
        for key in ("subject", "keywords"):
            val = meta.get(key, "") or ""
            m = DOI_RE.search(val)
            if m:
                doi = _strip_trailing_punct(m.group(1))
                if _validate_doi(doi):
                    return doi

    finally:
        doc.close()

    return None


def _normalise_isbn(raw: str) -> str:
    """Strip hyphens and spaces; return pure digit string (or with trailing X for ISBN-10)."""
    return re.sub(r"[- ]", "", raw).upper()


def extract_isbn_from_pdf(path: Path) -> Optional[str]:
    """Extract first ISBN from PDF text, preferring ISBN-13. Returns normalised digit string."""
    try:
        doc = fitz.open(str(path))
    except Exception as e:
        logger.warning("Cannot open PDF %s: %s", path, e)
        return None

    try:
        text_parts: list[str] = []
        for page_num in range(min(3, len(doc))):
            text_parts.append(doc[page_num].get_text())
        full_text = "\n".join(text_parts)

        # Prefixed ISBN (most reliable)
        m = ISBN_RE.search(full_text)
        if m:
            return _normalise_isbn(m.group(1))

        # Bare ISBN-13 (common on copyright pages)
        m2 = ISBN13_BARE_RE.search(full_text)
        if m2:
            return _normalise_isbn(m2.group(1))

    finally:
        doc.close()

    return None


async def resolve_metadata(doi: str, user_email: str, rate_limiter: "RateLimiter") -> Optional[Paper]:
    """Query Crossref for full metadata for a DOI."""
    url = f"{CROSSREF_BASE}/{doi}"
    headers = {"User-Agent": f"PaperBase/1.0 (mailto:{user_email})"}

    for attempt in range(3):
        await rate_limiter.acquire_crossref()
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
                resp = await client.get(url, headers=headers)
        except httpx.HTTPError as e:
            logger.warning("Crossref request failed for %s: %s", doi, e)
            return None

        if resp.status_code == 200:
            return _parse_crossref_response(resp.json(), doi)
        if resp.status_code == 404:
            return None
        if resp.status_code == 429:
            wait = 2 ** attempt
            logger.warning("Crossref rate limit, waiting %ds", wait)
            await asyncio.sleep(wait)
            continue
        logger.warning("Crossref returned %d for %s", resp.status_code, doi)
        return None

    return None


def _parse_crossref_response(data: dict, doi: str) -> Paper:
    msg = data.get("message", {})

    # Title
    titles = msg.get("title", [])
    title = titles[0] if titles else ""

    # Authors
    raw_authors = msg.get("author", [])
    authors: list[str] = []
    for a in raw_authors:
        family = a.get("family", "")
        given = a.get("given", "")
        if family:
            authors.append(f"{family}, {given}".strip(", "))
        elif given:
            authors.append(given)

    # Journal / publisher: prefer container-title; fall back to publisher field (common for books)
    containers = msg.get("container-title", [])
    journal = containers[0] if containers else (msg.get("publisher", "") or "")

    # Year
    year: Optional[int] = None
    pub = msg.get("published") or msg.get("published-print") or msg.get("published-online")
    if pub:
        date_parts = pub.get("date-parts", [[]])
        if date_parts and date_parts[0]:
            year = date_parts[0][0]

    volume = msg.get("volume", "") or ""
    issue = msg.get("issue", "") or ""
    pages = msg.get("page", "") or ""

    # Abstract — strip JATS XML
    abstract_raw = msg.get("abstract", "") or ""
    abstract = JATS_TAG_RE.sub("", abstract_raw).strip()

    # Keywords from subject
    keywords: list[str] = msg.get("subject", []) or []

    # Document type from Crossref type field
    cr_type = (msg.get("type", "") or "").lower()
    if cr_type in _BOOK_TYPES:
        document_type = "book"
    elif cr_type in _CHAPTER_TYPES:
        document_type = "book-chapter"
    elif cr_type == "proceedings-article":
        document_type = "proceedings"
    else:
        document_type = "article"

    # ISBN (Crossref returns a list for books)
    isbn: Optional[str] = None
    raw_isbns: list[str] = msg.get("ISBN", []) or []
    if raw_isbns:
        # Prefer ISBN-13 (starts with 978/979)
        for raw in raw_isbns:
            normalised = _normalise_isbn(raw)
            if normalised.startswith(("978", "979")):
                isbn = normalised
                break
        if isbn is None:
            isbn = _normalise_isbn(raw_isbns[0])

    now = _now_iso()
    return Paper(
        id=None,
        doi=doi,
        title=title,
        authors=authors,
        journal=journal,
        year=year,
        volume=str(volume),
        issue=str(issue),
        pages=str(pages),
        abstract=abstract,
        keywords=keywords,
        tags=[],
        collection_ids=[],
        file_path="",
        date_added=now,
        date_modified=now,
        metadata_source="crossref",
        needs_review=False,
        open_access=False,
        isbn=isbn,
        document_type=document_type,
    )


async def guess_metadata_from_text(path: Path, user_email: str, rate_limiter: "RateLimiter") -> Paper:
    """Best-effort metadata extraction when no DOI is found."""
    now = _now_iso()
    base_paper = Paper(
        id=None, doi=None, title="", authors=[], journal="", year=None,
        volume="", issue="", pages="", abstract="", keywords=[], tags=[],
        collection_ids=[], file_path=str(path), date_added=now, date_modified=now,
        metadata_source="filename", needs_review=True, open_access=False,
        isbn=None, document_type="article",
    )

    # Extract first-page text
    try:
        doc = fitz.open(str(path))
        first_page_text = doc[0].get_text() if len(doc) > 0 else ""
        xmp_meta = doc.metadata
        doc.close()
    except Exception:
        base_paper.title = path.name
        return base_paper

    # Candidate title: longest line in first 20 lines, >= 20 chars, has space
    lines = [l.strip() for l in first_page_text.splitlines() if l.strip()]
    candidate_title = ""
    for line in lines[:20]:
        if len(line) >= 20 and " " in line and len(line) > len(candidate_title):
            candidate_title = line

    if candidate_title:
        # Try Crossref bibliographic search
        found = await _crossref_bib_search(candidate_title, user_email, rate_limiter)
        if found:
            found.file_path = str(path)
            return found

    # Fall back to XMP metadata — skip generic placeholder values set by PDF tools
    _JUNK_TITLES = {"untitled", "untitled document", "microsoft word", "word document", ""}
    xmp_title = (xmp_meta.get("title", "") or "").strip()
    xmp_author = xmp_meta.get("author", "") or ""
    if xmp_title and xmp_title.lower() not in _JUNK_TITLES:
        base_paper.title = xmp_title
        if xmp_author:
            base_paper.authors = [xmp_author]
        base_paper.metadata_source = "xmp"
        return base_paper

    # Last resort: use the original filename (with extension) as the title
    base_paper.title = path.name
    return base_paper


async def _crossref_bib_search(
    title: str, user_email: str, rate_limiter: "RateLimiter"
) -> Optional[Paper]:
    params = {
        "query.bibliographic": title,
        "rows": "3",
        "select": "DOI,title,author,container-title,published,volume,issue,page,abstract,subject",
    }
    headers = {"User-Agent": f"PaperBase/1.0 (mailto:{user_email})"}
    await rate_limiter.acquire_crossref()
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            resp = await client.get(CROSSREF_BASE, params=params, headers=headers)
        if resp.status_code != 200:
            return None
        data = resp.json()
        items = data.get("message", {}).get("items", [])
    except httpx.HTTPError:
        return None

    for item in items:
        titles = item.get("title", [])
        if not titles:
            continue
        ratio = difflib.SequenceMatcher(None, title.lower(), titles[0].lower()).ratio()
        if ratio >= 0.75:
            doi = item.get("DOI", "")
            paper = _parse_crossref_response({"message": item}, doi)
            return paper

    return None


async def resolve_book_metadata(isbn: str, user_email: str, rate_limiter: "RateLimiter") -> Optional[Paper]:
    """
    Resolve book metadata by ISBN.
    Tries Open Library first; falls back to Google Books.
    Returns None if neither source yields a result.
    """
    paper = await _openlibrary_lookup(isbn, rate_limiter)
    if paper:
        return paper
    return await _googlebooks_lookup(isbn, rate_limiter)


async def _openlibrary_lookup(isbn: str, rate_limiter: "RateLimiter") -> Optional[Paper]:
    params = {"bibkeys": f"ISBN:{isbn}", "format": "json", "jscmd": "data"}
    await rate_limiter.acquire_crossref()  # reuse the crossref slot for general rate limiting
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            resp = await client.get(OPENLIBRARY_BASE, params=params)
        if resp.status_code != 200:
            return None
        data = resp.json()
    except httpx.HTTPError as e:
        logger.warning("Open Library request failed for ISBN %s: %s", isbn, e)
        return None

    key = f"ISBN:{isbn}"
    if key not in data:
        return None
    book = data[key]

    title = book.get("title", "")
    # subtitle
    subtitle = book.get("subtitle", "")
    if subtitle:
        title = f"{title}: {subtitle}"

    raw_authors = book.get("authors", [])
    authors = [a.get("name", "") for a in raw_authors if a.get("name")]

    publishers = book.get("publishers", [])
    publisher = publishers[0].get("name", "") if publishers else ""

    year: Optional[int] = None
    pub_date = book.get("publish_date", "")
    if pub_date:
        m = re.search(r"\b(1[89]\d\d|20\d\d)\b", pub_date)
        if m:
            year = int(m.group(1))

    # Description may be a string or {"value": "..."}
    desc_raw = book.get("description", "")
    if isinstance(desc_raw, dict):
        desc_raw = desc_raw.get("value", "")
    abstract = JATS_TAG_RE.sub("", desc_raw).strip()

    subjects = book.get("subjects", [])
    keywords = [s.get("name", s) if isinstance(s, dict) else str(s) for s in subjects]

    # Page count → store in pages field
    pages = str(book.get("number_of_pages", "") or "")

    now = _now_iso()
    return Paper(
        id=None,
        doi=None,
        title=title,
        authors=authors,
        journal=publisher,
        year=year,
        volume="",
        issue="",
        pages=pages,
        abstract=abstract,
        keywords=keywords[:20],  # cap to avoid noise
        tags=[],
        collection_ids=[],
        file_path="",
        date_added=now,
        date_modified=now,
        metadata_source="openlibrary",
        needs_review=False,
        open_access=False,
        isbn=isbn,
        document_type="book",
    )


async def _googlebooks_lookup(isbn: str, rate_limiter: "RateLimiter") -> Optional[Paper]:
    params = {"q": f"isbn:{isbn}"}
    await rate_limiter.acquire_crossref()
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            resp = await client.get(GOOGLEBOOKS_BASE, params=params)
        if resp.status_code != 200:
            return None
        data = resp.json()
    except httpx.HTTPError as e:
        logger.warning("Google Books request failed for ISBN %s: %s", isbn, e)
        return None

    items = data.get("items", [])
    if not items:
        return None
    info = items[0].get("volumeInfo", {})

    title = info.get("title", "")
    subtitle = info.get("subtitle", "")
    if subtitle:
        title = f"{title}: {subtitle}"

    authors = info.get("authors", [])
    publisher = info.get("publisher", "")

    year: Optional[int] = None
    pub_date = info.get("publishedDate", "")
    if pub_date:
        m = re.search(r"\b(1[89]\d\d|20\d\d)\b", pub_date)
        if m:
            year = int(m.group(1))

    abstract = JATS_TAG_RE.sub("", info.get("description", "") or "").strip()

    categories = info.get("categories", [])
    keywords = [c for c in categories]

    pages = str(info.get("pageCount", "") or "")

    now = _now_iso()
    return Paper(
        id=None,
        doi=None,
        title=title,
        authors=authors,
        journal=publisher,
        year=year,
        volume="",
        issue="",
        pages=pages,
        abstract=abstract,
        keywords=keywords,
        tags=[],
        collection_ids=[],
        file_path="",
        date_added=now,
        date_modified=now,
        metadata_source="googlebooks",
        needs_review=False,
        open_access=False,
        isbn=isbn,
        document_type="book",
    )


def extract_fulltext(path: Path) -> str:
    """Extract full text from a PDF for Tantivy indexing."""
    try:
        doc = fitz.open(str(path))
        parts = [page.get_text() for page in doc]
        doc.close()
        return "\n".join(parts)
    except Exception as e:
        logger.warning("Full-text extraction failed for %s: %s", path, e)
        return ""


class RateLimiter:
    """Enforces minimum inter-request delays for external APIs."""

    def __init__(self) -> None:
        self._crossref_lock = asyncio.Lock()
        self._unpaywall_lock = asyncio.Lock()
        self._last_crossref = 0.0
        self._last_unpaywall = 0.0

    async def acquire_crossref(self) -> None:
        async with self._crossref_lock:
            now = time.monotonic()
            wait = 0.02 - (now - self._last_crossref)  # 20ms minimum
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_crossref = time.monotonic()

    async def acquire_unpaywall(self) -> None:
        async with self._unpaywall_lock:
            now = time.monotonic()
            wait = 0.1 - (now - self._last_unpaywall)  # 100ms minimum
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_unpaywall = time.monotonic()
