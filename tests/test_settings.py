"""Tests for `Settings`: taxonomy path/top-k round-trip and `taxonomy_file()` resolution."""

import json
from pathlib import Path

from paperbase.ui.settings_dialog import Settings


def test_taxonomy_fields_round_trip(tmp_path: Path) -> None:
    settings = Settings()
    settings.taxonomy_path = "C:/custom/taxonomy.txt"
    settings.taxonomy_top_k = 7

    path = tmp_path / "settings.json"
    settings.save(path)
    loaded = Settings.load(path)

    assert loaded.taxonomy_path == "C:/custom/taxonomy.txt"
    assert loaded.taxonomy_top_k == 7


def test_taxonomy_file_explicit_path_wins(tmp_path: Path) -> None:
    explicit = Settings()
    explicit.taxonomy_path = "C:/custom/taxonomy.txt"
    explicit.library_root = "C:/library"
    assert explicit.taxonomy_file() == Path("C:/custom/taxonomy.txt")


def test_taxonomy_file_falls_back_to_library_root(tmp_path: Path) -> None:
    fallback = Settings()
    fallback.library_root = "C:/library"
    assert fallback.taxonomy_file() == Path("C:/library") / "taxonomy.txt"

    blank = Settings()
    assert blank.taxonomy_file() is None


def test_load_without_taxonomy_key_uses_defaults(tmp_path: Path) -> None:
    path = tmp_path / "legacy_settings.json"
    legacy_data = {
        "version": 1,
        "library_root": "C:/library",
        "user_email": "user@example.com",
        "folder_pattern": "{journal}/{year}/{author} ({year}) {title}.pdf",
        "last_import_dir": "",
        "secondary_dest": "",
        "categories": [],
        "auto_categorise": True,
        "category_threshold": 0.35,
        "tag_count": 5,
        "reduce_motion": False,
    }
    path.write_text(json.dumps(legacy_data), encoding="utf-8")

    loaded = Settings.load(path)

    assert loaded.taxonomy_path == ""
    assert loaded.taxonomy_top_k == 4
