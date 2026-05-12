# Auto-Categorisation (Tier 1 Embedding Pipeline)

## done-when
- `sentence-transformers` + `keybert` in pyproject.toml
- `core/categoriser.py`: `EmbeddingCategoriser` + `CategorizationWorker`
- `core/llm.py`: stub replaced by thin wrapper
- `core/db.py`: `get_all_paper_ids()` added
- `ui/settings_dialog.py`: categories editor + threshold/tag_count/auto_categorise fields
- `ui/categorisation_dialog.py`: progress dialog for retroactive run
- `ui/main_window.py`: Categorise button + categoriser lifecycle
- `core/importer.py`: calls categoriser after each successful insert

## Tasks
- [x] pyproject.toml: add sentence-transformers, keybert
- [x] core/db.py: add get_all_paper_ids()
- [x] core/categoriser.py: create EmbeddingCategoriser + CategorizationWorker
- [x] core/llm.py: replace stub
- [x] ui/settings_dialog.py: add categories settings + editor UI
- [x] ui/categorisation_dialog.py: create progress dialog
- [x] ui/main_window.py: add Categorise button, categoriser lifecycle, pass to ImportDialog
- [x] core/importer.py: accept optional categoriser, call after insert (5 sites)
- [x] Smoke-test imports — all OK
