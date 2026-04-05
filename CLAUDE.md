# PaperBase — CLAUDE.md

## Project Overview

PaperBase is a Windows desktop application for managing a library of ~130,000 academic paper PDFs.
It replaces the Zotero + ZotMoov + DocFetcher toolchain with a single fast application that:

- Provides blazing full-text search across all PDFs (Tantivy, Lucene-class inverted index)
- Extracts DOIs from PDFs via text heuristics and resolves full metadata from Crossref
- Downloads open-access PDFs via Unpaywall when given DOIs or URLs
- Organises PDFs into a folder hierarchy according to configurable naming patterns
- Stores metadata in a flat SQLite schema (no normalised author/journal tables — this is a hard architectural constraint)
- Supports hierarchical collections and free-form tags, with stub hooks for LLM auto-categorisation

## Target Platform

Windows 10/11 only. No cross-platform requirement. Python 3.12 exactly (run via `py -3.12`; no other version is supported).

---

## Technology Stack

| Layer | Choice | Rationale |
|---|---|---|
| UI | PyQt6 | Native Qt6 widgets; no Electron |
| Full-text search | `tantivy` (tantivy-py) | Rust inverted index, matches Lucene performance, pre-built Windows wheel, no JVM |
| PDF text extraction | `PyMuPDF` (fitz) | Fast, accurate, exposes XMP metadata and page text |
| Database | SQLite via `sqlite3` / `aiosqlite` | Flat schema, instant single-row edits, no ORM |
| HTTP client | `httpx` (async) | Used for Crossref and Unpaywall API calls |
| Qt/asyncio bridge | `qasync` | Integrates Python asyncio event loop with Qt event loop |
| Build | `pyproject.toml` + PyInstaller | Single-directory distributable |

---

## Directory Structure

```
paperbase/
├── main.py                  # Entry point: creates QApplication, initialises core, launches MainWindow
├── pyproject.toml
├── CLAUDE.md
├── ui/
│   ├── main_window.py       # QMainWindow; three-panel layout (collection tree | results | detail)
│   ├── search_panel.py      # Search bar, filter sidebar, QTableView results
│   ├── paper_detail.py      # Right panel: editable metadata fields, tag chips, open PDF button
│   ├── import_dialog.py     # Batch import: drop PDFs, paste DOIs, paste URLs; progress per item
│   ├── collection_tree.py   # Left panel: hierarchical QTreeView of collections
│   └── settings_dialog.py   # Library root path, folder naming pattern, user email for APIs
├── core/
│   ├── db.py                # SQLite schema creation, all CRUD queries, no ORM
│   ├── indexer.py           # Tantivy index: build, incremental update, search, field schema
│   ├── metadata.py          # DOI extraction from PDF text; Crossref REST API lookup
│   ├── scraper.py           # Landing page scraping: Highwire/Dublin Core/JSON-LD/COinS meta tags
│   ├── downloader.py        # Unpaywall API lookup; PDF download with content-type verification
│   ├── organiser.py         # File copy/rename/sort according to naming pattern
│   ├── importer.py          # Orchestrates metadata + download + organise pipelines; worker thread
│   └── llm.py               # STUB ONLY: LLM categorisation hook, not implemented in v1
└── models/
    ├── paper.py             # Paper dataclass
    ├── collection.py        # Collection dataclass
    └── search_result.py     # SearchResult dataclass (paper_id, title, snippet, score)
```

---

## SQLite Schema

**Critical constraint: NO normalised tables for authors, journals, keywords, or any other repeating
string values. Every field that could be normalised in a conventional relational schema MUST be
stored as plain text or a JSON array directly on the `papers` row. This is the primary
architectural divergence from Zotero and the reason metadata editing is instant.**

```sql
CREATE TABLE IF NOT EXISTS papers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    doi             TEXT UNIQUE,
    title           TEXT NOT NULL DEFAULT '',
    authors         TEXT NOT NULL DEFAULT '[]',  -- JSON array of strings: ["Lastname, Firstname", ...]
    journal         TEXT NOT NULL DEFAULT '',     -- plain text, no FK, no lookup table
    year            INTEGER,
    volume          TEXT NOT NULL DEFAULT '',
    issue           TEXT NOT NULL DEFAULT '',
    pages           TEXT NOT NULL DEFAULT '',
    abstract        TEXT NOT NULL DEFAULT '',
    keywords        TEXT NOT NULL DEFAULT '[]',   -- JSON array from Crossref/XMP metadata
    tags            TEXT NOT NULL DEFAULT '[]',   -- JSON array of user- or LLM-assigned tags
    collection_ids  TEXT NOT NULL DEFAULT '[]',   -- JSON array of integer collection IDs
    file_path       TEXT NOT NULL UNIQUE,         -- absolute path to PDF on disk
    date_added      TEXT NOT NULL,                -- ISO8601 UTC
    date_modified   TEXT NOT NULL,                -- ISO8601 UTC
    metadata_source TEXT NOT NULL DEFAULT 'unknown',  -- "crossref", "xmp", "manual", "filename"
    needs_review    INTEGER NOT NULL DEFAULT 0,   -- 1 = metadata incomplete or unresolved
    open_access     INTEGER NOT NULL DEFAULT 0    -- 1 = confirmed open access via Unpaywall
);

CREATE TABLE IF NOT EXISTS collections (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    name      TEXT NOT NULL,
    parent_id INTEGER REFERENCES collections(id) ON DELETE SET NULL,
    UNIQUE(name, parent_id)
);

CREATE INDEX IF NOT EXISTS idx_papers_doi   ON papers(doi);
CREATE INDEX IF NOT EXISTS idx_papers_year  ON papers(year);
CREATE INDEX IF NOT EXISTS idx_papers_title ON papers(title);
CREATE INDEX IF NOT EXISTS idx_papers_needs_review ON papers(needs_review);
```

There is no `authors` table. There is no `journals` table. There is no `keywords` table.
Editing an author name is a single `UPDATE papers SET authors=? WHERE id=?` — no joins, no lookups,
no latency.

---

## Data Models

```python
# models/paper.py
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class Paper:
    id: Optional[int]
    doi: Optional[str]
    title: str
    authors: list[str]          # ["Lastname, Firstname", ...]
    journal: str
    year: Optional[int]
    volume: str
    issue: str
    pages: str
    abstract: str
    keywords: list[str]
    tags: list[str]
    collection_ids: list[int]
    file_path: str
    date_added: str
    date_modified: str
    metadata_source: str
    needs_review: bool
    open_access: bool

@dataclass
class Collection:
    id: Optional[int]
    name: str
    parent_id: Optional[int]

@dataclass
class SearchResult:
    paper_id: int
    title: str
    authors: list[str]
    journal: str
    year: Optional[int]
    snippet: str                # Tantivy-generated excerpt with search terms highlighted
    score: float
```

---

## Full-Text Search: Tantivy

### Index Schema

```python
import tantivy

def build_schema() -> tantivy.Schema:
    builder = tantivy.SchemaBuilder()
    builder.add_integer_field("paper_id", stored=True, indexed=True)
    builder.add_text_field("title",    stored=True,  tokenizer_name="en_stem")
    builder.add_text_field("abstract", stored=False, tokenizer_name="en_stem")
    builder.add_text_field("authors",  stored=False, tokenizer_name="en_stem")
    builder.add_text_field("keywords", stored=False, tokenizer_name="en_stem")
    builder.add_text_field("fulltext", stored=False, tokenizer_name="en_stem")  # full PDF body
    builder.add_integer_field("year",  stored=True,  indexed=True)
    return builder.build()
```

`fulltext` is not stored in the index (it is large); only `title` is stored for snippet generation
from search results. All other display metadata comes from SQLite by paper_id.

### Index Lifecycle

1. **First run**: User selects library root in settings. Indexer scans for all PDFs (already in DB
   after the metadata import phase), extracts full text via PyMuPDF, indexes all documents.
   Runs in a `QThread` worker with progress signal emitted per document.
2. **Incremental update**: After any import that adds documents, `indexer.add_document()` is called
   immediately. The Tantivy writer commits after each batch.
3. **Search**: `indexer.search(query: str) -> list[SearchResult]`. Uses Tantivy's default BM25
   query parser across all text fields. Returns paper_ids which are then fetched from SQLite for
   full metadata. Results are uncapped — `searcher.num_docs` is passed as the limit to Tantivy.

### Query syntax

Tantivy's parser supports: `"exact phrase"`, `field:term` (fields: title, abstract, authors,
keywords, fulltext), `AND`/`OR`/`NOT`, `+required -excluded`,
`year:[2020 TO 2023]` (range on indexed integer fields), `word*` (prefix).
Invalid syntax triggers a fallback that wraps the whole input in quotes (phrase search).

---

## DOI Extraction Pipeline

Replicates Zotero's documented approach. Implemented in `core/metadata.py`.

### Per-PDF extraction (`extract_doi_from_pdf(path: Path) -> Optional[str]`)

```
1. Open PDF with fitz.open(path)
2. Extract text from pages 0, 1, 2 (first three pages). Concatenate.
3. Split into lines. Take first 100 lines.
4. Apply DOI regex to each line:
       pattern = r'\b(10\.\d{4,9}/[^\s"<>{|}\\^[\]`]+)'
   Return first match found, stripped of trailing punctuation (.,;)).
5. If no DOI found in first 100 lines, search entire first-page text.
6. If still no DOI: check fitz Document.metadata dict for 'subject' or 'keywords'
   fields which sometimes contain a DOI.
7. Return None if no DOI found anywhere.
```

### Per-PDF metadata resolution (`resolve_metadata(doi: str) -> Optional[Paper]`)

```
1. Query Crossref: GET https://api.crossref.org/works/{doi}
   Headers: {"User-Agent": "PaperBase/1.0 (mailto:{user_email})"}
   (Polite pool: higher rate limits when mailto is supplied)
2. On 200: parse JSON response:
       message.title[0]         -> title
       message.author           -> [f"{a['family']}, {a['given']}" for a in authors]
       message.container-title  -> journal
       message.published.date-parts[0][0] -> year
       message.volume           -> volume
       message.issue            -> issue
       message.page             -> pages
       message.abstract         -> abstract (strip JATS XML tags if present)
       message.subject          -> keywords
3. On 404 or no result: return None.
4. On rate limit (429): back off with exponential retry, max 3 attempts.
```

### Fallback when no DOI found (`guess_metadata_from_text(path: Path) -> Paper`)

```
1. Extract first-page text via PyMuPDF.
2. Candidate title: the longest line in the first 20 lines that is >= 20 chars
   and contains at least one space (avoids picking up author affiliation strings).
3. Query Crossref bibliographic search:
       GET https://api.crossref.org/works?query.bibliographic={title}&rows=3&select=DOI,title,author,...
4. Take the first result if its title similarity to the candidate (using
   difflib.SequenceMatcher ratio) is >= 0.75. If so, treat as a DOI match and
   run full resolution.
5. If similarity < 0.75: use XMP metadata from fitz.Document.metadata as fallback
   (title, author keys). Mark metadata_source = "xmp", needs_review = True.
6. If XMP also empty: use the PDF filename (strip extension, replace underscores/hyphens
   with spaces) as the title. Mark metadata_source = "filename", needs_review = True.
```

---

## Landing Page Scraper (`core/scraper.py`)

Handles publisher/repository URLs where the user submits an article page rather than a direct
PDF link. Replicates the core behaviour of Zotero's generic "Embedded Metadata" translator,
which is triggered on any page that doesn't have a site-specific translator.

### ScrapeResult dataclass

```python
@dataclass
class ScrapeResult:
    doi:             Optional[str]   # extracted DOI, if found
    pdf_url:         Optional[str]   # direct PDF link, if found
    is_open_access:  bool            # True if citation_pdf_url or OA indicator was present
    metadata:        Optional[Paper] # partial paper populated from page meta tags
                                     # (used only if Crossref lookup fails; always try Crossref first)
    source_url:      str             # original URL passed in
```

### `scrape_landing_page(url: str) -> ScrapeResult`

```
1. Fetch URL with httpx.
   User-Agent: "Mozilla/5.0 (compatible; PaperBase/1.0)"  -- bare-minimum spoofing; most
   publishers block Python's default UA but accept a generic browser UA.
   Follow up to 5 redirects. Timeout: 15s.
   If response Content-Type is application/pdf: raise ValueError("direct_pdf") — caller
   should reclassify and use the PDF pipeline instead.

2. Parse HTML with BeautifulSoup(response.text, "lxml").

3. Extract metadata in the following priority order (highest quality first):

   PRIORITY 1 — Highwire Press / Google Scholar meta tags
   (Used by Springer, Nature, Elsevier, Wiley, OUP, CUP, PLOS, PubMed Central, arXiv, bioRxiv,
   and most major publishers. These are the most reliable and most commonly present.)

   Tags to extract (all are <meta name="..." content="...">):
     citation_doi              → doi
     citation_pdf_url          → pdf_url (set is_open_access=True if present)
     citation_title            → metadata.title
     citation_author           → metadata.authors (may appear multiple times; collect all)
     citation_journal_title    → metadata.journal
     citation_publication_date → metadata.year (parse year from YYYY/MM/DD or YYYY)
     citation_volume           → metadata.volume
     citation_issue            → metadata.issue
     citation_firstpage +
     citation_lastpage         → metadata.pages as "{first}–{last}"
     citation_abstract         → metadata.abstract
     citation_keywords         → metadata.keywords (may be comma-separated single tag or multiple tags)
     citation_fulltext_html_url → store as fallback; sometimes PDF is accessible from there

   PRIORITY 2 — Dublin Core tags
   (Fallback for institutional repositories, OJS journals, and older publisher sites.)

   Tags to extract (all are <meta name="DC.xxx" content="..."> or <meta name="dc.xxx" ...>):
     DC.identifier   → doi (if content matches DOI pattern r'10\.\d{4,9}/')
     DC.title        → metadata.title (if not already set)
     DC.creator      → metadata.authors (may appear multiple times)
     DC.source       → metadata.journal (if not already set)
     DC.date         → metadata.year

   PRIORITY 3 — JSON-LD (application/ld+json script blocks)
   (Increasingly common on modern publisher sites; parse any <script type="application/ld+json">
   blocks and look for @type = "ScholarlyArticle" | "Article" | "CreativeWork".)

   Fields to extract:
     identifier (if DOI-shaped)   → doi
     url (if contains /pdf/)      → pdf_url candidate (verify with HEAD request)
     name / headline              → metadata.title
     author[].name                → metadata.authors
     isPartOf.name / publisher.name → metadata.journal
     datePublished                → metadata.year

   PRIORITY 4 — OpenGraph tags
   (Low-quality for academic metadata but common fallback for title/abstract.)

   Tags to extract (<meta property="og:xxx" content="...">):
     og:title       → metadata.title (only if nothing found above)
     og:description → metadata.abstract (only if nothing found above)

   PRIORITY 5 — DOI in page URL
   If the URL itself contains a DOI pattern (e.g. doi.org/10.xxxx/yyyy or
   /doi/10.xxxx/yyyy), extract it. This covers doi.org redirects and many
   publisher URL patterns (e.g. https://journals.plos.org/plosone/article?id=10.1371/...).

4. DOI normalisation:
   - Strip "https://doi.org/" prefix if present.
   - Strip trailing punctuation.
   - Validate against pattern r'^10\.\d{4,9}/' — discard if no match.

5. PDF URL verification:
   If pdf_url was found in meta tags, send a HEAD request to it.
   - If HEAD returns Content-Type: application/pdf → confirmed, use as-is.
   - If HEAD returns 301/302 to a login/SSO URL (heuristic: destination URL contains
     "login", "sso", "auth", "signin", "access" as substrings) → paywalled,
     set pdf_url = None, is_open_access = False.
   - If HEAD returns 200 with non-PDF Content-Type → set pdf_url = None.
   - If HEAD returns 401 or 403 → paywalled, set pdf_url = None.
   - On any network error → set pdf_url = None (treat as unavailable).

6. Return ScrapeResult with all collected fields.
   If doi was found, the caller will subsequently call resolve_metadata(doi) via Crossref
   to get authoritative metadata; the metadata field on ScrapeResult is only used as a
   fallback if Crossref returns nothing.
```

### `classify_url(url: str) -> str`

```
1. Send HEAD request to url (timeout 10s, follow 3 redirects).
2. If Content-Type header starts with "application/pdf" → return "pdf".
3. If url ends with ".pdf" (case-insensitive) → return "pdf".
4. Otherwise → return "landing_page".
5. On network error → return "landing_page" (safer default; GET will attempt scrape).
```

### Publisher coverage notes for Claude Code

The Highwire Press tag set covers virtually all major academic publishers without any
site-specific logic. Confirmed to use `citation_pdf_url`:
- Springer / SpringerLink
- Nature Publishing Group
- Elsevier / ScienceDirect (uses Highwire tags but may gate the PDF URL itself)
- Wiley Online Library
- Oxford University Press
- Cambridge University Press
- PLOS (always OA, `citation_pdf_url` always resolves)
- PubMed Central (always OA)
- arXiv (uses `citation_pdf_url` pointing to /pdf/ route)
- bioRxiv / medRxiv (preprint servers, always OA)
- American Chemical Society
- Royal Society of Chemistry
- IEEE Xplore (uses Highwire tags; PDF access depends on subscription)

No site-specific translators are needed for v1. The generic Highwire + Dublin Core +
JSON-LD pipeline covers >95% of academic URLs a researcher would submit.

Implemented in `core/downloader.py`.

```
Input: doi (str), user_email (str)

1. GET https://api.unpaywall.org/v2/{doi}?email={user_email}
2. On 200: parse JSON.
   - Check response['best_oa_location'] is not None.
   - Get url = response['best_oa_location']['url_for_pdf']
   - If url is None: try response['best_oa_location']['url'] as fallback (some
     locations serve the PDF at the landing URL with content negotiation).
3. If no OA PDF URL exists: return DownloadResult(success=False, reason="no_oa_pdf").
   DO NOT add the paper to the library.
4. Download PDF: GET {url} with httpx, stream=True, timeout=30s.
   Verify Content-Type header starts with "application/pdf".
   If Content-Type check fails: return DownloadResult(success=False, reason="not_pdf").
5. Save to a temporary path under the library root: tmp/{doi_sanitised}.pdf
   (sanitise DOI for filesystem: replace / with _ and strip other illegal chars).
6. Return DownloadResult(success=True, tmp_path=Path(...)).
7. Caller then runs DOI extraction pipeline on the downloaded file to confirm
   metadata and determine final organised path, then calls organiser.place_file().
```

---

## File Organisation

Implemented in `core/organiser.py`.

### Naming Pattern

User-configurable in settings. Default:

```
{journal}/{year}/{author} ({year}) {title}.pdf
```

Token names as implemented in `core/organiser.py` and `ui/settings_dialog.py`:
- `{journal}` = journal name with filesystem-illegal chars removed: `/:*?"<>|` → `_`.
  If journal is empty: literal `Unsorted`.
- `{year}` = integer year. If None: `Unknown`.
- `{author}` = first author's family name. If >2 authors: `{family} et al.`.
  If no authors: `Unknown`.
- `{title}` = title truncated to 80 characters, filesystem-safe.

### Placement (`place_file(source: Path, paper: Paper) -> Path`)

```
1. Compute destination path from naming pattern + paper metadata.
2. Create all intermediate directories (exist_ok=True).
3. If source is a temporary download: move (shutil.move).
   If source is a user-dropped existing PDF: copy (shutil.copy2); original is not deleted.
4. If destination already exists (duplicate import): append _2, _3, etc. to filename
   before extension until a free name is found. Log a warning.
5. Update paper.file_path to the final destination path and save to DB.
6. Return final Path.
```

---

## Batch Import Orchestration

Implemented in `core/importer.py` as `ImportWorker(QThread)`.

### Three input modes

**Mode 1: Drop PDFs** (list of local Paths)
```
For each path:
  1. Check if file_path already exists in DB — skip if so (duplicate).
  2. extract_doi_from_pdf(path)
  3. If DOI found: resolve_metadata(doi) -> paper
  4. Else: guess_metadata_from_text(path) -> paper
  5. place_file(path, paper)  [copy, not move]
  6. Insert paper into DB.
  7. indexer.add_document(paper, extract_fulltext(path))
  8. Emit signal: ItemImported(path.name, success, paper.needs_review)
```

**Mode 2: Paste DOIs** (list of DOI strings)
```
For each doi:
  1. Check if doi already exists in DB — skip if so.
  2. downloader.download(doi) -> result
  3. If result.success is False: emit ItemFailed(doi, result.reason); continue.
  4. resolve_metadata(doi) -> paper
  5. place_file(result.tmp_path, paper)  [move]
  6. Insert paper into DB.
  7. indexer.add_document(paper, extract_fulltext(paper.file_path))
  8. Emit signal: ItemImported(doi, success=True, needs_review=paper.needs_review)
```

**Mode 3: Paste URLs** (list of URL strings)

URLs may point to either a direct PDF file or a publisher/repository landing page.
The pipeline must distinguish these two cases and handle both.

```
For each url:
  1. Classify URL type: call scraper.classify_url(url) -> "pdf" | "landing_page"
     Classification: send a HEAD request; if Content-Type = application/pdf → "pdf".
     If HEAD returns non-PDF content type or fails → "landing_page".

  2a. If "pdf":
      Download directly via httpx, verify Content-Type on GET.
      Save to tmp path.
      extract_doi_from_pdf(tmp_path) → doi
      If doi: resolve_metadata(doi) → paper
      Else: guess_metadata_from_text(tmp_path) → paper
      paper.open_access = False (we don't know, assume not)
      place_file, insert, index — same as Mode 2.

  2b. If "landing_page":
      Call scraper.scrape_landing_page(url) → ScrapeResult
      (see scraper module below for full pipeline)

      If ScrapeResult.pdf_url is not None:
        Attempt download of ScrapeResult.pdf_url (GET, verify Content-Type).
        If download succeeds:
          Save to tmp, resolve_metadata(ScrapeResult.doi) if doi present,
          else use ScrapeResult.metadata as paper fields.
          paper.open_access = ScrapeResult.is_open_access
          place_file, insert, index.
        If download fails (redirect to login, non-PDF response, 401/403):
          → paper is paywalled. Fall through to Unpaywall check.

      If ScrapeResult.pdf_url is None OR direct download failed:
        If ScrapeResult.doi is not None:
          Try downloader.download(ScrapeResult.doi) via Unpaywall.
          If Unpaywall succeeds: place_file, insert, index. paper.open_access = True.
          If Unpaywall also fails:
            Emit ItemFailed(url, "no_oa_pdf"). Do NOT add any record to the library.
        If ScrapeResult.doi is None and ScrapeResult.pdf_url is None:
          Emit ItemFailed(url, "no_doi_no_pdf"). Do not add to library.
```

### Rate limiting

Crossref polite pool: enforce a minimum 20ms delay between requests (50 req/s).
Implement as a `RateLimiter` class with a `asyncio.Lock` and timestamp tracking.
Unpaywall: no stated rate limit, but apply a 100ms minimum delay as courtesy.

### First-run import of existing 130k flat dump

This is a special case of Mode 1 applied to every PDF found recursively under the user's
existing library path. Expected duration: 2–6 hours depending on network and Crossref response
times. Requirements:
- The worker persists its progress to a `import_state.json` file after every 100 papers.
- On restart, it reads `import_state.json` and skips already-processed files.
- The UI shows: total / processed / succeeded / needs_review / failed counts, plus a
  running ETA based on average processing time per paper.
- User can pause (worker finishes current item then waits) and resume at any time.
- The application is fully usable during import (search, browse, edit) for papers
  already processed.

---

## UI Layout

### Main Window (three-panel splitter)

```
┌─────────────────────────────────────────────────────────────┐
│  [Search bar ................................] [Import] [⚙]  │
├──────────────┬──────────────────────────────┬───────────────┤
│ Collections  │ Results (QTableView)         │ Paper Detail  │
│ (QTreeView)  │ Title | Authors | Jnl | Year │               │
│              │ ...                          │ [Editable     │
│ Tags         │ ...                          │  metadata     │
│ (flat list)  │ ...                          │  fields]      │
│              │                              │               │
│              │                              │ [Open PDF]    │
└──────────────┴──────────────────────────────┴───────────────┘
│ Status bar: 130,421 papers | 12 needs review | Index: ready │
└─────────────────────────────────────────────────────────────┘
```

### Paper Detail Panel — metadata editing rules

- Every field is a `QLineEdit` (single-line) or `QPlainTextEdit` (abstract).
- Authors: `QLineEdit`, comma-separated display. On save, split by `,` alternating between
  family and given — store as JSON array. No autocomplete. No lookup table.
- Tags: displayed as clickable chips; `QLineEdit` below to add new tags; click chip to remove.
- Changes are saved on `editingFinished` signal (focus-out or Enter). The save is a single
  `UPDATE papers SET {field}=? WHERE id=?`. No debounce, no secondary queries, no latency.
- "Needs Review" badge shown prominently if `needs_review = 1`. User dismisses it manually
  after correcting metadata.

### Search Panel

- Query sent to `indexer.search()` on every `returnPressed` or search button click.
- Filter controls (applied as post-filter in SQLite on result paper_ids):
  - Year range: two `QSpinBox` widgets
  - Journal: `QLineEdit` with `LIKE` filter
  - Tags: `QListWidget` (multi-select checkboxes)
  - Collection: selecting a collection node in the left panel filters results to that collection
  - "Needs Review only" checkbox
- Results sorted by Tantivy BM25 score by default; user can re-sort by year/title/journal/score via
  column headers. Score column shows 0–100 normalised against the top hit; blank when no query is active.

### Import Dialog

- Tab 1: Drop PDFs — `QListWidget` showing queued files; drag-drop target; progress column per item.
- Tab 2: Paste DOIs — `QPlainTextEdit` for pasting; one DOI per line; start button.
- Tab 3: Paste URLs — same structure as Tab 2.
- All tabs share a progress section: progress bar, counts (queued/done/failed), live log of
  last 20 operations.
- Dialog is non-modal: user can close it and monitor progress in the status bar.

---

## Collection and Tag Management

### Collections

- Hierarchical tree, unlimited depth, stored via `parent_id` self-reference in `collections` table.
- Right-click context menu on tree: New Collection, New Sub-collection, Rename, Delete.
- Deleting a collection does NOT delete papers — it removes the collection_id from all
  affected `papers.collection_ids` arrays.
- A paper appears in a collection if its `collection_ids` JSON array contains that collection's id.
  It also implicitly appears in all ancestor collections (handled in the tree view filter query).
- Multi-collection membership is supported; a paper can be in any number of collections.

### Tags

- Free-text strings stored in `papers.tags` JSON array.
- Tag list in left panel shows all unique tags across all papers (derived by a DB query on startup
  and updated incrementally).
- Clicking a tag filters the results panel to papers containing that tag.

### LLM Categorisation Stub (`core/llm.py`)

```python
class LLMCategoriser:
    """
    Stub for v1. Provides the interface that v2 will implement.
    """
    def categorise(
        self,
        paper: Paper,
        target_collections: list[str],
        existing_tags: list[str],
    ) -> tuple[Optional[str], list[str]]:
        """
        Returns (suggested_collection_name, suggested_tags).
        In v1: always returns (None, []).
        In v2: send paper.title + paper.abstract to an LLM API with a structured prompt
        instructing it to select from target_collections and suggest tags.
        Results shown to user for review before being applied.
        """
        return None, []
```

---

## Error Handling Conventions

- All network calls (`httpx`) wrapped in `try/except httpx.HTTPError`. On failure: log error,
  mark paper `needs_review = True`, continue processing.
- All PDF operations (`fitz.open`) wrapped in `try/except`. Corrupt or password-protected PDFs
  are logged and skipped; user sees them in the import summary as "failed".
- SQLite errors: fatal (raise, show error dialog, do not silently corrupt data).
- Worker threads communicate with the UI exclusively via Qt signals. Never call UI methods
  directly from a worker thread.

---

## Code Style

- Python 3.12 exactly (`py -3.12`). Type hints on all function signatures.
- `pathlib.Path` throughout. No `os.path` usage.
- `dataclasses` for all data transfer objects.
- No ORM. All SQL written as explicit parameterised query strings in `core/db.py`.
- No global mutable state. All major objects (DB connection, indexer, settings) passed via
  constructor injection.
- Qt signals defined as class attributes using `pyqtSignal`.
- `asyncio` for all HTTP I/O. Worker threads use `asyncio.run()` internally for the HTTP layer.
- Imports: stdlib → third-party → local, separated by blank lines.
- Line length: 100 chars max.

---

## Explicit Non-Requirements (do not build)

- No web server, browser extension, or network-facing interface of any kind.
- No BibTeX, RIS, or citation export.
- No built-in PDF viewer (use `os.startfile(path)` to open in system default application).
- No cloud sync or remote library.
- No Sci-Hub or any download source other than Unpaywall.
- No normalised author, journal, keyword, or any other lookup table in SQLite.
- No citation graph, reference parsing, or related-paper discovery.
- No Linux or macOS support.

---

## Dependencies (pyproject.toml)

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
]

[project.optional-dependencies]
dev = ["pyinstaller>=6.0", "pytest>=8.0", "pytest-qt>=4.4"]
```

---

## Build / Run

```
pip install -e .
python -m paperbase.main   # or: paperbase  (installed console script)
```

**Tests:** No test suite exists yet. `pytest` and `pytest-qt` are in dev deps as placeholders for future tests.

First-run wizard:
1. Select library root path (where organised PDFs will be stored).
2. Select existing PDF folder to import (can be skipped and done later).
3. Enter email address (used as Crossref polite pool identifier and Unpaywall identifier —
   never sent anywhere else).
4. Confirm folder naming pattern (shown with example derived from first found PDF).
5. Import begins in background. Application opens immediately.

---

## Implementation Notes

### Runtime data directory
All persistent state is stored under `%LOCALAPPDATA%\PaperBase\PaperBase\`:
- `paperbase.db` — SQLite database
- `index/` — Tantivy full-text index
- `settings.json` — library root, user email, folder pattern

First place to look when debugging index corruption or a missing/empty DB.
`import_state.json` (first-run import resume state) lives at `{library_root}/import_state.json` (alongside the PDFs, not in the app data dir).

### PaperTableModel column extension
- Adding a column requires touching three places in `ui/search_panel.py`: `_COLUMNS` (drives `columnCount` and `headerData` automatically), `data()` (new `if col == N` branch), and `sort()` (new `elif column == N` branch).
- Scores are not stored on `Paper`; `PaperTableModel` holds a separate `_scores: dict[int, float]` (paper_id → value) passed into `set_papers()`. Absent entries render as `""`.

### PyQt6 gotchas
- `QFlowLayout` does not exist in PyQt6. Tag chips in `ui/paper_detail.py` use `QHBoxLayout` with `AlignLeft`.
- Drag from `QTableView`: must implement `flags()` (add `ItemIsDragEnabled`), `mimeTypes()`, and `mimeData()` on the model — `setDragEnabled(True)` alone does nothing.
- Drop onto `QTreeView`: subclass the view and override `dragEnterEvent`/`dragMoveEvent`/`dropEvent`. Use `event.position().toPoint()` (not `event.pos()`) to get the drop point in PyQt6.
- Custom drag-drop MIME type used across the app: `application/x-paperbase-paper-ids` — comma-separated paper IDs encoded as UTF-8 bytes.

### Async pattern in importer
- Always initialise `paper = None` before a conditional `if doi: paper = await resolve_metadata(...)` block.
  Python raises `UnboundLocalError` on the `if paper is None:` fallback if `doi` was falsy and the branch never ran.
- `ImportWorker` (QThread) creates its own `asyncio.new_event_loop()` and is not connected to the `qasync` main-thread loop.

### Async from main-thread Qt slots
- `asyncio.ensure_future(coro)` works directly from synchronous Qt slots (e.g. button `clicked`).
  qasync installs its loop as the running loop on the main thread, so coroutines resume there and
  can safely update UI. Do NOT use QThread for this — QThread is only for CPU-bound or blocking work.

### SQLite schema migrations
- Adding new columns to an existing DB: call `ALTER TABLE papers ADD COLUMN ...` wrapped in
  `try/except sqlite3.OperationalError` inside a `_migrate()` method called from `Database.open()`.
  SQLite has no `IF NOT EXISTS` clause for `ALTER TABLE`.

### Extending the Paper dataclass
- New fields must be keyword arguments with defaults (e.g. `isbn: Optional[str] = None`) so that
  existing `Paper(...)` construction sites that omit them (scraper, metadata, tests) keep working.
  Required positional fields cannot be added after optional ones in a dataclass.

### Book metadata
- `journal` field stores publisher name when `document_type == "book"`. UI label reads "Journal/Publisher".
- Book lookup order: Open Library (`https://openlibrary.org/api/books?bibkeys=ISBN:{isbn}&format=json&jscmd=data`)
  then Google Books (`https://www.googleapis.com/books/v1/volumes?q=isbn:{isbn}`). Both are free, no API key.
- Import pipeline: DOI → (no result) → ISBN → `resolve_book_metadata` → `guess_metadata_from_text`.

### Folder naming pattern data flow
- `Settings.folder_pattern` (settings_dialog.py) → passed to `ImportDialog.__init__` → passed to
  `ImportWorker(folder_pattern=...)` → passed to every `place_file(..., folder_pattern=...)` call
  → forwarded to `compute_destination(paper, library_root, pattern)` in organiser.py.
- `DEFAULT_PATTERN` is exported from `organiser.py` as the canonical fallback.
- Papers with `metadata_source in ("xmp", "filename")` bypass the pattern entirely and land in `Unsorted/`.

### Smoke-testing without a test suite
- `py -3.12 -c "from paperbase.xxx import yyy; print('OK')"` is the fastest correctness check.
  Run after any change that touches imports or dataclass fields.

### SQLite variable limit
- `SQLITE_MAX_VARIABLE_NUMBER` is 999 on older SQLite builds. Any `IN (?,?...)` clause must be
  chunked at ≤900 items. `get_papers_by_ids` already does this. Do not add new unbounded IN
  clauses against the full papers table.
- `search_filter` accepts `paper_ids=None` as a sentinel meaning "no Tantivy pre-filter; query
  all papers". This avoids building a 134k-item IN clause for the no-query browse case.
  `paper_ids=[]` (empty list) is a distinct case meaning "no results".

### Tag/collection filter performance
- The tag and collection filter branches in `search_filter` call `get_paper(pid)` individually
  per matching ID. This is acceptable for Tantivy results (≤500 IDs) but will be slow if
  activated with `paper_ids=None` and a large result set. Known limitation; do not "fix" by
  loading all IDs into an IN clause.
