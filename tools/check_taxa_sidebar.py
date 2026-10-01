"""Check the Library panel's Taxa branch: populated branches only, unbranched runs folded,
open branches kept across a refresh, descendant filtering, and no branch without taxa.

    py -3.12 tools/check_taxa_sidebar.py
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtWidgets import QApplication

from paperbase.core.db import Database
from paperbase.core.taxa import TaxonTree, parse_taxa
from paperbase.models.paper import Paper
from paperbase.ui.collection_tree import TAXON_ROLE, CollectionTree

TAXA = parse_taxa(
    "Arthropoda\n"
    "Arthropoda > Hexapoda\n"
    "Arthropoda > Hexapoda > Coleoptera\n"
    "Arthropoda > Hexapoda > Coleoptera > Histeridae\n"
    "Arthropoda > Hexapoda > Diptera\n"
    "Arthropoda > Hexapoda > Odonata\n"
    "Arthropoda > Crustacea [informal]\n"
    "Arthropoda > Crustacea [informal] > Copepoda\n"
)


def _paper(path: str, taxa: list[str]) -> Paper:
    return Paper(
        id=None, doi=None, title="t", authors=[], journal="", year=None, volume="",
        issue="", pages="", abstract="", keywords=[], tags=[], collection_ids=[],
        file_path=path, date_added="2020-01-01T00:00:00+00:00",
        date_modified="2020-01-01T00:00:00+00:00", metadata_source="manual",
        needs_review=False, open_access=False, taxa=taxa,
    )


def _children(item) -> list[str]:
    return [item.child(r).text() for r in range(item.rowCount())]


def main() -> None:
    app = QApplication([])
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "paperbase.db")
        db.open()
        try:
            db.insert_paper(_paper("C:/x/a.pdf", ["Histeridae"]))
            db.insert_paper(_paper("C:/x/b.pdf", ["Diptera"]))
            db.insert_paper(_paper("C:/x/c.pdf", ["Crustacea"]))
            db.insert_paper(_paper("C:/x/d.pdf", ["Copepoda"]))

            widget = CollectionTree(db)
            widget.set_taxa(TAXA)
            widget.refresh()
            model = widget._model
            roots = [model.item(r) for r in range(model.rowCount())]
            assert [r.text() for r in roots] == ["Collections", "Taxa", "Tags"], roots
            taxa_root = roots[1]
            assert _children(taxa_root) == ["Arthropoda"]
            arthropoda = taxa_root.child(0)
            assert widget._tree.isExpanded(arthropoda.index()), "a sole child opens"
            assert _children(arthropoda) == ["Hexapoda", "Crustacea"]
            hexapoda = arthropoda.child(0)
            assert _children(hexapoda) == ["Histeridae", "Diptera"], (
                "Coleoptera folds into its one populated child; Odonata holds no papers"
            )
            assert "Coleoptera > Histeridae" in hexapoda.child(0).toolTip()
            crustacea = arthropoda.child(1)
            assert crustacea.font().italic(), "informal taxa are italic"
            assert _children(crustacea) == ["Copepoda"], "a stored taxon never folds"
            assert hexapoda.data(TAXON_ROLE) == "Hexapoda"

            emitted: list = []
            widget.taxon_selected.connect(emitted.append)
            widget._tree.setCurrentIndex(hexapoda.index())
            assert sorted(emitted[-1]) == [
                "Coleoptera", "Diptera", "Hexapoda", "Histeridae", "Odonata",
            ]
            widget._tree.setCurrentIndex(taxa_root.index())
            assert emitted[-1] is None

            widget._tree.expand(hexapoda.index())
            widget.refresh()
            hexapoda = model.item(1).child(0).child(0)
            assert hexapoda.text() == "Hexapoda"
            assert widget._tree.isExpanded(hexapoda.index()), "open branches survive"
            assert not widget._tree.isExpanded(model.item(1).child(0).child(1).index())

            widget.set_taxa(TaxonTree([]))
            widget.refresh()
            roots = [model.item(r).text() for r in range(model.rowCount())]
            assert roots == ["Collections", "Tags"], roots
        finally:
            db.close()
    app.quit()
    print("taxa sidebar OK")


if __name__ == "__main__":
    main()
