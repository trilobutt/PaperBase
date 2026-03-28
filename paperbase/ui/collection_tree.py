from typing import Optional

from PyQt6.QtCore import QModelIndex, Qt, pyqtSignal
from PyQt6.QtGui import QStandardItem, QStandardItemModel
from PyQt6.QtWidgets import (
    QInputDialog, QMenu, QMessageBox, QTreeView, QVBoxLayout, QWidget,
)

from paperbase.core.db import Database
from paperbase.models.collection import Collection

COLLECTION_ID_ROLE = Qt.ItemDataRole.UserRole + 1
TAG_ROLE = Qt.ItemDataRole.UserRole + 2


class CollectionTree(QWidget):
    collection_selected = pyqtSignal(object)   # Optional[int] — collection_id or None
    tag_selected        = pyqtSignal(object)   # Optional[str] — tag name or None

    def __init__(self, db: Database, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._db = db
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._model = QStandardItemModel()
        self._model.setHorizontalHeaderLabels(["Collections & Tags"])

        self._tree = QTreeView()
        self._tree.setModel(self._model)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._context_menu)
        self._tree.selectionModel().currentChanged.connect(self._on_selection_changed)
        self._tree.setHeaderHidden(True)

        layout.addWidget(self._tree)
        self.refresh()

    def refresh(self) -> None:
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
        # Build parent-first ordering
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

        self._tree.expandAll()

    def _on_selection_changed(self, current: QModelIndex, previous: QModelIndex) -> None:
        item = self._model.itemFromIndex(current)
        if item is None:
            return
        tag = item.data(TAG_ROLE)
        col_id = item.data(COLLECTION_ID_ROLE)
        if tag and isinstance(tag, str):
            self.tag_selected.emit(tag)
        elif col_id is not None:
            self.collection_selected.emit(col_id)
        else:
            # root items — clear filter
            self.collection_selected.emit(None)
            self.tag_selected.emit(None)

    def _context_menu(self, pos) -> None:
        index = self._tree.indexAt(pos)
        item = self._model.itemFromIndex(index)
        if item is None:
            return

        col_id = item.data(COLLECTION_ID_ROLE)
        tag = item.data(TAG_ROLE)

        menu = QMenu(self)

        if col_id is not None:
            # Existing collection
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
