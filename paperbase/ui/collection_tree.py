from typing import Optional

from PyQt6.QtCore import QModelIndex, QPoint, Qt, pyqtSignal
from PyQt6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent, QStandardItem, QStandardItemModel
from PyQt6.QtWidgets import (
    QAbstractItemView, QInputDialog, QMenu, QMessageBox, QTreeView, QVBoxLayout, QWidget,
)

from paperbase.core.db import Database
from paperbase.core.taxa import TaxonTree
from paperbase.models.collection import Collection

COLLECTION_ID_ROLE = Qt.ItemDataRole.UserRole + 1
TAG_ROLE           = Qt.ItemDataRole.UserRole + 2
TAXON_ROLE         = Qt.ItemDataRole.UserRole + 3

_PAPER_IDS_MIME = "application/x-paperbase-paper-ids"


class _CollectionTreeView(QTreeView):
    """QTreeView subclass that accepts paper-ID drops onto collection items."""

    papers_dropped = pyqtSignal(list, int)  # (paper_ids: list[int], collection_id: int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)
        self.setDropIndicatorShown(True)

    def _collection_id_at(self, pos: QPoint) -> Optional[int]:
        index = self.indexAt(pos)
        if not index.isValid():
            return None
        item = self.model().itemFromIndex(index)
        if item is None:
            return None
        col_id = item.data(COLLECTION_ID_ROLE)
        # col_id is None for root headers and tag items
        return col_id if isinstance(col_id, int) else None

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasFormat(_PAPER_IDS_MIME):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        if event.mimeData().hasFormat(_PAPER_IDS_MIME) and \
                self._collection_id_at(event.position().toPoint()) is not None:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        col_id = self._collection_id_at(event.position().toPoint())
        if col_id is None:
            event.ignore()
            return
        raw = bytes(event.mimeData().data(_PAPER_IDS_MIME)).decode()
        paper_ids = [int(x) for x in raw.split(",") if x.strip()]
        if paper_ids:
            self.papers_dropped.emit(paper_ids, col_id)
        event.acceptProposedAction()


class CollectionTree(QWidget):
    collection_selected       = pyqtSignal(object)  # Optional[int] — collection_id or None
    tag_selected              = pyqtSignal(object)  # Optional[str] — tag name or None
    taxon_selected            = pyqtSignal(object)  # Optional[list[str]] — taxon subtree or None
    papers_added_to_collection = pyqtSignal(list)   # list[int] of affected paper_ids

    def __init__(self, db: Database, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._db = db
        self._taxa: TaxonTree = TaxonTree([])
        self._build_ui()

    def set_taxa(self, taxa: TaxonTree) -> None:
        """Use `taxa` for the Taxa branch from the next refresh() on."""
        self._taxa = taxa

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._model = QStandardItemModel()
        self._model.setHorizontalHeaderLabels(["Collections & Tags"])

        self._tree = _CollectionTreeView()
        self._tree.setModel(self._model)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._context_menu)
        self._tree.selectionModel().currentChanged.connect(self._on_selection_changed)
        self._tree.setHeaderHidden(True)
        self._tree.papers_dropped.connect(self._on_papers_dropped)

        layout.addWidget(self._tree)
        # Populated by MainWindow.start_deferred_load, after the first paint.

    def refresh(self) -> None:
        open_taxa = {
            item.data(TAXON_ROLE) for item in self._taxon_items()
            if self._tree.isExpanded(item.index())
        }
        self._model.clear()

        # --- Collections ---
        col_root = QStandardItem("Collections")
        col_root.setEditable(False)
        col_root.setData(None, COLLECTION_ID_ROLE)
        font = col_root.font()
        font.setBold(True)
        col_root.setFont(font)
        self._model.appendRow(col_root)

        collections = self._db.get_collections()
        col_map: dict[Optional[int], QStandardItem] = {None: col_root}
        remaining = list(collections)
        max_passes = len(remaining) + 1
        passes = 0
        while remaining and passes < max_passes:
            passes += 1
            unresolved = []
            for col in remaining:
                parent_item = col_map.get(col.parent_id)
                if parent_item is None:
                    unresolved.append(col)
                    continue
                item = QStandardItem(col.name)
                item.setEditable(False)
                item.setData(col.id, COLLECTION_ID_ROLE)
                parent_item.appendRow(item)
                col_map[col.id] = item
            remaining = unresolved

        # --- Taxa ---
        # Absent without a taxa.txt, rather than a heading that can never hold anything.
        taxa_root: Optional[QStandardItem] = None
        if len(self._taxa):
            taxa_root = QStandardItem("Taxa")
            taxa_root.setEditable(False)
            taxa_root.setData("", TAXON_ROLE)
            font_taxa = taxa_root.font()
            font_taxa.setBold(True)
            taxa_root.setFont(font_taxa)
            self._model.appendRow(taxa_root)
            self._add_taxa(taxa_root)

        # --- Tags ---
        tag_root = QStandardItem("Tags")
        tag_root.setEditable(False)
        tag_root.setData(None, TAG_ROLE)
        font2 = tag_root.font()
        font2.setBold(True)
        tag_root.setFont(font2)
        self._model.appendRow(tag_root)

        for tag in self._db.get_all_tags():
            item = QStandardItem(tag)
            item.setEditable(False)
            item.setData(tag, TAG_ROLE)
            tag_root.appendRow(item)

        # Collections and tags open as before. The taxon tree opens one level, so its roots
        # are visible and the hundreds of taxa below them wait to be asked for, plus a row
        # with no sibling to choose instead, plus whatever was open before this rebuild:
        # every paper edit refreshes the tree, and a branch drilled into must not snap
        # shut behind it.
        self._tree.expandRecursively(col_root.index())
        if taxa_root is not None:
            self._tree.expand(taxa_root.index())
            only = taxa_root
            while only.rowCount() == 1:
                only = only.child(0)
                self._tree.expand(only.index())
            for item in self._taxon_items():
                if item.data(TAXON_ROLE) in open_taxa:
                    self._tree.expand(item.index())
        self._tree.expand(tag_root.index())

    def _add_taxa(self, root: QStandardItem) -> None:
        """Fill the Taxa branch with the taxa papers hold, and every taxon above them.

        Only populated branches: the full tree runs to hundreds of taxa, most of them
        empty in any one library. An unbranched run folds into one row named for its last
        taxon, since a rank no paper stores that leads to a single populated child offers
        nothing to choose, and taxa.txt nests Histeridae nineteen ranks deep, which
        indents it past the panel's edge. The folded ranks are in the row's tooltip, and
        the row filters to the same papers its first rank would.
        """
        stored = {name for name in self._db.get_taxon_counts() if name in self._taxa}
        populated = set(stored)
        for name in stored:
            populated.update(self._taxa.ancestors(name))

        def add(parent: QStandardItem, name: str) -> None:
            chain = [name]
            while chain[-1] not in stored:
                below = [c for c in self._taxa.children(chain[-1]) if c in populated]
                if len(below) != 1:
                    break
                chain.append(below[0])
            last = chain[-1]
            item = QStandardItem(last)
            item.setEditable(False)
            item.setData(last, TAXON_ROLE)
            tips = [" > ".join(chain)] if len(chain) > 1 else []
            taxon = self._taxa.get(last)
            if taxon is not None and taxon.informal:
                # Informal groupings are conveniences, not clades. Italic says so without
                # a word of chrome on every row.
                italic = item.font()
                italic.setItalic(True)
                item.setFont(italic)
                tips.append(f"{last} is an informal, non-monophyletic grouping")
            if tips:
                item.setToolTip("\n".join(tips))
            parent.appendRow(item)
            for child in self._taxa.children(last):
                if child in populated:
                    add(item, child)

        for name in self._taxa.children(None):
            if name in populated:
                add(root, name)

    def _taxon_items(self) -> list[QStandardItem]:
        """Every row under the Taxa heading, depth first; empty when there is none."""
        top = self._model.invisibleRootItem()
        stack = [
            top.child(r) for r in range(top.rowCount()) if top.child(r).data(TAXON_ROLE) == ""
        ]
        out: list[QStandardItem] = []
        while stack:
            item = stack.pop()
            for r in range(item.rowCount()):
                out.append(item.child(r))
                stack.append(item.child(r))
        return out

    def _on_selection_changed(self, current: QModelIndex, previous: QModelIndex) -> None:
        item = self._model.itemFromIndex(current)
        if item is None:
            return
        taxon = item.data(TAXON_ROLE)
        if isinstance(taxon, str) and taxon:
            # A taxon filters to itself and everything below it: papers store only their
            # most specific taxa, so the descendants are resolved here, from the tree.
            self.taxon_selected.emit(self._taxa.descendants_and_self(taxon))
            return
        tag = item.data(TAG_ROLE)
        col_id = item.data(COLLECTION_ID_ROLE)
        if tag and isinstance(tag, str):
            self.tag_selected.emit(tag)
        elif col_id is not None:
            self.collection_selected.emit(col_id)
        else:
            self.collection_selected.emit(None)
            self.tag_selected.emit(None)
            self.taxon_selected.emit(None)

    def _on_papers_dropped(self, paper_ids: list[int], collection_id: int) -> None:
        for pid in paper_ids:
            self._db.add_paper_to_collection(pid, collection_id)
        self.papers_added_to_collection.emit(paper_ids)

    def _context_menu(self, pos: QPoint) -> None:
        index = self._tree.indexAt(pos)
        item = self._model.itemFromIndex(index)
        if item is None:
            return
        if isinstance(item.data(TAXON_ROLE), str):
            return  # the taxon tree mirrors taxa.txt and is edited there, not here

        col_id = item.data(COLLECTION_ID_ROLE)
        tag = item.data(TAG_ROLE)

        menu = QMenu(self)

        if col_id is not None:
            menu.addAction("New Sub-collection", lambda: self._new_collection(parent_id=col_id))
            menu.addAction("Rename", lambda: self._rename_collection(col_id, item))
            menu.addAction("Delete", lambda: self._delete_collection(col_id))
        elif tag is None:
            # Collections root header — allow creating top-level
            menu.addAction("New Collection", lambda: self._new_collection(parent_id=None))

        if not menu.isEmpty():
            menu.exec(self._tree.viewport().mapToGlobal(pos))

    def _new_collection(self, parent_id: Optional[int]) -> None:
        name, ok = QInputDialog.getText(self, "New Collection", "Collection name:")
        if ok and name.strip():
            col = Collection(id=None, name=name.strip(), parent_id=parent_id)
            self._db.insert_collection(col)
            self.refresh()

    def _rename_collection(self, col_id: int, item: QStandardItem) -> None:
        col = self._db.get_collection(col_id)
        if col is None:
            return
        name, ok = QInputDialog.getText(self, "Rename Collection", "New name:", text=col.name)
        if ok and name.strip():
            col.name = name.strip()
            self._db.update_collection(col)
            self.refresh()

    def _delete_collection(self, col_id: int) -> None:
        col = self._db.get_collection(col_id)
        if col is None:
            return
        reply = QMessageBox.question(
            self, "Delete Collection",
            f'Delete "{col.name}"?\n\nPapers will not be deleted.',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._db.delete_collection(col_id)
            self.refresh()
