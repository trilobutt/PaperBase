# Import throughput and working deduplication for incremental runs

base: (fill with `git rev-parse HEAD` before dispatching Phase 1)

The library is already imported. The workload from here is repeated runs of roughly 200 new
PDFs, which makes two things matter: the run is network-bound almost end to end, and every one
of those runs is an opportunity to re-import something already held.

Two halves, and they share the same five import paths, which is why they are one plan:

- **Throughput.** One shared `httpx.AsyncClient` instead of one per request, four items in
  flight, and one PDF read instead of four. Roughly 132 s per 200-item run down to 18 s.
- **Deduplication.** A `content_hash` column, checked alongside DOI and ISBN through a single
  claim that holds under concurrency, plus a `Scan library…` button in Settings that
  fingerprints the rows imported before the column existed and reports the duplicates already
  in the library.

Where the two halves pull against each other, deduplication wins. See the design decisions
below.

**done-when:** `py -3.12 tools/bench_network.py` reports a per-DOI time at least 60% below the
baseline in `tasks/bench-baseline.txt`; `py -3.12 tools/bench_import.py --papers 60` reports a
per-paper time at least 20% below its baseline; `py -3.12 tools/check_import_flush.py` prints
`flush OK`, which is where the three concurrent-duplicate cases are proved; and
`py -3.12 -m paperbase.main` starts against a fixture data directory, lists papers, and shows
`Library maintenance` in Settings with a working `Scan library…` button.

## Where the time actually goes (measured on this machine, live Crossref)

| Term | Now | Per 200-item run |
|---|---|---|
| Crossref, a new `AsyncClient` per request | 627 ms/item | **125 s** |
| PDF text, four `fitz.open` calls per file | 36 ms/item | 7.2 s |
| Embedding, one `encode` per paper | ~30 ms/item | 6 s |
| Per-paper `commit()` under WAL | ~0.1 ms/item | 0.02 s |

Crossref round trips, same DOIs, same machine:

```
client per request :   627 ms/item      (TLS handshake every call)
shared client, seq :   152 ms/item
shared client,  2  :    96 ms/item
shared client,  4  :    56 ms/item
shared client,  8  :    35 ms/item
```

`metadata.py:136`, `metadata.py:312`, `metadata.py:350`, `metadata.py:426`,
`downloader.py:40`, `downloader.py:76`, `scraper.py:64`, `scraper.py:84` and `scraper.py:286`
each open `async with httpx.AsyncClient(...)` per call, so no connection is ever reused. That
one fact is 94% of a 200-item run.

Target after this plan: roughly 132 s down to roughly 18 s.

## Environment facts this plan depends on (verified on this machine)

- Python 3.12.10, 24 logical cores.
- Installed: `numpy`, `fitz` (PyMuPDF), `httpx`, `tantivy`, `PyQt6`, `pytest`.
- **Not installed: `sentence-transformers`, `keybert`.** No phase may have a `done-when:` that
  loads the real embedding model. Phase 6 reaches the model through a monkeypatchable
  module-level factory and verifies batching against a fake.
- `paperbase` is **not** pip-installed and there is no `tests/` directory. Verification scripts
  live in `tools/` and do their own `sys.path.insert`, exactly as `tools/make_fixture.py:18`
  already does. Do not add a pytest layout here; the vector plan's Phase 1 owns that.
- A `QThread` can be constructed and its `run()` called directly with no `QApplication` and no
  event loop; signals with no receivers emit harmlessly. Verified. This is what lets
  `tools/bench_import.py` drive `ImportWorker` in-process.
- `RateLimiter` (`metadata.py:498`) is already concurrency-safe: an `asyncio.Lock` plus a
  monotonic timestamp, so N coroutines are throttled to one request per 20 ms between them.
  It needs no change and it is what keeps 4 in flight inside Crossref's polite-pool guidance.
- The only callers of the network modules are `importer.py` and `paper_detail.py:498`,
  `:499`, `:523`. Nothing else needs updating when their signatures change.
- A synthetic 12-page PDF (33 KB of text) is read by `fitz` in ~23 ms; the current code reads
  each imported file four times.

## Design decisions already made (do not re-open)

- **One `httpx.AsyncClient` per import run, created and owned by `ImportWorker._run_async`
  and passed down as a parameter.** Not a module-level global: the worker's event loop is its
  own (`importer.py:113` builds one with `asyncio.new_event_loop()`), and an `AsyncClient`
  bound to a dead loop is a latent failure. The three `paper_detail.py` call sites open their
  own short-lived client, which is correct for a one-off UI lookup.
- **Four items in flight, as a module constant.** Measured 56 ms/item; 8 would give 35 ms but
  sits closer to Crossref's published polite-pool ceiling, and the existing 20 ms `RateLimiter`
  floor is the throttle either way. Not a setting: it would be set once and never touched.
- **The PDF read moves to `asyncio.to_thread`, not to a process pool.** A pool was the earlier
  plan; at 200 items the 2 to 4 s of Windows `spawn` startup cancels the ~3 s it saves.
  PyMuPDF releases the GIL during page extraction, so a thread gets the same overlap for
  nothing.
- **No `Database.batch()` transaction.** Under WAL with `synchronous=NORMAL` the per-paper
  commit costs about 20 ms across a whole 200-item run, and batching it would mean holding a
  write transaction on a connection the UI thread shares (`db.py:131` passes
  `check_same_thread=False`). Not worth it at this scale.
- **`_claim_paper` → `place_file` → `insert_paper` → `_buffer` is an atomic sequence and must
  stay one.** It contains no `await`, so the event loop cannot interleave another item between
  the duplicate check, the destination-name computation and the insert. **Never introduce an
  `await` inside that block**; doing so reopens both the duplicate race and a
  duplicate-destination race that no lock in the current code would catch. Phase 5's
  verification asserts this against the source text rather than trusting the comment.
- **Identity keys are claimed in memory for the length of a run.** With four items in flight,
  the gap between a database existence check and the insert spans an `await`, so two items
  carrying the same hash, DOI or ISBN can both pass. `_claim_paper` closes that. The three sets
  are bounded by the run's item count and die with the worker.
- **`INDEX_COMMIT_INTERVAL` drops from 200 to 50.** At 200 an incremental run of 200 papers
  flushes exactly once, at the end, so a crash costs the whole run's indexing. Four Tantivy
  commits per run is nothing.
- **`list(set(...))` becomes `sorted(set(...))` in the categorisation merge.** The existing
  comparison `new_col_ids != paper.collection_ids` is against an unordered `list(set(...))`, so
  it reports a change whenever the ordering differs and writes a row that did not need writing.
- **Where deduplication and throughput conflict, deduplication wins.** This is the user's
  stated priority and it decides three things in this plan: every path checks the content hash
  the moment the file exists locally, even though that costs a lookup on the common
  non-duplicate case; `_claim_paper` runs on every path including the ones where an earlier
  check already looked, because the earlier check is not authoritative; and the backfill in
  Phase 8 is a prerequisite for the feature working rather than an optional tidy-up, so
  Phase 9 surfaces incomplete coverage in the interface instead of letting it pass silently.
  A duplicate that gets in is permanent and manual to undo; a slower import is neither.
- **Deduplication has three keys: content hash, DOI, ISBN. Not fuzzy title matching.** A
  title-and-year match would catch the same paper arriving as two unrelated files, and it
  would also fire on errata, corrigenda, conference-then-journal pairs and series volumes.
  Silently skipping those is worse than importing a duplicate, and a confirm step is a
  different feature from an import run that must survive unattended.
- **The content hash is SHA-256 of the raw file bytes.** Not a cheaper fingerprint over the
  first N kilobytes: hashing runs at 2.3 GB/s here and the file is being read anyway, so the
  full hash costs essentially nothing over the partial one, and it never needs explaining.
- **Duplicates are reported, never deleted.** The fingerprint dialog lists every group of
  identical files with their full paths; choosing which copy to keep depends on file quality
  and folder placement, which no automated rule here has any basis to judge.
- **The backfill is a button in Settings, not a command-line tool.** It is a thing the user
  does once and occasionally repeats, on the machine that holds the library, and it needs
  progress and a stop control. A `tools/` script would need all of that built twice and would
  put the duplicate report somewhere the application cannot show it.
- **`_claim_paper` is the only thing that claims; `_is_known` only looks.** Splitting them is
  what lets the early per-key checks be opportunistic and vary between paths while the
  guarantee stays in exactly one place. See the dedupe contract at the head of Phase 5.

---

## Phase 1: benchmarks and baselines

skill: coding-standards:python
model: sonnet

**Intent:** the plan's `done-when:` is a before-and-after measurement on two axes, network and
local pipeline. Both instruments must exist and be run before anything changes. This phase
touches no library code.

**Anchor:** two new files, `tools/bench_network.py` and `tools/bench_import.py`. Both mirror
the `sys.path.insert` header of `tools/make_fixture.py:18` and the argparse style of
`tools/make_fixture.py:1-30`.

### `tools/bench_network.py`

```
py -3.12 tools/bench_network.py [--dois N] [--email ADDR]
```

Hits the live Crossref API with a fixed list of 24 real DOIs (repeat a list of 6 four times;
use the same set every run so the numbers compare). It measures the shape the application
actually uses, by calling `paperbase.core.metadata.resolve_metadata` itself rather than
reimplementing the request:

- builds one `RateLimiter`;
- times `--dois` sequential `resolve_metadata(...)` calls;
- prints one line:

```
network dois=24 mode=current per_doi=627.4ms total=15.06s
```

`--email` defaults to `paperbase-bench@example.invalid` and is passed as the `user_email`
argument, which `resolve_metadata` puts in the polite `User-Agent`. Do not read the user's
configured email from settings.

Phase 9 re-runs this file unchanged apart from a `mode=` label and the concurrency it can now
use; write it so that the concurrent variant is added later without restructuring.

### `tools/bench_import.py`

```
py -3.12 tools/bench_import.py --papers 60 [--pages 12] [--latency 0.05] [--out DIR]
```

Measures the local pipeline with the network stubbed out, so it isolates PDF reading,
placement, insertion and indexing:

- Builds `--papers` synthetic PDFs of `--pages` pages each into `DIR/pdfs` (default `DIR` is a
  fresh `tempfile.mkdtemp()`), using `fitz.open()` / `new_page()` / `insert_text(...)`. Every
  page carries 40 lines of filler words; on **even-numbered** papers only, page 1 line 3 reads
  `doi:10.1234/bench.{i}`, so both the DOI-found and DOI-absent paths are exercised.
- Opens a `Database` and `Indexer` under `DIR/data`.
- Replaces the network calls by rebinding them **on the importer module**, which is where the
  names the worker calls are resolved:

```python
import paperbase.core.importer as imp

async def _fake_resolve(doi, user_email, rl):
    await asyncio.sleep(latency)
    return Paper(id=None, doi=doi, title=f"Benchmark paper {doi}", authors=["Bench, A."],
                 journal="Journal of Benchmarks", year=2024, volume="1", issue="1",
                 pages="1-10", abstract="An abstract long enough to be worth embedding.",
                 keywords=[], tags=[], collection_ids=[], file_path="",
                 date_added=_now, date_modified=_now, metadata_source="crossref",
                 needs_review=False, open_access=False)

imp.resolve_metadata = _fake_resolve
imp.resolve_book_metadata = _fake_resolve
imp.guess_metadata_from_text = _fake_guess     # same shape, returns needs_review=True
```

- Constructs `ImportWorker(mode="pdfs", items=[str(p) for p in pdfs], db=db, indexer=indexer,
  library_root=DIR/"lib", user_email="bench@example.invalid", state_file=None,
  categoriser=None)` and calls `worker.run()` directly (no `QApplication`).
- Times the `run()` call with `time.perf_counter()` and prints exactly one line:

```
import papers=60 pages=12 latency=0.05 elapsed=12.34s per_paper=205.7ms
```

- Accepts `--out` so a later run can reuse a directory, and removes a temp directory it made
  itself.

**Note for the implementer:** these stubs match the *current* signatures. Phases 3 and 4 add a
`client` parameter and Phase 4 renames `guess_metadata_from_text`; Phase 9 updates the stubs.
Write them so that is a one-line change each.

Then run both and write their two output lines into `tasks/bench-baseline.txt`.

done-when: `py -3.12 tools/bench_network.py` prints one `network dois=24 …` line and exits 0;
`py -3.12 tools/bench_import.py --papers 60` prints one `import papers=60 …` line and exits 0;
`tasks/bench-baseline.txt` contains both lines.

---

## Phase 2: `content_hash` on the schema, and the duplicate queries

skill: coding-standards:python, coding-standards:sql
model: sonnet

**Intent:** deduplication currently has exactly one working key, the DOI. `paper_exists_by_path`
compares the *source* path against `papers.file_path`, but `place_file(move=False)` stores the
*destination*, so it never matches and mode 1 has no path-based check at all. ISBN is a column
and is never checked. This phase adds the column and the queries the import paths and the
backfill tool need; Phases 5 and 7 use them.

**Anchor:** `paperbase/models/paper.py` and `paperbase/core/db.py`, eight edits, **bottom-up
within each file, `db.py` first**.

**Edit 2h: `db.py:309-312`, method `paper_exists_by_path`.** Current state, verbatim:

```python
    def paper_exists_by_path(self, file_path: str) -> bool:
        conn = self._conn_required()
        row = conn.execute("SELECT 1 FROM papers WHERE file_path=?", (file_path,)).fetchone()
        return row is not None
```

**Delete it entirely.** Its only caller is `_import_pdf`, which loses it in Phase 5. It
compared the *source* path a user dropped against `file_path`, which `place_file(move=False)`
fills with the *destination*, so it never matched anything and mode 1 has had no path check at
all. The content hash added below answers the question it was asking, correctly.

Between this phase and Phase 5, `importer.py` names a method that no longer exists. That is
harmless: Python resolves the attribute at call time, and `importer.py` cannot run until Phase
7 completes it regardless.

**Edit 2g: `db.py:297`, immediately after `get_papers_slim_by_ids` ends and before
`delete_paper` at `db.py:299`.** `delete_paper` itself is unchanged. Insert:

```python
    def paper_exists_by_hash(self, content_hash: str) -> bool:
        conn = self._conn_required()
        row = conn.execute(
            "SELECT 1 FROM papers WHERE content_hash=?", (content_hash,)
        ).fetchone()
        return row is not None

    def paper_exists_by_isbn(self, isbn: str) -> bool:
        conn = self._conn_required()
        row = conn.execute("SELECT 1 FROM papers WHERE isbn=?", (isbn,)).fetchone()
        return row is not None

    def get_papers_missing_hash(self) -> list[tuple[int, str]]:
        """(id, file_path) for every row with no content hash yet.

        The backfill tool is resumable purely by re-running this: a row leaves the result set
        as soon as its hash is written.
        """
        conn = self._conn_required()
        rows = conn.execute(
            "SELECT id, file_path FROM papers "
            "WHERE content_hash IS NULL OR content_hash = '' ORDER BY id"
        ).fetchall()
        return [(r["id"], r["file_path"]) for r in rows]

    def get_hash_coverage(self) -> tuple[int, int]:
        """(rows carrying a content hash, rows in total).

        One pass over an indexed column, so the Settings dialog can state plainly how much of
        the library is protected from content duplicates every time it opens.
        """
        conn = self._conn_required()
        row = conn.execute(
            "SELECT COUNT(*) AS total, "
            "COUNT(CASE WHEN content_hash IS NOT NULL AND content_hash != '' "
            "           THEN 1 END) AS hashed "
            "FROM papers"
        ).fetchone()
        return row["hashed"], row["total"]

    def get_duplicate_hash_groups(self) -> list[tuple[str, list[int]]]:
        """Every content hash held by more than one row, with those rows' ids.

        Reporting only. Nothing in the application deletes on the strength of this.
        """
        conn = self._conn_required()
        rows = conn.execute(
            "SELECT content_hash, GROUP_CONCAT(id) AS ids FROM papers "
            "WHERE content_hash IS NOT NULL AND content_hash != '' "
            "GROUP BY content_hash HAVING COUNT(*) > 1 ORDER BY content_hash"
        ).fetchall()
        return [(r["content_hash"], [int(i) for i in r["ids"].split(",")]) for r in rows]
```

**Edit 2f: `db.py:244-252`, method `update_paper_field`, the `allowed` set.** Current state,
verbatim:

```python
        allowed = {
            "doi", "title", "authors", "journal", "year", "volume", "issue",
            "pages", "abstract", "keywords", "tags", "collection_ids",
            "file_path", "metadata_source", "needs_review", "open_access",
            "isbn", "document_type",
        }
```

Change to:

```python
        allowed = {
            "doi", "title", "authors", "journal", "year", "volume", "issue",
            "pages", "abstract", "keywords", "tags", "collection_ids",
            "file_path", "metadata_source", "needs_review", "open_access",
            "isbn", "document_type", "content_hash",
        }
```

This is what the backfill tool writes through, so the whitelist must carry it. The column name
is still never interpolated from anything outside this set.

**Edit 2e: `db.py:208-242`, method `update_paper`.** In the SQL, current state, verbatim:

```python
                needs_review=?, open_access=?, isbn=?, document_type=?
            WHERE id=?
```

Change to:

```python
                needs_review=?, open_access=?, isbn=?, document_type=?,
                content_hash=?
            WHERE id=?
```

and in the parameter tuple, current state, verbatim:

```python
                paper.isbn,
                paper.document_type,
                paper.id,
            ),
```

Change to:

```python
                paper.isbn,
                paper.document_type,
                paper.content_hash,
                paper.id,
            ),
```

**Edit 2d: `db.py:170-206`, method `insert_paper`.** In the column list, current state,
verbatim:

```python
                 date_added, date_modified, metadata_source, needs_review, open_access,
                 isbn, document_type)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
```

Change to:

```python
                 date_added, date_modified, metadata_source, needs_review, open_access,
                 isbn, document_type, content_hash)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
```

That is twenty-one placeholders, not twenty. Count them. In the parameter tuple, current
state, verbatim:

```python
                paper.isbn,
                paper.document_type,
            ),
        )
```

Change to:

```python
                paper.isbn,
                paper.document_type,
                paper.content_hash,
            ),
        )
```

**Edit 2c: `db.py:146-155`, method `_migrate`.** Current state, verbatim:

```python
        for stmt in (
            "ALTER TABLE papers ADD COLUMN isbn TEXT",
            "ALTER TABLE papers ADD COLUMN document_type TEXT NOT NULL DEFAULT 'article'",
        ):
            try:
                self._conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # column already exists
```

Change to:

```python
        for stmt in (
            "ALTER TABLE papers ADD COLUMN isbn TEXT",
            "ALTER TABLE papers ADD COLUMN document_type TEXT NOT NULL DEFAULT 'article'",
            "ALTER TABLE papers ADD COLUMN content_hash TEXT",
            "CREATE INDEX IF NOT EXISTS idx_papers_content_hash ON papers(content_hash)",
            "CREATE INDEX IF NOT EXISTS idx_papers_isbn ON papers(isbn)",
        ):
            try:
                self._conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # column or index already exists
```

The indexes are created here as well as in `SCHEMA_SQL` because on an existing database the
column does not exist until the `ALTER` above them has run, and `SCHEMA_SQL` is executed
before `_migrate`.

**Edit 2b: `db.py:11-50` and `db.py:60-64`, the schema, the indexes and the slim column list.**
In `SCHEMA_SQL`, current state, verbatim:

```
    isbn            TEXT,
    document_type   TEXT NOT NULL DEFAULT 'article'
);
```

Change to:

```
    isbn            TEXT,
    document_type   TEXT NOT NULL DEFAULT 'article',
    content_hash    TEXT
);
```

Append these two lines to the index block that ends at `db.py:50`:

```
CREATE INDEX IF NOT EXISTS idx_papers_content_hash ON papers(content_hash);
CREATE INDEX IF NOT EXISTS idx_papers_isbn         ON papers(isbn);
```

`_SLIM_COLS` at `db.py:60-64`, current state, verbatim:

```python
_SLIM_COLS = (
    "id, doi, title, authors, journal, year, volume, issue, pages, "
    "tags, collection_ids, file_path, date_added, date_modified, "
    "metadata_source, needs_review, open_access, isbn, document_type"
)
```

Change to:

```python
_SLIM_COLS = (
    "id, doi, title, authors, journal, year, volume, issue, pages, "
    "tags, collection_ids, file_path, date_added, date_modified, "
    "metadata_source, needs_review, open_access, isbn, document_type, content_hash"
)
```

A slim `Paper` is a whole `Paper` as far as `update_paper` is concerned, so a column left out
of this list is a column that gets written back as `NULL` by anything that round-trips one.
Sixty-four characters per cached row is not worth the hazard.

In **both** `_paper_from_row` (`db.py:67-91`) and `_paper_slim_from_row` (`db.py:94` onwards),
add as the final keyword argument, matching the late-column guard already used for `isbn` at
`db.py:89`:

```python
        content_hash=row["content_hash"] if "content_hash" in keys else None,
```

**Edit 2a: `paperbase/models/paper.py:26-27`.** Current state, verbatim:

```python
    isbn: Optional[str] = None          # ISBN-13 preferred; populated for books
    document_type: str = 'article'      # "article" | "book" | "book-chapter" | "proceedings"
```

Change to:

```python
    isbn: Optional[str] = None          # ISBN-13 preferred; populated for books
    document_type: str = 'article'      # "article" | "book" | "book-chapter" | "proceedings"
    content_hash: Optional[str] = None  # SHA-256 of the PDF bytes; None on pre-hash rows
```

A keyword argument with a default, so every existing `Paper(...)` construction site stays
valid.

done-when: `py -3.12 tools/check_hash_schema.py` prints `hash schema OK` and exits 0. Create
that file in this phase, following the `sys.path.insert` header of `tools/make_fixture.py:18`.
It must:

- open a `Database` on a fresh `tempfile.mkdtemp()` path;
- insert three papers: two sharing `content_hash="a" * 64` with distinct DOIs and file paths,
  one with `content_hash=None`;
- assert `paper_exists_by_hash("a" * 64)` is `True` and `paper_exists_by_hash("b" * 64)` is
  `False`;
- assert `paper_exists_by_isbn(...)` is `True` for an inserted ISBN and `False` otherwise;
- assert `get_papers_missing_hash()` returns exactly the one row with no hash;
- assert `get_hash_coverage()` returns `(2, 3)`, and that it returns `(0, 0)` on an empty
  database rather than raising;
- assert `get_duplicate_hash_groups()` returns exactly one group whose id list holds the two
  matching rows;
- round-trip a paper: `get_paper(id)`, then `update_paper(paper)`, then `get_paper(id)` again,
  and assert `content_hash` survived. Do the same through `get_papers_slim_by_ids`. This is the
  regression the `_SLIM_COLS` note above describes;
- `update_paper_field(id, "content_hash", "c" * 64)` and assert it reads back;
- assert `not hasattr(Database, "paper_exists_by_path")`, so the check that never matched is
  gone rather than merely unused;
- **migration**: create a second database file by executing a literal copy of the
  pre-`content_hash` `CREATE TABLE papers` statement (paste it into the script), then open a
  `Database` on it and assert `open()` succeeds, that `content_hash` appears in
  `PRAGMA table_info(papers)`, and that `get_papers_missing_hash()` runs without error;
- print `hash schema OK`.

---

## Phase 3: `paperbase/core/metadata.py`, injected client and one PDF read

skill: coding-standards:python
model: sonnet

**Intent:** two changes to one file, done in one pass so the anchors do not have to survive
each other. Every network function takes the caller's `httpx.AsyncClient` instead of building
one, which is worth 475 ms per DOI. And every PDF is read once into a `PdfText` instead of
being opened four times.

**Anchor:** `paperbase/core/metadata.py`, eleven edits, **bottom-up in the order given**.

**Edit 3k: `metadata.py:487-495`, function `extract_fulltext`.** Current state, verbatim:

```python
def extract_fulltext(path: Path) -> str:
    """Extract full text from a PDF for Tantivy indexing."""
    try:
        with fitz.open(str(path)) as doc:
            parts = [page.get_text() for page in doc]
        return "\n".join(parts)
    except Exception as e:
        logger.warning("Full-text extraction failed for %s: %s", path, e)
        return ""
```

**Delete it entirely.** Callers use `PdfText.fulltext`. Leave exactly two blank lines before
`class RateLimiter:`.

**Edit 3j: `metadata.py:421-426`, function `_googlebooks_lookup`.** Current state, verbatim:

```python
async def _googlebooks_lookup(isbn: str, rate_limiter: "RateLimiter") -> Optional[Paper]:
```

and, a few lines below inside it:

```python
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            resp = await client.get(GOOGLEBOOKS_BASE, params=params)
```

Change the signature to:

```python
async def _googlebooks_lookup(
    isbn: str, rate_limiter: "RateLimiter", client: httpx.AsyncClient
) -> Optional[Paper]:
```

and the request to:

```python
        resp = await client.get(GOOGLEBOOKS_BASE, params=params)
```

(one level of indentation less, since the `async with` is gone). Leave the surrounding
`try`/`except httpx.HTTPError` exactly as it is.

**Edit 3i: `metadata.py:346-351`, function `_openlibrary_lookup`.** The same shape. Signature:

```python
async def _openlibrary_lookup(
    isbn: str, rate_limiter: "RateLimiter", client: httpx.AsyncClient
) -> Optional[Paper]:
```

and

```python
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            resp = await client.get(OPENLIBRARY_BASE, params=params)
```

becomes

```python
        resp = await client.get(OPENLIBRARY_BASE, params=params)
```

**Edit 3h: `metadata.py:334`, function `resolve_book_metadata`.** Current state, verbatim:

```python
async def resolve_book_metadata(isbn: str, user_email: str, rate_limiter: "RateLimiter") -> Optional[Paper]:
```

Change to:

```python
async def resolve_book_metadata(
    isbn: str, rate_limiter: "RateLimiter", client: httpx.AsyncClient
) -> Optional[Paper]:
```

`user_email` goes: neither Open Library nor Google Books uses it, and the body never passed it
on. Inside the body, update the two calls to `_openlibrary_lookup(isbn, rate_limiter)` and
`_googlebooks_lookup(isbn, rate_limiter)` to pass `client` as the third argument.

**Edit 3g: `metadata.py:301-313`, function `_crossref_bib_search`.** Current state, verbatim:

```python
async def _crossref_bib_search(
    title: str, user_email: str, rate_limiter: "RateLimiter"
) -> Optional[Paper]:
```

Change to:

```python
async def _crossref_bib_search(
    title: str, user_email: str, rate_limiter: "RateLimiter", client: httpx.AsyncClient
) -> Optional[Paper]:
```

Current state, verbatim:

```python
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            resp = await client.get(CROSSREF_BASE, params=params, headers=headers)
```

Change to:

```python
        resp = await client.get(CROSSREF_BASE, params=params, headers=headers)
```

`user_email` stays here: it builds the polite-pool `User-Agent` two lines above.

**Edit 3f: `metadata.py:251-272`, function `guess_metadata_from_text`.** Current state, the
signature and the extraction block, verbatim:

```python
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
        with fitz.open(str(path)) as doc:
            first_page_text = doc[0].get_text() if len(doc) > 0 else ""
            xmp_meta = doc.metadata
    except Exception:
        base_paper.title = path.name
        return base_paper
```

Change to:

```python
async def guess_metadata(
    path: Path,
    pdf: "PdfText",
    user_email: str,
    rate_limiter: "RateLimiter",
    client: httpx.AsyncClient,
) -> Paper:
    """Best-effort metadata extraction when no DOI is found.

    `path` is still needed for `file_path` and the filename fallback; every byte of text
    comes from `pdf`.
    """
    now = _now_iso()
    base_paper = Paper(
        id=None, doi=None, title="", authors=[], journal="", year=None,
        volume="", issue="", pages="", abstract="", keywords=[], tags=[],
        collection_ids=[], file_path=str(path), date_added=now, date_modified=now,
        metadata_source="filename", needs_review=True, open_access=False,
        isbn=None, document_type="article",
    )

    if not pdf.ok:
        base_paper.title = path.name
        return base_paper

    first_page_text = pdf.first_page
    xmp_meta = pdf.xmp
```

Further down this function, `metadata.py:280`, current state, verbatim:

```python
        found = await _crossref_bib_search(candidate_title, user_email, rate_limiter)
```

Change to:

```python
        found = await _crossref_bib_search(candidate_title, user_email, rate_limiter, client)
```

The rest of the function is untouched: it already reads only `first_page_text` and `xmp_meta`.

**Edit 3e: `metadata.py:128-137`, function `resolve_metadata`.** Current state, verbatim:

```python
async def resolve_metadata(doi: str, user_email: str, rate_limiter: "RateLimiter") -> Optional[Paper]:
```

Change to:

```python
async def resolve_metadata(
    doi: str, user_email: str, rate_limiter: "RateLimiter", client: httpx.AsyncClient
) -> Optional[Paper]:
```

Current state, verbatim:

```python
            async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
                resp = await client.get(url, headers=headers)
```

Change to:

```python
            resp = await client.get(url, headers=headers)
```

The enclosing `for attempt in range(3):` retry loop and its `except httpx.HTTPError` are
unchanged.

**Edit 3d: `metadata.py:101-121`, function `extract_isbn_from_pdf`.** Current state, verbatim:

```python
def extract_isbn_from_pdf(path: Path) -> Optional[str]:
    """Extract first ISBN from PDF text, preferring ISBN-13. Returns normalised digit string."""
    try:
        doc = fitz.open(str(path))
    except Exception as e:
        logger.warning("Cannot open PDF %s: %s", path, e)
        return None

    with doc:
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

    return None
```

Change to:

```python
def extract_isbn(pdf: "PdfText") -> Optional[str]:
    """Extract first ISBN from an already-read PDF, preferring ISBN-13. Normalised digits."""
    full_text = pdf.head

    # Prefixed ISBN (most reliable)
    m = ISBN_RE.search(full_text)
    if m:
        return _normalise_isbn(m.group(1))

    # Bare ISBN-13 (common on copyright pages)
    m2 = ISBN13_BARE_RE.search(full_text)
    if m2:
        return _normalise_isbn(m2.group(1))

    return None
```

**Edit 3c: `metadata.py:51-89`, function `extract_doi_from_pdf`.** Current state, verbatim:

```python
def extract_doi_from_pdf(path: Path) -> Optional[str]:
    """Extract first DOI from PDF text and XMP metadata."""
    try:
        doc = fitz.open(str(path))
    except Exception as e:
        logger.warning("Cannot open PDF %s: %s", path, e)
        return None

    with doc:
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

    return None
```

Change to:

```python
def extract_doi(pdf: "PdfText") -> Optional[str]:
    """Extract first DOI from an already-read PDF's text and XMP metadata."""
    # Search first 100 lines of the first 3 pages
    for line in pdf.head.splitlines()[:100]:
        m = DOI_RE.search(line)
        if m:
            doi = _strip_trailing_punct(m.group(1))
            if _validate_doi(doi):
                return doi

    # Try entire first-page text
    m = DOI_RE.search(pdf.first_page)
    if m:
        doi = _strip_trailing_punct(m.group(1))
        if _validate_doi(doi):
            return doi

    # Check XMP metadata fields
    for key in ("subject", "keywords"):
        val = pdf.xmp.get(key, "") or ""
        m = DOI_RE.search(val)
        if m:
            doi = _strip_trailing_punct(m.group(1))
            if _validate_doi(doi):
                return doi

    return None
```

**Edit 3b: `metadata.py:47-48`, after `_validate_doi`.** Current state, verbatim:

```python
def _validate_doi(doi: str) -> bool:
    return bool(re.match(r"^10\.\d{4,9}/", doi))
```

Change to:

```python
def _validate_doi(doi: str) -> bool:
    return bool(re.match(r"^10\.\d{4,9}/", doi))


@dataclass
class PdfText:
    """Everything one read of a PDF yields.

    Built once per imported file and passed to every extraction step, so a PDF is opened and
    parsed once instead of four times. Plain strings only, so it can be handed across the
    thread boundary the import worker reads it on.
    """
    pages: list[str]
    xmp: dict[str, str]
    sha256: str
    ok: bool

    @property
    def head(self) -> str:
        """First three pages, which is where identifiers live."""
        return "\n".join(self.pages[:3])

    @property
    def first_page(self) -> str:
        return self.pages[0] if self.pages else ""

    @property
    def fulltext(self) -> str:
        """Every page, for the Tantivy `fulltext` field."""
        return "\n".join(self.pages)


def sha256_file(path: Path) -> str:
    """SHA-256 of a file's bytes, streamed in 1 MB chunks. "" if it cannot be read.

    The import path and the Settings fingerprint scan both go through this, so a paper's hash
    cannot depend on which one computed it. Streamed rather than read whole: a scanned book
    can run to hundreds of megabytes and four items are in flight at once.
    """
    digest = hashlib.sha256()
    try:
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as e:
        logger.warning("Cannot hash %s: %s", path, e)
        return ""
    return digest.hexdigest()


def read_pdf(path: Path) -> PdfText:
    """Read a PDF's bytes, text and XMP metadata.

    Blocking and I/O-bound; the import worker calls it through `asyncio.to_thread` so the
    other in-flight items keep going. Never raises: a file that cannot be opened comes back
    `ok=False` with an empty hash.

    The file is read twice, once to hash and once by `fitz`. The second read comes from the
    OS page cache, and SHA-256 runs at 2.3 GB/s here, so this costs far less than holding the
    whole document in memory to serve both.
    """
    sha = sha256_file(path)
    try:
        with fitz.open(str(path)) as doc:
            pages = [page.get_text() for page in doc]
            xmp = dict(doc.metadata or {})
    except Exception as e:
        logger.warning("Cannot read PDF %s: %s", path, e)
        return PdfText(pages=[], xmp={}, sha256=sha, ok=False)
    return PdfText(pages=pages, xmp=xmp, sha256=sha, ok=True)
```

**Edit 3a: `metadata.py:1-8`, the stdlib import block.** Current state, verbatim:

```python
import asyncio
import difflib
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
```

Change to:

```python
import asyncio
import difflib
import hashlib
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
```

**Note:** `importer.py` and `paper_detail.py` still call the old names and signatures. Phases 5
and 7 fix them. This phase's verification does not import either.

done-when: `py -3.12 tools/check_metadata_read.py` prints `metadata OK` and exits 0. Create
that file in this phase, following the `sys.path.insert` header of `tools/make_fixture.py:18`.
It must:

- build a one-page PDF in a temp directory with `fitz`, carrying the lines `Title Here`,
  `doi:10.1234/test.5678` and `ISBN 978-0-306-40615-7`;
- `t = read_pdf(p)` and assert `t.ok`, `len(t.pages) == 1`,
  `extract_doi(t) == "10.1234/test.5678"` and `extract_isbn(t) == "9780306406157"`;
- assert `len(t.sha256) == 64` and that `t.sha256 == sha256_file(p)`, so the two entry points
  agree;
- copy the PDF to a second path and assert the copy's `read_pdf(...).sha256` matches the
  original's, which is the property the whole dedupe rests on;
- write one differing byte into a third copy and assert its hash differs;
- `b = read_pdf(Path("nope.pdf"))` and assert `not b.ok`, `b.fulltext == ""`,
  `b.sha256 == ""` and `extract_doi(b) is None`;
- assert `not hasattr(metadata, "extract_fulltext")`;
- assert `"httpx.AsyncClient(" not in inspect.getsource(metadata)`, so no request builds its
  own client any more;
- assert `client` is a parameter of `resolve_metadata` and of `resolve_book_metadata`, and that
  `user_email` is **not** a parameter of `resolve_book_metadata`;
- print `metadata OK`.

---

## Phase 4: `downloader.py`, `scraper.py` and `paper_detail.py`, injected client

skill: coding-standards:python
model: sonnet

**Intent:** the remaining five `AsyncClient` construction sites. Mode 3 benefits most:
`classify_url`, `scrape_landing_page` and `_verify_pdf_url` usually hit the same host in
sequence and currently handshake three times.

**Anchor:** three files. Within each, work bottom-up.

### `paperbase/core/downloader.py`

**Edit 4f: `downloader.py:70-76`, function `_download_pdf`.** Add `client: httpx.AsyncClient`
as the final parameter and replace its `async with httpx.AsyncClient(...) as client:` block
with direct use of the parameter, dedenting the body one level. Keep every existing argument
that block passes (`follow_redirects`, `timeout`, and any headers) by moving them onto the
`client.get(...)`/`client.stream(...)` call as per-request arguments; `timeout=` and `headers=`
are both accepted per request by httpx.

**Edit 4e: `downloader.py:64`, function `download_pdf_direct`.** Current state, verbatim:

```python
async def download_pdf_direct(url: str, doi: Optional[str], tmp_dir: Path) -> DownloadResult:
```

Change to:

```python
async def download_pdf_direct(
    url: str, doi: Optional[str], tmp_dir: Path, client: httpx.AsyncClient
) -> DownloadResult:
```

and pass `client` through to `_download_pdf`.

**Edit 4d: `downloader.py:29-41`, function `download_via_unpaywall`.** Add
`client: httpx.AsyncClient` as the final parameter, replace the `async with` at line 40 with
direct use, and pass `client` to any `_download_pdf` call in its body. `user_email` stays: it
is the Unpaywall query parameter at line 41, not a User-Agent.

### `paperbase/core/scraper.py`

**Edit 4c: `scraper.py:283-286`, function `_verify_pdf_url`.** Add `client: httpx.AsyncClient`
as the final parameter, drop the `async with`, and move that block's `timeout` and any headers
onto the request call.

**Edit 4b: `scraper.py:78-84`, function `scrape_landing_page`.** Add
`client: httpx.AsyncClient` as the final parameter, drop the `async with`, and pass `client`
to the `_verify_pdf_url(...)` call in its body.

**Edit 4a: `scraper.py:61-64`, function `classify_url`.** Add `client: httpx.AsyncClient` as
the final parameter and drop the `async with`. This one uses `timeout=10.0`; keep that by
passing `timeout=10.0` on the request rather than dropping it.

### `paperbase/ui/paper_detail.py`

These are one-off UI lookups, not part of an import run, so they open their own client.

**Edit 4h: `paper_detail.py:521-534`, method `_do_isbn_lookup`.** Current state, verbatim:

```python
    async def _do_isbn_lookup(self, isbn: str) -> None:
        from paperbase.core.metadata import RateLimiter, resolve_book_metadata
        self._set_lookup_busy(True)
        try:
            rl = RateLimiter()
            paper = await resolve_book_metadata(isbn, self._user_email, rl)
```

Change to:

```python
    async def _do_isbn_lookup(self, isbn: str) -> None:
        import httpx

        from paperbase.core.metadata import RateLimiter, resolve_book_metadata
        self._set_lookup_busy(True)
        try:
            rl = RateLimiter()
            async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
                paper = await resolve_book_metadata(isbn, rl, client)
```

and indent the remainder of the `try:` body that uses `paper` (down to
`self._apply_lookup_result(paper)`) one level to sit inside the `async with`.

**Edit 4g: `paper_detail.py:497-520`, method `_do_doi_lookup`.** Current state, verbatim:

```python
    async def _do_doi_lookup(self, doi: str) -> None:
        from paperbase.core.metadata import RateLimiter, resolve_metadata
        from paperbase.core.scraper import scrape_landing_page
        self._set_lookup_busy(True)
        try:
            rl = RateLimiter()
            paper = await resolve_metadata(doi, self._user_email, rl)
```

Change to:

```python
    async def _do_doi_lookup(self, doi: str) -> None:
        import httpx

        from paperbase.core.metadata import RateLimiter, resolve_metadata
        from paperbase.core.scraper import scrape_landing_page
        self._set_lookup_busy(True)
        try:
            rl = RateLimiter()
            async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
                paper = await resolve_metadata(doi, self._user_email, rl, client)
```

Indent the rest of the `try:` body one level into the `async with`, and change the scrape call
inside it, currently verbatim:

```python
                    scrape = await scrape_landing_page(f"https://doi.org/{doi}")
```

to:

```python
                        scrape = await scrape_landing_page(f"https://doi.org/{doi}", client)
```

(one level deeper than before, because of the new `async with`).

done-when: `py -3.12 tools/check_clients.py` prints `clients OK` and exits 0. Create that file
in this phase, following the `sys.path.insert` header of `tools/make_fixture.py:18`. It must:

- assert `"httpx.AsyncClient("` does not appear in `inspect.getsource(downloader)` nor in
  `inspect.getsource(scraper)`, so no request there builds its own client;
- assert `client` is a parameter of `download_pdf_direct`, `download_via_unpaywall`,
  `classify_url` and `scrape_landing_page`;
- assert `user_email` is still a parameter of `download_via_unpaywall`, since Unpaywall takes
  it as a query argument rather than a User-Agent;
- assert `inspect.getsource(paper_detail).count("httpx.AsyncClient(")` is exactly 2, one per
  UI lookup;
- `py_compile.compile` all three files with `doraise=True`;
- print `clients OK`.

---

## Phase 5: `paperbase/core/importer.py`, the five import paths

skill: coding-standards:python
model: sonnet

**Intent:** switch every import path to the injected client and the `PdfText` API, claim DOIs
against concurrent items, and stop doing categorisation and indexing inline. The last of those
moves to a batched flush in Phase 7.

**This phase leaves the module referencing `self._buffer`, `self._is_known`, `self._claim_paper`,
`read_pdf`, `PdfText` and `httpx`, which Phase 7 adds.** The module still compiles; it will not
run correctly until Phase 7 lands. That is expected, and this phase's `done-when:` checks
compilation and structure, not behaviour. Do not add the missing pieces here.

**The dedupe contract these five paths implement**, defined once here so each edit below reads
as an instance of it rather than as five separate decisions:

- `self._is_known(kind, value)` is a **cheap early check against the database only**, used to
  catch a duplicate at the earliest point it can be recognised. It claims nothing and is not
  authoritative: another in-flight item may be about to insert the same key.
- **Every path calls it on the content hash as soon as the file exists locally**, which for
  mode 1 is before any network call and for modes 2 and 3 is immediately after the download.
  This is uniform across all five paths on purpose. It costs one indexed lookup on the common
  non-duplicate case, and that is the trade the priority above resolves.
- `self._claim_paper(paper, pdf)` is the **single authority**. It runs immediately before
  `place_file`, sets `paper.content_hash`, and checks and claims the content hash, the DOI and
  the ISBN together. It returns `False` if any of them is already held by the library or by
  another item in this run.
- Because there is no `await` between `_claim_paper` and `insert_paper`, the event loop cannot
  interleave another item inside that window. That is what makes the guarantee hold with four
  items in flight, and it is why **no `await` may ever be added between them**.
- When `_claim_paper` returns `False` on a path that downloaded to `tmp`, the temporary file
  is unlinked before returning. The item counts as a duplicate, not a failure.

**The guard goes into all five paths.** Mode 1's appears inside its whole-method replacement in
Edit 5a. The other four are inserted immediately **above** their existing `place_file(...)`
line, which is otherwise unchanged. Each of the four reads:

In `_import_doi`, above `place_file(result.tmp_path, paper, …)`:

```python
        if not self._claim_paper(paper, pdf):
            result.tmp_path.unlink(missing_ok=True)  # type: ignore[union-attr]
            self.log_message.emit(f"Skipped (already in library): {doi}")
            self.item_finished.emit(doi, True, False)
            return True, False, True
```

In `_import_direct_pdf_url`, above `place_file(tmp, paper, …)`:

```python
        if not self._claim_paper(paper, pdf):
            tmp.unlink(missing_ok=True)  # type: ignore[union-attr]
            self.log_message.emit(f"Skipped (already in library): {url}")
            self.item_finished.emit(url, True, False)
            return True, False, True
```

In `_import_landing_page`, above **each** of its two `place_file(...)` lines, at that block's
indentation (sixteen spaces), with `tmp` in the first block and `dl.tmp_path` in the second:

```python
                if not self._claim_paper(paper, pdf):
                    tmp.unlink(missing_ok=True)  # type: ignore[union-attr]
                    self.log_message.emit(f"Skipped (already in library): {url}")
                    self.item_finished.emit(url, True, False)
                    return True, False, True
```

Every one of them returns `(True, False, True)`, which is the tuple `_process_item` counts as
a duplicate rather than a failure.

**Anchor:** `paperbase/core/importer.py`, five edits, **bottom-up in the order given**.

**Edit 5e: `importer.py:439-447`, method `_append_state`.** Current state, verbatim:

```python
    def _append_state(self, item: str) -> None:
        """Append one processed item. O(1) per call, unlike a whole-set rewrite."""
        if not self._state_file:
            return
        try:
            with self._state_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(item, ensure_ascii=False) + "\n")
        except OSError as e:
            logger.warning("Failed to append import state: %s", e)
```

Change to:

```python
    def _append_state(self, items: list[str]) -> None:
        """Append a flush's worth of processed items in one open. Never rewrites the set."""
        if not self._state_file or not items:
            return
        try:
            with self._state_file.open("a", encoding="utf-8") as fh:
                fh.writelines(json.dumps(i, ensure_ascii=False) + "\n" for i in items)
        except OSError as e:
            logger.warning("Failed to append import state: %s", e)
```

**Edit 5d: `importer.py:332-402`, method `_import_landing_page`.** Signature, current state,
verbatim:

```python
    async def _import_landing_page(self, url: str, rl: RateLimiter) -> tuple[bool, bool, bool]:
        try:
            scrape: ScrapeResult = await scrape_landing_page(url)
        except ValueError as e:
            if str(e) == "direct_pdf":
                return await self._import_direct_pdf_url(url, rl)
```

Change to:

```python
    async def _import_landing_page(
        self, url: str, rl: RateLimiter, client: httpx.AsyncClient
    ) -> tuple[bool, bool, bool]:
        try:
            scrape: ScrapeResult = await scrape_landing_page(url, client)
        except ValueError as e:
            if str(e) == "direct_pdf":
                return await self._import_direct_pdf_url(url, rl, client)
```

Current state, verbatim:

```python
        if scrape.doi and self._db.paper_exists_by_doi(scrape.doi):
```

Change to:

```python
        if scrape.doi and self._is_known("doi", scrape.doi):
```

First success block, current state, verbatim:

```python
        if scrape.pdf_url:
            dl = await download_pdf_direct(scrape.pdf_url, scrape.doi, self._tmp_dir)
            if dl.success:
                tmp = dl.tmp_path
                doi = scrape.doi or extract_doi_from_pdf(tmp)  # type: ignore[arg-type]
                if doi and self._db.paper_exists_by_doi(doi):
```

Change to:

```python
        if scrape.pdf_url:
            dl = await download_pdf_direct(scrape.pdf_url, scrape.doi, self._tmp_dir, client)
            if dl.success:
                tmp = dl.tmp_path
                pdf = await asyncio.to_thread(read_pdf, tmp)  # type: ignore[arg-type]
                if pdf.sha256 and self._is_known("hash", pdf.sha256):
                    tmp.unlink(missing_ok=True)  # type: ignore[union-attr]
                    self.log_message.emit(
                        f"Skipped (identical file already in library): {url}"
                    )
                    self.item_finished.emit(url, True, False)
                    return True, False, True
                doi = scrape.doi or extract_doi(pdf)
                if doi and self._is_known("doi", doi):
```

Both checks are `_is_known`, so re-checking a DOI that came from the scrape costs one indexed
lookup and cannot make the item skip itself. Claiming happens once, in `_claim_paper`.

Still in the first block, current state, verbatim:

```python
                if doi:
                    paper = await resolve_metadata(doi, self._user_email, rl)
                else:
                    paper = None
                if paper is None:
                    paper = scrape.metadata or await guess_metadata_from_text(tmp, self._user_email, rl)  # type: ignore[arg-type]
```

Change to:

```python
                if doi:
                    paper = await resolve_metadata(doi, self._user_email, rl, client)
                else:
                    paper = None
                if paper is None:
                    paper = scrape.metadata or await guess_metadata(tmp, pdf, self._user_email, rl, client)  # type: ignore[arg-type]
```

Still in the first block, current state, verbatim:

```python
                fulltext = extract_fulltext(Path(paper.file_path))
                paper_id = self._db.insert_paper(paper)
                paper.id = paper_id
                self._apply_categorisation(paper)
                self._indexer.add_document(paper, fulltext)
                self.item_finished.emit(url, True, paper.needs_review)
                return True, paper.needs_review, False
```

Change to:

```python
                paper_id = self._db.insert_paper(paper)
                paper.id = paper_id
                self._buffer(paper, pdf.fulltext)
                self.item_finished.emit(url, True, paper.needs_review)
                return True, paper.needs_review, False
```

Second success block (the Unpaywall fallback), current state, verbatim:

```python
        if scrape.doi:
            dl = await download_via_unpaywall(scrape.doi, self._user_email, self._tmp_dir, rl)
            if dl.success:
                paper = await resolve_metadata(scrape.doi, self._user_email, rl)
                if paper is None:
                    paper = await guess_metadata_from_text(dl.tmp_path, self._user_email, rl)  # type: ignore[arg-type]
                paper.open_access = True
```

Change to:

```python
        if scrape.doi:
            dl = await download_via_unpaywall(
                scrape.doi, self._user_email, self._tmp_dir, rl, client
            )
            if dl.success:
                pdf = await asyncio.to_thread(read_pdf, dl.tmp_path)  # type: ignore[arg-type]
                if pdf.sha256 and self._is_known("hash", pdf.sha256):
                    dl.tmp_path.unlink(missing_ok=True)  # type: ignore[union-attr]
                    self.log_message.emit(
                        f"Skipped (identical file already in library): {url}"
                    )
                    self.item_finished.emit(url, True, False)
                    return True, False, True
                paper = await resolve_metadata(scrape.doi, self._user_email, rl, client)
                if paper is None:
                    paper = await guess_metadata(dl.tmp_path, pdf, self._user_email, rl, client)  # type: ignore[arg-type]
                paper.open_access = True
```

Second block, current state, verbatim:

```python
                fulltext = extract_fulltext(Path(paper.file_path))
                paper_id = self._db.insert_paper(paper)
                paper.id = paper_id
                self._apply_categorisation(paper)
                self._indexer.add_document(paper, fulltext)
                self.item_finished.emit(url, True, paper.needs_review)
                return True, paper.needs_review, False
```

Change to:

```python
                paper_id = self._db.insert_paper(paper)
                paper.id = paper_id
                self._buffer(paper, pdf.fulltext)
                self.item_finished.emit(url, True, paper.needs_review)
                return True, paper.needs_review, False
```

Reading before `place_file` is deliberate: `place_file` moves a tmp download, so the old
`extract_fulltext(Path(paper.file_path))` had to reopen the file at its new home.

**Edit 5c: `importer.py:294-330`, method `_import_direct_pdf_url`.** Signature and the
download, current state, verbatim:

```python
    async def _import_direct_pdf_url(self, url: str, rl: RateLimiter) -> tuple[bool, bool, bool]:
        result = await download_pdf_direct(url, None, self._tmp_dir)
```

Change to:

```python
    async def _import_direct_pdf_url(
        self, url: str, rl: RateLimiter, client: httpx.AsyncClient
    ) -> tuple[bool, bool, bool]:
        result = await download_pdf_direct(url, None, self._tmp_dir, client)
```

Current state, verbatim:

```python
        tmp = result.tmp_path
        doi = extract_doi_from_pdf(tmp)  # type: ignore[arg-type]

        if doi and self._db.paper_exists_by_doi(doi):
```

Change to:

```python
        tmp = result.tmp_path
        pdf = await asyncio.to_thread(read_pdf, tmp)  # type: ignore[arg-type]
        if pdf.sha256 and self._is_known("hash", pdf.sha256):
            tmp.unlink(missing_ok=True)  # type: ignore[union-attr]
            self.log_message.emit(f"Skipped (identical file already in library): {url}")
            self.item_finished.emit(url, True, False)
            return True, False, True
        doi = extract_doi(pdf)

        if doi and self._is_known("doi", doi):
```

Current state, verbatim:

```python
        paper = None
        if doi:
            paper = await resolve_metadata(doi, self._user_email, rl)
        if paper is None:
            isbn = extract_isbn_from_pdf(tmp)  # type: ignore[arg-type]
            if isbn:
                paper = await resolve_book_metadata(isbn, self._user_email, rl)
        if paper is None:
            paper = await guess_metadata_from_text(tmp, self._user_email, rl)  # type: ignore[arg-type]
```

Change to:

```python
        paper = None
        if doi:
            paper = await resolve_metadata(doi, self._user_email, rl, client)
        if paper is None:
            isbn = extract_isbn(pdf)
            if isbn:
                paper = await resolve_book_metadata(isbn, rl, client)
        if paper is None:
            paper = await guess_metadata(tmp, pdf, self._user_email, rl, client)  # type: ignore[arg-type]
```

Current state, verbatim:

```python
        fulltext = extract_fulltext(Path(paper.file_path))
        paper_id = self._db.insert_paper(paper)
        paper.id = paper_id
        self._apply_categorisation(paper)
        self._indexer.add_document(paper, fulltext)

        self.item_finished.emit(url, True, paper.needs_review)
```

Change to:

```python
        paper_id = self._db.insert_paper(paper)
        paper.id = paper_id
        self._buffer(paper, pdf.fulltext)

        self.item_finished.emit(url, True, paper.needs_review)
```

**Edit 5b: `importer.py:253-292`, methods `_import_doi` and `_import_url`.** `_import_doi`,
current state, verbatim:

```python
    async def _import_doi(self, doi: str, rl: RateLimiter) -> tuple[bool, bool, bool]:
        doi = doi.strip()
        if self._db.paper_exists_by_doi(doi):
            self.item_finished.emit(doi, True, False)
            return True, False, True

        from paperbase.core.downloader import download_via_unpaywall
        result = await download_via_unpaywall(doi, self._user_email, self._tmp_dir, rl)
        if not result.success:
            self.item_failed.emit(doi, result.reason)
            return False, False, False

        paper = await resolve_metadata(doi, self._user_email, rl)
        if paper is None:
            paper = await guess_metadata_from_text(result.tmp_path, self._user_email, rl)  # type: ignore[arg-type]
        paper.open_access = True
```

Change to:

```python
    async def _import_doi(
        self, doi: str, rl: RateLimiter, client: httpx.AsyncClient
    ) -> tuple[bool, bool, bool]:
        doi = doi.strip()
        if self._is_known("doi", doi):
            self.item_finished.emit(doi, True, False)
            return True, False, True

        result = await download_via_unpaywall(doi, self._user_email, self._tmp_dir, rl, client)
        if not result.success:
            self.item_failed.emit(doi, result.reason)
            return False, False, False

        pdf = await asyncio.to_thread(read_pdf, result.tmp_path)  # type: ignore[arg-type]
        if pdf.sha256 and self._is_known("hash", pdf.sha256):
            result.tmp_path.unlink(missing_ok=True)  # type: ignore[union-attr]
            self.log_message.emit(f"Skipped (identical file already in library): {doi}")
            self.item_finished.emit(doi, True, False)
            return True, False, True
        paper = await resolve_metadata(doi, self._user_email, rl, client)
        if paper is None:
            paper = await guess_metadata(result.tmp_path, pdf, self._user_email, rl, client)  # type: ignore[arg-type]
        paper.open_access = True
```

The function-local `from paperbase.core.downloader import download_via_unpaywall` goes: the
name is already imported at module level (`importer.py:24`), so the local import shadowed it
for no reason.

`_import_doi` continues, current state, verbatim:

```python
        fulltext = extract_fulltext(Path(paper.file_path))
        paper_id = self._db.insert_paper(paper)
        paper.id = paper_id
        self._apply_categorisation(paper)
        self._indexer.add_document(paper, fulltext)

        self.item_finished.emit(doi, True, paper.needs_review)
```

Change to:

```python
        paper_id = self._db.insert_paper(paper)
        paper.id = paper_id
        self._buffer(paper, pdf.fulltext)

        self.item_finished.emit(doi, True, paper.needs_review)
```

`_import_url`, current state, verbatim:

```python
    async def _import_url(self, url: str, rl: RateLimiter) -> tuple[bool, bool, bool]:
        url_type = await classify_url(url)

        if url_type == "pdf":
            return await self._import_direct_pdf_url(url, rl)
        else:
            return await self._import_landing_page(url, rl)
```

Change to:

```python
    async def _import_url(
        self, url: str, rl: RateLimiter, client: httpx.AsyncClient
    ) -> tuple[bool, bool, bool]:
        url_type = await classify_url(url, client)

        if url_type == "pdf":
            return await self._import_direct_pdf_url(url, rl, client)
        else:
            return await self._import_landing_page(url, rl, client)
```

**Edit 5a: `importer.py:213-247`, method `_import_pdf`.** Replace the **whole method**. Current
state, verbatim:

```python
    async def _import_pdf(self, path: Path, rl: RateLimiter) -> tuple[bool, bool, bool]:
        if self._db.paper_exists_by_path(str(path)):
            self.item_finished.emit(str(path), True, False)
            return True, False, True

        doi = extract_doi_from_pdf(path)
        if doi and self._db.paper_exists_by_doi(doi):
            self.log_message.emit(f"Skipped (already in library, DOI {doi}): {path.name}")
            self.item_finished.emit(str(path), True, False)
            return True, False, True

        paper = None
        if doi:
            paper = await resolve_metadata(doi, self._user_email, rl)

        # No DOI or Crossref returned nothing — try ISBN (book)
        if paper is None:
            isbn = extract_isbn_from_pdf(path)
            if isbn:
                paper = await resolve_book_metadata(isbn, self._user_email, rl)

        if paper is None:
            paper = await guess_metadata_from_text(path, self._user_email, rl)

        place_file(path, paper, self._library_root, move=False, folder_pattern=self._folder_pattern)
        if self._secondary_dest:
            copy_to_secondary(Path(paper.file_path), self._library_root, self._secondary_dest)
        fulltext = extract_fulltext(path)
        paper_id = self._db.insert_paper(paper)
        paper.id = paper_id
        self._apply_categorisation(paper)
        self._indexer.add_document(paper, fulltext)

        self.item_finished.emit(str(path), True, paper.needs_review)
        return True, paper.needs_review, False
```

Change to:

```python
    async def _import_pdf(
        self, path: Path, rl: RateLimiter, client: httpx.AsyncClient, pdf: PdfText
    ) -> tuple[bool, bool, bool]:
        """`pdf` is read by the caller, off the event loop, before this is entered."""
        # The content hash is checked first and costs nothing extra: reading the file to
        # extract its text already computed it. This is the check that replaces the
        # paper_exists_by_path call that never matched, because place_file(move=False)
        # stores the destination path while the check compared the source.
        if pdf.sha256 and self._is_known("hash", pdf.sha256):
            self.log_message.emit(f"Skipped (identical file already in library): {path.name}")
            self.item_finished.emit(str(path), True, False)
            return True, False, True

        doi = extract_doi(pdf)
        if doi and self._is_known("doi", doi):
            self.log_message.emit(f"Skipped (already in library, DOI {doi}): {path.name}")
            self.item_finished.emit(str(path), True, False)
            return True, False, True

        paper = None
        if doi:
            paper = await resolve_metadata(doi, self._user_email, rl, client)

        # No DOI or Crossref returned nothing — try ISBN (book)
        if paper is None:
            isbn = extract_isbn(pdf)
            if isbn:
                if self._is_known("isbn", isbn):
                    self.log_message.emit(
                        f"Skipped (already in library, ISBN {isbn}): {path.name}"
                    )
                    self.item_finished.emit(str(path), True, False)
                    return True, False, True
                paper = await resolve_book_metadata(isbn, rl, client)

        if paper is None:
            paper = await guess_metadata(path, pdf, self._user_email, rl, client)

        if not self._claim_paper(paper, pdf):
            self.log_message.emit(f"Skipped (already in library): {path.name}")
            self.item_finished.emit(str(path), True, False)
            return True, False, True

        # No await from here to _buffer: the event loop cannot interleave another item
        # between the claim, the destination name and the inserted row.
        place_file(path, paper, self._library_root, move=False, folder_pattern=self._folder_pattern)
        if self._secondary_dest:
            copy_to_secondary(Path(paper.file_path), self._library_root, self._secondary_dest)
        paper_id = self._db.insert_paper(paper)
        paper.id = paper_id
        self._buffer(paper, pdf.fulltext)

        self.item_finished.emit(str(path), True, paper.needs_review)
        return True, paper.needs_review, False
```

done-when: `py -3.12 tools/check_phase5.py` prints `phase5 OK` and exits 0. Create that file in
this phase; it is a structural check on the source text, since the module cannot run until
Phase 7. It must `py_compile.compile("paperbase/core/importer.py", doraise=True)`, read the
file, and assert:

- none of `extract_fulltext`, `extract_doi_from_pdf`, `extract_isbn_from_pdf`,
  `guess_metadata_from_text` appears anywhere in it;
- `paper_exists_by_path` no longer appears: the check that never matched is gone, not merely
  bypassed;
- `self._buffer(paper, pdf.fulltext)` appears exactly 5 times;
- `self._apply_categorisation(paper)` does not appear;
- `self._claim_paper(paper, pdf)` appears exactly 5 times, once per path;
- `asyncio.to_thread(read_pdf` appears exactly 4 times (mode 1 reads in `_process_item`);
- `self._is_known("hash"` appears exactly 5 times, once per path: every path checks the hash
  the moment the file is local;
- `self._is_known(` appears at least 10 times in total;
- **no `await` sits between a `_claim_paper` call and its `insert_paper`.** Check this by
  slicing the source between each `self._claim_paper(` occurrence and the next
  `self._db.insert_paper(` after it, and asserting `"await "` is absent from that slice. This
  is the invariant the whole concurrency guarantee rests on, so it is worth a real assertion
  rather than a comment;
- print `phase5 OK`.

---

## Phase 6: `paperbase/core/categoriser.py`, embed a batch in one model pass

skill: coding-standards:python
model: sonnet

**Intent:** `categoriser.py:110` calls `model.encode(text)` once per paper. On CPU the per-call
overhead and the single-row GEMM dominate a short document, so one `encode` over 64 texts is
several times faster than 64 calls. KeyBERT is the same story and is the larger half of the
cost, since it embeds candidate n-grams too. `_get_or_create_collection` additionally runs a
full `SELECT * FROM collections` per matched category per paper; a batch resolves each distinct
name once.

**Anchor:** `paperbase/core/categoriser.py`, five edits, **bottom-up in the order given**.

**Edit 6e: `categoriser.py:206-243`, the loop body inside `CategorizationWorker.run`.** Current
state, verbatim:

```python
        for paper_id in all_ids:
            if self._stop_requested:
                self.log_message.emit("Stopped by user.")
                break

            while self._pause_requested:
                time.sleep(0.2)

            if paper_id in processed:
                done += 1
                self.progress.emit(done, total)
                continue

            paper = self._db.get_paper(paper_id)
            if paper is None:
                processed.add(paper_id)
                done += 1
                continue

            try:
                col_ids, tags = self._categoriser.categorise_paper(paper, self._db)
                new_col_ids = list(set(paper.collection_ids) | set(col_ids))
                new_tags = list(set(paper.tags) | set(tags))

                if new_col_ids != paper.collection_ids or new_tags != paper.tags:
                    paper.collection_ids = new_col_ids
                    paper.tags = new_tags
                    self._db.update_paper(paper)
            except Exception as e:
                logger.warning("Categorisation failed for paper %s: %s", paper_id, e)

            processed.add(paper_id)
            done += 1
            self.progress.emit(done, total)

            if done % _STATE_SAVE_INTERVAL == 0:
                self._save_state(processed)
                self.log_message.emit(f"Progress: {done:,} / {total:,}")
```

Change to:

```python
        pending = [pid for pid in all_ids if pid not in processed]
        done = total - len(pending)
        self.progress.emit(done, total)

        for start in range(0, len(pending), _BATCH_SIZE):
            if self._stop_requested:
                self.log_message.emit("Stopped by user.")
                break

            while self._pause_requested:
                time.sleep(0.2)

            chunk = pending[start : start + _BATCH_SIZE]
            papers = self._db.get_papers_by_ids(chunk)

            try:
                results = self._categoriser.categorise_papers(papers, self._db)
                for paper, (col_ids, tags) in zip(papers, results):
                    new_col_ids = sorted(set(paper.collection_ids) | set(col_ids))
                    new_tags = sorted(set(paper.tags) | set(tags))
                    if new_col_ids != paper.collection_ids or new_tags != paper.tags:
                        paper.collection_ids = new_col_ids
                        paper.tags = new_tags
                        self._db.update_paper(paper)
            except Exception as e:
                logger.warning("Categorisation failed for a batch of %d: %s", len(chunk), e)

            processed.update(chunk)
            done += len(chunk)
            self.progress.emit(done, total)

            if done % _STATE_SAVE_INTERVAL < _BATCH_SIZE:
                self._save_state(processed)
                self.log_message.emit(f"Progress: {done:,} / {total:,}")
```

`get_papers_by_ids` drops ids that no longer exist and preserves order (`db.py:281`), which is
why the deleted-paper branch is gone: a missing id simply does not come back, and `chunk` is
still what gets marked processed.

**Edit 6d: `categoriser.py:143-151`, function `_get_or_create_collection`.** Current state,
verbatim:

```python
def _get_or_create_collection(name: str, db: Database) -> Optional[int]:
    for col in db.get_collections():
        if col.name == name and col.parent_id is None:
            return col.id
    try:
        return db.insert_collection(Collection(id=None, name=name, parent_id=None))
    except Exception as e:
        logger.error("Failed to create collection '%s': %s", name, e)
        return None
```

Change to:

```python
def _resolve_collections(names: set[str], db: Database) -> dict[str, int]:
    """Map top-level collection names to ids, creating any that are missing.

    One `get_collections()` for the whole batch: the per-paper version read the entire
    collections table once per matched category.
    """
    existing = {c.name: c.id for c in db.get_collections() if c.parent_id is None}
    resolved: dict[str, int] = {}
    for name in names:
        if name in existing:
            resolved[name] = existing[name]  # type: ignore[assignment]
            continue
        try:
            resolved[name] = db.insert_collection(Collection(id=None, name=name, parent_id=None))
        except Exception as e:
            logger.error("Failed to create collection '%s': %s", name, e)
    return resolved
```

**Edit 6c: `categoriser.py:94-140`, method `categorise_paper`.** Replace the **whole method**,
from `def categorise_paper` down to and including its final `return col_ids, tags`, with:

```python
    def categorise_paper(self, paper: Paper, db: Database) -> tuple[list[int], list[str]]:
        """Single-paper wrapper. Prefer categorise_papers: the model call costs nearly the
        same for one document as for a batch of 64."""
        return self.categorise_papers([paper], db)[0]

    def categorise_papers(
        self, papers: list[Paper], db: Database
    ) -> list[tuple[list[int], list[str]]]:
        """Categorise a batch in one model pass.

        Returns one (collection_ids, tags) pair per input paper, positionally, to merge onto
        that paper. Creates any missing top-level collections. Empty pairs throughout if the
        model is not loaded or no categories are configured.
        """
        empty: list[tuple[list[int], list[str]]] = [([], []) for _ in papers]
        if self._model is None or not self._categories or not papers:
            return empty

        import numpy as np

        texts = [f"{p.title} {p.abstract}".strip() for p in papers]
        live = [i for i, t in enumerate(texts) if t]
        if not live:
            return empty

        with self._lock:
            names = list(self._category_embeddings.keys())
            if not names:
                return empty
            cat_matrix = np.vstack([self._category_embeddings[n] for n in names])

            doc_embs = self._model.encode(
                [texts[i] for i in live],
                batch_size=_ENCODE_BATCH,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
            sims = np.asarray(doc_embs) @ cat_matrix.T          # (live, categories)
            hits = sims >= self._threshold

            matched: dict[int, list[str]] = {}
            for row, paper_idx in enumerate(live):
                matched[paper_idx] = [n for n, hit in zip(names, hits[row]) if hit]

            tags_by_idx: dict[int, list[str]] = {}
            abstract_idx = [i for i in live if papers[i].abstract]
            if self._kw_model and abstract_idx:
                try:
                    raw = self._kw_model.extract_keywords(
                        [papers[i].abstract for i in abstract_idx],
                        keyphrase_ngram_range=(1, 2),
                        stop_words="english",
                        use_mmr=True,
                        diversity=0.5,
                        top_n=self._tag_count,
                    )
                    # KeyBERT returns a flat list for a single document and a list of lists
                    # for several; normalise before zipping.
                    if raw and isinstance(raw[0], tuple):
                        raw = [raw]
                    for paper_idx, kws in zip(abstract_idx, raw):
                        tags_by_idx[paper_idx] = [kw for kw, _score in kws]
                except Exception as e:
                    logger.warning("Keyword extraction failed for a batch of %d: %s",
                                   len(abstract_idx), e)

        # DB work outside the lock, and once for the whole batch rather than once per paper.
        wanted = {n for ns in matched.values() for n in ns}
        col_by_name = _resolve_collections(wanted, db)

        results: list[tuple[list[int], list[str]]] = []
        for i in range(len(papers)):
            col_ids = [col_by_name[n] for n in matched.get(i, []) if n in col_by_name]
            results.append((col_ids, tags_by_idx.get(i, [])))
        return results
```

**Edit 6b: `categoriser.py:57-67`, inside `load_model`.** Current state, verbatim:

```python
            try:
                from sentence_transformers import SentenceTransformer
                from keybert import KeyBERT
            except ImportError:
                logger.error(
                    "sentence-transformers and keybert are required for auto-categorisation. "
                    "Run: pip install sentence-transformers keybert"
                )
                return
            self._model = SentenceTransformer(self.MODEL_NAME)
            self._kw_model = KeyBERT(model=self._model)
```

Change to:

```python
            try:
                model, kw_model = _load_models(self.MODEL_NAME)
            except ImportError:
                logger.error(
                    "sentence-transformers and keybert are required for auto-categorisation. "
                    "Run: pip install sentence-transformers keybert"
                )
                return
            self._model = model
            self._kw_model = kw_model
```

**Edit 6a: `categoriser.py:28`, the module constant.** Current state, verbatim:

```python
_STATE_SAVE_INTERVAL = 200
```

Change to:

```python
_STATE_SAVE_INTERVAL = 200
_BATCH_SIZE = 64          # papers per model pass in the retroactive worker
_ENCODE_BATCH = 64        # sentence-transformers internal batch size


def _load_models(name: str):
    """Import and construct the embedding and keyword models.

    A module-level factory so tests can substitute a fake: neither library is a hard
    dependency of the rest of the application, and neither is installed on every machine.
    """
    from sentence_transformers import SentenceTransformer
    from keybert import KeyBERT

    model = SentenceTransformer(name)
    return model, KeyBERT(model=model)
```

`paperbase/core/llm.py:21` calls `categorise_paper`, which survives as the wrapper, so that
dead module keeps importing. Do not touch `llm.py`.

done-when: `py -3.12 tools/check_batch_encode.py` prints `batch encode OK` and exits 0. Create
that file in this phase. With no real model installed it must:

- define a fake model whose `encode(texts, **kw)` records `len(texts)` per call into a list and
  returns `numpy.ones((len(texts), 4)) / 2.0`, accepting a single string too and returning
  shape `(4,)` for it, since `_recompute_embeddings` calls it that way;
- define a fake KeyBERT whose `extract_keywords(docs, **kw)` returns
  `[[("alpha", 0.9), ("beta", 0.8)] for _ in docs]`;
- monkeypatch `paperbase.core.categoriser._load_models` to return that pair;
- open a temp `Database`, build an `EmbeddingCategoriser`, call `update_settings` with two
  categories and `threshold=0.1`, then `load_model()`;
- call `categorise_papers` with 64 `Paper` objects carrying titles and abstracts;
- assert 64 pairs come back, that every pair has non-empty tags, and that the fake recorded
  **exactly one** `encode` call of length 64 after the two category-description calls;
- assert `categorise_paper(papers[0], db)` returns the same shape as
  `categorise_papers([papers[0]], db)[0]`;
- print `batch encode OK`.

---

## Phase 7: `paperbase/core/importer.py`, concurrent items and a batched flush

skill: coding-standards:python
model: sonnet

**Intent:** the run loop becomes four workers pulling from a queue against one shared
`AsyncClient`, which is where the 125 s of a 200-item run collapses to about 11 s.
Categorisation, indexing and the state write collapse into one flush per 50 papers, which is
what lets the embedding be batched. The state write moves after the index commit, closing a
pre-existing hole where a crash left papers in the database, marked processed, and absent from
the index forever.

**Anchor:** `paperbase/core/importer.py`, four edits, **bottom-up in the order given**. Line
numbers are as the file stands **after Phase 5**, which shortened five methods; locate each
edit by its quoted text and enclosing symbol, not by counting lines.

**Edit 7d: method `_run_async` (originally `importer.py:121-207`).** Replace the **whole
method**, from `async def _run_async(self) -> None:` down to and including the closing
parenthesis of the final `self.log_message.emit(...)` block. The replacement, which also adds
the three methods that follow it:

```python
    async def _run_async(self) -> None:
        rate_limiter = RateLimiter()
        processed = self._load_state()
        if processed:
            # Collapse a legacy whole-set state file into the append-only format once.
            self._rewrite_state(processed)

        # Identical entries in one list are dropped before anything looks at them. Without
        # this, four workers can pull the same path or DOI concurrently and race each other
        # to the claim, which wastes a download to discover what the list already said.
        # dict.fromkeys preserves the user's ordering; a set would not.
        self._items = list(dict.fromkeys(self._items))

        self._total = len(self._items)
        # Invariant held from here on: done == imported + dupes + failed + skipped.
        self._done = 0
        self._skipped = 0
        self._imported = 0
        self._review_count = 0
        self._failed = 0
        self._dupes = 0
        self._worked = 0
        self._start_time = time.monotonic()

        # Resume skips are settled before any worker starts, so the queue holds only real work.
        queue: asyncio.Queue[str] = asyncio.Queue()
        for item in self._items:
            if item in processed:
                self._skipped += 1
                self._done += 1
            else:
                queue.put_nowait(item)
        self._emit_progress()

        # One client for the whole run. A fresh AsyncClient per request pays a TLS handshake
        # every time: measured against Crossref, 627ms per DOI against 152ms reused.
        limits = httpx.Limits(
            max_connections=_MAX_CONCURRENT_ITEMS * 2,
            max_keepalive_connections=_MAX_CONCURRENT_ITEMS * 2,
        )
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=30.0, limits=limits
        ) as client:
            workers = [
                asyncio.create_task(self._worker(queue, client, rate_limiter))
                for _ in range(_MAX_CONCURRENT_ITEMS)
            ]
            try:
                await asyncio.gather(*workers)
            finally:
                self._flush_pending()

        if self._skipped:
            self.log_message.emit(
                f"Skipped {self._skipped} already-processed item(s) from a previous run."
            )

    async def _worker(
        self, queue: "asyncio.Queue[str]", client: httpx.AsyncClient, rl: RateLimiter
    ) -> None:
        """One of _MAX_CONCURRENT_ITEMS pullers. Items overlap; one item's own steps do not."""
        while True:
            if self._stop_requested:
                return

            while self._pause_requested:
                await asyncio.sleep(0.5)
                if self._stop_requested:
                    return

            try:
                item = queue.get_nowait()
            except asyncio.QueueEmpty:
                return

            await self._process_item(item, client, rl)

    async def _process_item(
        self, item: str, client: httpx.AsyncClient, rl: RateLimiter
    ) -> None:
        self.item_started.emit(item)

        try:
            if self._mode == "pdfs":
                path = Path(item)
                # Off the event loop: PyMuPDF releases the GIL, so the other in-flight
                # items keep their network calls moving during the read.
                pdf = await asyncio.to_thread(read_pdf, path)
                ok, nr, is_dupe = await self._import_pdf(path, rl, client, pdf)
            elif self._mode == "dois":
                ok, nr, is_dupe = await self._import_doi(item, rl, client)
            else:  # urls
                ok, nr, is_dupe = await self._import_url(item, rl, client)
        except Exception as e:
            logger.exception("Unexpected error importing %s", item)
            self.item_failed.emit(item, str(e))
            ok, nr, is_dupe = False, False, False

        if ok and is_dupe:
            self._dupes += 1
        elif ok:
            self._imported += 1
            if nr:
                self._review_count += 1
        else:
            self._failed += 1

        self._pending_state.append(item)
        self._done += 1
        self._worked += 1

        if (len(self._pending) >= INDEX_COMMIT_INTERVAL
                or self._pending_bytes >= _PENDING_TEXT_LIMIT):
            self._flush_pending()

        self._emit_progress()
        self.log_message.emit(
            f"[{self._done}/{self._total}] "
            f"{Path(item).name if self._mode == 'pdfs' else item}{self._eta()}"
        )

    def _emit_progress(self) -> None:
        self.progress.emit(self._done, self._total, self._imported,
                           self._review_count, self._failed, self._dupes)

    def _eta(self) -> str:
        """Rate over items this run did real work for; items skipped on resume complete
        instantly and would otherwise collapse the estimate."""
        if self._worked <= 0:
            return ""
        avg = (time.monotonic() - self._start_time) / self._worked
        remaining = avg * max(self._total - self._done, 0)
        if remaining <= 0:
            return ""
        m, s = divmod(int(remaining), 60)
        return f" | ETA {m}m {s}s"
```

The `pending_index` counter is gone; `len(self._pending)` is the same number and cannot drift
from the buffer it describes. Log lines from different items now interleave, which is expected.

**Edit 7c: method `_apply_categorisation` (originally `importer.py:85-98`).** Replace the
**whole method**. Current state, verbatim:

```python
    def _apply_categorisation(self, paper: Paper) -> None:
        """Merge auto-categorisation results onto paper and update DB. No-op if not configured."""
        if self._categoriser is None or not self._categoriser.is_loaded:
            return
        try:
            col_ids, tags = self._categoriser.categorise_paper(paper, self._db)
            new_col_ids = list(set(paper.collection_ids) | set(col_ids))
            new_tags = list(set(paper.tags) | set(tags))
            if new_col_ids != paper.collection_ids or new_tags != paper.tags:
                paper.collection_ids = new_col_ids
                paper.tags = new_tags
                self._db.update_paper(paper)
        except Exception as e:
            logger.warning("Auto-categorisation failed for paper %s: %s", paper.id, e)
```

Change to:

```python
    def _is_known(self, kind: str, value: Optional[str]) -> bool:
        """Cheap pre-check against the library only. Not authoritative.

        Saves a Crossref round trip or a download for something obviously already held.
        Another in-flight item may be about to insert the same key, which is why
        `_claim_paper` and not this is what decides.
        """
        if not value:
            return False
        if kind == "doi":
            return self._db.paper_exists_by_doi(value)
        if kind == "isbn":
            return self._db.paper_exists_by_isbn(value)
        if kind == "hash":
            return self._db.paper_exists_by_hash(value)
        raise ValueError(f"Unknown dedupe key: {kind}")

    def _claim_paper(self, paper: Paper, pdf: PdfText) -> bool:
        """The one authority on whether this paper is a duplicate. Claims its keys if not.

        Called immediately before place_file, with no await between here and insert_paper, so
        the event loop cannot interleave another item inside the window. That is what makes
        the guarantee hold with four items in flight; the database checks alone cannot,
        because the gap between a check and its insert spans an await.

        Keys are checked hash first (definitive: the same bytes are the same file), then DOI
        and ISBN (the same paper arriving as a different file). The claimed sets are bounded
        by the run's item count and die with the worker.
        """
        paper.content_hash = pdf.sha256 or None
        for kind, value in (("hash", pdf.sha256), ("doi", paper.doi), ("isbn", paper.isbn)):
            if not value:
                continue
            if value in self._claimed[kind] or self._is_known(kind, value):
                return False
        for kind, value in (("hash", pdf.sha256), ("doi", paper.doi), ("isbn", paper.isbn)):
            if value:
                self._claimed[kind].add(value)
        return True

    def _buffer(self, paper: Paper, fulltext: str) -> None:
        """Queue a placed paper for the next categorise-index-record flush."""
        self._pending.append((paper, fulltext))
        self._pending_bytes += len(fulltext)

    def _flush_pending(self) -> None:
        """Categorise, index and record everything buffered since the last flush.

        One flush point rather than per-paper work: a single encode over 50 documents is
        several times faster on CPU than 50 calls. Synchronous on purpose, and it contains no
        await, so the four workers cannot interleave inside it and no lock is needed. The
        model pass does stall them for its duration, which is the trade being made.
        """
        if self._pending:
            batch = self._pending
            self._pending = []
            self._pending_bytes = 0
            self._apply_categorisation_batch([p for p, _ in batch])
            for paper, fulltext in batch:
                self._indexer.add_document(paper, fulltext)
            self._indexer.commit()

        # State is written last, after the index commit: an item counts as processed once its
        # row, its index entry and its tags are all durable. A crash before this costs a redo
        # of the batch, which the DOI duplicate check absorbs.
        state = self._pending_state
        self._pending_state = []
        self._append_state(state)

    def _apply_categorisation_batch(self, papers: list[Paper]) -> None:
        """Merge auto-categorisation onto a batch. No-op if no categoriser is configured or
        its model never loaded."""
        if self._categoriser is None or not self._categoriser.is_loaded:
            return
        try:
            results = self._categoriser.categorise_papers(papers, self._db)
        except Exception as e:
            logger.warning("Auto-categorisation failed for a batch of %d: %s", len(papers), e)
            return
        for paper, (col_ids, tags) in zip(papers, results):
            new_col_ids = sorted(set(paper.collection_ids) | set(col_ids))
            new_tags = sorted(set(paper.tags) | set(tags))
            if new_col_ids != paper.collection_ids or new_tags != paper.tags:
                paper.collection_ids = new_col_ids
                paper.tags = new_tags
                try:
                    self._db.update_paper(paper)
                except Exception as e:
                    logger.warning("Writing categorisation for paper %s failed: %s",
                                   paper.id, e)
```

**Edit 7b: `ImportWorker.__init__` (originally `importer.py:56-83`).** Current state, the last
three lines of the body, verbatim:

```python
        self._pause_requested = False
        self._stop_requested = False
        self._tmp_dir = library_root / "tmp"
```

Change to:

```python
        self._pause_requested = False
        self._stop_requested = False
        self._tmp_dir = library_root / "tmp"
        self._pending: list[tuple[Paper, str]] = []
        self._pending_state: list[str] = []
        self._pending_bytes = 0
        # Identity keys claimed by items already in flight this run. Bounded by the item
        # count and discarded with the worker.
        self._claimed: dict[str, set[str]] = {"hash": set(), "doi": set(), "isbn": set()}
        self._total = 0
        self._done = 0
        self._skipped = 0
        self._imported = 0
        self._review_count = 0
        self._failed = 0
        self._dupes = 0
        self._worked = 0
        self._start_time = 0.0
```

**Edit 7a: the module header (originally `importer.py:12-41`).** Current state, verbatim:

```python
import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QThread, pyqtSignal

from paperbase.core.categoriser import EmbeddingCategoriser
from paperbase.core.db import Database
from paperbase.core.downloader import download_pdf_direct, download_via_unpaywall
from paperbase.core.indexer import Indexer
from paperbase.core.metadata import (
    RateLimiter,
    extract_doi_from_pdf,
    extract_fulltext,
    extract_isbn_from_pdf,
    guess_metadata_from_text,
    resolve_book_metadata,
    resolve_metadata,
)
from paperbase.core.organiser import DEFAULT_PATTERN, copy_to_secondary, place_file
from paperbase.core.scraper import ScrapeResult, classify_url, scrape_landing_page
from paperbase.models.paper import Paper

logger = logging.getLogger(__name__)

INDEX_COMMIT_INTERVAL = 200   # Tantivy commits are fsyncs; batch them
```

Change to:

```python
import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from PyQt6.QtCore import QThread, pyqtSignal

from paperbase.core.categoriser import EmbeddingCategoriser
from paperbase.core.db import Database
from paperbase.core.downloader import download_pdf_direct, download_via_unpaywall
from paperbase.core.indexer import Indexer
from paperbase.core.metadata import (
    PdfText,
    RateLimiter,
    extract_doi,
    extract_isbn,
    guess_metadata,
    read_pdf,
    resolve_book_metadata,
    resolve_metadata,
)
from paperbase.core.organiser import DEFAULT_PATTERN, copy_to_secondary, place_file
from paperbase.core.scraper import ScrapeResult, classify_url, scrape_landing_page
from paperbase.models.paper import Paper

logger = logging.getLogger(__name__)

# 50 rather than 200: an incremental run is about 200 papers, and at 200 it would flush once,
# at the very end, so a crash would cost the whole run's indexing.
INDEX_COMMIT_INTERVAL = 50
_MAX_CONCURRENT_ITEMS = 4                # measured 56ms/DOI against Crossref; 1 gives 152ms
_PENDING_TEXT_LIMIT = 8 * 1024 * 1024    # flush early rather than hold this much text
```

done-when: `py -3.12 tools/check_import_flush.py` prints `flush OK` and exits 0. Create that
file in this phase. It must:

- build 24 synthetic single-page PDFs in a temp directory (same `fitz` recipe as
  `tools/bench_import.py`), each carrying a **distinct** `doi:10.1234/flush.{i}`;
- rebind `paperbase.core.importer.resolve_metadata`, `.resolve_book_metadata` and
  `.guess_metadata` to async stubs that accept the new signatures (including `client`) and
  return a canned `Paper` after `await asyncio.sleep(0.01)`, so concurrency is exercised;
- monkeypatch `paperbase.core.importer.INDEX_COMMIT_INTERVAL` to `5`, so at least four flushes
  happen;
- open a temp `Database` and `Indexer` and run
  `ImportWorker(mode="pdfs", …, state_file=<temp path>, categoriser=None).run()`;
- assert `db.get_paper_count() == 24`;
- assert the state file has exactly 24 lines;
- assert an `indexer.search(...)` for a term in the canned title returns 24 results, proving
  documents were committed rather than left buffered;
- run a **second** worker over the same 24 items with the same state file and assert
  `db.get_paper_count()` is still 24, proving resume skips them;
- assert every one of the 24 rows has a 64-character `content_hash`.

Then three dedupe cases, each a fresh worker with a **fresh state file** (otherwise the resume
set, not the dedupe, is what skips them). Each must add exactly **one** row:

- **six byte-identical copies of one PDF at six distinct paths, each with a distinct DOI in
  its text.** Only the content hash can catch these. Assert the other five are counted as
  `dupes`, not as `failed`, and that no `_2.pdf` companion file was created in the library.
  This is the case the old `paper_exists_by_path` was meant to catch and never did;
- **six PDFs with different bytes carrying the same DOI**, proving the DOI claim holds when
  four workers are in flight;
- **six PDFs with different bytes and no DOI, carrying the same ISBN**, proving the ISBN claim
  holds. Books were never deduplicated before this plan.

Run each with `_MAX_CONCURRENT_ITEMS` left at 4 so the workers genuinely overlap; a stub that
sleeps 10 ms in `resolve_metadata` is what makes the race real rather than theoretical.

Finally, re-run the first dedupe case a second time against a database that already holds the
row, and assert it adds nothing: the claim must work against the library, not only against
items in the same run.

- print `flush OK`.

---

## Phase 8: `paperbase/core/backfill.py`, the fingerprinting worker

skill: coding-standards:python
model: sonnet

**Intent:** the content-hash check added in Phase 5 is only as good as the column behind it,
and the library holds roughly 130,000 rows imported before that column existed. Until they are
fingerprinted, every one of them is invisible to content deduplication, which under the
priority stated at the head of this plan makes this a prerequisite rather than a tidy-up.

This phase is the engine only. Phase 9 gives it a button, a progress surface and the duplicate
report.

Fingerprinting is disk-bound, not CPU-bound: SHA-256 runs at 2.3 GB/s on this machine, so the
run costs one read of the library. Expect minutes on an SSD and considerably longer on a
spinning disk.

**Anchor:** new file `paperbase/core/backfill.py`. Mirror `CategorizationWorker` in
`paperbase/core/categoriser.py:154-247`: a `QThread` subclass, signals as class attributes,
`request_stop`, and all database writes on the worker's own thread.

**Change:** create `paperbase/core/backfill.py`:

```python
"""
HashBackfillWorker: fills papers.content_hash for rows imported before the column existed.

Resumable with no state file: get_papers_missing_hash() stops returning a row the moment its
hash is written, so a stopped, closed or crashed run resumes simply by starting again.
"""
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QThread, pyqtSignal

from paperbase.core.db import Database
from paperbase.core.metadata import sha256_file

logger = logging.getLogger(__name__)

_HASH_WORKERS = 8   # file reads, not computation: hashlib releases the GIL and the disk
                    # services several small reads better than one serial stream
_CHUNK = 200        # rows per submitted batch, which is also the granularity of Stop


def _hash_one(row: tuple[int, str]) -> tuple[int, str]:
    """(paper_id, digest). An empty digest means the file is gone or unreadable; that row
    keeps its NULL hash and is picked up again on a later run if the file comes back."""
    paper_id, file_path = row
    return paper_id, sha256_file(Path(file_path))


class HashBackfillWorker(QThread):
    progress = pyqtSignal(int, int, int)    # done, total, unreadable
    log_message = pyqtSignal(str)
    finished_all = pyqtSignal(int, int)     # hashed, unreadable

    def __init__(self, db: Database, parent: Optional[object] = None) -> None:
        super().__init__(parent)
        self._db = db
        self._stop_requested = False

    def request_stop(self) -> None:
        self._stop_requested = True

    def run(self) -> None:
        rows = self._db.get_papers_missing_hash()
        total = len(rows)
        if not total:
            self.log_message.emit("Every paper already carries a fingerprint.")
            self.finished_all.emit(0, 0)
            return

        self.log_message.emit(f"Fingerprinting {total:,} paper(s).")
        hashed = 0
        unreadable = 0
        done = 0

        with ThreadPoolExecutor(max_workers=_HASH_WORKERS) as pool:
            for start in range(0, total, _CHUNK):
                if self._stop_requested:
                    self.log_message.emit(
                        f"Stopped. {hashed:,} fingerprint(s) written and kept; "
                        f"starting again resumes from here."
                    )
                    break

                chunk = rows[start : start + _CHUNK]
                results = list(pool.map(_hash_one, chunk))

                # Writes happen on this thread only. The sqlite3 connection is shared with
                # the UI thread, and writing it from the pool's threads as well would be a
                # third writer on one connection.
                for paper_id, digest in results:
                    if digest:
                        self._db.update_paper_field(paper_id, "content_hash", digest)
                        hashed += 1
                    else:
                        unreadable += 1
                        logger.warning("No fingerprint for paper %s: file unreadable", paper_id)
                    done += 1

                self.progress.emit(done, total, unreadable)

        if unreadable:
            self.log_message.emit(
                f"{unreadable:,} file(s) could not be read and have no fingerprint."
            )
        self.finished_all.emit(hashed, unreadable)
```

`update_paper_field` commits per row, which under WAL costs about 0.1 ms; across 130,000 rows
that is a few seconds against a run measured in minutes, and it is what makes the run resumable
at any point rather than only at chunk boundaries.

done-when: `py -3.12 tools/check_backfill.py` prints `backfill OK` and exits 0. Create that
file in this phase, following the `sys.path.insert` header of `tools/make_fixture.py:18`. A
`QThread` runs with no `QApplication` when `run()` is called directly, so this needs no display.
It must:

- write four files to a temp directory: `a.pdf` and `b.pdf` with **identical** bytes, `c.pdf`
  with different bytes, and nothing at all at a fourth path;
- open a `Database` on a temp path and insert four papers pointing at those four paths, all
  with `content_hash=None`;
- construct `HashBackfillWorker(db)`, connect `finished_all` to a recorder, and call `run()`
  directly;
- assert the three existing rows now carry a 64-character `content_hash`, that `a.pdf` and
  `b.pdf` share theirs, and that `c.pdf` differs;
- assert the row whose file is missing still has `content_hash` `None`, and that
  `finished_all` reported `(3, 1)`;
- assert `get_duplicate_hash_groups()` returns exactly one group of exactly two ids;
- assert `get_hash_coverage()` is now `(3, 4)`;
- run a second worker and assert it reports `(0, 0)`, since only the unreadable row remains
  and it yields no digest;
- print `backfill OK`.

---

## Phase 9: the Settings button, the fingerprint dialog and the duplicate report

skill: coding-standards:python, coding-standards:aesthetic, coding-standards:prose
model: opus

**Intent:** give Phase 8's worker a place in the interface, and make incomplete fingerprint
coverage visible where it matters instead of leaving the user to assume deduplication is
working when it is half blind.

This is the only phase in the plan that produces a surface a person looks at, which is why it
carries `aesthetic` and `prose` and runs on Opus. The anchors below fix the structure, the
accent and the copy; the spacing, the state transitions and the final wording are judgement,
and making them well is what this phase is for.

**Palette, decided here so it is not decided at implementation time.** The whole feature wears
`theme.ACCENT_AMBER` (`#FFB03A`), which the token table in `CLAUDE.md` already assigns to
import and to needs-review. Fingerprinting is the import family's maintenance job and its
incomplete state is a caution, so amber is right on both counts. Completion with no duplicates
found resolves to `theme.ACCENT_LIME` (`#8FE84A`), the success hue. `theme.ACCENT_RED` is for
unreadable files only. Do not introduce a hue outside the token table.

**Anchor:** four files.

### `paperbase/ui/backfill_dialog.py` (new)

Mirror `paperbase/ui/categorisation_dialog.py:1-181` closely: same `CanvasBackdrop` plus
`GlassPanel` structure, same 20px margin and 20px spacing, same chrome control strip, same
`accent_glow` on the progress bar disabled at rest, same `LogView` object name and
`ClickFocus` policy on the log.

```python
class BackfillDialog(QDialog):
    def __init__(self, db: Database, parent: Optional[QWidget] = None) -> None:
```

Two panels stacked on the backdrop:

1. **`GlassPanel("Fingerprint library", accent=theme.ACCENT_AMBER)`** holding a status line, a
   `QProgressBar` with `setProperty("accent", "amber")` and `setTextVisible(False)`, and the
   log view. The bar's `accent_glow` starts disabled and is enabled only while running, exactly
   as `categorisation_dialog.py:58-61` does it, and for the same reason recorded in that
   comment: the status line says the same thing in words.
2. **`GlassPanel("Duplicates", accent=theme.ACCENT_AMBER)`** holding a `QStackedWidget` with
   three designed states. Per `CLAUDE.md`, every absence here is an `EmptyState` from
   `paperbase/ui/glass.py`, never a blank area:
   - **before a scan has run**: an `EmptyState` explaining that the report appears after a
     scan;
   - **scan finished, nothing found**: an `EmptyState` in `ACCENT_LIME` confirming the library
     holds no identical files;
   - **groups found**: a read-only list, one block per group, each naming the group's size and
     then each row's id, year, first author, title and full path. The path is the field that
     tells the user which copy to keep, so it must be selectable and must not be truncated to
     fit; let it scroll horizontally in its own container rather than eliding, per the
     `overflow-x` rule the results table already follows.

Control strip (`GlassPanel(chrome=True)`): **Start** (`setObjectName("primary")`), **Stop**
(disabled at rest), and **Close**. No Pause: the run resumes from the database on its own, so a
Stop that keeps its progress is the same affordance with one fewer state to explain.

Wire the worker exactly as `categorisation_dialog.py:113-181` wires `CategorizationWorker`.
On `finished_all(hashed, unreadable)`, populate the duplicates panel from
`db.get_duplicate_hash_groups()` and `db.get_papers_by_ids(...)`, and switch the stack to
whichever of the two end states applies.

**Copy.** Use these as the starting text and improve them under `coding-standards:prose`; do
not ship placeholders:

- idle status: `Ready. This reads every PDF once and records a fingerprint for it.`
- running status: `{done:,} of {total:,} fingerprinted`
- stopped: `Stopped. Progress is kept; starting again resumes from here.`
- complete, clean: `All {total:,} papers fingerprinted. No identical files in the library.`
- complete, with duplicates: `{n} group(s) of identical files found.`
- unreadable, appended: `{n} file(s) could not be read.`
- duplicates empty state, before a scan: title `Nothing scanned yet`, body
  `Run a scan to find papers stored twice under different names.`
- duplicates empty state, clean: title `No duplicates`, body
  `Every fingerprinted paper in the library is a distinct file.`

Nothing in this dialog deletes, merges or edits a paper. Say so in the duplicates panel: the
report exists so the user can act on it themselves, and a button that removed rows here would
be making a judgement about file quality and folder placement that this dialog cannot make.

### `paperbase/ui/settings_dialog.py`

Two edits, **in this order**, since the second sits above the first and would otherwise shift
its line number.

**Edit 9c: `settings_dialog.py:251`, immediately after `layout.addWidget(appearance_box)` and
before `layout.addStretch(1)`.** Insert a new group box, built in the same idiom as the
`Appearance` group directly above it (a `QGroupBox` with a `QFormLayout`, `theme.SPACE`
vertical spacing, `theme.SPACE * 2` horizontal, and a `FieldNote` label for the explanation):

- Group title: `Library maintenance`.
- A row labelled `Fingerprints:` whose value comes from `self._db.get_hash_coverage()`, read
  once when the dialog is built. Three cases, and the difference between them is the point of
  the row:
  - `total == 0`: `No papers in the library yet.`
  - `hashed == total`: `All {total:,} papers fingerprinted.`
  - otherwise: `{hashed:,} of {total:,} papers fingerprinted.` **This case is the caution
    state and must read as one**: give the label `theme.ACCENT_AMBER` and follow it with
    `Papers without a fingerprint cannot be recognised as duplicates by their contents.`
    An amber-bordered field is the vocabulary `needs_review` already uses in `PaperDetail`
    (see `CLAUDE.md`, UI Layout); match it rather than inventing a second warning idiom.
- A `Scan library…` button that calls `self.backfill_requested.emit()` and then
  `self._accept()`, so whatever the user changed in Settings is saved and the modal closes
  before the long-running dialog opens. Disable it when `total == 0`.
- A `FieldNote` under the row, starting from: `Reads every PDF once and records a fingerprint,
  so the same file is never imported twice. Papers added before this existed have none until
  the scan runs. It can be stopped and resumed, and it changes nothing else about a paper.`

**Edit 9b: `settings_dialog.py:74-79`, `SettingsDialog.__init__`.** Current state, verbatim:

```python
    def __init__(self, settings: Settings, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(560)
        self._settings = settings
        self._build_ui()
```

Change to:

```python
    backfill_requested = pyqtSignal()

    def __init__(
        self, settings: Settings, db: Database, parent: Optional[QWidget] = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(560)
        self._settings = settings
        self._db = db
        self._build_ui()
```

Add `from PyQt6.QtCore import pyqtSignal` and `from paperbase.core.db import Database` to the
imports at the top of the file.

### `paperbase/ui/main_window.py`

**Edit 9a: `main_window.py:257-271`, method `_open_settings`.** It currently constructs
`SettingsDialog(self._settings, self)` and, on acceptance, nulls `self._import_dialog` and
`self._cat_dialog`. Change it to:

- construct `SettingsDialog(self._settings, self._db, self)`;
- connect `dlg.backfill_requested` to a closure that records the request, before `dlg.exec()`;
- keep the existing body of the `if dlg.exec():` branch exactly as it is, including both
  dialog resets, which `CLAUDE.md` records as deliberate;
- after that branch, if the request was recorded, call a new `self._open_backfill()`.

Then add `_open_backfill`, mirroring `_open_categorisation` at `main_window.py:245-255`: it
caches the dialog on `self._backfill_dialog`, connects its `finished` to `self.refresh_all`,
then calls `show()` and `raise_()`. Non-modal, exactly as categorisation is, because a run over
130,000 files must not hold the rest of the application shut.

**Only then** edit `main_window.py:40`, adding
`self._backfill_dialog: Optional[BackfillDialog] = None` beside the existing
`self._cat_dialog` line, plus the `BackfillDialog` import. This edit is last because it sits
above the other two and would otherwise move their line numbers.

### `paperbase/ui/import_dialog.py`

**Edit 9d: the import dialog's own panel, above its controls.** When
`db.get_hash_coverage()` shows incomplete coverage, show one amber line there:
`{n:,} papers have no fingerprint yet, so identical files may import twice. Settings → Scan
library.` Read the coverage when the dialog is built, and show nothing at all when coverage is
complete. This is the one place the gap actually costs the user something, so it is the one
place worth saying it. Keep it to a single line and do not block the import.

done-when: this is a visual phase and its condition is the surface itself. Run
`py -3.12 tools/make_fixture.py --papers 2000 --out %TEMP%\pb_ui`, set
`PAPERBASE_DATA_DIR=%TEMP%\pb_ui`, start `py -3.12 -m paperbase.main` per the `run` skill, and
confirm by looking at it:

1. Settings shows `Library maintenance` with `0 of 2,000 papers fingerprinted.` in amber, the
   caution sentence beneath it, and an enabled `Scan library…` button.
2. Clicking it closes Settings and opens the fingerprint dialog non-modally: the main window is
   still usable behind it.
3. The fixture writes no PDFs, so every file is unreadable. The run must complete, report
   `2,000 file(s) could not be read.`, leave coverage at zero, and show the `Nothing scanned
   yet` state resolving to the clean end state. Nothing may crash and no traceback may reach
   the console. **This is the error path, and it is the one the fixture can actually exercise**,
   so do not skip it.
4. Point the fixture's rows at three real files instead (two identical, one different) with a
   short script, re-run, and confirm the duplicates panel lists exactly one group of two with
   full paths, and that the Import dialog's amber line disappears once coverage is complete.
5. Screenshot the dialog in the running state and in both end states, and confirm against
   `coding-standards:aesthetic`: hierarchy, spacing on the token scale, the accent landing on
   the one thing that matters, and every state designed rather than blank.

---

## Phase 10: measure, and confirm the application still runs

skill: coding-standards:python
model: sonnet

**Intent:** the plan's justification is two numbers. Take them.

**Anchor:** `tools/bench_network.py` and `tools/bench_import.py` from Phase 1.

**Change:**

1. `tools/bench_import.py`: update the stubs for the new signatures.
   `imp.guess_metadata_from_text` becomes `imp.guess_metadata` with
   `(path, pdf, user_email, rate_limiter, client)`; `imp.resolve_metadata` takes
   `(doi, user_email, rl, client)`; `imp.resolve_book_metadata` takes `(isbn, rl, client)`.
   No other change.
2. `tools/bench_network.py`: add a `--concurrency N` argument defaulting to
   `paperbase.core.importer._MAX_CONCURRENT_ITEMS`, run the DOIs through an
   `asyncio.Semaphore(N)` against one shared `httpx.AsyncClient` passed to `resolve_metadata`,
   and print `mode=pooled` in the summary line.

Then:

1. `py -3.12 tools/bench_network.py` and `py -3.12 tools/bench_import.py --papers 60`, appending
   both lines to `tasks/bench-baseline.txt` prefixed `after: `.
2. `py -3.12 tools/make_fixture.py --papers 2000 --out %TEMP%\pb_perf`, then
   `set PAPERBASE_DATA_DIR=%TEMP%\pb_perf` and `py -3.12 -m paperbase.main`. Confirm the window
   opens, the results table lists papers, and no traceback reaches the console.
3. Open the Import dialog and confirm its labels still read sensibly now that four items are in
   flight: the counters must still satisfy done == imported + dupes + failed + skipped, and the
   log pane will interleave items, which is expected. Close it.
4. Open Settings and confirm the `Library maintenance` row reads correctly against the fixture,
   and that `Scan library…` opens the fingerprint dialog non-modally. Do not run a scan over a
   real library here.
5. Re-run every verification script the run created, in one go, and read each one's output:
   `check_hash_schema.py`, `check_metadata_read.py`, `check_clients.py`, `check_phase5.py`,
   `check_batch_encode.py`, `check_import_flush.py`, `check_backfill.py`. They are the only
   standing description of what this run is supposed to do.

done-when: `tasks/bench-baseline.txt` holds four lines; the `after:` network line's `per_doi`
is at least 60% below the baseline network line's, and the `after:` import line's `per_paper`
is at least 20% below the baseline import line's; all seven verification scripts print their
`OK` line and exit 0; and `py -3.12 -m paperbase.main` against the fixture starts and lists
papers with a clean console.

If either throughput threshold is missed, **halt and report the four numbers** rather than
tuning `_MAX_CONCURRENT_ITEMS` to reach them. The thresholds come from measurements taken while
planning; missing one means the model was wrong, which is a plan question.

**One step is left for the user, and this phase must say so in its report rather than attempt
it:** open Settings on the machine that holds the library and press `Scan library…`. Until that
has run, content deduplication only sees papers imported after this run, and the duplicate
report is empty. It is a full read of roughly 130,000 files, which is minutes on an SSD and
considerably longer on a spinning disk, and it is the user's call when to start it. Do not run
it against a real library from an automated phase, and do not point a fixture at the real data
directory: `PAPERBASE_DATA_DIR` must be set to a throwaway path for every check in this plan.

---

## Consequence for the vector plan below

This run rewrites the regions the vector/taxonomy plan anchors against. Before dispatching that
plan, re-anchor these phases against the post-run files:

- **Its Phase 7** (`EmbeddingCategoriser`) quotes `categorise_paper`'s body and the inline
  `from sentence_transformers import SentenceTransformer` block. Both are gone. The
  `_load_models` factory added here is the seam its `_load_sentence_transformer` wanted, and
  `categorise_papers` is where its `vector=` parameter now belongs.
- **Its Phase 9** (`ImportWorker`) quotes `_apply_categorisation` and the `pending_index`
  commit block. Both are gone. Its vector-store `flush()` call belongs in `_flush_pending`, and
  its per-paper `encode` belongs inside `categorise_papers`, which already has the batch.
- **Its Phase 13** touches `ImportWorker.__init__`, which gained a block of state here.

Everything else in that plan (new modules, settings, dialogs, taxonomy, YAKE) is untouched.

## Not in this plan, deliberately

- **A process pool for PDF reads.** It was in the earlier draft of this plan, sized for the
  130k backfill. At 200 items the Windows `spawn` cost cancels the gain; `asyncio.to_thread`
  gets the overlap for nothing.
- **Batching the SQLite commits.** About 20 ms across a whole run, against holding a write
  transaction on a connection the UI thread shares.
- **Fuzzy duplicate detection on title and year.** See the design decisions above: it belongs
  to a review-and-confirm feature, not to an unattended import run.
- **Acting on the duplicates the scan finds.** Phase 9 lists them with enough detail to decide
  by; deleting, merging or re-filing them from inside that dialog is separate work with its own
  confirmation design.
- **Deduplicating on `file_path` at all.** `paper_exists_by_path` is deleted rather than
  repaired: it compared the source path against a column that holds the destination, and once
  the content hash exists there is nothing left for a path comparison to add. Any future check
  on paths would be answering a question the hash already answers better.
- **Giving `ImportWorker` its own SQLite connection.** `db.py:131` passes
  `check_same_thread=False` and every thread shares one connection. A genuine latent problem,
  and wholly separate from import throughput.
- **Raising `_MAX_CONCURRENT_ITEMS` above 4.** 8 measured 35 ms/DOI against 56 ms, but sits
  closer to Crossref's polite-pool ceiling. It is one constant to revisit with real data.

---

# Persistent paper vectors, taxonomy assignment, and YAKE keywording

> **Blocked: re-anchor before dispatching.** The import-throughput plan above runs first
> and rewrites `categoriser.py`, `importer.py` and `db.py`. Phases 7, 9 and 13 below quote
> text that will no longer exist. Re-read those three files and re-anchor those phases
> before this plan is executed; see **Consequence for the vector plan** above for what
> moved where.

base: (fill with `git rev-parse HEAD` before dispatching Phase 1)

Implements sections 2 to 4 of `docs/auto-categorisation.md`. Everything here is buildable and
verifiable on a machine with no PDF library, which is the constraint the plan is written to.

**done-when:** `py -3.12 -m pytest tests -q` reports 0 failed with at least 9 test files
collected, and `py -3.12 -m paperbase.main` starts against a fixture data directory with the
Settings dialog showing a taxonomy row.

## Environment facts this plan depends on (verified on this machine)

- Installed: `numpy` 2.5.0, `yake`, `pytest` 9.0.3, `tantivy`, `fitz`, `PyQt6`.
- **Not installed: `sentence-transformers`, `keybert`.** No phase may have a `done-when:` that
  loads the real embedding model. Every test reaches the model through a monkeypatchable
  module-level factory instead.
- `paperbase` is **not** pip-installed; `pythonpath = ["."]` in `pyproject.toml` is what makes
  `import paperbase` work under pytest. Phase 1 adds it.
- A `QThread` can be constructed and its `run()` called directly with no `QApplication` and no
  event loop; signals with no receivers emit harmlessly. This is verified, and it is why the
  worker tests need neither `pytest-qt` nor a display.

## Design decisions already made (do not re-open)

- **Vector file format is raw little-endian float32, not `.npy`.** The doc names
  `paper_vectors.npy`, but a `.npy` header carries the shape, so every append would rewrite it.
  A headerless `paper_vectors.f32` plus a `paper_vectors.json` sidecar holding `dim`, `model`
  and the row-ordered `ids` appends with a plain write and reads with one `np.memmap`.
- **Vectors live in the data directory** (beside `paperbase.db`), not in `library_root`. They
  are derived from the DB and are meaningless without it; `import_state.json` sits in
  `library_root` because it is about the user's files.
- **The vector store replaces `categorisation_state.json`.** Its id map is the resume record for
  the expensive stage. The cheap stage (assignment plus keywords, about 10 minutes for 150k)
  is idempotent and simply re-runs.
- **Free-text categories stay** alongside the taxonomy. Assigned collections are the union of both.
- **YAKE replaces KeyBERT** for keywords, per the doc. `keybert` leaves `pyproject.toml`.
- **`numpy` is declared explicitly** in `pyproject.toml`. It is already present transitively via
  `sentence-transformers`; the project now imports it directly, so it gets named. This adds no
  package to any environment.
- **Nothing ships as a starter taxonomy.** `tools/seed_taxonomy.py` derives a draft on the target
  machine; tests write their own taxonomy files.

---

## Phase 1 — pytest scaffolding

skill: coding-standards:python
model: sonnet

**Anchor:** `pyproject.toml:9` (`dependencies` list) and `pyproject.toml:24`
(`[project.optional-dependencies]`).

**Current state** (`pyproject.toml:9-25`), verbatim:

```toml
dependencies = [
    "PyQt6>=6.6",
    "tantivy>=0.22",
    "PyMuPDF>=1.24",
    "httpx>=0.27",
    "aiosqlite>=0.20",
    "qasync>=0.27",
    "platformdirs>=4.2",
    "beautifulsoup4>=4.12",
    "lxml>=5.0",
    "sentence-transformers>=3.0",
    "keybert>=0.8",
    "yake>=0.7",
]

[project.optional-dependencies]
dev = ["pyinstaller>=6.0", "pytest>=8.0", "pytest-qt>=4.4"]
```

**Change:** add `"numpy>=1.26",` immediately after `"lxml>=5.0",`. Leave `keybert` alone (Phase 7
removes it). After the `[project.optional-dependencies]` block, add:

```toml
[tool.pytest.ini_options]
pythonpath = ["."]
testpaths = ["tests"]
addopts = "-q"
```

**Also create `tests/conftest.py`**, containing exactly these helpers and no test-only production
API:

- `def make_paper(**overrides) -> Paper` — builds a valid `Paper` with `id=None`, `doi=None`,
  `title="A test paper"`, `authors=["Smith, J."]`, `journal="Journal of Tests"`, `year=2020`,
  `volume="1"`, `issue="1"`, `pages="1-10"`, `abstract=""`, `keywords=[]`, `tags=[]`,
  `collection_ids=[]`, `file_path` unique per call (module-level counter, because
  `papers.file_path` is `UNIQUE`), `date_added="2020-01-01T00:00:00+00:00"`,
  `date_modified="2020-01-01T00:00:00+00:00"`, `metadata_source="crossref"`, `needs_review=False`,
  `open_access=False`, then applies `overrides`.
- `@pytest.fixture def db(tmp_path) -> Iterator[Database]` — opens `Database(tmp_path / "t.db")`,
  yields, closes.
- `@pytest.fixture def store_path(tmp_path) -> Path` — returns `tmp_path / "paper_vectors.f32"`.
- `def unit_vectors(n: int, dim: int = 384, seed: int = 0) -> np.ndarray` — deterministic
  `np.random.default_rng(seed)` normals, L2-normalised row-wise, `float32`.

**Also create `tests/test_smoke.py`**: one test that inserts two papers via the `db` fixture and
asserts `db.get_paper_count() == 2` and that `db.get_paper(pid).title` round-trips.

**Intent:** every later phase's `done-when:` is a pytest run, so the runner has to exist and import
the package before anything else is built.

**done-when:** `py -3.12 -m pytest tests/test_smoke.py -q` reports `1 passed`.

---

## Phase 2 — `paperbase/core/vectors.py`: the persistent vector store

skill: coding-standards:python
model: sonnet

**Anchor:** new file `paperbase/core/vectors.py`; new file `tests/test_vectors.py`.

**Intent:** this is the change the whole document turns on. `EmbeddingCategoriser` currently throws
away every vector it computes, so re-running categorisation re-embeds 150,000 PDFs. Persisting them
turns a re-run into a matmul.

**Change:** create `paperbase/core/vectors.py` with exactly this public surface. The module
docstring states the on-disk format.

```python
DIM = 384
BUFFER_LIMIT = 200          # rows held in memory before an automatic flush

class VectorStore:
    def __init__(self, path: Path, dim: int = DIM, model_name: str = "") -> None: ...
    @property
    def dim(self) -> int: ...
    def open(self) -> None: ...
    def close(self) -> None: ...
    def __len__(self) -> int: ...
    def has(self, paper_id: int) -> bool: ...
    def get(self, paper_id: int) -> Optional[np.ndarray]: ...
    def missing(self, paper_ids: Iterable[int]) -> list[int]: ...
    def add(self, paper_id: int, vector: np.ndarray) -> None: ...
    def flush(self) -> None: ...
    def matrix(self) -> tuple[np.ndarray, list[int]]: ...
    def clear(self) -> None: ...
```

Semantics, all of which the tests below pin:

- `path` is the float32 blob. The sidecar is `path.with_suffix(".json")` holding
  `{"dim": int, "model": str, "ids": [int, ...]}`, where `ids[i]` is the paper id of row `i`.
- `open()` reads the sidecar if present, else starts empty. Then it reconciles: with
  `rows_on_disk = path.stat().st_size // (dim * 4)`, if `rows_on_disk != len(ids)` keep
  `n = min(rows_on_disk, len(ids))`, truncate the blob to `n * dim * 4` bytes, truncate `ids` to
  `n`, rewrite the sidecar, and `logger.warning` the recovery. A half-written pair of files is the
  expected outcome of a power cut mid-import, and this is derived data, so it recovers rather than
  raising.
- A sidecar whose `dim` differs from `self._dim` means the model changed: log a warning, delete
  both files, start empty. Do not raise.
- `add` validates `vector.shape == (dim,)` and that all values are finite. Anything else raises
  `ValueError` naming the paper id. The vector is cast to `float32` and copied, never stored by
  reference.
- `add` on an id already flushed to disk overwrites that row in place (`r+b`, seek to
  `row * dim * 4`). On an id still in the buffer it replaces the buffered value. `__len__` does not
  change in either case.
- `add` calls `flush()` itself once the buffer reaches `BUFFER_LIMIT`, so a caller that never
  flushes still has bounded memory.
- `has`/`get`/`missing` see buffered rows as well as flushed ones.
- `flush()` appends the buffer to the blob, extends `ids`, and writes the sidecar atomically
  (write `path.with_suffix(".json.tmp")`, then `Path.replace`). No-op on an empty buffer.
- `matrix()` flushes, then returns
  `(np.memmap(path, dtype=np.float32, mode="r").reshape(-1, dim), list(ids))`. On an empty store it
  returns `(np.zeros((0, dim), dtype=np.float32), [])` without touching disk.
- `clear()` deletes both files and resets in-memory state.
- Every public method holds a `threading.Lock` for its duration. `ImportWorker` and
  `CategorizationWorker` are separate `QThread`s and both can be running.
- `pathlib.Path` only, type hints on every signature, no `os.path`.

**Also create `tests/test_vectors.py`** with these tests:

1. `test_empty_store` — `len() == 0`, `matrix()` gives shape `(0, 384)` and `[]`, `has(1) is False`,
   `get(1) is None`.
2. `test_add_flush_reopen` — add 5, flush, open a second `VectorStore` on the same path: `len() == 5`,
   every `get(id)` is `np.allclose` to what went in, `matrix()` rows are in insertion order and the
   returned id list matches.
3. `test_buffered_reads` — add 3 without flushing: `has`/`get` see them; `matrix()` flushes and
   returns 3 rows.
4. `test_auto_flush_at_limit` — add `BUFFER_LIMIT + 50` with no explicit flush, then assert the blob
   on disk already holds at least `BUFFER_LIMIT` rows.
5. `test_overwrite_flushed_row` — add 3, flush, `add` a different vector for the middle id;
   `len() == 3` and `get` returns the new vector after a reopen.
6. `test_rejects_bad_vector` — wrong length, and a vector containing `np.inf`, each raise `ValueError`.
7. `test_truncated_blob_recovers` — flush 10 rows, truncate the blob to 7 rows' worth of bytes,
   reopen: `len() == 7`, no exception, sidecar rewritten with 7 ids.
8. `test_dim_mismatch_resets` — write a sidecar with `"dim": 128`, reopen with `dim=384`: `len() == 0`,
   no exception.
9. `test_missing` — with ids 1 and 3 stored, `missing([1, 2, 3, 4]) == [2, 4]`.

**done-when:** `py -3.12 -m pytest tests/test_vectors.py -q` reports `9 passed`.

---

## Phase 3 — `paperbase/core/taxonomy.py`: the label file

skill: coding-standards:python
model: sonnet

**Anchor:** new file `paperbase/core/taxonomy.py`; new file `tests/test_taxonomy.py`.

**Intent:** 150 to 300 topic labels have to be hand-edited, so they live in a plain text file the
user owns rather than in `settings.json`. The file is untrusted input and is validated at the
boundary.

**Change:** create `paperbase/core/taxonomy.py`:

```python
MAX_NAME = 120
MAX_LABELS = 2000

class TaxonomyError(Exception): ...

@dataclass(frozen=True)
class Label:
    name: str
    description: str = ""
    @property
    def text(self) -> str: ...        # f"{name}. {description}" when described, else name

def parse_taxonomy(text: str) -> list[Label]: ...
def load_taxonomy(path: Optional[Path]) -> list[Label]: ...
def save_taxonomy(path: Path, labels: Sequence[Label]) -> None: ...
```

File format, which `save_taxonomy` documents in a header comment block it writes:

- One label per line: `Name` or `Name: description`. Only the **first** colon splits; colons inside
  a description are kept.
- Lines whose first non-space character is `#` are comments. Blank lines are ignored.
- `Name` and `description` are stripped.

Validation rules, each skipped with a `logger.warning` rather than raising, because one bad line
must not cost the user the other 299:

- empty name after stripping;
- name longer than `MAX_NAME`;
- duplicate name compared with `str.casefold`, first occurrence wins.

`MAX_LABELS` is a hard stop: once reached, stop parsing and log. `load_taxonomy(None)` and a
non-existent path both return `[]`. A path that exists but is not a file, or that fails to decode as
UTF-8, raises `TaxonomyError`.

**Also create `tests/test_taxonomy.py`:**

1. `test_parse_basic` — bare names, `Name: description`, and `Label.text` for both shapes.
2. `test_comments_and_blanks_ignored`.
3. `test_only_first_colon_splits` — `"Isotopes: C, N: ratios"` gives description `"C, N: ratios"`.
4. `test_duplicates_dropped_case_insensitively` — `Ecology` then `ECOLOGY` yields one label keeping
   the first spelling.
5. `test_invalid_lines_skipped` — over-long names and empty names are skipped, valid neighbours survive.
6. `test_cap_enforced` — 2500 lines yields exactly `MAX_LABELS`.
7. `test_round_trip` — `save_taxonomy` then `load_taxonomy` returns an equal list, and the saved file
   re-parses through its own header comments.
8. `test_missing_file_returns_empty` — missing path and `None` both give `[]`.
9. `test_directory_raises` — passing a directory raises `TaxonomyError`.

**done-when:** `py -3.12 -m pytest tests/test_taxonomy.py -q` reports `9 passed`.

---

## Phase 4 — `paperbase/core/assign.py`: the assignment maths

skill: coding-standards:python
model: sonnet

**Anchor:** new file `paperbase/core/assign.py`; new file `tests/test_assign.py`.

**Intent:** with vectors on disk, assigning the whole library is one matmul. Keeping it as pure
functions over arrays is what lets it be tested on a machine with no embedding model.

**Change:** create `paperbase/core/assign.py`:

```python
def normalise(vectors: np.ndarray) -> np.ndarray: ...
def top_labels(
    doc_vecs: np.ndarray,
    label_vecs: np.ndarray,
    threshold: float,
    top_k: int,
    chunk: int = 4096,
) -> list[list[tuple[int, float]]]: ...
```

- `normalise` divides each row by its L2 norm; a zero row stays zero (no divide-by-zero warning, no
  NaN). Returns `float32`.
- `top_labels` assumes both inputs are unit-normalised (the store holds normalised vectors), so the
  score is a plain dot product. For each document row it returns at most `top_k`
  `(label_index, score)` pairs with `score >= threshold`, sorted by score descending, ties broken by
  ascending label index.
- Documents are processed `chunk` rows at a time so peak memory is `chunk * n_labels` floats, not
  `150000 * n_labels`. The result must not depend on `chunk`.
- `doc_vecs` with 0 rows returns `[]`. `label_vecs` with 0 rows returns one empty list per document.
  A zero-norm document row returns an empty list.

**Also create `tests/test_assign.py`:**

1. `test_orthogonal_basis` — 4 documents that are exactly `e0..e3` against labels `e0..e3`: each gets
   its own label at score 1.0 and nothing else at `threshold=0.5`.
2. `test_threshold_filters` — a document at cosine 0.4 to a label yields nothing at `threshold=0.5`
   and one pair at `threshold=0.3`.
3. `test_top_k_truncates_and_orders` — a document similar to 5 labels with distinct scores returns
   exactly `top_k=3` pairs in descending score order.
4. `test_chunk_invariance` — 10 documents, `chunk=3` and `chunk=100` give identical results.
5. `test_zero_vector_gets_nothing`.
6. `test_empty_inputs` — 0 documents gives `[]`; 0 labels gives `[[], [], ...]`.
7. `test_normalise_zero_row` — a zero row stays zero and no NaN appears anywhere.

**done-when:** `py -3.12 -m pytest tests/test_assign.py -q` reports `7 passed`.

---

## Phase 5 — `paperbase/core/keywords.py`: YAKE with the overlap filter

skill: coding-standards:python
model: sonnet

**Anchor:** new file `paperbase/core/keywords.py`; new file `tests/test_keywords.py`.

**Intent:** the taxonomy will never contain *Suillus bovinus*. YAKE supplies the specific terms, and
`docs/auto-categorisation.md` section 2 has the measured settings: `n=2`, 25 raw candidates,
`dedupLim=0.9`, then a token-overlap walk keeping 6.

**Change:** create `paperbase/core/keywords.py`:

```python
DEFAULT_TOP = 6
CANDIDATES = 25
MIN_TEXT_CHARS = 40

STOP_TOKENS: frozenset[str] = frozenset({
    "form", "forms", "formed", "including", "include", "includes", "using", "used", "use",
    "based", "show", "shows", "showed", "shown", "present", "presents", "presented",
    "report", "reports", "reported", "suggest", "suggests", "suggested", "found", "find",
    "observed", "examined", "described", "describe", "provide", "provides", "results",
    "result", "study", "studies", "paper", "here", "these", "this", "those", "their",
    "our", "may", "can", "also", "well", "however", "whereas", "thus", "therefore",
    "between", "within", "across", "during", "over", "under", "both", "two", "three",
})

def extract_keywords(text: str, top: int = DEFAULT_TOP) -> list[str]: ...
```

Algorithm, following the document's snippet and adding the stop filter it calls for:

1. `text` shorter than `MIN_TEXT_CHARS` after stripping returns `[]`.
2. Get `CANDIDATES` raw candidates from a module-level cached
   `yake.KeywordExtractor(lan="en", n=2, top=CANDIDATES, dedupLim=0.9)`. Build it lazily on first use
   and keep the single instance; it is stateless, and constructing it per paper across 150,000
   papers is waste.
3. Walk candidates best-first (YAKE yields ascending score, lower is better).
4. Drop a candidate whose **first or last** token, lowercased and stripped of `.,;:()`, is in
   `STOP_TOKENS`. This is what removes "form symbiotic" and "including gut" while keeping
   "Mycorrhizal fungi".
5. Drop a candidate sharing any token with a candidate already kept.
6. Stop at `top`.

`import yake` happens inside the function, matching the optional-import pattern already in
`core/categoriser.py`. `ImportError` logs once and returns `[]`.

**Also create `tests/test_keywords.py`**, using this abstract as `MYCO_TEXT` (the one the document
measured against):

```
Mycorrhizal fungi form symbiotic associations with the roots of most land plants.
We examined ectomycorrhizal colonisation of Pinus sylvestris seedlings by Suillus bovinus in a
podzol soil in northern Sweden, and measured nitrogen transfer over two growing seasons.
```

1. `test_returns_at_most_top` — `len(extract_keywords(MYCO_TEXT)) <= 6` and is non-empty.
2. `test_no_shared_tokens` — no two returned phrases share a lowercased token.
3. `test_stop_tokens_filtered` — `"form symbiotic"` is not in the result while `"Mycorrhizal fungi"`
   is. (Verified against the installed `yake`: "Mycorrhizal fungi" is the top-ranked candidate for
   this text, with "fungi form" and "form symbiotic" just below it.)
4. `test_short_text_returns_empty` — `extract_keywords("Too short.") == []`.
5. `test_deterministic` — two calls on the same text return the same list.
6. `test_top_parameter_respected` — `top=2` returns at most 2.

**done-when:** `py -3.12 -m pytest tests/test_keywords.py -q` reports `6 passed`.

---

## Phase 6 — `CategorizationWorker`: two stages, resume from the vector store

skill: coding-standards:python
model: sonnet

**Anchor:** `paperbase/core/categoriser.py:154-266`, class `CategorizationWorker`, from
`class CategorizationWorker(QThread):` to the end of the file.

This phase edits the **lower** half of `categoriser.py` and Phase 7 edits the upper half, so the
file is worked bottom-up and Phase 7's anchors do not move. The tests for both halves arrive in
Phase 8; this phase's own `done-when:` is a signature check.

**Current state**, keyed by these verbatim fragments:

```python
    def __init__(
        self,
        db: Database,
        categoriser: EmbeddingCategoriser,
        state_file: Optional[Path] = None,
        parent=None,
    ) -> None:
```

```python
        self.log_message.emit("Model ready. Starting categorisation…")
        processed = self._load_state()
        all_ids = self._db.get_all_paper_ids()
```

```python
    def _load_state(self) -> set[int]:
```

**Change:** replace the whole class with a two-stage worker.

Signals: keep `progress = pyqtSignal(int, int)`, `log_message = pyqtSignal(str)`,
`finished_all = pyqtSignal()`. Add `stage_changed = pyqtSignal(str)`, emitting exactly
`"embedding"`, `"assigning"` or `"done"`.

Constructor:

```python
    def __init__(
        self,
        db: Database,
        categoriser: EmbeddingCategoriser,
        store: VectorStore,
        batch_size: int = 64,
        parent=None,
    ) -> None:
```

`state_file`, `_load_state` and `_save_state` are deleted outright, along with the
`_STATE_SAVE_INTERVAL` constant at `categoriser.py:28`. The store's id map is the resume record, so
`categorisation_state.json` stops being written.

`run()`:

1. Emit `"Loading embedding model…"`, call `self._categoriser.load_model()`. If it did not load, log
   that collections cannot be assigned without it and **continue anyway**: keyword extraction needs
   no model, and a run that produces keywords is still worth having.
2. **Stage `"embedding"`.** `all_ids = self._db.get_all_paper_ids()`;
   `todo = self._store.missing(all_ids)`. Skip the stage entirely when `todo` is empty or the model
   is not loaded. Otherwise walk `todo` in chunks of 900 (the `SQLITE_MAX_VARIABLE_NUMBER` ceiling
   documented in `CLAUDE.md`), fetch with `self._db.get_papers_by_ids(chunk)`, build one text per
   paper with `paper_text(paper)` from Phase 7, encode with `self._categoriser.encode_batch(texts)`
   in sub-batches of `batch_size`, and `store.add` each result. Call `self._store.flush()` after
   every chunk. Emit `progress(done, len(todo))` per chunk and a `log_message` every 10 chunks.
   Honour `_stop_requested` (break) and `_pause_requested` (`time.sleep(0.2)` loop) at the top of
   each chunk, exactly as the current loop does.
3. **Stage `"assigning"`.** `matrix, row_ids = self._store.matrix()`. Take the label matrix from the
   categoriser. When there are labels, compute
   `assignments = top_labels(matrix, label_matrix, threshold, top_k)` once. Then walk `all_ids` in
   chunks of 900: for each paper, union its existing `collection_ids` with the collection ids for
   its assigned labels (resolved through the categoriser, which creates missing collections) and
   with the free-text category matches for its stored vector; union its existing `tags` with
   `extract_keywords(paper.abstract or paper.title, top=tag_count)`. Write with
   `self._db.update_paper(paper)` **only when something changed**, comparing sorted lists so
   ordering churn causes no write. Never replace: the merge rule in `CLAUDE.md` is that
   categorisation adds and user edits survive.
4. Emit `stage_changed("done")`, a final `log_message` naming papers embedded and papers updated,
   then `finished_all`.

Imports needed by this class (`from paperbase.core.assign import top_labels`,
`from paperbase.core.keywords import extract_keywords`, `from paperbase.core.taxonomy import Label`,
`from paperbase.core.vectors import VectorStore`, `import numpy as np`) are added by Phase 7, which
owns the import block.

**Intent:** the expensive stage is the only one that needs a resume record, and the vector store
already is one. Two stages also make the progress bar honest: hours, then seconds.

**done-when:** `py -3.12 -c "import inspect; from paperbase.core.categoriser import CategorizationWorker as W; s=inspect.signature(W.__init__); assert list(s.parameters)==['self','db','categoriser','store','batch_size','parent'], s; assert not hasattr(W,'_load_state'); print('OK')"`
prints `OK`. If it fails only on an ImportError for the Phase 7 imports, run Phase 7 and re-check
before reporting either phase done.

---

## Phase 7 — `EmbeddingCategoriser`: taxonomy, reusable vectors, YAKE

skill: coding-standards:python
model: sonnet

**Anchor:** `paperbase/core/categoriser.py:1-151`: module docstring, imports, constants,
`class EmbeddingCategoriser`, `_get_or_create_collection`.

**Current state**, keyed by these verbatim fragments:

```python
    def __init__(self) -> None:
        self._model = None
        self._kw_model = None
        self._lock = threading.Lock()
```

```python
            try:
                from sentence_transformers import SentenceTransformer
                from keybert import KeyBERT
            except ImportError:
```

```python
    def update_settings(
        self,
        categories: list[dict],
        threshold: float,
        tag_count: int,
    ) -> None:
```

```python
            tags: list[str] = []
            if self._kw_model and paper.abstract:
```

**Change:**

Module level, replacing the KeyBERT import path:

```python
def _load_sentence_transformer(name: str):
    """Import and construct the model. Patched in tests, which have no model installed."""
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer(name)


def paper_text(paper: Paper) -> str:
    """The text a paper is embedded as: title, then abstract."""
    return f"{paper.title}. {paper.abstract}".strip(" .")
```

`load_model` calls `_load_sentence_transformer(self.MODEL_NAME)` inside its `try`, catches
`ImportError` with the existing log line minus the keybert mention, and no longer builds a `KeyBERT`.
Delete `self._kw_model` everywhere.

New state in `__init__`: `self._labels: list[Label] = []`,
`self._label_matrix: Optional[np.ndarray] = None`, `self._top_k: int = 4`. Keep
`_category_embeddings`, `_categories`, `_threshold`, `_tag_count`.

New signature:

```python
    def update_settings(
        self,
        categories: list[dict],
        threshold: float,
        tag_count: int,
        labels: Optional[list[Label]] = None,
        top_k: int = 4,
    ) -> None:
```

It stores the labels and, when the model is loaded, recomputes both the free-text category
embeddings (as now) and `self._label_matrix` as an `(L, 384) float32` array from
`self._model.encode([lab.text for lab in labels], normalize_embeddings=True)`. With no labels the
matrix is `None`.

New methods:

```python
    @property
    def has_labels(self) -> bool: ...
    @property
    def labels(self) -> list[Label]: ...
    def label_matrix(self) -> Optional[np.ndarray]: ...
    def encode(self, text: str) -> Optional[np.ndarray]: ...               # (384,) float32, normalised
    def encode_batch(self, texts: list[str]) -> Optional[np.ndarray]: ...  # (N, 384) float32
    def collections_for_labels(self, indices: Sequence[int], db: Database) -> list[int]: ...
    def categories_for_vector(self, vector: np.ndarray) -> list[str]: ...
```

`encode`/`encode_batch` return `None` when the model is not loaded, hold `self._lock` around the
model call, and cast to `float32`.

`has_categories` becomes `bool(self._categories) or bool(self._labels)`, since it gates the preload
and the dialog's Start button.

`categorise_paper` changes to:

```python
    def categorise_paper(
        self, paper: Paper, db: Database, vector: Optional[np.ndarray] = None
    ) -> tuple[list[int], list[str]]:
```

- When `vector is None` and the model is loaded, encode `paper_text(paper)`.
- Collections: the union of `categories_for_vector(vector)` (resolved to ids through the existing
  `_get_or_create_collection`) and, when `label_matrix` exists, the labels chosen by
  `top_labels(vector.reshape(1, -1), matrix, self._threshold, self._top_k)[0]` resolved through
  `collections_for_labels`. With no vector, collections are `[]`.
- Tags: `extract_keywords(paper.abstract or paper.title, top=self._tag_count)`. This no longer
  depends on the model, so a run with no model still tags.
- Keep the existing rule that DB work happens outside the lock.

`_get_or_create_collection` is unchanged. Delete `_STATE_SAVE_INTERVAL` if Phase 6 did not.

**Also edit `pyproject.toml`:** delete the line `    "keybert>=0.8",` from `dependencies`. Nothing
imports it after this phase.

**Intent:** the categoriser stops being the thing that recomputes everything and becomes a thin shell
over the model plus the pure functions from Phases 3 to 5, which is what makes it testable on a
machine with no model.

**done-when:** `py -3.12 -c "from paperbase.core.categoriser import EmbeddingCategoriser, paper_text, CategorizationWorker; c=EmbeddingCategoriser(); assert c.encode('x') is None; assert not c.has_categories; assert not hasattr(c,'_kw_model'); print('OK')"`
prints `OK`, and
`py -3.12 -c "import pathlib,sys; sys.exit('keybert still declared' if 'keybert' in pathlib.Path('pyproject.toml').read_text() else 0)"`
exits 0.

---

## Phase 8 — Categoriser and worker tests with a stubbed model

skill: coding-standards:python
model: sonnet

**Anchor:** new file `tests/test_categoriser.py`.

**Intent:** Phases 6 and 7 are the two pieces that cannot be smoke-tested by hand here, because the
real model is not installed. The monkeypatch seam added in Phase 7 is what makes them testable at
all, and the resume claim (a second run embeds nothing) is the central promise of the design.

**Change:** create `tests/test_categoriser.py` with a `FakeModel` whose
`encode(texts, normalize_embeddings=True)` returns deterministic unit vectors derived from a stable
hash of each text (same text always gives the same vector), handling both a single string and a
list, and counting calls. Install it with
`monkeypatch.setattr(categoriser, "_load_sentence_transformer", lambda name: FakeModel())`.

Tests:

1. `test_encode_shapes` — `encode` gives `(384,)` float32 with unit norm; `encode_batch` of 3 gives
   `(3, 384)`.
2. `test_update_settings_builds_label_matrix` — 5 labels produce a `(5, 384)` matrix; zero labels
   produce `None`.
3. `test_categorise_paper_uses_supplied_vector` — passing `vector=` does not call the model (assert
   the FakeModel call count is unchanged).
4. `test_categorise_paper_creates_collections` — matching labels create top-level collections in the
   DB and return their ids.
5. `test_tags_without_model` — with the model never loaded, `categorise_paper` still returns keywords
   for a paper with a real abstract, and no collections.
6. `test_worker_embeds_and_stores` — 20 papers with abstracts, a taxonomy of 4 labels, a fresh
   `VectorStore`; construct `CategorizationWorker(db, cat, store)` and call `run()` directly (no
   `QApplication`, verified to work). Assert `len(store) == 20` and every paper id is present.
7. `test_worker_second_run_embeds_nothing` — run the worker twice; assert the FakeModel's encode call
   count does not grow across the second run beyond the label-matrix rebuild. **This is the test the
   entire document exists to make pass.**
8. `test_worker_merges_never_replaces` — a paper pre-loaded with `tags=["mine"]` and a manual
   collection id keeps both after a run.
9. `test_worker_without_model_still_tags` — with `_load_sentence_transformer` raising `ImportError`,
   `run()` completes, `len(store) == 0`, and papers gain keyword tags.

**done-when:** `py -3.12 -m pytest tests/test_categoriser.py -q` reports `9 passed`.

---

## Phase 9 — `ImportWorker`: store the vector it already computed

skill: coding-standards:python
model: sonnet

**Anchor:** `paperbase/core/importer.py`, three edits. Apply them **bottom-up in this order** so the
earlier line numbers stay valid.

**Edit 9c first — `importer.py:182-184` and `importer.py:201-202`, inside `_run_async`.**

Current state, verbatim:

```python
            if pending_index >= INDEX_COMMIT_INTERVAL:
                self._indexer.commit()
                pending_index = 0
```

Change to:

```python
            if pending_index >= INDEX_COMMIT_INTERVAL:
                self._indexer.commit()
                if self._vector_store is not None:
                    self._vector_store.flush()
                pending_index = 0
```

Current state, verbatim:

```python
        if pending_index:
            self._indexer.commit()
```

Change to:

```python
        if pending_index:
            self._indexer.commit()
        if self._vector_store is not None:
            self._vector_store.flush()
```

**Edit 9b — `importer.py:85-98`, method `_apply_categorisation`.**

Current state, verbatim:

```python
    def _apply_categorisation(self, paper: Paper) -> None:
        """Merge auto-categorisation results onto paper and update DB. No-op if not configured."""
        if self._categoriser is None or not self._categoriser.is_loaded:
            return
        try:
            col_ids, tags = self._categoriser.categorise_paper(paper, self._db)
```

Change to:

```python
    def _apply_categorisation(self, paper: Paper) -> None:
        """Merge auto-categorisation results onto paper, store its vector, update DB.

        The vector is computed here rather than inside categorise_paper so it can be
        persisted: re-running categorisation over the library must never re-embed.
        """
        if self._categoriser is None or not self._categoriser.is_loaded:
            return
        try:
            vector = self._categoriser.encode(paper_text(paper))
            col_ids, tags = self._categoriser.categorise_paper(paper, self._db, vector=vector)
```

and, after the existing `self._db.update_paper(paper)` line and outside the `if` that guards it, but
still inside the `try`, add:

```python
            if vector is not None and self._vector_store is not None and paper.id is not None:
                self._vector_store.add(paper.id, vector)
```

**Edit 9a — `importer.py:55-84`, `ImportWorker.__init__`.**

Current state, verbatim:

```python
        categoriser: Optional[EmbeddingCategoriser] = None,
        parent=None,
    ) -> None:
```

Change to:

```python
        categoriser: Optional[EmbeddingCategoriser] = None,
        vector_store: Optional[VectorStore] = None,
        parent=None,
    ) -> None:
```

Current state, verbatim:

```python
        self._categoriser = categoriser
        self._pause_requested = False
```

Change to:

```python
        self._categoriser = categoriser
        self._vector_store = vector_store
        self._pause_requested = False
```

Replace the import at `importer.py:22` with
`from paperbase.core.categoriser import EmbeddingCategoriser, paper_text`, and add
`from paperbase.core.vectors import VectorStore` after the `scraper` import at `importer.py:36`.

**Intent:** import already pays for one embedding per paper. Throwing it away is what makes the
retroactive run cost hours a second time.

**done-when:** `py -3.12 -c "import inspect; from paperbase.core.importer import ImportWorker; assert 'vector_store' in inspect.signature(ImportWorker.__init__).parameters; src=inspect.getsource(ImportWorker._apply_categorisation); assert 'self._vector_store.add' in src and 'vector=vector' in src; print('OK')"`
prints `OK`, and `py -3.12 -m pytest tests -q` still reports 0 failed.

---

## Phase 10 — `Settings`: taxonomy path and top-k

skill: coding-standards:python
model: sonnet

**Anchor:** `paperbase/ui/settings_dialog.py`, class `Settings`, lines 18 to 70. Three edits,
bottom-up: `load` (63-67), `save` (44-48), `__init__` (26-29).

`CLAUDE.md` names five places a settings field must appear. This phase does three; Phase 11 does the
dialog's two. The categoriser is not constructed by `ImportWorker`, so no worker plumbing is needed
for these fields.

**Edit 10c — `Settings.load`.** Current state, verbatim:

```python
                s.categories = data.get("categories", [])
                s.auto_categorise = data.get("auto_categorise", True)
                s.category_threshold = float(data.get("category_threshold", 0.35))
                s.tag_count = int(data.get("tag_count", 5))
```

Change to:

```python
                s.categories = data.get("categories", [])
                s.auto_categorise = data.get("auto_categorise", True)
                s.category_threshold = float(data.get("category_threshold", 0.35))
                s.tag_count = int(data.get("tag_count", 5))
                s.taxonomy_path = data.get("taxonomy_path", "")
                s.taxonomy_top_k = int(data.get("taxonomy_top_k", 4))
```

**Edit 10b — `Settings.save`.** Current state, verbatim:

```python
            "categories": self.categories,
            "auto_categorise": self.auto_categorise,
            "category_threshold": self.category_threshold,
            "tag_count": self.tag_count,
```

Change to:

```python
            "categories": self.categories,
            "auto_categorise": self.auto_categorise,
            "category_threshold": self.category_threshold,
            "tag_count": self.tag_count,
            "taxonomy_path": self.taxonomy_path,
            "taxonomy_top_k": self.taxonomy_top_k,
```

**Edit 10a — `Settings.__init__`.** Current state, verbatim:

```python
        self.category_threshold: float = 0.35  # min cosine similarity to assign a category
        self.tag_count: int = 5                # keywords to extract per paper
```

Change to:

```python
        self.category_threshold: float = 0.35  # min cosine similarity to assign a category
        self.tag_count: int = 5                # keywords to extract per paper
        self.taxonomy_path: str = ""           # blank means {library_root}/taxonomy.txt
        self.taxonomy_top_k: int = 4           # max taxonomy labels assigned per paper
```

**Also add a method to `Settings`**, immediately after `is_configured` (lines 33-34):

```python
    def taxonomy_file(self) -> Optional[Path]:
        """Resolved taxonomy file, or None when there is nowhere to put one yet."""
        if self.taxonomy_path:
            return Path(self.taxonomy_path)
        if self.library_root:
            return Path(self.library_root) / "taxonomy.txt"
        return None
```

**Also create `tests/test_settings.py`:** save a `Settings` with both new fields set, load it back,
assert both survive; assert `taxonomy_file()` falls back to `library_root/taxonomy.txt` when
`taxonomy_path` is blank, returns the explicit path when set, and returns `None` when both are
blank; assert a settings file written before this change (JSON with no `taxonomy_path` key) loads
with the defaults rather than raising.

**Intent:** one resolution rule for where the taxonomy lives, in one method, so the dialog, the
window and the seeding tool cannot disagree about it.

**done-when:** `py -3.12 -m pytest tests/test_settings.py -q` reports `4 passed`.

---

## Phase 11 — Settings dialog: the taxonomy row

skill: coding-standards:python, coding-standards:aesthetic
model: opus

**Anchor:** `paperbase/ui/settings_dialog.py`, class `SettingsDialog`. Two edits, bottom-up:
`_accept` (was 310-335) then `_build_ui`'s Auto-Categorisation group (was 161-230). Phase 10 adds
about six lines above both, so the quoted text is unchanged but the line numbers are lower than the
truth: search for the quotes.

**Edit 11b — `_accept`.** Current state, verbatim:

```python
        self._settings.category_threshold = self._threshold_spin.value()
        self._settings.tag_count = self._tag_count_spin.value()
```

Change to:

```python
        self._settings.category_threshold = self._threshold_spin.value()
        self._settings.tag_count = self._tag_count_spin.value()
        self._settings.taxonomy_path = self._taxonomy_edit.text().strip()
        self._settings.taxonomy_top_k = self._top_k_spin.value()
```

**Edit 11a — `_build_ui`, inside the Auto-Categorisation group.** Current state, verbatim:

```python
        self._tag_count_spin = QSpinBox()
        self._tag_count_spin.setRange(1, 20)
        self._tag_count_spin.setValue(self._settings.tag_count)
        cat_form.addRow("Keywords per paper:", self._tag_count_spin)

        cat_layout.addLayout(cat_form)
```

Between the tag-count row and `cat_layout.addLayout(cat_form)`, add:

- `self._taxonomy_edit = QLineEdit(self._settings.taxonomy_path)` with placeholder text naming the
  default location in words rather than a raw token, plus a `Browse…` button wired to a new
  `_browse_taxonomy` using `QFileDialog.getOpenFileName` filtered to `Text files (*.txt)`, laid out
  in a `QWidget` + `QHBoxLayout(0 margins)` exactly as the existing `root_row` and `sec_row` do. Row
  label: `"Taxonomy file:"`.
- `self._top_k_spin = QSpinBox()` ranged 1 to 10, valued from `self._settings.taxonomy_top_k`, row
  label `"Labels per paper:"`, tooltip explaining it is the maximum number of taxonomy labels a
  single paper can be assigned.
- A `QLabel` with `setObjectName("FieldNote")` and `setWordWrap(True)` stating in one or two
  sentences that the taxonomy is a plain text file of one label per line with an optional
  `Name: description`, that `tools/seed_taxonomy.py` generates a draft, and how many labels the
  current file holds. Compute the count with `load_taxonomy(self._settings.taxonomy_file())` inside
  a `try/except TaxonomyError`, and when the file is missing say so plainly rather than showing `0`.

Add `_browse_taxonomy` beside `_browse_root` and `_browse_secondary_dest`, matching their shape.

Imports to add: `from paperbase.core.taxonomy import TaxonomyError, load_taxonomy`.

**Aesthetic requirements** (`coding-standards:aesthetic` is loaded for this phase; tokens live in
`paperbase/ui/theme.py` and the group already carries `accent="lime"`):

- No hardcoded colour anywhere in this edit.
- The two new rows join the **form**, above `cat_layout.addLayout(cat_form)`, so the group keeps one
  rhythm rather than growing a second cluster.
- The note about the taxonomy file sits next to the field it describes, not appended after the
  category table, or it reads as a footnote to the wrong control.
- The state that matters here is "no taxonomy file yet", which is what every fresh install has. Say
  what to do about it, in a sentence, without an alarm colour.
- Re-read the whole group after the edit: it now configures two different mechanisms (a fixed
  taxonomy and free-text categories). If the seam between them is not legible, add the minimum
  separation that makes it so rather than leaving two interleaved concerns.

**Intent:** the taxonomy is the thing the user maintains by hand, so the one screen that mentions it
has to say where it is, how many labels it has, and how to make one.

**done-when:** `py -3.12 -c "import os; os.environ['QT_QPA_PLATFORM']='offscreen'; from PyQt6.QtWidgets import QApplication; from paperbase.ui import theme; from paperbase.ui.settings_dialog import Settings, SettingsDialog; app=QApplication([]); theme.apply_theme(app); s=Settings(); s.library_root=os.getcwd(); d=SettingsDialog(s); d._taxonomy_edit.setText('x.txt'); d._top_k_spin.setValue(6); d._accept(); assert s.taxonomy_path=='x.txt' and s.taxonomy_top_k==6; print('OK')"`
prints `OK`. Then open the dialog for real (`py -3.12 -m paperbase.main` against a fixture data
directory) and look at the group before reporting the phase done.

---

## Phase 12 — Categorisation dialog: two stages, and a rebuild that means something

skill: coding-standards:python, coding-standards:aesthetic
model: opus

**Anchor:** `paperbase/ui/categorisation_dialog.py`, whole file (181 lines).

**Current state**, keyed by these verbatim fragments:

```python
    def __init__(
        self,
        db: Database,
        categoriser: EmbeddingCategoriser,
        state_file: Path,
        parent: Optional[QWidget] = None,
    ) -> None:
```

```python
        self._worker = CategorizationWorker(
            db=self._db,
            categoriser=self._categoriser,
            state_file=self._state_file,
            parent=self,
        )
```

```python
    def _reset_state(self) -> None:
        if self._state_file.exists():
            self._state_file.unlink()
        self._log.appendPlainText(
            "Progress reset. All papers will be re-categorised on the next run."
        )
```

**Change:**

- The constructor takes `store: VectorStore` in place of `state_file: Path`, stored as
  `self._store`. Import `from paperbase.core.vectors import VectorStore`.
- The worker construction passes `store=self._store` and drops `state_file`.
- Connect `self._worker.stage_changed` to a new `@pyqtSlot(str) def _on_stage(self, stage: str)`.
- `_reset_state` becomes `_rebuild_vectors`: the button reads `Rebuild vectors`, and because it now
  discards hours of work rather than a JSON file it asks first with a `QMessageBox.question` naming
  the cost in the user's terms (how many vectors are stored, and that rebuilding re-reads every
  paper). On confirmation it calls `self._store.clear()` and logs what happened. It stays disabled
  during a run, as it already does.
- The progress bar now drives two stages that differ by three orders of magnitude in duration.
  `_on_stage` must make that legible: the status line says which stage is running in words, and the
  embedding stage is the one that carries the ETA. A bar that crawls for an hour and then jumps to
  100% in a second must not read as a stall.
- The idle state currently says nothing about what is about to happen. Give it one sentence naming
  the two stages and roughly what the first run costs, since the first run is the hours-long one and
  the user deserves that before pressing Start rather than after.

**Aesthetic requirements** (`coding-standards:aesthetic` is loaded; the dialog is lime throughout and
already uses `GlassPanel`, `accent_glow` and the 20px canvas measure):

- Keep the two-island layout and the lime accent. No new hue.
- No hardcoded colours; `theme` constants or object-name selectors only.
- The glow on the progress bar is the running state and is off at rest. Hold that rule across both
  stages, including the transition between them.
- Every state this dialog can be in (idle, embedding, assigning, paused, stopped, complete, no
  labels configured, model missing) must say something true in the status line. "Model missing" is
  new: the worker now continues without it and produces keywords only, so the dialog has to say that
  rather than implying a full run happened.

**Intent:** the retroactive run stopped being one uniform loop. A dialog that still presents it as
one lies about what it is doing for the first hour.

**done-when:** `py -3.12 -c "import os; os.environ['QT_QPA_PLATFORM']='offscreen'; import inspect; from PyQt6.QtWidgets import QApplication; from paperbase.ui import theme; from paperbase.ui.categorisation_dialog import CategorizationDialog as D; app=QApplication([]); theme.apply_theme(app); p=list(inspect.signature(D.__init__).parameters); assert 'store' in p and 'state_file' not in p, p; assert hasattr(D,'_on_stage'); print('OK')"`
prints `OK`. Then run the dialog against a fixture data directory, press Start with no model
installed, and read what the status line actually says before reporting the phase done.

---

## Phase 13 — Wire the vector store through the application

skill: coding-standards:python
model: sonnet

**Anchor:** eight edits across `paperbase/main.py`, `paperbase/ui/main_window.py` and
`paperbase/ui/import_dialog.py`. Mechanical; every one is named.

**13a — `paperbase/main.py:183-186`.** Current state, verbatim:

```python
    indexer = Indexer(index_dir)
    indexer.open()

    window = MainWindow(db, indexer, settings, settings_path)
```

Change to:

```python
    indexer = Indexer(index_dir)
    indexer.open()

    vector_store = VectorStore(data / "paper_vectors.f32")
    vector_store.open()

    window = MainWindow(db, indexer, settings, settings_path, vector_store)
```

Add `from paperbase.core.vectors import VectorStore` to the local import block at `main.py:20-25`.
At `main.py:198-199`, current state:

```python
    indexer.close()
    db.close()
```

change to:

```python
    vector_store.close()
    indexer.close()
    db.close()
```

**13b — `main_window.py:26-33`, `MainWindow.__init__` signature.** Current state, verbatim:

```python
        settings: Settings,
        settings_path: Path,
        parent: Optional[QWidget] = None,
    ) -> None:
```

Change to:

```python
        settings: Settings,
        settings_path: Path,
        vector_store: VectorStore,
        parent: Optional[QWidget] = None,
    ) -> None:
```

Add `self._vector_store = vector_store` beside `self._settings_path = settings_path` (line 38), and
`from paperbase.core.vectors import VectorStore` to the imports.

**13c — `main_window.py:42-47`, the categoriser construction.** Current state, verbatim:

```python
        self._categoriser = EmbeddingCategoriser()
        self._categoriser.update_settings(
            categories=settings.categories,
            threshold=settings.category_threshold,
            tag_count=settings.tag_count,
        )
```

Change to:

```python
        self._categoriser = EmbeddingCategoriser()
        self._apply_categoriser_settings()
```

and add a new method beside `_preload_categoriser`:

```python
    def _apply_categoriser_settings(self) -> None:
        """Push settings plus the taxonomy file onto the categoriser. One place, two callers."""
        try:
            labels = load_taxonomy(self._settings.taxonomy_file())
        except TaxonomyError as e:
            logger.warning("Taxonomy could not be read: %s", e)
            labels = []
        self._categoriser.update_settings(
            categories=self._settings.categories,
            threshold=self._settings.category_threshold,
            tag_count=self._settings.tag_count,
            labels=labels,
            top_k=self._settings.taxonomy_top_k,
        )
```

`main_window.py` has no logger today: add `import logging` and
`logger = logging.getLogger(__name__)` at module level, plus
`from paperbase.core.taxonomy import TaxonomyError, load_taxonomy`.

**13d — `main_window.py:68-70`, `_preload_categoriser`.** Current state, verbatim:

```python
    def _preload_categoriser(self) -> None:
        if self._settings.auto_categorise and self._settings.categories:
            threading.Thread(target=self._categoriser.load_model, daemon=True).start()
```

Change the condition to `if self._settings.auto_categorise and self._categoriser.has_categories:` so
a library configured with a taxonomy and no free-text categories still preloads.

**13e — `main_window.py:222-231`, the `ImportDialog` construction.** Current state, verbatim:

```python
                state_file=state_file,
                categoriser=self._categoriser,
                parent=self,
            )
```

Change to:

```python
                state_file=state_file,
                categoriser=self._categoriser,
                vector_store=self._vector_store,
                parent=self,
            )
```

**13f — `main_window.py:245-253`, the `CategorizationDialog` construction.** Current state, verbatim:

```python
        if self._cat_dialog is None:
            state_file = Path(self._settings.library_root) / "categorisation_state.json"
            self._cat_dialog = CategorizationDialog(
                db=self._db,
                categoriser=self._categoriser,
                state_file=state_file,
                parent=self,
            )
```

Change to:

```python
        if self._cat_dialog is None:
            self._cat_dialog = CategorizationDialog(
                db=self._db,
                categoriser=self._categoriser,
                store=self._vector_store,
                parent=self,
            )
```

**13g — `main_window.py:262-266`, inside `_open_settings`.** Current state, verbatim:

```python
            self._categoriser.update_settings(
                categories=self._settings.categories,
                threshold=self._settings.category_threshold,
                tag_count=self._settings.tag_count,
            )
```

Change to `self._apply_categoriser_settings()`. The comment about preloading and the two dialog
invalidations below it stay exactly as they are: `CLAUDE.md` records that resetting `_import_dialog`
and `_cat_dialog` here is deliberate.

**13h — `paperbase/ui/import_dialog.py`.** Add `vector_store: Optional[VectorStore] = None` to
`ImportDialog.__init__` after `categoriser` (line 33), store it as `self._vector_store`, add
`from paperbase.core.vectors import VectorStore` to the imports, and pass
`vector_store=self._vector_store` in the `ImportWorker(...)` call (lines 226-237), immediately after
the `categoriser=` argument.

**Intent:** one `VectorStore` instance for the process, owned by `main()` beside the database and the
index, injected everywhere it is needed. No module-level singleton.

**done-when:** `py -3.12 -c "import os; os.environ['QT_QPA_PLATFORM']='offscreen'; import inspect; from PyQt6.QtWidgets import QApplication; from paperbase.ui import theme; from paperbase.ui.main_window import MainWindow; from paperbase.ui.import_dialog import ImportDialog; app=QApplication([]); theme.apply_theme(app); assert 'vector_store' in inspect.signature(MainWindow.__init__).parameters; assert 'vector_store' in inspect.signature(ImportDialog.__init__).parameters; assert hasattr(MainWindow,'_apply_categoriser_settings'); print('OK')"`
prints `OK`, and `py -3.12 -m pytest tests -q` still reports 0 failed.

---

## Phase 14 — `tools/seed_taxonomy.py`

skill: coding-standards:python
model: sonnet

**Anchor:** new file `tools/seed_taxonomy.py`; new file `tests/test_seed_taxonomy.py`.

**Intent:** writing 200 labels from a blank page is what stops this feature being used. Deriving a
draft from the tags, keywords and journals already in the library turns it into an editing job. It
runs on the target machine, where the data is, so it must need nothing but the database.

**Change:** create `tools/seed_taxonomy.py`, modelled on `tools/make_fixture.py`: same
`sys.path.insert` preamble, same `main() -> int` plus `raise SystemExit(main())` shape, same
`argparse` style, docstring showing the invocation.

```python
def main(argv: Optional[list[str]] = None) -> int: ...
```

Arguments: `--db` (Path, required), `--out` (Path, required), `--min-count` (int, default 3),
`--max-labels` (int, default 300), `--force` (flag).

Behaviour:

- Open the DB read-only: `sqlite3.connect(f"file:{path}?mode=ro", uri=True)`. This tool only reads,
  so it takes only read permission.
- Count candidates with parameterised queries over `json_each`, following the pattern already in
  `db.get_all_tags`: tags from `papers.tags`, keywords from `papers.keywords`. Merge the two counts
  case-insensitively, keeping the most frequent spelling of each. Do not import the categoriser; no
  model is involved.
- Drop candidates shorter than 3 characters, purely numeric ones, and anything over
  `taxonomy.MAX_NAME`.
- Keep candidates with `count >= min_count`, sorted by count descending then name ascending,
  truncated to `max_labels`.
- Write with `taxonomy.save_taxonomy`, so the output parses through `load_taxonomy` by construction.
  Labels get no description; the user writes those.
- Append a commented block listing the top 40 journals by paper count as raw material. They are
  venues rather than topics, so they are comments and never labels.
- Print a summary to stdout: candidates considered, labels written, output path.
- Refuse to overwrite an existing `--out` unless `--force` is passed, returning non-zero. This file
  is hand-edited and losing an afternoon of it to a re-run is unacceptable.

**Also create `tests/test_seed_taxonomy.py`:** build a temp DB via the `db` fixture with papers whose
`tags` and `keywords` give known counts, run `main(["--db", ..., "--out", ...])`, then assert the
output parses through `load_taxonomy`; that a label seen 5 times is present and one seen twice is
absent at `--min-count 3`; that `--max-labels 2` truncates; that a second run without `--force`
returns non-zero and leaves the file untouched; and that journals appear only as comment lines.

**done-when:** `py -3.12 -m pytest tests/test_seed_taxonomy.py -q` reports `5 passed`.

---

## Phase 15 — Fixture: abstracts worth embedding

skill: coding-standards:python
model: sonnet

**Anchor:** `tools/make_fixture.py`, three edits, bottom-up: the settings dict (100-113), the `Paper`
construction (67-90), then the word lists (24-38).

**15c — the settings dict.** Current state, verbatim:

```python
        "categories": [],
        "auto_categorise": False,
        "category_threshold": 0.35,
        "tag_count": 5,
    }
```

Change to:

```python
        "categories": [],
        "auto_categorise": False,
        "category_threshold": 0.35,
        "tag_count": 5,
        "taxonomy_path": "",
        "taxonomy_top_k": 4,
        "reduce_motion": False,
    }
```

(`reduce_motion` is missing from the fixture today and every real settings file has it.)

Also, after `(out / "library").mkdir(exist_ok=True)`, write `out / "library" / "taxonomy.txt"`
through `paperbase.core.taxonomy.save_taxonomy` with 12 labels drawn from the fixture's own subject
vocabulary, some with descriptions and some without, so a fixture run exercises both shapes.

**15b — the `Paper` construction.** Current state, verbatim:

```python
            abstract=" ".join(rng.choices(WORDS, k=60)),
```

Change to `abstract=_abstract(rng),`.

**15a — the word lists.** Add an `_abstract(rng)` helper beside `_title`, building a three or four
sentence abstract from a handful of sentence templates with slots for organisms, places, techniques
and epochs. It has to read like prose, not a word bag: `yake` scores by word position and
co-occurrence, so the current 60-word soup produces meaningless keywords and the fixture cannot
exercise the keywording path at all. Add the small vocabulary lists the templates need (organisms,
places, techniques) beside `WORDS`.

**Intent:** the fixture is the only library this machine has. If its abstracts are noise, every
manual check of assignment and keywording is meaningless.

**done-when:** `py -3.12 tools/make_fixture.py --papers 200 --out %TEMP%\pb_plan_fix` exits 0, then
`py -3.12 -c "import os,sys; from pathlib import Path; from paperbase.core.db import Database; from paperbase.core.taxonomy import load_taxonomy; from paperbase.core.keywords import extract_keywords; d=Path(os.environ['TEMP'])/'pb_plan_fix'; db=Database(d/'paperbase.db'); db.open(); p=db.get_paper(1); kw=extract_keywords(p.abstract); print(len(load_taxonomy(d/'library'/'taxonomy.txt')), kw); assert len(kw)>=3; db.close()"`
prints 12 and at least three plausible multi-word phrases.

---

## Phase 16 — End-to-end test of the whole pipeline

skill: coding-standards:python
model: sonnet

**Anchor:** new file `tests/test_end_to_end.py`.

**Intent:** every prior phase tested one piece. This runs a library through the assembled pipeline,
and it is the closest this machine can get to the real thing.

**Change:** create `tests/test_end_to_end.py`. Reuse the `FakeModel` and monkeypatch seam from
`tests/test_categoriser.py`: either import it from there or lift it into `conftest.py` in this phase
and have both files use it. One definition, either way.

One test, `test_full_pipeline`:

1. Build a temp data directory with a `Database` of 40 papers carrying real-prose abstracts (reuse
   the `_abstract` generator from `tools/make_fixture.py` or an equivalent local one).
2. Write a taxonomy file of 6 labels with `save_taxonomy`.
3. Build `Settings` pointing `taxonomy_path` at it, load labels with `load_taxonomy`, construct
   `EmbeddingCategoriser`, call `update_settings(..., labels=labels, top_k=3)`, patch the model
   factory, `load_model()`.
4. Construct `VectorStore` in the temp directory and `CategorizationWorker(db, cat, store)`; call
   `run()`.
5. Assert: `len(store) == 40`; every paper has at least one tag; no paper has more than 3 taxonomy
   collections; collections named after the taxonomy labels exist in the DB.
6. Re-open the store from disk in a fresh `VectorStore`: `len() == 40` and a spot vector round-trips.
7. Reset the FakeModel's call counter, run a second worker, assert **zero** paper embeddings in the
   second run.
8. Add one new paper, run a third time, assert exactly one new embedding and `len(store) == 41`.

**done-when:** `py -3.12 -m pytest tests/test_end_to_end.py -q` reports `1 passed`, and
`py -3.12 -m pytest tests -q` reports 0 failed across all test files.

---

## Phase 17 — Update the design document

skill: none
model: sonnet

**Anchor:** `docs/auto-categorisation.md:3`, plus its sections 4 and 5.

**Current state**, verbatim, at line 3:

```
Exploration document. Nothing here is built yet. This replaces the earlier RAG and cloud
documents, both dropped.
```

**Change:** replace that opening with a statement that the vector store, taxonomy assignment and
YAKE keywording are implemented, that what remains is the first backfill run on the target machine
and writing the taxonomy itself, and keep the sentence about superseding the RAG and cloud
documents. In section 4 ("What changes in the code"), replace the forward-looking framing with what
the code now does, naming the modules that exist (`core/vectors.py`, `core/taxonomy.py`,
`core/assign.py`, `core/keywords.py`) and recording that `paper_vectors.f32` plus its JSON sidecar
replaced the proposed `.npy`, and why. In section 5 ("Order"), strike the completed items and leave
what remains: run the backfill, seed and edit the taxonomy. Section 6's two open questions stay.

Do not turn the document into a changelog of this run. State what is true now.

**Intent:** the document's first line claims nothing is built. After this run that is the first thing
a reader is told, and it is false.

**done-when:** `py -3.12 -c "import pathlib,sys; sys.exit('stale opening' if 'Nothing here is built yet' in pathlib.Path('docs/auto-categorisation.md').read_text(encoding='utf-8') else 0)"`
exits 0, and the document reads coherently top to bottom.

---

## Not in this plan, deliberately

- **The abstract-extraction stage** ("extract title and abstract where missing from PDF text", 1 to
  3 hours in the doc's cost table). It cannot be exercised here: real publisher PDFs are what it has
  to survive, and this machine has none.
- **The first backfill run.** It needs the 150,000 papers and `sentence-transformers` installed.
- **Writing the taxonomy.** `tools/seed_taxonomy.py` produces the draft on the target machine.
- **Folding anything into `CLAUDE.md`.** That is the review session's job, per `agentic.md`. The
  facts worth folding: the vector store's format and location, that `categorisation_state.json` is
  gone, that the categoriser's model factory is a monkeypatch seam, and that `tests/` now exists and
  how it runs without the embedding model.
