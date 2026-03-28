import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

from paperbase.core.metadata import RateLimiter

logger = logging.getLogger(__name__)

UNPAYWALL_BASE = "https://api.unpaywall.org/v2"
_BROWSER_UA = "Mozilla/5.0 (compatible; PaperBase/1.0)"


def _sanitise_doi_for_path(doi: str) -> str:
    """Make a DOI filesystem-safe."""
    return re.sub(r"[/\\:*?\"<>|]", "_", doi)


@dataclass
class DownloadResult:
    success: bool
    tmp_path: Optional[Path] = None
    reason: str = ""        # "no_oa_pdf" | "not_pdf" | "http_error" | ""


async def download_via_unpaywall(
    doi: str,
    user_email: str,
    tmp_dir: Path,
    rate_limiter: RateLimiter,
) -> DownloadResult:
    """Look up Unpaywall and download the best OA PDF for a DOI."""
    await rate_limiter.acquire_unpaywall()

    url = f"{UNPAYWALL_BASE}/{doi}"
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            resp = await client.get(url, params={"email": user_email})
    except httpx.HTTPError as e:
        logger.warning("Unpaywall request failed for %s: %s", doi, e)
        return DownloadResult(success=False, reason="http_error")

    if resp.status_code != 200:
        logger.debug("Unpaywall returned %d for %s", resp.status_code, doi)
        return DownloadResult(success=False, reason="no_oa_pdf")

    data = resp.json()
    best = data.get("best_oa_location")
    if not best:
        return DownloadResult(success=False, reason="no_oa_pdf")

    pdf_url: Optional[str] = best.get("url_for_pdf")
    if not pdf_url:
        pdf_url = best.get("url")  # some locations serve PDF at landing URL
    if not pdf_url:
        return DownloadResult(success=False, reason="no_oa_pdf")

    return await _download_pdf(pdf_url, doi, tmp_dir)


async def download_pdf_direct(url: str, doi: Optional[str], tmp_dir: Path) -> DownloadResult:
    """Download a PDF directly from a URL."""
    label = doi or "unknown"
    return await _download_pdf(url, label, tmp_dir)


async def _download_pdf(url: str, label: str, tmp_dir: Path) -> DownloadResult:
    tmp_dir.mkdir(parents=True, exist_ok=True)
    safe_name = _sanitise_doi_for_path(label)
    tmp_path = tmp_dir / f"{safe_name}.pdf"

    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=60.0,
            headers={"User-Agent": _BROWSER_UA},
        ) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code not in (200, 206):
                    return DownloadResult(success=False, reason="http_error")
                ct = resp.headers.get("content-type", "")
                if not ct.startswith("application/pdf"):
                    return DownloadResult(success=False, reason="not_pdf")
                with open(tmp_path, "wb") as fh:
                    async for chunk in resp.aiter_bytes(chunk_size=65536):
                        fh.write(chunk)
    except httpx.HTTPError as e:
        logger.warning("PDF download failed for %s: %s", label, e)
        return DownloadResult(success=False, reason="http_error")

    return DownloadResult(success=True, tmp_path=tmp_path)
