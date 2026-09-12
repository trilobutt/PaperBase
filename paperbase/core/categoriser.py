"""
Embedding-based auto-categorisation and keyword tagging.

Uses sentence-transformers (all-MiniLM-L6-v2, ~23 MB, CPU-friendly) for:
  - Category assignment: cosine similarity between paper text and user-defined
    category descriptions. Assigns papers to matching collections in the DB.
  - Tag extraction: KeyBERT-style keyword extraction from paper abstract.

Both dependencies (sentence-transformers, keybert) are optional at import time;
they are loaded lazily inside load_model(). If not installed the categoriser
returns empty results silently.
"""
import json
import logging
import threading
import time
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QThread, pyqtSignal

from paperbase.core.db import Database
from paperbase.models.collection import Collection
from paperbase.models.paper import Paper

logger = logging.getLogger(__name__)

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


class EmbeddingCategoriser:
    MODEL_NAME = "all-MiniLM-L6-v2"

    def __init__(self) -> None:
        self._model = None
        self._kw_model = None
        self._lock = threading.Lock()
        # name -> numpy array (populated after load_model)
        self._category_embeddings: dict[str, object] = {}
        self._categories: list[dict] = []
        self._threshold: float = 0.35
        self._tag_count: int = 5

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    @property
    def has_categories(self) -> bool:
        return bool(self._categories)

    def load_model(self) -> None:
        """Load the embedding model (blocking). Safe to call from any thread."""
        with self._lock:
            if self._model is not None:
                return
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
            if self._categories:
                self._recompute_embeddings()
            logger.info("Loaded embedding model %s", self.MODEL_NAME)

    def update_settings(
        self,
        categories: list[dict],
        threshold: float,
        tag_count: int,
    ) -> None:
        """Update categories and parameters. Recomputes embeddings if model is loaded."""
        self._categories = categories
        self._threshold = threshold
        self._tag_count = tag_count
        if self._model is not None:
            with self._lock:
                self._recompute_embeddings()

    def _recompute_embeddings(self) -> None:
        # Caller must hold self._lock.
        self._category_embeddings = {}
        for cat in self._categories:
            text = (cat.get("description") or "").strip() or cat["name"]
            emb = self._model.encode(text, normalize_embeddings=True)
            self._category_embeddings[cat["name"]] = emb

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


class CategorizationWorker(QThread):
    """
    Retroactive categorisation worker. Iterates all papers in the DB, runs
    EmbeddingCategoriser on each, and merges the results into existing tags and
    collection_ids (never overwrites user edits). Supports pause/stop and
    persists progress to a state file for resumable runs.
    """

    progress = pyqtSignal(int, int)   # done, total
    log_message = pyqtSignal(str)
    finished_all = pyqtSignal()

    def __init__(
        self,
        db: Database,
        categoriser: EmbeddingCategoriser,
        state_file: Optional[Path] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._db = db
        self._categoriser = categoriser
        self._state_file = state_file
        self._pause_requested = False
        self._stop_requested = False

    def request_pause(self) -> None:
        self._pause_requested = True

    def request_resume(self) -> None:
        self._pause_requested = False

    def request_stop(self) -> None:
        self._stop_requested = True
        self._pause_requested = False

    def run(self) -> None:
        self.log_message.emit("Loading embedding model…")
        self._categoriser.load_model()
        if not self._categoriser.is_loaded:
            self.log_message.emit(
                "Model failed to load. Install sentence-transformers and keybert."
            )
            self.finished_all.emit()
            return

        self.log_message.emit("Model ready. Starting categorisation…")
        processed = self._load_state()
        all_ids = self._db.get_all_paper_ids()
        total = len(all_ids)
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

        self._save_state(processed)
        self.log_message.emit(f"Done. {done:,} / {total:,} papers processed.")
        self.finished_all.emit()

    def _load_state(self) -> set[int]:
        if self._state_file and self._state_file.exists():
            try:
                data = json.loads(self._state_file.read_text(encoding="utf-8"))
                return set(data.get("processed", []))
            except Exception:
                pass
        return set()

    def _save_state(self, processed: set[int]) -> None:
        if self._state_file:
            try:
                self._state_file.write_text(
                    json.dumps({"processed": list(processed)}, indent=2),
                    encoding="utf-8",
                )
            except Exception as e:
                logger.warning("Failed to save categorisation state: %s", e)
