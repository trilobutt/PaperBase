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

CROSSREF_BASE = "https://api.crossref.org/works"


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

    # Journal
    containers = msg.get("container-title", [])
    journal = containers[0] if containers else ""

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
    )


async def guess_metadata_from_text(path: Path, user_email: str, rate_limiter: "RateLimiter") -> Paper:
    """Best-effort metadata extraction when no DOI is found."""
    now = _now_iso()
    base_paper = Paper(
        id=None, doi=None, title="", authors=[], journal="", year=None,
        volume="", issue="", pages="", abstract="", keywords=[], tags=[],
        collection_ids=[], file_path=str(path), date_added=now, date_modified=now,
        metadata_source="filename", needs_review=True, open_access=False,
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
