"""Shared pytest fixtures and helpers for the PaperBase test suite."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from paperbase.core.db import Database
from paperbase.models.paper import Paper

_file_path_counter = 0


def make_paper(**overrides: Any) -> Paper:
    """Build a valid Paper with sane defaults, overridable per call.

    `file_path` is unique per call (module-level counter) because `papers.file_path`
    is UNIQUE in the schema.
    """
    global _file_path_counter
    _file_path_counter += 1
    defaults: dict[str, Any] = {
        "id": None,
        "doi": None,
        "title": "A test paper",
        "authors": ["Smith, J."],
        "journal": "Journal of Tests",
        "year": 2020,
        "volume": "1",
        "issue": "1",
        "pages": "1-10",
        "abstract": "",
        "keywords": [],
        "tags": [],
        "collection_ids": [],
        "file_path": f"C:/tmp/paper_{_file_path_counter}.pdf",
        "date_added": "2020-01-01T00:00:00+00:00",
        "date_modified": "2020-01-01T00:00:00+00:00",
        "metadata_source": "crossref",
        "needs_review": False,
        "open_access": False,
    }
    defaults.update(overrides)
    return Paper(**defaults)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "t.db")
    database.open()
    yield database
    database.close()


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    return tmp_path / "paper_vectors.f32"


def unit_vectors(n: int, dim: int = 384, seed: int = 0) -> np.ndarray:
    """Deterministic, L2-normalised random unit vectors for embedding-store tests."""
    rng = np.random.default_rng(seed)
    vectors = rng.standard_normal((n, dim)).astype(np.float32)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors / norms
