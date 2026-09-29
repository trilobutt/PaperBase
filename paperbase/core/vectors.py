"""
VectorStore: a persistent float32 embedding cache backing EmbeddingCategoriser.

On-disk format is two files sharing a stem:

- `path` is a headerless blob of `float32` rows, `dim` values each, written in insertion
  order with no header and no per-row metadata.
- `path.with_suffix(".json")` is the sidecar, `{"dim": int, "model": str, "ids": [int, ...]}`,
  where `ids[i]` is the paper id stored in row `i` of the blob.

The pairing is derived data, not a source of truth: it always trails behind the vectors it was
computed from, and a half-written pair (blob and sidecar out of step) is the expected outcome of
a crash or power cut mid-import. `open()` reconciles rather than raising, and a sidecar whose
`dim` or `model` no longer matches the running model is a model change, not corruption, so it
resets rather than raising too.
"""

import json
import logging
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

DIM = 384
BUFFER_LIMIT = 200  # rows held in memory before an automatic flush


class VectorStore:
    """Paper-id-keyed float32 vector cache backed by a blob file plus a JSON sidecar."""

    def __init__(self, path: Path, dim: int = DIM, model_name: str = "") -> None:
        self._path = path
        self._sidecar = path.with_suffix(".json")
        self._dim = dim
        self._model_name = model_name
        self._lock = threading.Lock()
        self._ids: list[int] = []
        self._id_index: dict[int, int] = {}
        self._buffer: dict[int, np.ndarray] = {}

    @property
    def dim(self) -> int:
        with self._lock:
            return self._dim

    def open(self) -> None:
        """Read the sidecar if present, else start empty, then reconcile against the blob."""
        with self._lock:
            self._load()

    def close(self) -> None:
        """Flush any buffered rows so nothing added since the last flush is lost."""
        with self._lock:
            self._flush_locked()

    def __len__(self) -> int:
        with self._lock:
            return len(self._ids) + len(self._buffer)

    def has(self, paper_id: int) -> bool:
        with self._lock:
            return paper_id in self._buffer or paper_id in self._id_index

    def get(self, paper_id: int) -> Optional[np.ndarray]:
        with self._lock:
            if paper_id in self._buffer:
                return self._buffer[paper_id].copy()
            row = self._id_index.get(paper_id)
            if row is None:
                return None
            return self._read_row(row)

    def missing(self, paper_ids: Iterable[int]) -> list[int]:
        with self._lock:
            return [
                pid for pid in paper_ids if pid not in self._buffer and pid not in self._id_index
            ]

    def add(self, paper_id: int, vector: np.ndarray) -> None:
        """Validate and store `vector` under `paper_id`, overwriting any prior value.

        Raises:
            ValueError: `vector` is not shape `(dim,)`, or contains a non-finite value.
        """
        with self._lock:
            if vector.shape != (self._dim,):
                raise ValueError(
                    f"vector for paper {paper_id} has shape {vector.shape}, "
                    f"expected ({self._dim},)"
                )
            if not np.all(np.isfinite(vector)):
                raise ValueError(f"vector for paper {paper_id} contains non-finite values")

            stored = vector.astype(np.float32, copy=True)

            row = self._id_index.get(paper_id)
            if row is not None:
                self._write_row(row, stored)
                return

            self._buffer[paper_id] = stored
            if len(self._buffer) >= BUFFER_LIMIT:
                self._flush_locked()

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def matrix(self) -> tuple[np.ndarray, list[int]]:
        """Flush, then return every stored vector as one contiguous, memory-mapped array."""
        with self._lock:
            self._flush_locked()
            if not self._ids:
                return np.zeros((0, self._dim), dtype=np.float32), []
            mat = np.memmap(self._path, dtype=np.float32, mode="r").reshape(-1, self._dim)
            return mat, list(self._ids)

    def clear(self) -> None:
        with self._lock:
            self._reset_files()

    # -- internals; callers must already hold self._lock -----------------------------------

    def _load(self) -> None:
        self._ids = []
        self._id_index = {}
        self._buffer = {}

        if not self._sidecar.exists():
            # The first flush writes the blob before the sidecar, so a crash between the
            # two leaves rows with no id map. Appending after them would put every later
            # vector at the wrong row, so the orphaned rows go.
            if self._path.exists():
                logger.warning(
                    "Vector store %s: blob present with no sidecar; discarding it.",
                    self._path,
                )
                self._path.unlink()
            return

        try:
            payload = json.loads(self._sidecar.read_text(encoding="utf-8"))
            sidecar_dim = int(payload["dim"])
            sidecar_model = str(payload.get("model", ""))
            ids = [int(i) for i in payload["ids"]]
        except (ValueError, KeyError, TypeError) as exc:
            logger.warning("Vector store %s: unreadable sidecar (%s); resetting.", self._path, exc)
            self._reset_files()
            return

        if sidecar_dim != self._dim:
            logger.warning(
                "Vector store %s: sidecar dim %d does not match configured dim %d; resetting.",
                self._path,
                sidecar_dim,
                self._dim,
            )
            self._reset_files()
            return
        # Two models can share a dimension (all-MiniLM-L6-v2 and bge-small-en-v1.5 are
        # both 384), so dim alone cannot tell that the model changed. Vectors from two
        # embedding spaces in one matrix would assign labels by noise.
        if sidecar_model and self._model_name and sidecar_model != self._model_name:
            logger.warning(
                "Vector store %s: built with model %r, now %r; resetting.",
                self._path,
                sidecar_model,
                self._model_name,
            )
            self._reset_files()
            return

        row_bytes = self._dim * 4
        rows_on_disk = self._path.stat().st_size // row_bytes if self._path.exists() else 0
        if rows_on_disk != len(ids):
            n = min(rows_on_disk, len(ids))
            logger.warning(
                "Vector store %s: %d rows on disk but %d ids in sidecar; truncating to %d rows.",
                self._path,
                rows_on_disk,
                len(ids),
                n,
            )
            if self._path.exists():
                with self._path.open("r+b") as f:
                    f.truncate(n * row_bytes)
            ids = ids[:n]
            self._write_sidecar(ids)

        self._ids = ids
        self._rebuild_index()

    def _reset_files(self) -> None:
        self._path.unlink(missing_ok=True)
        self._sidecar.unlink(missing_ok=True)
        self._ids = []
        self._id_index = {}
        self._buffer = {}

    def _rebuild_index(self) -> None:
        self._id_index = {pid: i for i, pid in enumerate(self._ids)}

    def _read_row(self, row: int) -> np.ndarray:
        row_bytes = self._dim * 4
        with self._path.open("rb") as f:
            f.seek(row * row_bytes)
            data = f.read(row_bytes)
        return np.frombuffer(data, dtype=np.float32).copy()

    def _write_row(self, row: int, vector: np.ndarray) -> None:
        row_bytes = self._dim * 4
        with self._path.open("r+b") as f:
            f.seek(row * row_bytes)
            f.write(vector.tobytes())

    def _flush_locked(self) -> None:
        if not self._buffer:
            return
        with self._path.open("ab") as f:
            for pid, vector in self._buffer.items():
                f.write(vector.tobytes())
                self._ids.append(pid)
                self._id_index[pid] = len(self._ids) - 1
        self._buffer.clear()
        self._write_sidecar(self._ids)

    def _write_sidecar(self, ids: list[int]) -> None:
        payload = {"dim": self._dim, "model": self._model_name, "ids": list(ids)}
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(self._sidecar)
