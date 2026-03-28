import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from paperbase.models.paper import Paper

logger = logging.getLogger(__name__)

DOI_RE = re.compile(r"(10\.\d{4,9}/[^\s\"<>{|}\\^[\]`]+)")
PAYWALLED_KEYWORDS = {"login", "sso", "auth", "signin", "access"}

_BROWSER_UA = "Mozilla/5.0 (compatible; PaperBase/1.0)"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _blank_paper() -> Paper:
    now = _now_iso()
    return Paper(
        id=None, doi=None, title="", authors=[], journal="", year=None,
        volume="", issue="", pages="", abstract="", keywords=[], tags=[],
        collection_ids=[], file_path="", date_added=now, date_modified=now,
        metadata_source="scrape", needs_review=True, open_access=False,
    )


def _strip_doi(raw: str) -> Optional[str]:
    """Normalise and validate a raw DOI string."""
    raw = raw.strip()
    # Remove doi.org prefix
    raw = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", raw)
    raw = raw.rstrip(".,;)>\"'")
    m = DOI_RE.search(raw)
    if m and re.match(r"^10\.\d{4,9}/", m.group(1)):
        return m.group(1)
    return None


def _parse_year(s: str) -> Optional[int]:
    m = re.search(r"\b(1[89]\d\d|20\d\d)\b", s)
    return int(m.group(1)) if m else None


@dataclass
class ScrapeResult:
    doi: Optional[str]
    pdf_url: Optional[str]
    is_open_access: bool
    metadata: Optional[Paper]
    source_url: str


async def classify_url(url: str) -> str:
    """Return "pdf" or "landing_page" by inspecting Content-Type via HEAD."""
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=10.0,
                                     headers={"User-Agent": _BROWSER_UA}) as client:
            resp = await client.head(url)
        ct = resp.headers.get("content-type", "")
        if ct.startswith("application/pdf"):
            return "pdf"
    except httpx.HTTPError:
        pass

    if url.lower().endswith(".pdf"):
        return "pdf"
    return "landing_page"


async def scrape_landing_page(url: str) -> ScrapeResult:
    """Scrape academic landing page for DOI, PDF URL, and metadata."""
    result = ScrapeResult(doi=None, pdf_url=None, is_open_access=False,
                          metadata=_blank_paper(), source_url=url)

    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=15.0,
            headers={"User-Agent": _BROWSER_UA},
            max_redirects=5,
        ) as client:
            resp = await client.get(url)
    except httpx.HTTPError as e:
        logger.warning("Failed to fetch %s: %s", url, e)
        return result

    ct = resp.headers.get("content-type", "")
    if ct.startswith("application/pdf"):
        raise ValueError("direct_pdf")

    soup = BeautifulSoup(resp.text, "lxml")
    meta = result.metadata
    assert meta is not None

    # ---- PRIORITY 1: Highwire Press tags ----
    def hw(name: str) -> Optional[str]:
        tag = soup.find("meta", attrs={"name": name})
        return tag["content"].strip() if tag and tag.get("content") else None  # type: ignore[index]

    def hw_all(name: str) -> list[str]:
        tags = soup.find_all("meta", attrs={"name": name})
        return [t["content"].strip() for t in tags if t.get("content")]  # type: ignore[index]

    if doi_raw := hw("citation_doi"):
        result.doi = _strip_doi(doi_raw)

    if pdf_url := hw("citation_pdf_url"):
        result.pdf_url = pdf_url
        result.is_open_access = True

    if v := hw("citation_title"):
        meta.title = v
    authors_hw = hw_all("citation_author")
    if authors_hw:
        meta.authors = authors_hw
    if v := hw("citation_journal_title"):
        meta.journal = v
    if v := hw("citation_publication_date"):
        meta.year = _parse_year(v)
    if v := hw("citation_volume"):
        meta.volume = v
    if v := hw("citation_issue"):
        meta.issue = v
    first_page = hw("citation_firstpage")
    last_page = hw("citation_lastpage")
    if first_page and last_page:
        meta.pages = f"{first_page}\u2013{last_page}"
    elif first_page:
        meta.pages = first_page
    if v := hw("citation_abstract"):
        meta.abstract = v
    kw_tags = hw_all("citation_keywords")
    if kw_tags:
        keywords: list[str] = []
        for kw in kw_tags:
            keywords.extend(k.strip() for k in kw.split(",") if k.strip())
        meta.keywords = keywords

    # ---- PRIORITY 2: Dublin Core ----
    def dc(name: str) -> Optional[str]:
        for variant in (name, name.lower(), name.upper()):
            tag = soup.find("meta", attrs={"name": variant})
            if tag and tag.get("content"):
                return tag["content"].strip()  # type: ignore[union-attr]
        return None

    def dc_all(name: str) -> list[str]:
        results: list[str] = []
        for variant in (name, name.lower(), name.upper()):
            for tag in soup.find_all("meta", attrs={"name": variant}):
                if tag.get("content"):
                    results.append(tag["content"].strip())
        return results

    if not result.doi:
        if v := dc("DC.identifier"):
            result.doi = _strip_doi(v)
    if not meta.title:
        if v := dc("DC.title"):
            meta.title = v
    dc_creators = dc_all("DC.creator")
    if dc_creators and not meta.authors:
        meta.authors = dc_creators
    if not meta.journal:
        if v := dc("DC.source"):
            meta.journal = v
    if not meta.year:
        if v := dc("DC.date"):
            meta.year = _parse_year(v)

    # ---- PRIORITY 3: JSON-LD ----
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or "")
        except Exception:
            continue
        if isinstance(data, list):
            # take first ScholarlyArticle or Article
            for item in data:
                if item.get("@type") in ("ScholarlyArticle", "Article", "CreativeWork"):
                    data = item
                    break
            else:
                continue

        at = data.get("@type", "")
        if at not in ("ScholarlyArticle", "Article", "CreativeWork"):
            continue

        if not result.doi:
            ident = data.get("identifier", "")
            if isinstance(ident, str):
                result.doi = _strip_doi(ident)

        if not result.pdf_url:
            u = data.get("url", "")
            if "/pdf/" in u:
                result.pdf_url = u  # will verify below

        if not meta.title:
            meta.title = data.get("name") or data.get("headline") or ""

        if not meta.authors:
            ld_authors = data.get("author", [])
            if isinstance(ld_authors, dict):
                ld_authors = [ld_authors]
            meta.authors = [
                a.get("name", "") for a in ld_authors if isinstance(a, dict) and a.get("name")
            ]

        if not meta.journal:
            part = data.get("isPartOf", {})
            if isinstance(part, dict):
                meta.journal = part.get("name", "")
            pub = data.get("publisher", {})
            if isinstance(pub, dict) and not meta.journal:
                meta.journal = pub.get("name", "")

        if not meta.year:
            if dp := data.get("datePublished"):
                meta.year = _parse_year(str(dp))
        break  # first matching block is enough

    # ---- PRIORITY 4: OpenGraph ----
    def og(prop: str) -> Optional[str]:
        tag = soup.find("meta", attrs={"property": f"og:{prop}"})
        return tag["content"].strip() if tag and tag.get("content") else None  # type: ignore[index]

    if not meta.title:
        meta.title = og("title") or ""
    if not meta.abstract:
        meta.abstract = og("description") or ""

    # ---- PRIORITY 5: DOI in URL ----
    if not result.doi:
        m = DOI_RE.search(url)
        if m:
            result.doi = _strip_doi(m.group(1))

    # ---- PDF URL verification ----
    if result.pdf_url:
        result.pdf_url = await _verify_pdf_url(result.pdf_url)
        if result.pdf_url is None:
            result.is_open_access = False

    if result.doi:
        meta.doi = result.doi

    return result


async def _verify_pdf_url(url: str) -> Optional[str]:
    """HEAD-check a candidate PDF URL; return it if confirmed, None otherwise."""
    try:
        async with httpx.AsyncClient(
            follow_redirects=False, timeout=10.0,
            headers={"User-Agent": _BROWSER_UA}
        ) as client:
            resp = await client.head(url)

        # Redirect to login/SSO
        if resp.status_code in (301, 302):
            location = resp.headers.get("location", "").lower()
            if any(k in location for k in PAYWALLED_KEYWORDS):
                return None
            # Follow the redirect once more
            return url  # optimistic — caller will get actual content-type on GET

        if resp.status_code in (401, 403):
            return None

        ct = resp.headers.get("content-type", "")
        if resp.status_code == 200 and not ct.startswith("application/pdf"):
            return None

    except httpx.HTTPError:
        return None

    return url
