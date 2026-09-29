"""Tests for tools/seed_taxonomy.py against a temp SQLite database."""

from pathlib import Path

from paperbase.core.taxonomy import load_taxonomy
from tools.seed_taxonomy import main

from tests.conftest import make_paper


def _seed(db, *, tags_by_count: dict[str, int], journal: str = "Journal of Tests") -> None:
    """Insert one paper per occurrence of each tag in `tags_by_count`."""
    for tag, count in tags_by_count.items():
        for i in range(count):
            db.insert_paper(
                make_paper(title=f"{tag} paper {i}", tags=[tag], journal=journal)
            )


def test_seed_taxonomy_writes_labels_above_min_count(db, tmp_path: Path) -> None:
    _seed(db, tags_by_count={"common tag": 5, "rare tag": 2})
    out = tmp_path / "taxonomy.txt"

    rc = main(["--db", str(tmp_path / "t.db"), "--out", str(out), "--min-count", "3"])

    assert rc == 0
    labels = load_taxonomy(out)
    names = {label.name for label in labels}
    assert "common tag" in names
    assert "rare tag" not in names


def test_seed_taxonomy_truncates_to_max_labels(db, tmp_path: Path) -> None:
    _seed(db, tags_by_count={"tag a": 10, "tag b": 8, "tag c": 6})
    out = tmp_path / "taxonomy.txt"

    rc = main(
        ["--db", str(tmp_path / "t.db"), "--out", str(out), "--min-count", "1", "--max-labels", "2"]
    )

    assert rc == 0
    labels = load_taxonomy(out)
    assert len(labels) == 2
    assert [label.name for label in labels] == ["tag a", "tag b"]


def test_seed_taxonomy_refuses_overwrite_without_force(db, tmp_path: Path) -> None:
    _seed(db, tags_by_count={"common tag": 5})
    out = tmp_path / "taxonomy.txt"
    rc = main(["--db", str(tmp_path / "t.db"), "--out", str(out), "--min-count", "3"])
    assert rc == 0
    original = out.read_text(encoding="utf-8")

    rc2 = main(["--db", str(tmp_path / "t.db"), "--out", str(out), "--min-count", "3"])

    assert rc2 != 0
    assert out.read_text(encoding="utf-8") == original


def test_seed_taxonomy_second_run_with_force_succeeds(db, tmp_path: Path) -> None:
    _seed(db, tags_by_count={"common tag": 5})
    out = tmp_path / "taxonomy.txt"
    main(["--db", str(tmp_path / "t.db"), "--out", str(out), "--min-count", "3"])

    rc = main(
        ["--db", str(tmp_path / "t.db"), "--out", str(out), "--min-count", "3", "--force"]
    )

    assert rc == 0
    labels = load_taxonomy(out)
    assert [label.name for label in labels] == ["common tag"]


def test_seed_taxonomy_journals_are_comments_only(db, tmp_path: Path) -> None:
    _seed(db, tags_by_count={"common tag": 5}, journal="Distinctive Journal Name")
    out = tmp_path / "taxonomy.txt"

    rc = main(["--db", str(tmp_path / "t.db"), "--out", str(out), "--min-count", "3"])

    assert rc == 0
    text = out.read_text(encoding="utf-8")
    assert "Distinctive Journal Name" in text

    labels = load_taxonomy(out)
    assert "Distinctive Journal Name" not in {label.name for label in labels}
    for line in text.splitlines():
        if "Distinctive Journal Name" in line:
            assert line.lstrip().startswith("#")


def test_seed_taxonomy_skips_names_that_cannot_round_trip(db, tmp_path: Path) -> None:
    # A colon would split the tag into name and description, and a leading '#' would
    # turn its line into a comment; neither survives save then load as the same label.
    _seed(db, tags_by_count={"common tag": 5, "ratio: C/N": 5, "#fieldwork": 5})
    out = tmp_path / "taxonomy.txt"

    rc = main(["--db", str(tmp_path / "t.db"), "--out", str(out), "--min-count", "3"])

    assert rc == 0
    assert [label.name for label in load_taxonomy(out)] == ["common tag"]
