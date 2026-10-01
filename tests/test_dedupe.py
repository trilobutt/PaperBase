"""DedupeWorker: one survivor per identical-file group, merged tags, guarded deletion."""

from pathlib import Path

from paperbase.core.dedupe import DedupeWorker, choose_keeper
from paperbase.core.metadata import sha256_file
from tests.conftest import make_paper


class FakeIndexer:
    def __init__(self) -> None:
        self.deleted: list[int] = []

    def delete_documents(self, ids: list[int]) -> None:
        self.deleted.extend(ids)


def _add(db, tmp_path: Path, name: str, data: bytes, **kw) -> int:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    paper = make_paper(file_path=str(path), content_hash=sha256_file(path), **kw)
    return db.insert_paper(paper)


def test_keeps_best_row_merges_tags_deletes_files(db, tmp_path) -> None:
    good = _add(db, tmp_path, "J/a.pdf", b"same", tags=["x"], collection_ids=[1], taxa=["Araneae"])
    bad = _add(db, tmp_path, "Unsorted/b.pdf", b"same", metadata_source="filename",
               needs_review=True, tags=["y"], collection_ids=[2], taxa=["Histeridae"],
               taxa_locked=True)
    groups = db.get_duplicate_hash_groups()
    assert len(groups) == 1

    idx = FakeIndexer()
    worker = DedupeWorker(db, idx, groups)
    worker.run()

    assert db.get_paper(bad) is None
    kept = db.get_paper(good)
    assert kept.tags == ["x", "y"] and kept.collection_ids == [1, 2]
    assert kept.taxa == ["Araneae", "Histeridae"] and kept.taxa_locked is True
    assert (tmp_path / "J/a.pdf").exists() and not (tmp_path / "Unsorted/b.pdf").exists()
    assert idx.deleted == [bad]


def test_changed_copy_is_left_alone(db, tmp_path) -> None:
    keep = _add(db, tmp_path, "a.pdf", b"same")
    other = _add(db, tmp_path, "b.pdf", b"same", metadata_source="xmp")
    groups = db.get_duplicate_hash_groups()
    (tmp_path / "b.pdf").write_bytes(b"edited since the scan")

    DedupeWorker(db, FakeIndexer(), groups).run()

    assert db.get_paper(other) is not None and (tmp_path / "b.pdf").exists()
    assert db.get_paper(keep) is not None


def test_keeper_ranking() -> None:
    a = make_paper(id=2, metadata_source="crossref")
    b = make_paper(id=1, metadata_source="filename")
    assert choose_keeper([a, b]) is a
