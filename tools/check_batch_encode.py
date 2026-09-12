"""Verify categorise_papers batches the model encode call instead of calling it per paper.

    py -3.12 tools/check_batch_encode.py

Prints "batch encode OK" and exits 0 on success; raises AssertionError on the first
failing check otherwise. Uses fake model/KeyBERT stand-ins via _load_models so it needs
neither sentence-transformers nor keybert installed.
"""
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import paperbase.core.categoriser as categoriser
from paperbase.core.categoriser import EmbeddingCategoriser
from paperbase.core.db import Database
from paperbase.models.paper import Paper


class FakeModel:
    def __init__(self) -> None:
        self.calls: list[int] = []

    def encode(self, texts, **kwargs):
        if isinstance(texts, str):
            self.calls.append(1)
            return np.ones(4) / 2.0
        self.calls.append(len(texts))
        return np.ones((len(texts), 4)) / 2.0


class FakeKeyBERT:
    def extract_keywords(self, docs, **kwargs):
        return [[("alpha", 0.9), ("beta", 0.8)] for _ in docs]


def _make_paper(i: int) -> Paper:
    return Paper(
        id=None,
        doi=f"10.1/{i}",
        title=f"Title {i}",
        authors=["Lastname, Firstname"],
        journal="A Journal",
        year=2020,
        volume="1",
        issue="1",
        pages="1-10",
        abstract=f"Abstract text for paper {i}.",
        keywords=[],
        tags=[],
        collection_ids=[],
        file_path=f"C:/lib/{i}.pdf",
        date_added="",
        date_modified="",
        metadata_source="manual",
        needs_review=False,
        open_access=False,
    )


def main() -> None:
    fake_model = FakeModel()
    fake_kw = FakeKeyBERT()
    categoriser._load_models = lambda name: (fake_model, fake_kw)

    db_path = Path(tempfile.mkdtemp()) / "paperbase.db"
    db = Database(db_path)
    db.open()

    cat = EmbeddingCategoriser()
    cat.update_settings(
        categories=[
            {"name": "Physics", "description": "particles and forces"},
            {"name": "Biology", "description": "cells and organisms"},
        ],
        threshold=0.1,
        tag_count=5,
    )
    cat.load_model()

    papers = [_make_paper(i) for i in range(64)]

    results = cat.categorise_papers(papers, db)
    assert len(results) == 64, f"expected 64 results, got {len(results)}"
    for col_ids, tags in results:
        assert tags, "expected every pair to carry non-empty tags"

    # Two single-text calls from _recompute_embeddings (one per category), then exactly
    # one batch call of length 64 for the papers.
    assert fake_model.calls == [1, 1, 64], (
        f"expected calls [1, 1, 64], got {fake_model.calls}"
    )

    single = cat.categorise_paper(papers[0], db)
    batch_one = cat.categorise_papers([papers[0]], db)[0]
    assert single == batch_one, (
        f"categorise_paper and categorise_papers([p]) diverged: {single!r} vs {batch_one!r}"
    )

    db.close()
    print("batch encode OK")


if __name__ == "__main__":
    main()
