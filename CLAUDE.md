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

# Debug DB directly (replace DOI as needed)
py -3.12 -c "import sqlite3; from pathlib import Path; from platformdirs import user_data_dir; conn = sqlite3.connect(str(Path(user_data_dir('PaperBase','PaperBase'))/'paperbase.db')); conn.row_factory = sqlite3.Row; print(dict(conn.execute('SELECT id,title,needs_review,file_path FROM papers WHERE doi=?',('10.xxxx/yyy',)).fetchone()))"
```

**Runtime data dir:** `%LOCALAPPDATA%\PaperBase\PaperBase\` — `paperbase.db`, `index/`, `settings.json`.
`import_state.json` and `categorisation_state.json` live at `{library_root}/` (alongside PDFs, not in app data dir).

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
| Embedding / tagging | `sentence-transformers` `all-MiniLM-L6-v2` + `keybert` | CPU-only, ~23 MB |

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
│   └── categorisation_dialog.py  # Progress dialog for retroactive categorisation
├── core/
│   ├── db.py          # SQLite schema + all CRUD; no ORM
│   ├── indexer.py     # Tantivy: build, incremental update, search
│   ├── metadata.py    # DOI extraction from PDF text; Crossref + book metadata lookup
│   ├── scraper.py     # Landing page: Highwire/DC/JSON-LD/OG meta scraping
│   ├── downloader.py  # Unpaywall lookup + PDF download
│   ├── organiser.py   # File copy/move per naming pattern; compute_destination
│   ├── importer.py    # ImportWorker(QThread): orchestrates all pipelines
│   ├── categoriser.py # EmbeddingCategoriser + CategorizationWorker
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
    document_type   TEXT NOT NULL DEFAULT 'article'
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

Query syntax: `"exact phrase"`, `field:term` (fields: title, abstract, authors, keywords, fulltext), `AND`/`OR`/`NOT`, `+required -excluded`, `year:[2020 TO 2023]`, `word*`. Invalid syntax falls back to whole-input phrase search.

Results uncapped (`searcher.num_docs` as limit). Scores normalised 0–100 against top hit; blank column when no query is active.

---

## DOI Extraction Pipeline (`core/metadata.py`)

**`extract_doi_from_pdf(path)`:** Regex `r'\b(10\.\d{4,9}/[^\s"<>{|}\\^[\]`]+)'` across first 3 pages (first 100 lines), then full first-page text, then `fitz.Document.metadata` subject/keywords fields. Strips trailing `.,;)`.

**`resolve_metadata(doi)`:** GET `https://api.crossref.org/works/{doi}` with polite-pool User-Agent (`mailto:` included). Crossref legitimately returns `title: []`/`author: []` for some valid DOIs — these set `needs_review=True`. Exponential retry on 429, max 3 attempts.

**`guess_metadata_from_text(path)`:** Candidate title = longest line ≥20 chars in first 20 lines. Queries Crossref bibliographic search; accepts result if `difflib.SequenceMatcher` ratio ≥0.75. Falls back to XMP metadata, then filename — both set `needs_review=True`.

**Book metadata:** `journal` stores publisher name. Lookup order: Open Library → Google Books (both free, no API key). Import pipeline: DOI → ISBN → `resolve_book_metadata` → `guess_metadata_from_text`.

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

`scrape_landing_page(url)` extracts metadata in priority order:

1. **Highwire Press tags** (`citation_doi`, `citation_pdf_url`, `citation_title`, `citation_author`, `citation_journal_title`, `citation_publication_date`, `citation_volume`, `citation_issue`, `citation_firstpage`/`citation_lastpage`, `citation_abstract`, `citation_keywords`) — covers Springer, Nature, Elsevier, Wiley, OUP, CUP, PLOS, PMC, arXiv, bioRxiv, ACS, RSC, IEEE. `citation_pdf_url` presence sets `is_open_access=True`.
2. **Dublin Core** (`DC.identifier` → doi, `DC.title`, `DC.creator`, `DC.source` → journal, `DC.date`) — institutional repos, OJS.
3. **JSON-LD** (`<script type="application/ld+json">`) — `@type` of `ScholarlyArticle`/`Article`/`CreativeWork`.
4. **OpenGraph** (`og:title`, `og:description`) — title/abstract only if nothing found above.
5. **DOI in URL** — regex `r'10\.\d{4,9}/'` against the URL itself.

**DOI normalisation:** strip `https://doi.org/` prefix, strip trailing punctuation, validate `r'^10\.\d{4,9}/'`.

**PDF URL verification (HEAD request):** confirm `Content-Type: application/pdf`. Login redirect (URL contains `login`/`sso`/`auth`/`signin`/`access`), non-PDF content type, 401/403, or network error → `pdf_url = None`.

`classify_url(url)`: HEAD → `"pdf"` if `application/pdf` or URL ends `.pdf`; else `"landing_page"`. Network error → `"landing_page"`.

---

## Unpaywall Downloader (`core/downloader.py`)

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

**Mode 1 (drop PDFs):** Check `file_path` duplicate → extract DOI → Crossref resolve or guess metadata → copy → insert DB → index.

**Mode 2 (paste DOIs):** Check DOI duplicate → Unpaywall download → resolve Crossref → move → insert → index.

**Mode 3 (paste URLs):** `classify_url` → if `"pdf"`: download directly, extract DOI post-download. If `"landing_page"`: scrape → check DOI duplicate → attempt `pdf_url` download → fallback Unpaywall → `ItemFailed` if nothing works.

**Duplicate detection:** All modes check DOI in DB. URL mode: if DOI found in PDF body post-download and already in DB, unlink tmp and skip.

**Rate limiting:** Crossref min 20ms (`RateLimiter` with `asyncio.Lock`). Unpaywall min 100ms.

**Resume state:** Progress persisted to `{library_root}/import_state.json` every 100 papers. UI shows total/processed/succeeded/needs\_review/failed and running ETA. User can pause/resume at any time; app remains fully usable during import.

---

## Auto-Categorisation (`core/categoriser.py`)

- `EmbeddingCategoriser`: owned by `MainWindow`, shared (with `threading.Lock`) to `ImportDialog` and `CategorizationDialog`.
- `load_model()` is blocking — always call from a worker thread or daemon thread. `MainWindow` preloads at startup if `auto_categorise=True` and categories non-empty.
- `categorise_paper()` returns `(collection_ids, tags)` to **merge onto** the paper, never replace. Creates missing top-level collections automatically.
- Model: `all-MiniLM-L6-v2` (~23 MB, cached in `~/.cache/torch/sentence_transformers/`). Do NOT switch to Qwen3-Embedding-0.6B (27× slower on CPU). Quality upgrade path: `BAAI/bge-small-en-v1.5`.
- Retroactive batch: `CategorizationWorker(QThread)`, state in `{library_root}/categorisation_state.json`.
- `core/llm.py` is dead code — do not import it.

---

## UI Layout

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

- Authors field: comma-separated display, split on save into JSON array. No autocomplete, no lookup table.
- Tags: clickable chips, removed by clicking; `QLineEdit` below to add.
- Changes saved on `editingFinished` (focus-out or Enter) — single `UPDATE`, no debounce.
- Deleting a collection removes its ID from all `papers.collection_ids` arrays; does not delete papers.

---

## UI Theme

All visual styling lives in `paperbase/ui/theme.py`. `apply_theme(app)` is called in `main.py`
immediately after `QApplication()`. The colour palette is documented in comments at the top of
that file.

- Never hardcode colours in widget files; use object-name selectors or reference palette values.
- Inline `setStyleSheet` on individual widgets is only acceptable for state-specific overrides
  (e.g. TagChip hover, review badge) that cannot be expressed via global selectors.
- Primary-action buttons get `setObjectName("primary")` to activate the orange accent style.
- Accent colour: `#F26822` (orange). Dark base: `#1c1917`. Surface: `#242018`. Raised: `#3a3530`.

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

**PaperTableModel columns** (`ui/search_panel.py`): adding a column requires `_COLUMNS`, `data()`, and `sort()`. Scores live in `_scores: dict[int, float]` on the model, not on `Paper`.

**Async unbound local:** Always initialise `paper = None` before `if doi: paper = await resolve_metadata(...)`. Python raises `UnboundLocalError` on the fallback if `doi` was falsy and the branch never executed.

**Settings field — 5 places:** `Settings.__init__` (default), `.save`, `.load`, `SettingsDialog._build_ui`, `SettingsDialog._accept`. If consumed by `ImportWorker`: also `ImportWorker.__init__` and `ImportDialog._start_import`.

**`place_file` call sites:** Exactly 5 in `importer.py` (`_import_pdf`, `_import_doi`, `_import_direct_pdf_url`, two in `_import_landing_page`). Post-placement hooks and `_apply_categorisation` must be added at all 5. `_apply_categorisation` is called immediately after `paper.id = paper_id` at all 5 `insert_paper` sites.

**Dialog cache invalidation:** `_open_settings` nulls `_import_dialog` and `_cat_dialog` — intentional. Both cache categoriser settings at construction; must be recreated after settings change. Do not add lazy-init guards that skip this reset.

**SQLite variable limit:** Chunk `IN (?,?...)` at ≤900 items (`SQLITE_MAX_VARIABLE_NUMBER` = 999 on older builds). `get_papers_by_ids` already does this; do not add new unbounded IN clauses. `search_filter` takes `paper_ids=None` (no pre-filter, query all) vs `paper_ids=[]` (no results — distinct case).

**Tag/collection filter:** Calls `get_paper(pid)` individually per ID — acceptable for ≤500 Tantivy results, slow for `paper_ids=None` on large result sets. Known limitation; do not "fix" with an unbounded IN clause.

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
    "sentence-transformers>=3.0",
    "keybert>=0.8",
]

[project.optional-dependencies]
dev = ["pyinstaller>=6.0", "pytest>=8.0", "pytest-qt>=4.4"]
```

No test suite exists yet. `pytest`/`pytest-qt` are dev placeholders.
