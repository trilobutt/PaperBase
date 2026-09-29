"""
Embedding-based auto-categorisation and keyword tagging.

Uses sentence-transformers (all-MiniLM-L6-v2, ~23 MB, CPU-friendly) for category
assignment: cosine similarity between a paper's embedding and either user-defined
free-text category descriptions or a fixed taxonomy (paperbase.core.taxonomy). Both
assign papers to matching collections in the DB. Tag extraction uses YAKE
(paperbase.core.keywords) and needs no model at all.

sentence-transformers is optional at import time; it is loaded lazily inside
load_model(). If not installed the categoriser returns empty results silently.
"""
import logging
import threading
import time
from typing import Optional

import numpy as np
from PyQt6.QtCore import QThread, pyqtSignal

from paperbase.core.assign import top_labels
from paperbase.core.db import Database
from paperbase.core.keywords import extract_keywords
from paperbase.core.taxonomy import Label
from paperbase.core.vectors import VectorStore
from paperbase.models.collection import Collection
from paperbase.models.paper import Paper

logger = logging.getLogger(__name__)

_BATCH_SIZE = 64          # papers per model pass in the retroactive worker
_ENCODE_BATCH = 64        # sentence-transformers internal batch size


def _load_sentence_transformer(name: str):
    """Import and construct the embedding model.

    A module-level factory so tests can substitute a fake: sentence-transformers is not a
    hard dependency of the rest of the application, and is not installed on every machine.
    """
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer(name)


def paper_text(paper: Paper) -> str:
    """The text a paper is embedded as: title, then abstract."""
    return f"{paper.title}. {paper.abstract}".strip(" .")


class EmbeddingCategoriser:
    MODEL_NAME = "all-MiniLM-L6-v2"

    def __init__(self) -> None:
        self._model = None
        self._lock = threading.Lock()
        # name -> numpy array (populated after load_model)
        self._category_embeddings: dict[str, object] = {}
        self._categories: list[dict] = []
        self._threshold: float = 0.35
        self._tag_count: int = 5
        self._labels: list[Label] = []
        self._label_matrix: Optional[np.ndarray] = None
        self._top_k: int = 4

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    @property
    def has_categories(self) -> bool:
        return bool(self._categories) or bool(self._labels)

    @property
    def has_labels(self) -> bool:
        return bool(self._labels)

    @property
    def labels(self) -> list[Label]:
        return list(self._labels)

    def label_matrix(self) -> Optional[np.ndarray]:
        return self._label_matrix

    @property
    def threshold(self) -> float:
        return self._threshold

    @property
    def top_k(self) -> int:
        return self._top_k

    @property
    def tag_count(self) -> int:
        return self._tag_count

    def load_model(self) -> None:
        """Load the embedding model (blocking). Safe to call from any thread."""
        with self._lock:
            if self._model is not None:
                return
            try:
                model = _load_sentence_transformer(self.MODEL_NAME)
            except ImportError:
                logger.error(
                    "sentence-transformers is required for auto-categorisation. "
                    "Run: pip install sentence-transformers"
                )
                return
            self._model = model
            if self._categories:
                self._recompute_embeddings()
            if self._labels:
                self._recompute_label_matrix()
            logger.info("Loaded embedding model %s", self.MODEL_NAME)

    def update_settings(
        self,
        categories: list[dict],
        threshold: float,
        tag_count: int,
        labels: Optional[list[Label]] = None,
        top_k: int = 4,
    ) -> None:
        """Update categories, taxonomy labels and parameters. Recomputes embeddings and the
        label matrix if the model is loaded."""
        self._categories = categories
        self._threshold = threshold
        self._tag_count = tag_count
        self._labels = list(labels) if labels else []
        self._top_k = top_k
        if self._model is not None:
            with self._lock:
                self._recompute_embeddings()
                self._recompute_label_matrix()

    def _recompute_embeddings(self) -> None:
        # Caller must hold self._lock.
        self._category_embeddings = {}
        for cat in self._categories:
            text = (cat.get("description") or "").strip() or cat["name"]
            emb = self._model.encode(text, normalize_embeddings=True)
            self._category_embeddings[cat["name"]] = emb

    def _recompute_label_matrix(self) -> None:
        # Caller must hold self._lock.
        if not self._labels:
            self._label_matrix = None
            return
        vecs = self._model.encode(
            [lab.text for lab in self._labels],
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        self._label_matrix = np.asarray(vecs, dtype=np.float32)

    def encode(self, text: str) -> Optional[np.ndarray]:
        if self._model is None or not text:
            return None
        with self._lock:
            vec = self._model.encode(text, normalize_embeddings=True)
        return np.asarray(vec, dtype=np.float32)

    def encode_batch(self, texts: list[str]) -> Optional[np.ndarray]:
        if self._model is None or not texts:
            return None
        with self._lock:
            vecs = self._model.encode(
                texts, batch_size=_ENCODE_BATCH, normalize_embeddings=True,
                show_progress_bar=False,
            )
        return np.asarray(vecs, dtype=np.float32)

    def categories_for_vector(self, vector: np.ndarray) -> list[str]:
        """Free-text category names whose embedding is within threshold of vector."""
        if vector is None or not self._category_embeddings:
            return []
        names = list(self._category_embeddings.keys())
        cat_matrix = np.vstack([self._category_embeddings[n] for n in names])
        sims = cat_matrix @ vector
        return [n for n, s in zip(names, sims) if s >= self._threshold]

    def categorise_paper(self, paper: Paper, db: Database) -> tuple[list[int], list[str]]:
        """Single-paper wrapper. Prefer categorise_papers: the model call costs nearly the
        same for one document as for a batch of 64."""
        return self.categorise_papers([paper], db)[0]

    def categorise_papers(
        self,
        papers: list[Paper],
        db: Database,
        vector_store: Optional[VectorStore] = None,
    ) -> list[tuple[list[int], list[str]]]:
        """Categorise a batch in one model pass.

        Returns one (collection_ids, tags) pair per input paper, positionally, to merge onto
        that paper. Creates any missing top-level collections. Tags come from YAKE and need no
        model. When vector_store is given, every live paper's own embedding is persisted into
        it as part of this same batched encode, so a caller never has to encode a second time
        just to store what this method already computed.
        """
        if not papers:
            return []

        tags_by_idx: dict[int, list[str]] = {}
        for i, p in enumerate(papers):
            kw_text = p.abstract or p.title
            if kw_text:
                tags_by_idx[i] = extract_keywords(kw_text, top=self._tag_count)

        if self._model is None:
            return [([], tags_by_idx.get(i, [])) for i in range(len(papers))]

        texts = [paper_text(p) for p in papers]
        live = [i for i, t in enumerate(texts) if t]
        if not live:
            return [([], tags_by_idx.get(i, [])) for i in range(len(papers))]

        matched: dict[int, list[str]] = {}
        label_hits: dict[int, list[int]] = {}
        with self._lock:
            doc_embs = np.asarray(
                self._model.encode(
                    [texts[i] for i in live],
                    batch_size=_ENCODE_BATCH,
                    normalize_embeddings=True,
                    show_progress_bar=False,
                ),
                dtype=np.float32,
            )

            if vector_store is not None:
                for row, paper_idx in enumerate(live):
                    pid = papers[paper_idx].id
                    if pid is not None:
                        vector_store.add(pid, doc_embs[row])

            names = list(self._category_embeddings.keys())
            if names:
                cat_matrix = np.vstack([self._category_embeddings[n] for n in names])
                hits = (doc_embs @ cat_matrix.T) >= self._threshold
                for row, paper_idx in enumerate(live):
                    matched[paper_idx] = [n for n, hit in zip(names, hits[row]) if hit]

            if self._label_matrix is not None:
                assignments = top_labels(
                    doc_embs, self._label_matrix, self._threshold, self._top_k
                )
                for row, paper_idx in enumerate(live):
                    label_hits[paper_idx] = [i for i, _score in assignments[row]]

        # DB work outside the lock, and one get_collections() for the whole batch rather than
        # once per paper: free-text categories and taxonomy labels share the same resolve call.
        wanted_names = {n for ns in matched.values() for n in ns}
        label_name_by_index = {
            i: self._labels[i].name for i in {j for js in label_hits.values() for j in js}
            if 0 <= i < len(self._labels)
        }
        wanted_names |= set(label_name_by_index.values())
        col_by_name = _resolve_collections(wanted_names, db)

        results: list[tuple[list[int], list[str]]] = []
        for i in range(len(papers)):
            col_ids = [col_by_name[n] for n in matched.get(i, []) if n in col_by_name]
            for label_idx in label_hits.get(i, []):
                name = label_name_by_index.get(label_idx)
                if name and name in col_by_name:
                    col_ids.append(col_by_name[name])
            results.append((sorted(set(col_ids)), tags_by_idx.get(i, [])))
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
    Retroactive categorisation worker, run in two stages.

    Stage "embedding" encodes every paper the vector store does not already hold and
    persists the vectors; stage "assigning" derives collection and tag assignments from
    those vectors and merges them (never replaces) onto each paper's existing
    collection_ids and tags. The vector store's id map is the resume record for the
    expensive stage, so no separate state file is written or read.
    """

    progress = pyqtSignal(int, int)   # done, total
    log_message = pyqtSignal(str)
    stage_changed = pyqtSignal(str)   # "embedding" | "assigning" | "done"
    finished_all = pyqtSignal()

    _CHUNK = 900  # SQLITE_MAX_VARIABLE_NUMBER ceiling

    def __init__(
        self,
        db: Database,
        categoriser: EmbeddingCategoriser,
        store: VectorStore,
        batch_size: int = _BATCH_SIZE,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._db = db
        self._categoriser = categoriser
        self._store = store
        self._batch_size = batch_size
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
                "Model failed to load; collections cannot be assigned. Keyword "
                "extraction needs no model and will still run."
            )

        all_ids = self._db.get_all_paper_ids()
        embedded = 0

        self.stage_changed.emit("embedding")
        todo = self._store.missing(all_ids)
        if todo and self._categoriser.is_loaded:
            embedded = self._run_embedding_stage(todo)
        elif todo:
            self.log_message.emit("Skipping embedding stage: model not loaded.")

        if self._stop_requested:
            self.log_message.emit("Stopped by user.")
            self.stage_changed.emit("done")
            self.finished_all.emit()
            return

        self.stage_changed.emit("assigning")
        updated = self._run_assigning_stage(all_ids)

        self.stage_changed.emit("done")
        summary = f"{embedded:,} papers embedded, {updated:,} papers updated."
        if self._stop_requested:
            self.log_message.emit(f"Stopped by user. {summary}")
        else:
            self.log_message.emit(f"Done. {summary}")
        self.finished_all.emit()

    def _run_embedding_stage(self, todo: list[int]) -> int:
        """Encode and persist every paper id in `todo`. Returns the count embedded."""
        total = len(todo)
        done = 0
        embedded = 0
        chunk_count = 0
        self.progress.emit(done, total)

        for start in range(0, total, self._CHUNK):
            if self._stop_requested:
                break
            while self._pause_requested:
                time.sleep(0.2)

            chunk = todo[start : start + self._CHUNK]
            # A paper with neither title nor abstract embeds as noise, and its labels
            # would be noise too; categorise_papers skips the same papers on import.
            papers = [
                p for p in self._db.get_papers_by_ids(chunk)
                if p.id is not None and paper_text(p)
            ]
            texts = [paper_text(p) for p in papers]

            for sub_start in range(0, len(papers), self._batch_size):
                sub_papers = papers[sub_start : sub_start + self._batch_size]
                sub_texts = texts[sub_start : sub_start + self._batch_size]
                vectors = self._categoriser.encode_batch(sub_texts)
                if vectors is None:
                    continue
                for paper, vector in zip(sub_papers, vectors):
                    # One bad vector costs that paper its vector, never the run: an
                    # exception escaping run() leaves the dialog waiting on a
                    # finished_all that never comes.
                    try:
                        self._store.add(paper.id, vector)
                    except ValueError as e:
                        logger.warning("Paper %d not embedded: %s", paper.id, e)
                    else:
                        embedded += 1

            self._store.flush()
            done += len(chunk)
            chunk_count += 1
            self.progress.emit(done, total)
            if chunk_count % 10 == 0:
                self.log_message.emit(f"Embedding: {done:,} / {total:,}")

        return embedded

    def _run_assigning_stage(self, all_ids: list[int]) -> int:
        """Merge label/category collections and keyword tags onto every paper.

        Returns the count of papers whose row actually changed.
        """
        matrix, row_ids = self._store.matrix()
        row_index = {pid: i for i, pid in enumerate(row_ids)}

        # Labels and their matrix are read once, together, so an index from one always
        # names a row of the other even if Settings replaces the taxonomy mid-run.
        labels = self._categoriser.labels
        label_matrix = self._categoriser.label_matrix()
        assignments: list[list[tuple[int, float]]] = []
        if labels and label_matrix is not None and len(label_matrix) == len(labels):
            assignments = top_labels(
                matrix, label_matrix, self._categoriser.threshold, self._categoriser.top_k
            )

        total = len(all_ids)
        done = 0
        updated = 0
        self.progress.emit(done, total)

        for start in range(0, total, self._CHUNK):
            if self._stop_requested:
                break
            while self._pause_requested:
                time.sleep(0.2)

            chunk = all_ids[start : start + self._CHUNK]
            papers = self._db.get_papers_by_ids(chunk)

            # First pass: work out what each paper matched, with no DB writes yet, so
            # collection resolution below is one get_collections() for the whole chunk
            # rather than one per paper, which at 150k papers is the same cost the
            # per-paper predecessor of categorise_papers paid.
            matched_labels: dict[int, list[str]] = {}
            matched_categories: dict[int, list[str]] = {}
            wanted_names: set[str] = set()
            for paper in papers:
                if paper.id is None:
                    continue
                row = row_index.get(paper.id)
                if row is None:
                    continue
                if assignments:
                    names = [
                        labels[idx].name for idx, _score in assignments[row]
                        if 0 <= idx < len(labels)
                    ]
                    if names:
                        matched_labels[paper.id] = names
                        wanted_names |= set(names)
                category_names = self._categoriser.categories_for_vector(matrix[row])
                if category_names:
                    matched_categories[paper.id] = category_names
                    wanted_names |= set(category_names)

            col_by_name = _resolve_collections(wanted_names, self._db) if wanted_names else {}

            for paper in papers:
                col_ids = set(paper.collection_ids)
                tags = set(paper.tags)

                if paper.id is not None:
                    for name in matched_labels.get(paper.id, []):
                        if name in col_by_name:
                            col_ids.add(col_by_name[name])
                    for name in matched_categories.get(paper.id, []):
                        if name in col_by_name:
                            col_ids.add(col_by_name[name])

                kw_text = paper.abstract or paper.title
                if kw_text:
                    tags |= set(extract_keywords(kw_text, top=self._categoriser.tag_count))

                new_col_ids = sorted(col_ids)
                new_tags = sorted(tags)
                if new_col_ids != sorted(paper.collection_ids) or new_tags != sorted(paper.tags):
                    paper.collection_ids = new_col_ids
                    paper.tags = new_tags
                    self._db.update_paper(paper)
                    updated += 1

            done += len(chunk)
            self.progress.emit(done, total)

        return updated
