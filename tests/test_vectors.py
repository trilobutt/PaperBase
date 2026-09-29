"""Tests for VectorStore: the persistent float32 embedding cache."""

import json

import numpy as np
import pytest

from paperbase.core.vectors import BUFFER_LIMIT, DIM, VectorStore

from tests.conftest import unit_vectors


def test_empty_store(store_path):
    store = VectorStore(store_path)
    store.open()

    assert len(store) == 0
    mat, ids = store.matrix()
    assert mat.shape == (0, DIM)
    assert ids == []
    assert store.has(1) is False
    assert store.get(1) is None


def test_add_flush_reopen(store_path):
    vectors = unit_vectors(5)
    store = VectorStore(store_path)
    store.open()
    for i, v in enumerate(vectors, start=1):
        store.add(i, v)
    store.flush()

    reopened = VectorStore(store_path)
    reopened.open()

    assert len(reopened) == 5
    for i, v in enumerate(vectors, start=1):
        got = reopened.get(i)
        assert got is not None
        assert np.allclose(got, v)

    mat, ids = reopened.matrix()
    assert ids == [1, 2, 3, 4, 5]
    assert np.allclose(mat, vectors)


def test_buffered_reads(store_path):
    vectors = unit_vectors(3)
    store = VectorStore(store_path)
    store.open()
    for i, v in enumerate(vectors, start=1):
        store.add(i, v)

    for i, v in enumerate(vectors, start=1):
        assert store.has(i)
        assert np.allclose(store.get(i), v)

    mat, ids = store.matrix()
    assert mat.shape == (3, DIM)
    assert ids == [1, 2, 3]


def test_auto_flush_at_limit(store_path):
    n = BUFFER_LIMIT + 50
    vectors = unit_vectors(n)
    store = VectorStore(store_path)
    store.open()
    for i, v in enumerate(vectors, start=1):
        store.add(i, v)

    assert store_path.stat().st_size // (DIM * 4) >= BUFFER_LIMIT


def test_overwrite_flushed_row(store_path):
    vectors = unit_vectors(3, seed=1)
    store = VectorStore(store_path)
    store.open()
    for i, v in enumerate(vectors, start=1):
        store.add(i, v)
    store.flush()

    replacement = unit_vectors(1, seed=99)[0]
    store.add(2, replacement)
    assert len(store) == 3

    reopened = VectorStore(store_path)
    reopened.open()
    assert len(reopened) == 3
    assert np.allclose(reopened.get(2), replacement)


def test_rejects_bad_vector(store_path):
    store = VectorStore(store_path)
    store.open()

    bad_length = np.zeros(10, dtype=np.float32)
    with pytest.raises(ValueError):
        store.add(1, bad_length)

    bad_values = unit_vectors(1)[0].copy()
    bad_values[0] = np.inf
    with pytest.raises(ValueError):
        store.add(2, bad_values)


def test_truncated_blob_recovers(store_path):
    vectors = unit_vectors(10, seed=2)
    store = VectorStore(store_path)
    store.open()
    for i, v in enumerate(vectors, start=1):
        store.add(i, v)
    store.flush()

    row_bytes = DIM * 4
    with store_path.open("r+b") as f:
        f.truncate(7 * row_bytes)

    reopened = VectorStore(store_path)
    reopened.open()

    assert len(reopened) == 7

    sidecar = store_path.with_suffix(".json")
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert len(payload["ids"]) == 7


def test_dim_mismatch_resets(store_path):
    sidecar = store_path.with_suffix(".json")
    sidecar.write_text(json.dumps({"dim": 128, "model": "", "ids": [1, 2]}), encoding="utf-8")
    store_path.write_bytes(b"\x00" * (128 * 4 * 2))

    store = VectorStore(store_path, dim=DIM)
    store.open()

    assert len(store) == 0


def test_missing(store_path):
    vectors = unit_vectors(2, seed=3)
    store = VectorStore(store_path)
    store.open()
    store.add(1, vectors[0])
    store.add(3, vectors[1])
    store.flush()

    assert store.missing([1, 2, 3, 4]) == [2, 4]


def test_orphan_blob_without_sidecar_is_discarded(store_path):
    # The first flush writes the blob before the sidecar; a crash between them must not
    # leave rows that later appends would sit behind, misaligned with their ids.
    store_path.write_bytes(unit_vectors(3, seed=4).tobytes())

    store = VectorStore(store_path)
    store.open()
    assert len(store) == 0

    fresh = unit_vectors(1, seed=5)[0]
    store.add(7, fresh)
    store.flush()

    reopened = VectorStore(store_path)
    reopened.open()
    assert len(reopened) == 1
    assert np.allclose(reopened.get(7), fresh)


def test_unreadable_sidecar_resets(store_path):
    store_path.with_suffix(".json").write_text("{not json", encoding="utf-8")
    store_path.write_bytes(unit_vectors(2).tobytes())

    store = VectorStore(store_path)
    store.open()

    assert len(store) == 0


def test_model_change_resets(store_path):
    store = VectorStore(store_path, model_name="all-MiniLM-L6-v2")
    store.open()
    store.add(1, unit_vectors(1)[0])
    store.flush()

    same = VectorStore(store_path, model_name="all-MiniLM-L6-v2")
    same.open()
    assert len(same) == 1

    # Same dimension, different embedding space: dim alone cannot catch this.
    other = VectorStore(store_path, model_name="BAAI/bge-small-en-v1.5")
    other.open()
    assert len(other) == 0
