"""Benchmark the Crossref lookup path PaperBase's import pipeline actually uses.

    py -3.12 tools/bench_network.py [--dois 24] [--email ADDR] [--concurrency N]

Times paperbase.core.metadata.resolve_metadata calls, through one shared RateLimiter and
one shared httpx.AsyncClient, bounded by an asyncio.Semaphore(N), against a fixed set of
24 real DOIs (6 repeated 4 times) so numbers compare run to run. The DOI list and the
shape of _run stay put for that reason.
"""
import argparse
import asyncio
import itertools
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from paperbase.core.importer import _MAX_CONCURRENT_ITEMS
from paperbase.core.metadata import RateLimiter, resolve_metadata

DEFAULT_EMAIL = "paperbase-bench@example.invalid"

# Six real DOIs, repeated to reach the requested count — fixed so successive runs compare.
BASE_DOIS = [
    "10.1038/nature12373",
    "10.1126/science.1157784",
    "10.1016/j.cell.2015.05.001",
    "10.1073/pnas.0709640104",
    "10.1371/journal.pone.0000308",
    "10.1103/PhysRevLett.116.061102",
]


async def _run(dois: list[str], email: str, concurrency: int) -> float:
    rate_limiter = RateLimiter()
    semaphore = asyncio.Semaphore(concurrency)

    async def _bounded(doi: str, client: httpx.AsyncClient) -> None:
        async with semaphore:
            await resolve_metadata(doi, email, rate_limiter, client)

    start = time.perf_counter()
    async with httpx.AsyncClient(timeout=30.0) as client:
        await asyncio.gather(*(_bounded(doi, client) for doi in dois))
    return time.perf_counter() - start


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dois", type=int, default=24)
    ap.add_argument("--email", default=DEFAULT_EMAIL)
    ap.add_argument("--concurrency", type=int, default=_MAX_CONCURRENT_ITEMS)
    args = ap.parse_args()

    dois = list(itertools.islice(itertools.cycle(BASE_DOIS), args.dois))
    total = asyncio.run(_run(dois, args.email, args.concurrency))
    per_doi_ms = (total / len(dois) * 1000) if dois else 0.0

    print(f"network dois={len(dois)} mode=pooled concurrency={args.concurrency} "
          f"per_doi={per_doi_ms:.1f}ms total={total:.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
