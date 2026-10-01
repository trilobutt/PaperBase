"""Tests for paperbase.core.assign: pure array maths, no embedding model required."""

import warnings

import numpy as np
import pytest

from paperbase.core.assign import labels_above, normalise


def test_orthogonal_basis() -> None:
    docs = np.eye(4, dtype=np.float32)
    labels = np.eye(4, dtype=np.float32)

    result = labels_above(docs, labels, threshold=0.5)

    assert len(result) == 4
    for i, pairs in enumerate(result):
        assert pairs == [(i, 1.0)]


def test_threshold_filters() -> None:
    label = np.array([[1.0, 0.0]], dtype=np.float32)
    doc = np.array([[0.4, float(np.sqrt(1 - 0.4**2))]], dtype=np.float32)

    assert labels_above(doc, label, threshold=0.5) == [[]]

    result = labels_above(doc, label, threshold=0.3)
    assert len(result[0]) == 1
    idx, score = result[0][0]
    assert idx == 0
    assert score == pytest.approx(0.4, abs=1e-5)


def test_returns_every_label_above_threshold_in_score_order() -> None:
    dim = 6
    doc = np.zeros((1, dim), dtype=np.float32)
    doc[0, 0] = 1.0

    cosines = [0.9, 0.7, 0.5, 0.3, 0.1]
    labels = np.zeros((5, dim), dtype=np.float32)
    for i, c in enumerate(cosines):
        labels[i, 0] = c
        labels[i, i + 1] = float(np.sqrt(1 - c**2))

    everything = labels_above(doc, labels, threshold=0.0)
    assert [idx for idx, _ in everything[0]] == [0, 1, 2, 3, 4]

    pairs = labels_above(doc, labels, threshold=0.4)[0]
    assert [idx for idx, _ in pairs] == [0, 1, 2]
    for (_, score), expected in zip(pairs, cosines[:3], strict=True):
        assert score == pytest.approx(expected, abs=1e-5)


def test_chunk_invariance() -> None:
    doc_raw = np.random.default_rng(1).standard_normal((10, 8)).astype(np.float32)
    label_raw = np.random.default_rng(2).standard_normal((4, 8)).astype(np.float32)
    docs = normalise(doc_raw)
    labels = normalise(label_raw)

    small = labels_above(docs, labels, threshold=0.0, chunk=3)
    large = labels_above(docs, labels, threshold=0.0, chunk=100)

    # Compared with a tolerance, not `==`: matmul batched over a different chunk size sums
    # in a different order, so float32 scores can differ in the last bit. The label set and
    # its order must still be identical.
    assert len(small) == len(large)
    for row_small, row_large in zip(small, large, strict=True):
        assert [idx for idx, _ in row_small] == [idx for idx, _ in row_large]
        for (_, score_small), (_, score_large) in zip(row_small, row_large, strict=True):
            assert score_small == pytest.approx(score_large, abs=1e-5)


def test_zero_vector_gets_nothing() -> None:
    docs = np.array([[0.0, 0.0], [1.0, 0.0]], dtype=np.float32)
    labels = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)

    # threshold is deliberately below the zero-vector's own score (0.0), so a non-special-
    # cased implementation would still emit matches for the zero row.
    result = labels_above(docs, labels, threshold=-1.0)

    assert result[0] == []
    assert result[1] != []


def test_empty_inputs() -> None:
    labels = np.zeros((3, 4), dtype=np.float32)
    docs = np.zeros((0, 4), dtype=np.float32)
    assert labels_above(docs, labels, threshold=0.0) == []

    docs2 = np.zeros((2, 4), dtype=np.float32)
    empty_labels = np.zeros((0, 4), dtype=np.float32)
    assert labels_above(docs2, empty_labels, threshold=0.0) == [[], []]


def test_normalise_zero_row() -> None:
    vectors = np.array([[0.0, 0.0, 0.0], [3.0, 4.0, 0.0]], dtype=np.float32)

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        result = normalise(vectors)

    assert result.dtype == np.float32
    assert not np.any(np.isnan(result))
    assert np.array_equal(result[0], np.zeros(3, dtype=np.float32))
    assert np.allclose(result[1], [0.6, 0.8, 0.0])
