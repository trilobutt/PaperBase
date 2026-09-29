"""
Label assignment maths: pure functions over arrays, no I/O and no model.

With vectors already on disk (`core/vectors.py`), assigning the whole library to the
taxonomy is one matmul per chunk of documents. Keeping it as pure `numpy` functions is what
lets it be tested on a machine with no embedding model installed.
"""

import numpy as np


def normalise(vectors: np.ndarray) -> np.ndarray:
    """L2-normalise each row of `vectors`.

    A zero row stays zero rather than raising a divide-by-zero warning or producing NaN.

    Args:
        vectors: array of shape `(n, dim)`.

    Returns:
        `float32` array of the same shape, each non-zero row scaled to unit length.
    """
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    safe_norms = np.where(norms == 0, 1.0, norms)
    return (vectors / safe_norms).astype(np.float32)


def top_labels(
    doc_vecs: np.ndarray,
    label_vecs: np.ndarray,
    threshold: float,
    top_k: int,
    chunk: int = 4096,
) -> list[list[tuple[int, float]]]:
    """Match each document row to its best-scoring label rows.

    Both `doc_vecs` and `label_vecs` are assumed already unit-normalised, so the score
    between a document and a label is a plain dot product.

    Args:
        doc_vecs: array of shape `(n_docs, dim)`.
        label_vecs: array of shape `(n_labels, dim)`.
        threshold: minimum score for a label to be included.
        top_k: maximum number of labels returned per document.
        chunk: documents processed per batch, so peak memory is `chunk * n_labels` floats
            rather than `n_docs * n_labels`. The result does not depend on this value.

    Returns:
        One list per document row, each holding at most `top_k` `(label_index, score)`
        pairs with `score >= threshold`, sorted by descending score then ascending label
        index. A document row with zero norm returns an empty list regardless of
        `threshold`. `doc_vecs` with 0 rows returns `[]`; `label_vecs` with 0 rows returns
        one empty list per document.
    """
    n_docs = doc_vecs.shape[0]
    n_labels = label_vecs.shape[0]

    if n_docs == 0:
        return []
    if n_labels == 0:
        return [[] for _ in range(n_docs)]

    k = max(0, min(top_k, n_labels))
    if k == 0:
        return [[] for _ in range(n_docs)]

    # Selection stays in numpy. A Python loop over every (document, label) score took
    # 12 s for 150,000 papers against 300 labels, and a full per-row sort 4 s, where the
    # matmul itself takes a fraction of a second.
    results: list[list[tuple[int, float]]] = []
    for start in range(0, n_docs, chunk):
        block = doc_vecs[start : start + chunk]
        scores = block @ label_vecs.T
        # Each row's k-th best score, found by partition rather than a sort. Everything
        # at or above it (and above threshold) survives; ties at the cut can let more
        # than k through, and the lexsort below settles those by ascending label index.
        kth = np.partition(scores, n_labels - k, axis=1)[:, n_labels - k]
        keep = scores >= np.maximum(kth, threshold)[:, None]
        keep[~np.any(block, axis=1)] = False
        rows, cols = np.nonzero(keep)
        vals = scores[rows, cols]
        order = np.lexsort((cols, -vals, rows))
        counts = np.bincount(rows, minlength=block.shape[0]).tolist()
        cols_l = cols[order].tolist()
        vals_l = vals[order].tolist()
        pos = 0
        for n in counts:
            take = min(n, k)
            results.append(list(zip(cols_l[pos : pos + take], vals_l[pos : pos + take])))
            pos += n

    return results
