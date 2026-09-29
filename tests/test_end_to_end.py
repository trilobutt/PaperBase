"""End-to-end test of the assembled categorisation pipeline.

Every prior test exercises one piece (the embedding maths, the vector store, the worker's
two stages) against a stubbed model. This test runs the whole thing together: a taxonomy
file on disk, a real `Database`, a real `VectorStore`, and `CategorizationWorker` driving
both, with only the embedding model itself stubbed via the `FakeModel`/`_load_sentence_transformer`
seam already defined in `tests.test_categoriser` (imported rather than redefined, so there
is one definition of the fake model for the whole suite).

The real-prose abstracts come from `tools.make_fixture._abstract`, imported directly:
`tools/make_fixture.py` guards its own script body behind `if __name__ == "__main__"`, so
importing `_abstract` from it pulls in no side effects and there is no reason to keep a
second copy of the same template logic in the test suite.
"""

import random

import numpy as np

import paperbase.core.categoriser as categoriser
from paperbase.core.categoriser import CategorizationWorker, EmbeddingCategoriser
from paperbase.core.taxonomy import Label, load_taxonomy, save_taxonomy
from paperbase.core.vectors import VectorStore
from paperbase.ui.settings_dialog import Settings
from tests.conftest import make_paper
from tests.test_categoriser import FakeModel
from tools.make_fixture import _abstract

TAXONOMY_LABELS = [
    Label(
        name="Palaeoecology",
        description="Interactions between fossil organisms and ancient environments",
    ),
    Label(name="Phylogenetics"),
    Label(
        name="Sedimentology",
        description="Formation and structure of sedimentary rock layers",
    ),
    Label(
        name="Isotope geochemistry",
        description="Stable and radiogenic isotopes as environmental tracers",
    ),
    Label(name="Cambrian biota"),
    Label(name="Speciation", description="Processes by which new species arise"),
]
_LABEL_NAMES = {label.name for label in TAXONOMY_LABELS}


def test_full_pipeline(db, store_path, tmp_path, monkeypatch) -> None:
    # 1. A library of 40 papers with real-prose abstracts.
    rng = random.Random(1234)
    papers = [make_paper(title=f"Paper {i}", abstract=_abstract(rng)) for i in range(40)]
    for paper in papers:
        paper.id = db.insert_paper(paper)

    # 2. Taxonomy file of 6 labels.
    taxonomy_path = tmp_path / "taxonomy.txt"
    save_taxonomy(taxonomy_path, TAXONOMY_LABELS)

    # 3. Settings pointing at it, categoriser built from the loaded labels, model stubbed.
    settings = Settings()
    settings.taxonomy_path = str(taxonomy_path)
    labels = load_taxonomy(settings.taxonomy_file())
    assert labels == TAXONOMY_LABELS

    model = FakeModel()
    monkeypatch.setattr(categoriser, "_load_sentence_transformer", lambda name: model)

    cat = EmbeddingCategoriser()
    # threshold=-1.0: cosine similarity never falls below it, so every paper matches its
    # top_k=3 closest labels regardless of FakeModel's vectors carrying real semantic
    # structure, the same device test_categoriser.py uses for a deterministic match.
    cat.update_settings(categories=[], threshold=-1.0, tag_count=5, labels=labels, top_k=3)
    cat.load_model()

    # 4. Vector store and worker, run synchronously (QThread.run() called directly).
    store = VectorStore(store_path)
    store.open()

    worker = CategorizationWorker(db, cat, store)
    worker.run()

    # 5. First-run assertions.
    assert len(store) == 40
    for paper in papers:
        updated = db.get_paper(paper.id)
        assert updated is not None
        assert updated.tags, f"paper {paper.id} got no tags"
        assert len(updated.collection_ids) <= 3, (
            f"paper {paper.id} has {len(updated.collection_ids)} collections, "
            "more than top_k=3"
        )

    collections = db.get_collections()
    collection_names = {c.name for c in collections}
    assert collection_names, "expected taxonomy collections to have been created"
    assert collection_names <= _LABEL_NAMES

    # 6. Re-open the store from disk in a fresh instance: persistence round-trips.
    reopened = VectorStore(store_path)
    reopened.open()
    assert len(reopened) == 40
    spot_id = papers[0].id
    assert spot_id is not None
    original_vec = store.get(spot_id)
    reopened_vec = reopened.get(spot_id)
    assert original_vec is not None
    assert reopened_vec is not None
    assert np.allclose(original_vec, reopened_vec)

    # 7. A second worker run against the same store embeds nothing new.
    model.calls.clear()
    worker_two = CategorizationWorker(db, cat, store)
    worker_two.run()
    assert model.calls == [], "second run should not re-embed any paper"
    assert len(store) == 40

    # 8. One new paper: exactly one new embedding, store grows by one.
    new_paper = make_paper(title="Paper 40", abstract=_abstract(rng))
    new_paper.id = db.insert_paper(new_paper)

    model.calls.clear()
    worker_three = CategorizationWorker(db, cat, store)
    worker_three.run()

    assert sum(model.calls) == 1, "expected exactly one paper embedded in the third run"
    assert len(store) == 41
    assert store.has(new_paper.id)
