# Persistent paper vectors, taxonomy assignment, and YAKE keywording

> **Blocked: re-anchor before dispatching.** The import-throughput work has landed and
> rewrote `categoriser.py`, `importer.py` and `db.py`, so Phases 7, 9 and 13 below quote text
> that no longer exists. Re-read those three files and re-anchor those phases first:
>
> - **Phase 7** (`EmbeddingCategoriser`) quotes `categorise_paper`'s body and the inline
>   `from sentence_transformers import SentenceTransformer` block. Both are gone. The
>   `_load_models` factory is the seam `_load_sentence_transformer` wanted, and
>   `categorise_papers` is where the `vector=` parameter now belongs.
> - **Phase 9** (`ImportWorker`) quotes `_apply_categorisation` and the `pending_index` commit
>   block. Both are gone. The vector store's `flush()` belongs in `_flush_pending`, and the
>   per-paper `encode` belongs inside `categorise_papers`, which already batches.
> - **Phase 13** touches `ImportWorker.__init__`, which now carries a block of run state.
>
> Everything else here (new modules, settings, dialogs, taxonomy, YAKE) is untouched.

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
