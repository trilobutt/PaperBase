# PaperBase — CLAUDE.md

## Project Overview

PaperBase is a Windows desktop application for managing a library of ~130,000 academic paper PDFs.
Replaces Zotero + ZotMoov + DocFetcher with a single application providing:
full-text search (Tantivy BM25), DOI/ISBN extraction, Crossref/Open Library/Unpaywall metadata,
landing page scraping, and file organisation into a configurable folder hierarchy.
Metadata stored in a flat SQLite schema (no normalised tables — hard architectural constraint).

**Platform:** Windows 10/11 only. Python 3.12 exactly (`py -3.12`).

---

## NEVER

- Never add normalised tables (authors, journals, keywords, or any repeating string). Every field is stored flat on the `papers` row or as a JSON array. Metadata editing must be a single `UPDATE papers SET field=? WHERE id=?` with no joins.
- Never add BibTeX, RIS, or citation export.
- Never add a built-in PDF viewer — use `os.startfile(path)`.
- Never add cloud sync, web server, or any network-facing interface.
- Never use Sci-Hub or any OA source other than Unpaywall.
- Never add citation graph, reference parsing, or related-paper discovery.
- Never use `os.path`; use `pathlib.Path` throughout.
- Never call UI methods from worker threads; use Qt signals exclusively.
- Never add Linux/macOS support.
- Never add new dependencies without asking first.

---

## Commands

```
# Install and run
pip install -e .
py -3.12 -m paperbase.main

# Smoke-test after any import or dataclass change
py -3.12 -c "from paperbase.xxx import yyy; print('OK')"

# Tests: needs neither sentence-transformers nor a display (see Dependencies)
py -3.12 -m pytest tests

# Behavioural checks for the import path
py -3.12 tools/check_hash_schema.py      # content_hash column, queries, migration
py -3.12 tools/check_hash_indexes.py     # both late-column indexes, fresh and migrated
py -3.12 tools/check_metadata_read.py    # read_pdf / extract_doi / extract_isbn / sha256
py -3.12 tools/check_clients.py          # no module builds its own AsyncClient
py -3.12 tools/check_phase5.py           # the no-await-between-claim-and-insert invariant
py -3.12 tools/check_batch_encode.py     # one encode per batch, against a fake model
py -3.12 tools/check_import_flush.py     # concurrent run, flush order, all three dedupe keys
py -3.12 tools/check_backfill.py         # HashBackfillWorker over readable and missing files

# Throughput, against the numbers in tasks/bench-baseline.txt
py -3.12 tools/bench_network.py          # live Crossref; ~110ms/DOI pooled
py -3.12 tools/bench_import.py --papers 60   # network stubbed; ~76ms/paper

# Draft a taxonomy from the library's tags and keywords (read-only; refuses to overwrite)
py -3.12 tools/seed_taxonomy.py --db <paperbase.db> --out <library_root>/taxonomy.txt

# Debug DB directly (replace DOI as needed)
py -3.12 -c "import sqlite3; from pathlib import Path; from platformdirs import user_data_dir; conn = sqlite3.connect(str(Path(user_data_dir('PaperBase','PaperBase'))/'paperbase.db')); conn.row_factory = sqlite3.Row; print(dict(conn.execute('SELECT id,title,needs_review,file_path FROM papers WHERE doi=?',('10.xxxx/yyy',)).fetchone()))"
```

**Runtime data dir:** `%LOCALAPPDATA%\PaperBase\PaperBase\` — `paperbase.db`, `index/`, `settings.json`,
`paper_vectors.f32` + `paper_vectors.json` (the vector store; see Auto-Categorisation).
`PAPERBASE_DATA_DIR` overrides it (must be an absolute path, else `SystemExit`); `tools/make_fixture.py` builds a throwaway one to test against.
`import_state.json` and the default `taxonomy.txt` live at `{library_root}/` (alongside PDFs, not in app data dir).

---

## Technology Stack

| Layer | Choice | Notes |
|---|---|---|
| UI | PyQt6 | Native Qt6, no Electron |
| Full-text search | `tantivy` (tantivy-py) | Rust BM25, pre-built Windows wheel, no JVM |
| PDF text extraction | `PyMuPDF` (fitz) | XMP metadata + page text |
| Database | SQLite (`sqlite3` / `aiosqlite`) | Flat schema, no ORM |
| HTTP client | `httpx` (async) | Crossref, Unpaywall, scraping |
| Qt/asyncio bridge | `qasync` | Main-thread asyncio loop |
| Embedding | `sentence-transformers` `all-MiniLM-L6-v2` + `numpy` | CPU-only, ~23 MB |
| Keywords | `yake` | Statistical, no model |

---

## Directory Structure

```
paperbase/
├── main.py
├── ui/
│   ├── main_window.py            # QMainWindow; three-panel layout
│   ├── search_panel.py           # Search bar, filters, QTableView + PaperTableModel
│   ├── paper_detail.py           # Editable metadata fields, tag chips
│   ├── import_dialog.py          # Batch import: drop PDFs / paste DOIs / paste URLs
│   ├── collection_tree.py        # Left panel: hierarchical QTreeView
│   ├── settings_dialog.py        # Library root, folder pattern, user email
│   ├── categorisation_dialog.py  # Progress dialog for retroactive categorisation
│   └── backfill_dialog.py        # Fingerprint scan progress + duplicate report
├── core/
│   ├── db.py          # SQLite schema + all CRUD; no ORM
│   ├── indexer.py     # Tantivy: build, incremental update, search
│   ├── metadata.py    # DOI extraction from PDF text; Crossref + book metadata lookup
│   ├── scraper.py     # Landing page: Highwire/DC/JSON-LD/OG meta scraping
│   ├── downloader.py  # Unpaywall lookup + PDF download
│   ├── organiser.py   # File copy/move per naming pattern; compute_destination
│   ├── importer.py    # ImportWorker(QThread): orchestrates all pipelines
│   ├── categoriser.py # EmbeddingCategoriser + CategorizationWorker
│   ├── vectors.py     # VectorStore: persistent paper embeddings
│   ├── taxonomy.py    # Label; parse/load/save the hand-edited taxonomy file
│   ├── assign.py      # top_labels: chunked cosine assignment, pure numpy
│   ├── keywords.py    # extract_keywords: YAKE plus stop-token and overlap filters
│   ├── backfill.py    # HashBackfillWorker: fills content_hash on pre-hash rows
│   └── llm.py         # Dead code — thin adapter over EmbeddingCategoriser, not imported anywhere
└── models/
    ├── paper.py
    ├── collection.py
    └── search_result.py
```

---

## SQLite Schema

**Hard constraint: NO normalised tables for authors, journals, keywords, or any repeating string.**

```sql
CREATE TABLE IF NOT EXISTS papers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    doi             TEXT UNIQUE,
    title           TEXT NOT NULL DEFAULT '',
    authors         TEXT NOT NULL DEFAULT '[]',  -- JSON array: ["Lastname, Firstname", ...]
    journal         TEXT NOT NULL DEFAULT '',     -- publisher name for books
    year            INTEGER,
    volume          TEXT NOT NULL DEFAULT '',
    issue           TEXT NOT NULL DEFAULT '',
    pages           TEXT NOT NULL DEFAULT '',
    abstract        TEXT NOT NULL DEFAULT '',
    keywords        TEXT NOT NULL DEFAULT '[]',
    tags            TEXT NOT NULL DEFAULT '[]',
    collection_ids  TEXT NOT NULL DEFAULT '[]',  -- JSON array of int collection IDs
    file_path       TEXT NOT NULL UNIQUE,
    date_added      TEXT NOT NULL,               -- ISO8601 UTC
    date_modified   TEXT NOT NULL,
    metadata_source TEXT NOT NULL DEFAULT 'unknown',
    needs_review    INTEGER NOT NULL DEFAULT 0,
    open_access     INTEGER NOT NULL DEFAULT 0,
    isbn            TEXT,                        -- ISBN-13 preferred; populated for books
    document_type   TEXT NOT NULL DEFAULT 'article',
    content_hash    TEXT                         -- SHA-256 of PDF bytes; NULL pre-backfill
);

CREATE TABLE IF NOT EXISTS collections (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    name      TEXT NOT NULL,
    parent_id INTEGER REFERENCES collections(id) ON DELETE SET NULL,
    UNIQUE(name, parent_id)
);

CREATE INDEX IF NOT EXISTS idx_papers_doi          ON papers(doi);
CREATE INDEX IF NOT EXISTS idx_papers_year         ON papers(year);
CREATE INDEX IF NOT EXISTS idx_papers_title        ON papers(title);
CREATE INDEX IF NOT EXISTS idx_papers_needs_review ON papers(needs_review);
```

`idx_papers_content_hash` and `idx_papers_isbn` are created in `_migrate`, never in
`SCHEMA_SQL`. `open()` runs `executescript(SCHEMA_SQL)` before `_migrate`, so against a
database predating a column the `CREATE INDEX` aborts `open()` with `no such column` and the
`ALTER TABLE` that would have fixed it never runs. An index on a late-added column belongs
after its `ALTER` and outside the `try/except sqlite3.OperationalError` wrapping the `ALTER`s:
a failed `ALTER` is the expected no-op that guard exists for, while a failed `CREATE INDEX
IF NOT EXISTS` on a column known to exist is a real fault worth raising.

`metadata_source` values: `"crossref"` | `"openlibrary"` | `"googlebooks"` | `"xmp"` | `"manual"` | `"filename"`.
`document_type` values: `"article"` | `"book"` | `"book-chapter"` | `"proceedings"`.

---

## Data Models

```python
@dataclass
class Paper:
    id: Optional[int]
    doi: Optional[str]
    title: str
    authors: list[str]       # ["Lastname, Firstname", ...]
    journal: str             # publisher name when document_type == "book"
    year: Optional[int]
    volume: str
    issue: str
    pages: str
    abstract: str
    keywords: list[str]
    tags: list[str]
    collection_ids: list[int]
    file_path: str
    date_added: str          # ISO8601 UTC
    date_modified: str
    metadata_source: str
    needs_review: bool
    open_access: bool
    isbn: Optional[str] = None        # keyword default — keeps construction sites without it valid
    document_type: str = 'article'
    content_hash: Optional[str] = None   # SHA-256 of the PDF bytes; None on pre-backfill rows

@dataclass
class SearchResult:
    paper_id: int
    title: str
    authors: list[str]
    journal: str
    year: Optional[int]
    snippet: str   # Tantivy-generated excerpt with search terms highlighted
    score: float
```

New fields must be keyword arguments with defaults so existing `Paper(...)` call sites keep working.
Extend schema via `ALTER TABLE papers ADD COLUMN ...` in `_migrate()` wrapped in `try/except sqlite3.OperationalError`.

---

## Full-Text Search (Tantivy)

Index fields: `paper_id` (stored, indexed int), `title` (stored text), `abstract`/`authors`/`keywords`/`fulltext` (not stored text), `year` (stored, indexed int).

`fulltext` is not stored; only `title` is available from the index. All other display metadata fetched from SQLite by `paper_id`.

Query syntax: `"exact phrase"`, `field:term` (fields: title, abstract, authors, keywords, fulltext), `AND`/`OR`/`NOT`, `+required -excluded`, `year:[2020 TO 2023]`, `word*`/`*word`/`wo*rd`. Invalid syntax falls back to whole-input phrase search.

Results uncapped (`searcher.num_docs` as limit). Scores normalised 0–100 against top hit; blank column when no query is active.

**An empty index must never reach `searcher.search`:** tantivy's top-score collector asserts its limit is non-zero and a limit of 0 raises a Rust panic that takes the process down. `Indexer.search` returns `[]` when `num_docs == 0`, which is the ordinary first-run state.

**Wildcards (`*`):** Tantivy's `parse_query` has no native wildcard support — it silently drops `*` from terms. Any query containing `*` is instead routed through `Indexer._parse_wildcard_query`/`_build_clause_query` (`core/indexer.py`), which tokenises the string (respecting quoted phrases and `field:[range]` tokens), converts each `*`-containing clause to an anchored regex via `tantivy.Query.regex_query`, and recombines clauses with `tantivy.Query.boolean_query` using the same `AND`/`OR`/`NOT`/`+`/`-` semantics. Queries without `*` are unaffected — they still go straight through `parse_query`.

All text fields use the `en_stem` tokenizer, so `regex_query` matches against **stemmed, lowercased** terms, not the raw word. Trailing wildcards (`word*`) are reliable since stemming only ever removes a suffix — a literal prefix always still matches. Leading/infix wildcards (`*word`, `wo*rd`) are correctly implemented but can miss matches where the wildcard's fixed text spans a suffix the stemmer would otherwise strip (e.g. `*genesis` won't match "biogenesis", because `en_stem` indexes it as `biogenesi`). This is a property of the stemmed index, not a parser bug — do not try to "fix" it by switching tokenizers.

---

## DOI Extraction Pipeline (`core/metadata.py`)

**One PDF read per file.** `read_pdf(path) -> PdfText` opens the file once and returns
`pages: list[str]`, `xmp: dict`, `sha256: str` and `ok: bool`, with `head` (first 3 pages),
`first_page` and `fulltext` as properties. Every extraction step takes that `PdfText`; nothing
in the import path opens a PDF a second time. `read_pdf` never raises — an unreadable file
comes back `ok=False` with an empty hash. It is blocking, so callers reach it through
`asyncio.to_thread` (PyMuPDF releases the GIL, so the other in-flight items keep moving).
`sha256_file(path)` streams the same digest in 1 MB chunks for the Settings fingerprint scan,
so a paper's hash never depends on which entry point computed it.

**`extract_doi(pdf)`:** Regex `r'\b(10\.\d{4,9}/[^\s"<>{|}\\^[\]`]+)'` across the first 100 lines of `pdf.head`, then all of
`pdf.first_page`, then `pdf.xmp` subject/keywords. Strips trailing `.,;)`.
`extract_isbn(pdf)` searches `pdf.head`, ISBN-13 preferred.

**Every network function takes the caller's `httpx.AsyncClient`** as its last argument and
builds none of its own: `resolve_metadata(doi, user_email, rate_limiter, client)`,
`guess_metadata(path, pdf, user_email, rate_limiter, client)`,
`resolve_book_metadata(isbn, rate_limiter, client)` (no `user_email` — neither book source
uses one), `_crossref_bib_search(title, user_email, rate_limiter, client)`. A fresh client per
request pays a TLS handshake every call: 627ms per DOI measured, against 152ms reused.

**`resolve_metadata`:** GET `https://api.crossref.org/works/{doi}` with polite-pool
User-Agent (`mailto:` included). Crossref legitimately returns `title: []`/`author: []` for
some valid DOIs — these set `needs_review=True`. Exponential retry on 429, max 3 attempts.

**`guess_metadata`:** Candidate title = longest line >=20 chars in the first 20 lines of
`pdf.first_page`. Queries Crossref bibliographic search; accepts the result if
`difflib.SequenceMatcher` ratio >=0.75. Falls back to XMP metadata, then filename — both set
`needs_review=True`. `path` is still needed for `file_path` and the filename fallback.

**Book metadata:** `journal` stores publisher name. Lookup order: Open Library → Google Books
(both free, no API key). Import pipeline: DOI → ISBN → `resolve_book_metadata` →
`guess_metadata`.

---

## Landing Page Scraper (`core/scraper.py`)

```python
@dataclass
class ScrapeResult:
    doi:            Optional[str]
    pdf_url:        Optional[str]
    is_open_access: bool
    metadata:       Optional[Paper]  # fallback only if Crossref fails; always try Crossref first
    source_url:     str
```

`scrape_landing_page(url, client)` extracts metadata in priority order:

1. **Highwire Press tags** (`citation_doi`, `citation_pdf_url`, `citation_title`, `citation_author`, `citation_journal_title`, `citation_publication_date`, `citation_volume`, `citation_issue`, `citation_firstpage`/`citation_lastpage`, `citation_abstract`, `citation_keywords`) — covers Springer, Nature, Elsevier, Wiley, OUP, CUP, PLOS, PMC, arXiv, bioRxiv, ACS, RSC, IEEE. `citation_pdf_url` presence sets `is_open_access=True`.
2. **Dublin Core** (`DC.identifier` → doi, `DC.title`, `DC.creator`, `DC.source` → journal, `DC.date`) — institutional repos, OJS.
3. **JSON-LD** (`<script type="application/ld+json">`) — `@type` of `ScholarlyArticle`/`Article`/`CreativeWork`.
4. **OpenGraph** (`og:title`, `og:description`) — title/abstract only if nothing found above.
5. **DOI in URL** — regex `r'10\.\d{4,9}/'` against the URL itself.

**DOI normalisation:** strip `https://doi.org/` prefix, strip trailing punctuation, validate `r'^10\.\d{4,9}/'`.

**PDF URL verification (HEAD request):** confirm `Content-Type: application/pdf`. Login redirect (URL contains `login`/`sso`/`auth`/`signin`/`access`), non-PDF content type, 401/403, or network error → `pdf_url = None`.

`classify_url(url, client)`: HEAD → `"pdf"` if `application/pdf` or URL ends `.pdf`; else
`"landing_page"`. Network error → `"landing_page"`.

Both, plus `_verify_pdf_url(url, client)`, take the caller's client. **`max_redirects` is an
`httpx` client-construction argument and is not accepted per request**, unlike `timeout`,
`headers` and `follow_redirects`; passing it to `client.get` raises `TypeError`, which
`except httpx.HTTPError` does not catch, so the call site dies rather than degrading. When
moving a client argument onto a request, check it is in the per-request signature.

---

## Unpaywall Downloader (`core/downloader.py`)

`download_via_unpaywall(doi, user_email, tmp_dir, rate_limiter, client)` and
`download_pdf_direct(url, doi, tmp_dir, client)` take the caller's client; `user_email` stays
because Unpaywall takes it as a query parameter rather than a User-Agent.
GET `https://api.unpaywall.org/v2/{doi}?email={user_email}`. Uses `best_oa_location.url_for_pdf`; falls back to `best_oa_location.url`. No OA PDF → `DownloadResult(success=False, reason="no_oa_pdf")` — do NOT add to library. Saves to `{library_root}/tmp/{doi_sanitised}.pdf` (replace `/` with `_`, strip illegal chars).

---

## File Organisation (`core/organiser.py`)

Default pattern: `{journal}/{year}/{author} ({year}) {title}.pdf`

Token rules: `{journal}` (illegal chars `/:*?"<>|` → `_`; empty → `Unsorted`), `{year}` (None → `Unknown`), `{author}` (first family name; >2 authors → `et al.`; empty → `Unknown`), `{title}` (truncated 80 chars, filesystem-safe).

Papers with `metadata_source in ("xmp", "filename")` bypass the pattern → land in `Unsorted/`.

`place_file`: copies user-dropped PDFs (original kept), moves tmp downloads. Duplicate destination → appends `_2`, `_3`, etc. Updates `paper.file_path` in-place; read it immediately after the call.

`DEFAULT_PATTERN` exported from `organiser.py` as the canonical fallback.

---

## Batch Import (`core/importer.py` — `ImportWorker(QThread)`)

**Mode 1 (drop PDFs):** `read_pdf` → hash check → extract DOI → Crossref resolve or guess
metadata → claim → copy → insert DB → buffer for flush.

**Mode 2 (paste DOIs):** DOI check → Unpaywall download → `read_pdf` → hash check → resolve
Crossref → claim → move → insert → buffer.

**Mode 3 (paste URLs):** `classify_url` → if `"pdf"`: download, then `read_pdf` and extract
the DOI from it. If `"landing_page"`: scrape → DOI check → attempt `pdf_url` download →
fallback Unpaywall → `ItemFailed` if nothing works.

**Four items in flight.** `_run_async` builds one `httpx.AsyncClient` for the whole run and
`_MAX_CONCURRENT_ITEMS` (4) `_worker` coroutines pulling from an `asyncio.Queue`. Not a
module-level client: the worker owns its event loop (`asyncio.new_event_loop()`), and a client
bound to a dead loop is a latent failure. 8 in flight measured 35ms/DOI against 56ms but sits
closer to Crossref's polite-pool ceiling. Identical entries in one item list are collapsed
(`dict.fromkeys`, which keeps the user's order) before any worker sees them.

**Duplicate detection has three keys: content hash, DOI, ISBN.** Not fuzzy title matching,
which would fire on errata, corrigenda and conference-then-journal pairs. Two methods, and the
split is load-bearing:

- `_is_known(kind, value)` is a cheap early look at the database only. It claims nothing and
  is not authoritative. Every path calls it on the content hash the moment the file exists
  locally, which costs one indexed lookup on the common non-duplicate case.
- `_claim_paper(paper, pdf)` is the single authority. It sets `paper.content_hash`, then checks
  and claims hash, DOI and ISBN against both the database and `self._claimed` (the keys taken
  by items already in flight this run, bounded by the item count and discarded with the
  worker). **There is no `await` between `_claim_paper` and `insert_paper`, and none may ever
  be added**: that unbroken window is what stops the event loop interleaving another item
  between the duplicate check, the destination name and the insert. Adding one reopens both
  the duplicate race and a duplicate-destination race. `tools/check_import_flush.py` asserts
  it against the source text.

A path whose claim fails unlinks its tmp download and counts the item as a duplicate, never as
a failure. `paper_exists_by_path` is deliberately gone rather than repaired: it compared the
source path against `file_path`, which `place_file(move=False)` fills with the destination, so
it never matched anything.

**Rate limiting:** Crossref min 20ms (`RateLimiter` with `asyncio.Lock`). Unpaywall min 100ms.

**Counters:** the worker holds `done == imported + dupes + failed + skipped`, and `ImportDialog` labels them so they visibly sum. `needs_review` is a subset of `imported`, never a separate bucket. ETA divides by `worked` (items this run did real work for), not by `done`, or resumed items collapse the estimate.

**One flush point, not per-paper work.** `_buffer(paper, fulltext)` queues a placed paper;
`_flush_pending()` categorises the batch in one model pass, indexes it, commits Tantivy, and
only then appends the state lines. It runs every `INDEX_COMMIT_INTERVAL` (50) papers, or when
buffered text passes `_PENDING_TEXT_LIMIT` (8 MB), and once more in `_run_async`'s `finally`.
50 rather than 200 because an incremental run is about 200 papers and at 200 it would flush
once, at the very end. `_flush_pending` is synchronous and contains no `await`, so the four
workers cannot interleave inside it and it needs no lock; the model pass does stall them for
its duration, which is the trade. A Tantivy commit is an fsync and a new segment; never call
`commit()` per document.

State is written last, after the index commit and the vector-store flush, so an item counts as
processed once its row, its index entry, its tags and its vector are all durable. A crash
before that costs a redo of the batch.

**Resume state:** `{library_root}/import_state.json` is append-only, one JSON string per line,
written one flush at a time (`_append_state(items: list[str])`, one file open per flush). A legacy `{"processed": [...]}` object is still read and collapsed once via `_rewrite_state`. Do not go back to rewriting the whole set: at 150k items that was tens of GB of bookkeeping writes. UI shows total/processed/succeeded/needs\_review/failed and running ETA. User can pause/resume at any time; app remains fully usable during import.

---

## Auto-Categorisation (`core/categoriser.py`)

- `EmbeddingCategoriser`: owned by `MainWindow`, shared (with `threading.Lock`) to `ImportDialog` and `CategorizationDialog`.
- `load_model()` is blocking — always call from a worker thread or daemon thread. `MainWindow` preloads at startup if `auto_categorise=True` and `has_categories` (free-text categories or taxonomy labels).
- `categorise_papers(papers, db, vector_store=None)` returns one `(collection_ids, tags)` pair
  per input paper, positionally, to **merge onto** that paper, never replace. Collections are
  the union of free-text category matches and at most `top_k` taxonomy labels, both against
  the one threshold; tags are YAKE (`core/keywords.py`) and need no model. Given a
  `vector_store`, it persists each paper's embedding from the same batched encode, which is how
  the importer stores vectors without a second model call. It creates missing top-level
  collections through `_resolve_collections`, one `get_collections()` per batch. Prefer it to
  `categorise_paper`, which survives only as a single-paper wrapper: on CPU one `encode` over
  64 texts costs little more than one over a single document. `_BATCH_SIZE` is 64.
- The merge uses `sorted(set(...))`, not `list(set(...))`: comparing an unordered
  `list(set(...))` against `paper.collection_ids` reports a change whenever the ordering
  differs and writes a row that did not need writing.
- `_load_sentence_transformer(name)` is a module-level factory. sentence-transformers is not a
  hard dependency, so tests monkeypatch that one seam rather than the import.
- **Vector store** (`core/vectors.py`): one `VectorStore` owned by `main()` beside the DB and
  index, injected down to both workers. `paper_vectors.f32` is headerless little-endian
  float32 rows (a `.npy` header carries the shape, so every append would rewrite it) and
  `paper_vectors.json` holds `dim`, `model` and the row-ordered paper ids. Derived data:
  `open()` truncates a half-written pair back into step, and resets rather than raising on an
  unreadable sidecar or a `dim`/`model` mismatch (bge-small is also 384-dim, so the model name
  is what catches a model switch). A paper whose title or abstract is edited keeps its old
  vector until `Rebuild vectors`. Windows refuses to delete the blob while a `matrix()` memmap
  is alive, so `clear()` runs only with no worker holding one; the button is disabled during
  a run.
- **Taxonomy** (`core/taxonomy.py`): a plain text file, `Name` or `Name: description` per
  line, `{library_root}/taxonomy.txt` unless `taxonomy_path` is set; `Settings.taxonomy_file()`
  is the one resolution rule. Bad lines are skipped with a warning; an unreadable file raises
  `TaxonomyError`, which every caller catches, since `MainWindow` loads it during construction.
  No starter taxonomy ships; `tools/seed_taxonomy.py` drafts one on the target machine.
- Model: `all-MiniLM-L6-v2` (~23 MB, cached in `~/.cache/torch/sentence_transformers/`). Do NOT switch to Qwen3-Embedding-0.6B (27× slower on CPU). Quality upgrade path: `BAAI/bge-small-en-v1.5`.
- CPU-only by design. Install `torch` from PyPI, whose default Windows wheel is the CPU
  build. A CUDA build buys nothing on a GTX 1050 Ti: PyTorch dropped Pascal (`sm_61`) from
  its CUDA 12.8 and 12.9 builds from release 2.8 onward, so a CUDA install fails at runtime
  with `no kernel image is available for execution on the device`. Embedding a
  150,000-paper library (title plus abstract, one vector each) is roughly an hour on 4 to 6
  cores, so the CPU build is sufficient for everything here.
- Retroactive batch: `CategorizationWorker(QThread)` runs two stages. `embedding` encodes the
  papers the vector store lacks (the store's id map is the resume record; there is no separate
  state file), and `assigning` runs `assign.top_labels` over `store.matrix()` in one pass plus
  YAKE per paper, re-run in full every time because it is idempotent and takes minutes. With
  no model it skips `embedding` and still tags. `ImportWorker` categorises only when the model
  is loaded, so papers imported without one wait for this run.
- `core/llm.py` is dead code — do not import it.

---

## UI Layout

Three rows of floating `GlassPanel`s on a `CanvasBackdrop`, 20px window margin and 20px
between rows, with the splitter handle at the same 20px so the black reads as one field.

```
╭─────────────────────────────────────────────────────────────╮
│  [Search .......................] [Search] [Import] [Categorise] [Settings]
╰─────────────────────────────────────────────────────────────╯
╭────────────╮ ╭──────────────────────────────╮ ╭────────────╮
│ Library    │ │ Papers            2,000 papers│ │ Paper      │
│ (lime)     │ │ (cyan)                        │ │ (magenta)  │
│ Collections│ │ Filters │ Title Authors Jnl Yr│ │ Identity   │
│ Tags       │ │         │ Score               │ │ Publication│
│            │ │                               │ │ Identifiers│
│            │ │                               │ │ [Open PDF] │
╰────────────╯ ╰──────────────────────────────╯ ╰────────────╯
╭─────────────────────────────────────────────────────────────╮
│ 130,421 papers · 12 need review · index 130,000             │
╰─────────────────────────────────────────────────────────────╯
```

Splitter stretch factors 1:4:2. The panel title carries the region accent; the results count
sits in the Papers panel's header slot via `GlassPanel.add_header_widget`.

- Authors field: comma-separated display, split on save into JSON array. No autocomplete, no lookup table.
- Tags: clickable chips (`QPushButton#TagChip`), removed by clicking; `QLineEdit` below to add.
- Changes saved on `editingFinished` (focus-out or Enter) — single `UPDATE`, no debounce.
- Deleting a collection removes its ID from all `papers.collection_ids` arrays; does not delete papers.
- `needs_review` is shown as an amber chip plus an amber left border on the four fields the
  guessing pipeline fills (title, authors, journal, year), never as a banner.
- The results panel is a `QStackedWidget`: table, loading, empty library, no query match, no
  filter match. Every absence is a designed `EmptyState`, not a blank grid.
- Settings carries a `Library maintenance` group whose `Fingerprints:` row states hash coverage
  (read once at construction) and, when it is short of the whole library, goes amber with the
  consequence spelled out beneath an amber left edge — the same vocabulary `needs_review` uses
  on the paper form. `Scan library…` emits `backfill_requested` and closes Settings, which
  saves; `MainWindow` opens `BackfillDialog` non-modally afterwards, because a scan over
  130,000 files must not hold the application shut.
- `BackfillDialog` (`ui/backfill_dialog.py`) is amber like the rest of the import family: a run
  panel (status line, bar, capped log) over a `Duplicates` report whose three states are all
  designed — nothing scanned, no duplicates (lime), and the groups list. Its end-state bar
  shows actual coverage rather than 100%, or a run where every file was unreadable would read
  as a clean finish. Nothing in it deletes, merges or edits a paper, and it says so on its
  face: which copy to keep depends on file quality and folder placement, which no rule here
  can judge.
- The Import dialog shows one amber line when coverage is incomplete, and nothing at all when
  it is complete. That gap is the one place the missing fingerprints cost the user something.

---

## UI Theme

Deep black canvas, translucent glass panels floating as islands with canvas visible between
them, one accent hue per region. Tokens live as module constants in `paperbase/ui/theme.py`;
`apply_theme(app)` is called in `main.py` immediately after `QApplication()` and sets
`Fusion` before the sheet (the native Windows style ignores parts of it).

| Token | Value | Owns |
|---|---|---|
| `CANVAS` / `CANVAS_DEEP` / `ANCHOR` | `#0A0A0C` / `#060608` / `#1A1046` | ground, gradient far end, glow base |
| `ACCENT_CYAN` | `#22E1FF` | results/search, primary action, focus, selection |
| `ACCENT_LIME` | `#8FE84A` | collections/tags, categorisation, success |
| `ACCENT_MAGENTA` | `#FF5CB0` | paper detail, tag chips |
| `ACCENT_AMBER` | `#FFB03A` | import, needs-review |
| `ACCENT_RED` | `#FF5A5A` | destructive, errors |
| `TEXT_PRIMARY` / `TEXT_SECONDARY` / `TEXT_ON_ACCENT` | `#EDEDF2` / `#B9BAC7` / `#0A0A0C` | |
| `SPACE` 8, `RADIUS_PANEL` 14, `RADIUS_CONTROL` 8, `RADIUS_INPUT` 6 | | 3u between panels, 2u panel padding, 1u inside a group |
| `MOTION_HOVER` 140, `MOTION_BASE` 220, `MOTION_CELEBRATE` 600 | ms | |

`paperbase/ui/glass.py` holds the primitives, since Qt has no backdrop blur and no
`box-shadow`: `CanvasBackdrop` (window ground), `GlassPanel` (the floating island, with
`header_layout` / `content_layout` and a region `accent`), `panel_shadow`, `accent_glow`,
`StackFader`, `EmptyState`, `display_font`.

- Never hardcode colours in widget files; use object-name selectors or `theme` constants.
- Panels are styled by object name (`#GlassPanel`), never by class. `chrome=True` selects the
  heavier `PANEL_FILL_HI` fill for the command bar and dialog control strips.
- `QWidget` carries `CANVAS` as the global base. Never set the `QWidget` base to a panel
  colour: it paints every anonymous layout container and destroys the floating-island layout.
- Inline `setStyleSheet` on individual widgets is only for state-specific overrides that
  cannot be expressed via global selectors.
- Primary-action buttons get `setObjectName("primary")` (cyan fill); `#danger` fills red.
- `theme.reduced_motion()` gates every animation. `PAPERBASE_REDUCED_MOTION`
  (`1`/`true`/`yes`) overrides the `reduce_motion` setting in either direction; the setting
  reaches it via `theme.set_reduced_motion`, called from `main.main` and
  `SettingsDialog._accept`.

---

## Code Style

- Python 3.12 (`py -3.12`). Type hints on all signatures.
- `pathlib.Path` throughout; no `os.path`.
- `dataclasses` for all DTOs. No ORM.
- All SQL as parameterised strings in `core/db.py`.
- No global mutable state; constructor injection for DB, indexer, settings.
- Qt signals as class attributes with `pyqtSignal`.
- `asyncio` for all HTTP. `ImportWorker` creates its own `asyncio.new_event_loop()` — not connected to the qasync main loop.
- `asyncio.ensure_future(coro)` from synchronous Qt slots works via qasync. Do NOT use QThread for async HTTP — QThread is for CPU-bound/blocking work only.
- Imports: stdlib → third-party → local, blank-line separated. 100-char line limit.

---

## Error Handling

- Network (`httpx`): catch `httpx.HTTPError`, log, set `needs_review=True`, continue.
- PDF (`fitz.open`): catch all exceptions, log, skip item, report as "failed" in import summary.
- SQLite: fatal — raise, show error dialog, never silently corrupt.
- Worker → UI: Qt signals only.

---

## PyQt6 Gotchas

- `QFlowLayout` doesn't exist. Tag chips use `QHBoxLayout(AlignLeft)`.
- Drag from `QTableView`: must implement `flags()` (+`ItemIsDragEnabled`), `mimeTypes()`, `mimeData()` on the model. `setDragEnabled(True)` alone does nothing.
- Drop onto `QTreeView`: subclass + override `dragEnterEvent`/`dragMoveEvent`/`dropEvent`. Use `event.position().toPoint()` (not `event.pos()`).
- MIME type for drag-drop: `application/x-paperbase-paper-ids` (comma-separated IDs, UTF-8).
- Drag/focus event types (`QDragEnterEvent`, `QDragMoveEvent`, `QDropEvent`, `QFocusEvent`) are in `PyQt6.QtGui`; `QPoint`, `QObject` are in `PyQt6.QtCore` — not `QtWidgets`.

---

## Implementation Gotchas

**PaperTableModel is id-backed** (`ui/search_panel.py`): it holds `_ids: list[int]` and an LRU `_cache` of at most `CACHE_LIMIT` papers, fetching a `BLOCK` of 200 rows from SQLite as the view asks for them. Never materialise the whole result set. Adding a column requires `_COLUMNS`, `_SORT_KEYS` (column index → `search_filter` sort key), and `data()`. `sort()` does not sort: it emits `sort_requested`, and `SearchPanel._apply_filters` re-runs the query with an `ORDER BY`. Scores live in `_scores: dict[int, float]` on the model, not on `Paper`.

**Sort indicator must be synced by hand:** `setSortingEnabled(True)` makes Qt call `model.sort()` during construction, leaving an arrow on Title while the rows are in `date_added` order. `SearchPanel._sync_sort_indicator` corrects it and blocks header signals, since `setSortIndicator` re-enters `sortByColumn`.

**`QItemSelectionModel::reset()` clears the selection without emitting anything**, so `currentRowChanged` does not fire on a model reset and the detail panel would keep showing a paper the results no longer list. `_apply_filters` emits `paper_selected(None)` after `set_ids` for that reason.

**Async unbound local:** Always initialise `paper = None` before `if doi: paper = await resolve_metadata(...)`. Python raises `UnboundLocalError` on the fallback if `doi` was falsy and the branch never executed.

**Settings field — 5 places:** `Settings.__init__` (default), `.save`, `.load`, `SettingsDialog._build_ui`, `SettingsDialog._accept`. If consumed by `ImportWorker`: also `ImportWorker.__init__` and `ImportDialog._start_import`.

**`place_file` call sites:** Exactly 5 in `importer.py` (`_import_pdf`, `_import_doi`,
`_import_direct_pdf_url`, two in `_import_landing_page`). Each is preceded by its
`_claim_paper` guard and followed by `insert_paper` then `_buffer(paper, pdf.fulltext)`, with
no `await` anywhere between the claim and the insert. A post-placement hook must be added at
all 5, and `tools/check_phase5.py` counts every one of those calls.

**Nothing heavy before the first paint:** `MainWindow.__init__` only builds widgets. `main.py` calls `window.show()` then `window.start_deferred_load()`, which stages the listing, the collection tree, and the categoriser preload behind `QTimer.singleShot`. `CollectionTree.__init__` deliberately does not call `refresh()`. `SearchPanel._apply_filters` returns early until `_loaded`, or the sort Qt triggers during construction runs a full-library query before the window is up.

**A `QScrollArea` with the horizontal bar off clips instead of scrolling.** In `PaperDetail` the tag chips are one `QHBoxLayout` (Qt has no flow layout), so their width would otherwise become the whole form's minimum and silently cut the right-hand edge off every field. `_tags_container.setMinimumWidth(1)` makes the chips give first. Watch for this with any unbounded row added to that form.

**Table columns must add up:** the results table's Title column is `QHeaderView.ResizeMode.Stretch` and the other four are fixed, so the five always fit the panel. Fixed widths for all five overflowed and pushed Year and Score out of sight.

**Dialog cache invalidation:** `_open_settings` nulls `_import_dialog` and `_cat_dialog` — intentional. Both cache categoriser settings at construction; must be recreated after settings change. Do not add lazy-init guards that skip this reset. `_on_backfill_finished` nulls `_import_dialog` for the same reason: `ImportDialog` reads hash coverage once at construction, so a finished scan would otherwise not reach an already-built one.

**SQLite variable limit:** Chunk `IN (?,?...)` at ≤900 items (`SQLITE_MAX_VARIABLE_NUMBER` = 999 on older builds). `get_papers_by_ids` already does this; do not add new unbounded IN clauses. `search_filter` instead loads its candidate ids into the per-connection `temp.search_ids` table (created in `open()`) and joins against it, which has no variable limit and lets SQLite do the ordering. `search_filter` takes `paper_ids=None` (no pre-filter, query all) vs `paper_ids=[]` (no results — distinct case).

**Tag/collection filter:** handled inside SQLite by `EXISTS (SELECT 1 FROM json_each(p.tags) …)`, not in Python. `get_all_tags` uses `json_each` for the same reason. Do not reintroduce a per-id `get_paper` loop.

**Tantivy `commit()` can fail transiently on Windows** with
`ValueError: An IO error occurred: 'Access is denied. (os error 5)'`, reproducible at roughly
1 run in 25 with frequent commits (segment files touched by another handle, typically a
virus scanner). It is not caught anywhere, so it aborts an import run mid-flush, and the
batch's papers are then in the database but absent from the index with nothing to retry them:
the state lines were never written, and on a redo the content-hash check classes them as
duplicates. Unfixed; a bounded retry inside `Indexer.commit` is the obvious remedy and is its
own piece of work.

**fitz context manager:** `fitz.Document` supports `with fitz.open(str(path)) as doc:` (PyMuPDF >= 1.18; project requires >= 1.24). Prefer this over manual `.close()` — bare `.close()` inside a `try` without `finally` leaks on exception.

**Windows file actions:**
- Reveal in Explorer: `subprocess.Popen(["explorer", "/select," + file_path])`
- Copy to clipboard: `QMimeData.setUrls([QUrl.fromLocalFile(path)])`; apply with `QApplication.clipboard().setMimeData(mime)`
- Full text retrieval: `fitz.open(path)` — Tantivy `fulltext` is `stored=False`

**Debugging import failures:** Check DB by DOI (not file path) using the debug command above. Crossref legitimately returns `title: []`/`author: []` for some valid DOIs — these set `needs_review=True`; verify at `https://api.crossref.org/works/{doi}`.

---

## Dependencies

```toml
[project]
name = "paperbase"
version = "0.1.0"
requires-python = "==3.12.*"
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
    "numpy>=1.26",
    "sentence-transformers>=3.0",
    "yake>=0.7",
]

[project.optional-dependencies]
dev = ["pyinstaller>=6.0", "pytest>=8.0", "pytest-qt>=4.4"]

[tool.pytest.ini_options]
pythonpath = ["."]
testpaths = ["tests"]
addopts = "-q"
```

`tests/` is the pytest suite. `paperbase` is not pip-installed on the development machine, and
`pythonpath = ["."]` is what lets pytest import it. The suite needs neither
sentence-transformers nor a display: every categoriser test installs `FakeModel`
(`tests/test_categoriser.py`, deterministic unit vectors from a hash of the text) through the
`_load_sentence_transformer` seam, and worker tests call `QThread.run()` directly, which works
with no `QApplication` and no event loop. `pytest-qt` is a dev placeholder.

The `tools/check_*.py` scripts listed under Commands are the behavioural checks for the import
path: each does its own `sys.path.insert`, runs against a throwaway `tempfile.mkdtemp()`
fixture, prints one `OK` line and exits 0. Run all eight, and the pytest suite, after touching
the import, metadata, dedupe or categorisation paths.

Every check must point `PAPERBASE_DATA_DIR` at a throwaway path, never at the real data
directory, and must `db.close()` before its `TemporaryDirectory` exits: Windows holds the
sqlite file open otherwise and cleanup raises `PermissionError`, masking a clean pass as a
failure.

`yake` pulls seven transitive dependencies (`click`, `colorama`, `jellyfish`, `networkx`, `segtok`, `tabulate`, `regex`),
all with prebuilt cp312 Windows wheels.
