"""Check the paper panel's taxon chips: validated adds, removal, locking and unlocking.

    py -3.12 tools/check_taxa_chips.py
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtWidgets import QApplication

from paperbase.core.db import Database
from paperbase.core.taxa import parse_taxa
from paperbase.models.paper import Paper
from paperbase.ui.paper_detail import PaperDetail

TAXA = parse_taxa("Arthropoda\nArthropoda > Araneae: spiders\nArthropoda > Coleoptera: beetles\n")


def main() -> None:
    app = QApplication([])
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "paperbase.db")
        db.open()
        try:
            pid = db.insert_paper(Paper(
                id=None, doi=None, title="t", authors=[], journal="", year=None,
                volume="", issue="", pages="", abstract="", keywords=[], tags=[],
                collection_ids=[], file_path="C:/x/a.pdf",
                date_added="2020-01-01T00:00:00+00:00",
                date_modified="2020-01-01T00:00:00+00:00", metadata_source="manual",
                needs_review=False, open_access=False, taxa=["Araneae"],
            ))
            panel = PaperDetail(db)
            panel.set_taxa(TAXA)
            panel.show_paper(db.get_paper(pid))
            assert panel._taxa_lock_row.isHidden()

            panel._taxon_input.setText("coleoptera")
            panel._add_taxon()
            stored = db.get_paper(pid)
            assert stored.taxa == ["Araneae", "Coleoptera"] and stored.taxa_locked
            assert not panel._taxa_lock_row.isHidden()

            panel._taxon_input.setText("Nonsensea")
            panel._add_taxon()
            assert panel._taxon_input.property("invalid") is True
            assert not panel._taxon_error.isHidden() and "Nonsensea" in panel._taxon_error.text()
            assert db.get_paper(pid).taxa == ["Araneae", "Coleoptera"]

            panel._remove_taxon("Araneae")
            assert db.get_paper(pid).taxa == ["Coleoptera"]

            panel._unlock_taxa()
            assert db.get_paper(pid).taxa_locked is False
            assert panel._taxa_lock_row.isHidden()
        finally:
            db.close()
    app.quit()
    print("taxa chips OK")


if __name__ == "__main__":
    main()
