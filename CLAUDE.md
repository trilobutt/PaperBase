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
`PAPERBASE_DATA_DIR` overrides it (must be an absolute path, else `SystemExit`); `tools/make_fixture.py` builds a throwaway one to test against.
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

Query syntax: `"exact phrase"`, `field:term` (fields: title, abstract, authors, keywords, fulltext), `AND`/`OR`/`NOT`, `+required -excluded`, `year:[2020 TO 2023]`, `word*`/`*word`/`wo*rd`. Invalid syntax falls back to whole-input phrase search.

Results uncapped (`searcher.num_docs` as limit). Scores normalised 0–100 against top hit; blank column when no query is active.

**An empty index must never reach `searcher.search`:** tantivy's top-score collector asserts its limit is non-zero and a limit of 0 raises a Rust panic that takes the process down. `Indexer.search` returns `[]` when `num_docs == 0`, which is the ordinary first-run state.

**Wildcards (`*`):** Tantivy's `parse_query` has no native wildcard support — it silently drops `*` from terms. Any query containing `*` is instead routed through `Indexer._parse_wildcard_query`/`_build_clause_query` (`core/indexer.py`), which tokenises the string (respecting quoted phrases and `field:[range]` tokens), converts each `*`-containing clause to an anchored regex via `tantivy.Query.regex_query`, and recombines clauses with `tantivy.Query.boolean_query` using the same `AND`/`OR`/`NOT`/`+`/`-` semantics. Queries without `*` are unaffected — they still go straight through `parse_query`.

All text fields use the `en_stem` tokenizer, so `regex_query` matches against **stemmed, lowercased** terms, not the raw word. Trailing wildcards (`word*`) are reliable since stemming only ever removes a suffix — a literal prefix always still matches. Leading/infix wildcards (`*word`, `wo*rd`) are correctly implemented but can miss matches where the wildcard's fixed text spans a suffix the stemmer would otherwise strip (e.g. `*genesis` won't match "biogenesis", because `en_stem` indexes it as `biogenesi`). This is a property of the stemmed index, not a parser bug — do not try to "fix" it by switching tokenizers.

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

**Counters:** the worker holds `done == imported + dupes + failed + skipped`, and `ImportDialog` labels them so they visibly sum. `needs_review` is a subset of `imported`, never a separate bucket. ETA divides by `worked` (items this run did real work for), not by `done`, or resumed items collapse the estimate.

**Index commits are batched** every `INDEX_COMMIT_INTERVAL` (200) documents plus once at the end. A Tantivy commit is an fsync and a new segment; never call `commit()` per document.

**Resume state:** `{library_root}/import_state.json` is append-only, one JSON string per line, written per item (`_append_state`). A legacy `{"processed": [...]}` object is still read and collapsed once via `_rewrite_state`. Do not go back to rewriting the whole set: at 150k items that was tens of GB of bookkeeping writes. UI shows total/processed/succeeded/needs\_review/failed and running ETA. User can pause/resume at any time; app remains fully usable during import.

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

**`place_file` call sites:** Exactly 5 in `importer.py` (`_import_pdf`, `_import_doi`, `_import_direct_pdf_url`, two in `_import_landing_page`). Post-placement hooks and `_apply_categorisation` must be added at all 5. `_apply_categorisation` is called immediately after `paper.id = paper_id` at all 5 `insert_paper` sites.

**Nothing heavy before the first paint:** `MainWindow.__init__` only builds widgets. `main.py` calls `window.show()` then `window.start_deferred_load()`, which stages the listing, the collection tree, and the categoriser preload behind `QTimer.singleShot`. `CollectionTree.__init__` deliberately does not call `refresh()`. `SearchPanel._apply_filters` returns early until `_loaded`, or the sort Qt triggers during construction runs a full-library query before the window is up.

**A `QScrollArea` with the horizontal bar off clips instead of scrolling.** In `PaperDetail` the tag chips are one `QHBoxLayout` (Qt has no flow layout), so their width would otherwise become the whole form's minimum and silently cut the right-hand edge off every field. `_tags_container.setMinimumWidth(1)` makes the chips give first. Watch for this with any unbounded row added to that form.

**Table columns must add up:** the results table's Title column is `QHeaderView.ResizeMode.Stretch` and the other four are fixed, so the five always fit the panel. Fixed widths for all five overflowed and pushed Year and Score out of sight.

**Dialog cache invalidation:** `_open_settings` nulls `_import_dialog` and `_cat_dialog` — intentional. Both cache categoriser settings at construction; must be recreated after settings change. Do not add lazy-init guards that skip this reset.

**SQLite variable limit:** Chunk `IN (?,?...)` at ≤900 items (`SQLITE_MAX_VARIABLE_NUMBER` = 999 on older builds). `get_papers_by_ids` already does this; do not add new unbounded IN clauses. `search_filter` instead loads its candidate ids into the per-connection `temp.search_ids` table (created in `open()`) and joins against it, which has no variable limit and lets SQLite do the ordering. `search_filter` takes `paper_ids=None` (no pre-filter, query all) vs `paper_ids=[]` (no results — distinct case).

**Tag/collection filter:** handled inside SQLite by `EXISTS (SELECT 1 FROM json_each(p.tags) …)`, not in Python. `get_all_tags` uses `json_each` for the same reason. Do not reintroduce a per-id `get_paper` loop.

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
