# PaperBase

A Windows desktop application for managing a large library of academic paper PDFs. Replaces the Zotero + ZotMoov + DocFetcher toolchain with a single, fast, self-contained application.

Built for researchers with libraries of 100k+ papers who need sub-second full-text search, reliable metadata resolution, and a UI that does not get in the way.

## Features

- **Full-text search** across all PDFs using Tantivy (Rust inverted index, BM25 ranking, Lucene-class performance); results include a 0–100 relevance score normalised against the top hit
- **Automatic metadata resolution** — extracts DOIs from PDF text and fetches full bibliographic data from Crossref, including abstracts
- **Book support** — extracts ISBNs from PDFs and landing pages; resolves metadata via Open Library and Google Books; `document_type` field distinguishes articles, books, book chapters, and proceedings
- **Manual metadata lookup** — DOI and ISBN lookup buttons in the detail panel fetch and populate all fields (including abstract) on demand
- **Open-access PDF download** via Unpaywall when given DOIs or publisher landing page URLs
- **Publisher landing page scraping** — supports Highwire Press, Dublin Core, JSON-LD, and OpenGraph meta tags, covering Springer, Nature, Elsevier, Wiley, OUP, CUP, PLOS, PubMed Central, arXiv, bioRxiv, and more
- **Automatic file organisation** into a configurable folder hierarchy (e.g. `{journal}/{year}/{author} ({year}) {title}.pdf`)
- **Hierarchical collections** and free-form tags; drag papers from the results table directly onto a collection to assign membership
- **Flat SQLite schema** — no normalised author/journal tables; every metadata edit is a single `UPDATE`, no joins, no latency
- **Duplicate detection** — all import modes skip papers already in the library by DOI, with counts reported in the import summary
- **Batch import** with resumable progress for first-run ingestion of existing large libraries
- Non-modal import: the application is fully usable during background import

## Requirements

- Windows 10 or 11
- Python 3.12 (exactly — use `py -3.12`; other versions are not supported)

No other system dependencies. All libraries are pure Python or ship pre-built wheels.

## Installation

```
git clone https://github.com/yourname/paperbase.git
cd paperbase
py -3.12 -m pip install -e .
```

## Running

```
paperbase
```

Or directly:

```
python -m paperbase.main
```

On first launch a setup wizard will ask for:

1. **Library root folder** — where organised PDFs will be stored (can be an existing folder of PDFs)
2. **Email address** — used in API request headers for the Crossref polite pool and Unpaywall; never sent to any other service

The application opens immediately after setup. Import runs in the background.

## Application data

All persistent state is written to `%LOCALAPPDATA%\PaperBase\PaperBase\`:

| Path | Contents |
|---|---|
| `paperbase.db` | SQLite database |
| `index/` | Tantivy full-text index |
| `settings.json` | Library root, email, folder naming pattern |

`import_state.json` (first-run import resume state) lives at `{library_root}\import_state.json`.

## Importing papers

Open the **Import** dialog from the toolbar. Three input modes are available.

### Drop PDFs

Drag and drop PDF files onto the queue. PaperBase will:

1. Extract the DOI from the first few pages of each PDF
2. Resolve full metadata from Crossref, including abstract (or fall back to ISBN → book metadata → XMP metadata → filename if no DOI is found)
3. Move each file into the organised folder hierarchy
4. Index the full text

Files already present in the library (matched by DOI) are skipped and counted as duplicates in the summary.

### Paste DOIs

One DOI per line. PaperBase will look up the open-access PDF via Unpaywall, download it, organise it, and index it. Papers with no available open-access PDF are skipped and reported as failed.

### Paste URLs

One URL per line. Accepts both direct PDF links and publisher/repository landing pages. PaperBase will:

1. Detect whether the URL points to a PDF or a landing page
2. For landing pages: scrape metadata tags and attempt to download the PDF directly; fall back to Unpaywall if the direct link is paywalled
3. Report any URL where no PDF could be obtained

The import dialog is non-modal. Close it and the import continues in the status bar.

## First-run import of an existing library

Pointing PaperBase at a folder containing tens of thousands of PDFs triggers the bulk import pipeline:

- Progress is saved every 100 papers to `import_state.json`; if the application is closed mid-import, it resumes where it left off on next launch
- The UI shows total / processed / succeeded / needs review / duplicates skipped / failed counts with a running ETA
- Import can be paused and resumed at any time
- The application is fully usable for papers already processed while import continues

Expected duration for 130k papers: 2–6 hours depending on network conditions and Crossref response times.

## Search

Enter any query in the search bar and press Enter. Tantivy's query syntax is supported:

| Syntax | Example |
|---|---|
| Phrase | `"neural scaling laws"` |
| Field-scoped | `title:attention authors:vaswani` |
| Boolean | `transformer AND vision NOT language` |
| Year range | `year:[2020 TO 2023]` |
| Prefix | `embed*` |

Results show a **score** column (0–100) normalised against the top hit. Re-sort by any column via the header. Filter the results further by year range, journal, document type (articles / books), tags, or collection using the controls in the left panel.

## Collections

The left panel shows a hierarchical collection tree. To assign papers to a collection, drag one or more rows from the results table and drop them onto the target collection node. Right-click a collection node to create sub-collections, rename, or delete (deleting a collection does not delete its papers).

## Metadata review

Papers where metadata could not be confirmed automatically are flagged with a **Needs Review** badge in the detail panel. Click through each to verify and correct. The badge is dismissed manually once you are satisfied.

All metadata fields are directly editable in the right-hand panel. Every save is a single SQL `UPDATE` — no secondary queries, no latency.

The **DOI** and **ISBN** fields each have a **Lookup** button. Enter or correct an identifier and click Lookup to fetch and overwrite all available fields — including abstract — from Crossref (DOI) or Open Library / Google Books (ISBN). Only non-empty fields in the fetched result are written; existing data is not blanked.

## Folder naming pattern

Configurable in **Settings**. Default:

```
{journal}/{year}/{author} ({year}) {title}.pdf
```

Available tokens: `{journal}`, `{year}`, `{author}`, `{title}`. Change this before importing; files already in the library are not automatically renamed when the pattern changes.

## Technology

| Layer | Library |
|---|---|
| UI | PyQt6 |
| Full-text search | tantivy-py (Rust) |
| PDF text extraction | PyMuPDF |
| Database | SQLite (sqlite3 / aiosqlite) |
| HTTP | httpx (async) |
| Qt/asyncio bridge | qasync |

## Building a standalone executable

```
pip install -e ".[dev]"
pyinstaller --onedir --name PaperBase paperbase/main.py
```

The `dist/PaperBase/` directory contains a self-contained application with no Python installation required.

## Non-features

The following are explicit non-requirements and will not be added:

- BibTeX / RIS / citation export
- Built-in PDF viewer
- Cloud sync or remote library
- Sci-Hub or any download source other than Unpaywall
- Linux or macOS support
- Normalised author, journal, or keyword lookup tables
- Citation graph or related-paper discovery
- Browser extension or any network-facing interface

## License

MIT
