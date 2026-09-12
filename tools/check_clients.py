"""Verify Phase 4's injected-client refactor of downloader.py, scraper.py and paper_detail.py.

    py -3.12 tools/check_clients.py
"""
import inspect
import py_compile
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from paperbase.core import downloader, scraper
from paperbase.ui import paper_detail

assert "httpx.AsyncClient(" not in inspect.getsource(downloader), (
    "downloader.py must not construct its own AsyncClient"
)
assert "httpx.AsyncClient(" not in inspect.getsource(scraper), (
    "scraper.py must not construct its own AsyncClient"
)

assert "client" in inspect.signature(downloader.download_pdf_direct).parameters
assert "client" in inspect.signature(downloader.download_via_unpaywall).parameters
assert "client" in inspect.signature(scraper.classify_url).parameters
assert "client" in inspect.signature(scraper.scrape_landing_page).parameters

assert "user_email" in inspect.signature(downloader.download_via_unpaywall).parameters, (
    "user_email is the Unpaywall query parameter, not a User-Agent — it must stay"
)

paper_detail_source = inspect.getsource(paper_detail)
count = paper_detail_source.count("httpx.AsyncClient(")
assert count == 2, f"expected exactly 2 httpx.AsyncClient( construction sites, found {count}"

for mod_path in (
    Path(downloader.__file__),
    Path(scraper.__file__),
    Path(paper_detail.__file__),
):
    py_compile.compile(str(mod_path), doraise=True)

print("clients OK")
