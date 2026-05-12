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
        """
        Returns (collection_ids, tags) to merge onto the paper.
        Creates any missing top-level collections in the DB.
        Returns ([], []) if model not loaded or no categories configured.
        """
        if self._model is None or not self._categories:
            return [], []

        text = f"{paper.title} {paper.abstract}".strip()
        if not text:
            return [], []

        import numpy as np

        with self._lock:
            doc_emb = self._model.encode(text, normalize_embeddings=True)

            matched_names: list[str] = []
            for name, cat_emb in self._category_embeddings.items():
                sim = float(np.dot(doc_emb, cat_emb))
                if sim >= self._threshold:
                    matched_names.append(name)

            tags: list[str] = []
            if self._kw_model and paper.abstract:
                try:
                    kws = self._kw_model.extract_keywords(
                        paper.abstract,
                        keyphrase_ngram_range=(1, 2),
                        stop_words="english",
                        use_mmr=True,
                        diversity=0.5,
                        top_n=self._tag_count,
                    )
                    tags = [kw for kw, _score in kws]
                except Exception as e:
                    logger.warning("Keyword extraction failed for paper %s: %s", paper.id, e)

        # DB operations outside the lock to avoid holding it longer than needed.
        col_ids: list[int] = []
        for name in matched_names:
            col_id = _get_or_create_collection(name, db)
            if col_id is not None:
                col_ids.append(col_id)

        return col_ids, tags


def _get_or_create_collection(name: str, db: Database) -> Optional[int]:
    for col in db.get_collections():
        if col.name == name and col.parent_id is None:
            return col.id
    try:
        return db.insert_collection(Collection(id=None, name=name, parent_id=None))
    except Exception as e:
        logger.error("Failed to create collection '%s': %s", name, e)
        return None


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
        done = 0

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
