"""Tests for paperbase.core.categoriser, against a stubbed embedding model.

No real sentence-transformers model is installed here, so every test installs
`FakeModel` through the `_load_sentence_transformer` seam
(`monkeypatch.setattr(categoriser, "_load_sentence_transformer", ...)`) rather than
calling the real network/disk-backed loader.
"""

import hashlib

import numpy as np
import pytest

import paperbase.core.categoriser as categoriser
from paperbase.core.categoriser import CategorizationWorker, EmbeddingCategoriser, paper_text
from paperbase.core.taxonomy import Label
from paperbase.core.vectors import VectorStore
from paperbase.models.collection import Collection

from tests.conftest import make_paper

ABSTRACT_TEXT = (
    "This study examines particle interactions and cellular biology processes across a "
    "controlled laboratory experiment, measuring nitrogen transfer and radiative decay "
    "rates over several growing seasons."
)


def _vector_for_text(text: str) -> np.ndarray:
    """A deterministic unit vector derived from a stable hash of `text`."""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    seed = int.from_bytes(digest[:8], "big")
    rng = np.random.default_rng(seed)
    vec = rng.standard_normal(384).astype(np.float32)
    return vec / np.linalg.norm(vec)


class FakeModel:
    """Deterministic stand-in for SentenceTransformer: same text always gives the same
    unit vector, and every call is recorded so tests can assert on model traffic."""

    def __init__(self) -> None:
        self.calls: list[int] = []

    def encode(self, texts, normalize_embeddings: bool = True, **kwargs):
        if isinstance(texts, str):
            self.calls.append(1)
            return _vector_for_text(texts)
        texts = list(texts)
        self.calls.append(len(texts))
        return np.vstack([_vector_for_text(t) for t in texts])


@pytest.fixture
def fake_model(monkeypatch) -> FakeModel:
    model = FakeModel()
    monkeypatch.setattr(categoriser, "_load_sentence_transformer", lambda name: model)
    return model


def test_encode_shapes(fake_model: FakeModel) -> None:
    cat = EmbeddingCategoriser()
    cat.load_model()

    vec = cat.encode("hello world")
    assert vec.shape == (384,)
    assert vec.dtype == np.float32
    assert np.linalg.norm(vec) == pytest.approx(1.0, abs=1e-5)

    batch = cat.encode_batch(["one", "two", "three"])
    assert batch.shape == (3, 384)
    assert batch.dtype == np.float32


def test_update_settings_builds_label_matrix(fake_model: FakeModel) -> None:
    cat = EmbeddingCategoriser()
    cat.load_model()

    labels = [Label(name=f"Label {i}") for i in range(5)]
    cat.update_settings(categories=[], threshold=0.3, tag_count=5, labels=labels)
    matrix = cat.label_matrix()
    assert matrix is not None
    assert matrix.shape == (5, 384)

    cat.update_settings(categories=[], threshold=0.3, tag_count=5, labels=[])
    assert cat.label_matrix() is None


def test_categorise_papers_persists_to_vector_store(fake_model, db, store_path) -> None:
    """Adapted from the plan's original "uses_supplied_vector" test: `categorise_paper`
    no longer takes a `vector=` argument (that seam moved to `categorise_papers`'s
    `vector_store=`, per Phase 7). The real thing worth testing is that a supplied
    `vector_store` receives exactly the vector `categorise_papers` computed for each
    paper, matching a fresh `encode()` of the same text.
    """
    cat = EmbeddingCategoriser()
    cat.update_settings(categories=[], threshold=0.3, tag_count=5)
    cat.load_model()

    paper = make_paper(title="Persisted Vector Paper", abstract=ABSTRACT_TEXT)
    paper.id = db.insert_paper(paper)

    store = VectorStore(store_path)
    store.open()

    results = cat.categorise_papers([paper], db, vector_store=store)
    assert len(results) == 1
    assert store.has(paper.id)

    expected = cat.encode(paper_text(paper))
    stored = store.get(paper.id)
    assert stored is not None
    assert np.allclose(stored, expected)


def test_categorise_paper_creates_collections(fake_model, db) -> None:
    cat = EmbeddingCategoriser()
    cat.update_settings(
        categories=[{"name": "Physics", "description": "particles and forces"}],
        # -1.0 always matches: cosine similarity never falls below it, so the match does
        # not depend on FakeModel's vectors carrying real semantic structure.
        threshold=-1.0,
        tag_count=5,
    )
    cat.load_model()

    paper = make_paper(title="Quantum Mechanics", abstract=ABSTRACT_TEXT)
    paper.id = db.insert_paper(paper)

    col_ids, _tags = cat.categorise_paper(paper, db)
    assert col_ids, "expected at least one collection id"

    collections = db.get_collections()
    names = {c.name for c in collections}
    assert "Physics" in names
    assert set(col_ids) <= {c.id for c in collections}


def test_tags_without_model(db) -> None:
    cat = EmbeddingCategoriser()  # model never loaded

    paper = make_paper(title="Untagged Paper", abstract=ABSTRACT_TEXT)
    paper.id = db.insert_paper(paper)

    col_ids, tags = cat.categorise_paper(paper, db)
    assert col_ids == []
    assert tags, "expected keyword tags even without a model"


def test_worker_embeds_and_stores(fake_model, db, store_path) -> None:
    cat = EmbeddingCategoriser()
    labels = [Label(name=f"Topic {i}", description=f"about topic {i}") for i in range(4)]
    cat.update_settings(categories=[], threshold=0.3, tag_count=5, labels=labels)

    papers = [make_paper(title=f"Paper {i}", abstract=ABSTRACT_TEXT) for i in range(20)]
    for p in papers:
        p.id = db.insert_paper(p)

    store = VectorStore(store_path)
    store.open()

    worker = CategorizationWorker(db, cat, store)
    worker.run()

    assert len(store) == 20
    for p in papers:
        assert store.has(p.id)


def test_worker_second_run_embeds_nothing(fake_model, db, store_path) -> None:
    cat = EmbeddingCategoriser()
    labels = [Label(name=f"Topic {i}") for i in range(4)]
    cat.update_settings(categories=[], threshold=0.3, tag_count=5, labels=labels)

    papers = [make_paper(title=f"Paper {i}", abstract=ABSTRACT_TEXT) for i in range(20)]
    for p in papers:
        p.id = db.insert_paper(p)

    store = VectorStore(store_path)
    store.open()

    worker_one = CategorizationWorker(db, cat, store)
    worker_one.run()
    calls_after_first_run = len(fake_model.calls)
    assert calls_after_first_run > 0

    # Same categoriser (model stays loaded, settings unchanged), a fresh worker sharing the
    # same store: this is the resume path, and it is the central promise of the design.
    worker_two = CategorizationWorker(db, cat, store)
    worker_two.run()
    calls_after_second_run = len(fake_model.calls)

    assert calls_after_second_run == calls_after_first_run
    assert len(store) == 20


def test_worker_merges_never_replaces(fake_model, db, store_path) -> None:
    cat = EmbeddingCategoriser()
    labels = [Label(name="Topic A"), Label(name="Topic B")]
    cat.update_settings(categories=[], threshold=-1.0, tag_count=5, labels=labels)

    manual_collection_id = db.insert_collection(
        Collection(id=None, name="My Manual Collection", parent_id=None)
    )
    paper = make_paper(
        title="Paper with manual tags",
        abstract=ABSTRACT_TEXT,
        tags=["mine"],
        collection_ids=[manual_collection_id],
    )
    paper.id = db.insert_paper(paper)

    store = VectorStore(store_path)
    store.open()

    worker = CategorizationWorker(db, cat, store)
    worker.run()

    updated = db.get_paper(paper.id)
    assert updated is not None
    assert "mine" in updated.tags
    assert manual_collection_id in updated.collection_ids
    # A label match (threshold=-1.0 always matches) must add a collection on top of the
    # manual one, never replace it.
    assert len(updated.collection_ids) > 1


def test_worker_without_model_still_tags(monkeypatch, db, store_path) -> None:
    def _raise_import_error(name):
        raise ImportError("no model available")

    monkeypatch.setattr(categoriser, "_load_sentence_transformer", _raise_import_error)

    cat = EmbeddingCategoriser()
    papers = [make_paper(title=f"Paper {i}", abstract=ABSTRACT_TEXT) for i in range(5)]
    for p in papers:
        p.id = db.insert_paper(p)

    store = VectorStore(store_path)
    store.open()

    worker = CategorizationWorker(db, cat, store)
    worker.run()

    assert len(store) == 0
    for p in papers:
        updated = db.get_paper(p.id)
        assert updated is not None
        assert updated.tags, "expected keyword tags even without a model"
