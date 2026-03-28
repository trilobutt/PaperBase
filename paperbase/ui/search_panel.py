import os
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, QAbstractTableModel, QModelIndex, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMenu, QMessageBox, QSpinBox,
    QTableView, QVBoxLayout, QWidget,
)

from paperbase.core.db import Database
from paperbase.core.indexer import Indexer
from paperbase.models.paper import Paper
from paperbase.models.search_result import SearchResult

_COLUMNS = ["Title", "Authors", "Journal", "Year"]


class PaperTableModel(QAbstractTableModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._papers: list[Paper] = []

    def set_papers(self, papers: list[Paper]) -> None:
        self.beginResetModel()
        self._papers = papers
        self.endResetModel()

    def paper_at(self, row: int) -> Optional[Paper]:
        if 0 <= row < len(self._papers):
            return self._papers[row]
        return None

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._papers)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(_COLUMNS)

    def headerData(self, section: int, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return _COLUMNS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or role != Qt.ItemDataRole.DisplayRole:
            return None
        p = self._papers[index.row()]
        col = index.column()
        if col == 0:
            return p.title or Path(p.file_path).name
        if col == 1:
            authors = p.authors[:2]
            suffix = " et al." if len(p.authors) > 2 else ""
            return ", ".join(a.split(",")[0] for a in authors) + suffix
        if col == 2:
            return p.journal or ""
        if col == 3:
            return str(p.year) if p.year else ""
        return None

    def sort(self, column: int, order=Qt.SortOrder.AscendingOrder) -> None:
        self.beginResetModel()
        reverse = order == Qt.SortOrder.DescendingOrder
        if column == 0:
            self._papers.sort(key=lambda p: p.title.lower(), reverse=reverse)
        elif column == 2:
            self._papers.sort(key=lambda p: p.journal.lower(), reverse=reverse)
        elif column == 3:
            self._papers.sort(key=lambda p: p.year or 0, reverse=reverse)
        self.endResetModel()


class SearchPanel(QWidget):
    paper_selected = pyqtSignal(object)   # Paper or None
    paper_deleted  = pyqtSignal(int)      # paper_id

    def __init__(self, db: Database, indexer: Indexer, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._db = db
        self._indexer = indexer
        self._active_collection: Optional[int] = None
        self._active_tags: list[str] = []
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # Filter sidebar + results, arranged horizontally
        hbox = QHBoxLayout()

        # ---- Filter sidebar ----
        sidebar = QWidget()
        sidebar.setFixedWidth(170)
        sbl = QVBoxLayout(sidebar)
        sbl.setContentsMargins(4, 4, 4, 4)

        sbl.addWidget(QLabel("Year from:"))
        self._year_from = QSpinBox()
        self._year_from.setRange(0, 2100)
        self._year_from.setSpecialValueText("Any")
        self._year_from.valueChanged.connect(self._apply_filters)
        sbl.addWidget(self._year_from)

        sbl.addWidget(QLabel("Year to:"))
        self._year_to = QSpinBox()
        self._year_to.setRange(0, 2100)
        self._year_to.setSpecialValueText("Any")
        self._year_to.valueChanged.connect(self._apply_filters)
        sbl.addWidget(self._year_to)

        sbl.addWidget(QLabel("Journal:"))
        self._journal_filter = QLineEdit()
        self._journal_filter.setPlaceholderText("contains…")
        self._journal_filter.textChanged.connect(self._apply_filters)
        sbl.addWidget(self._journal_filter)

        self._needs_review_cb = QCheckBox("Needs review only")
        self._needs_review_cb.stateChanged.connect(self._apply_filters)
        sbl.addWidget(self._needs_review_cb)

        sbl.addWidget(QLabel("Tags:"))
        self._tag_list = QListWidget()
        self._tag_list.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        self._tag_list.itemSelectionChanged.connect(self._apply_filters)
        sbl.addWidget(self._tag_list)
        sbl.addStretch()

        hbox.addWidget(sidebar)

        # ---- Results table ----
        results_widget = QWidget()
        rl = QVBoxLayout(results_widget)
        rl.setContentsMargins(0, 0, 0, 0)

        self._result_count_label = QLabel("0 papers")
        rl.addWidget(self._result_count_label)

        self._model = PaperTableModel()
        self._table = QTableView()
        self._table.setModel(self._model)
        self._table.setSortingEnabled(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        self._table.selectionModel().currentRowChanged.connect(self._on_row_changed)
        self._table.doubleClicked.connect(self._on_double_click)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_context_menu)
        rl.addWidget(self._table)

        hbox.addWidget(results_widget)
        layout.addLayout(hbox)

        self._last_query = ""
        self._last_result_ids: list[int] = []

    # ------------------------------------------------------------------

    def run_search(self, query: str) -> None:
        self._last_query = query
        if query.strip():
            results = self._indexer.search(query, limit=500)
            self._last_result_ids = [r.paper_id for r in results]
        else:
            # No query: show all (up to 2000 most recent)
            papers = self._db.get_all_papers_paginated(0, 2000)
            self._last_result_ids = [p.id for p in papers if p.id is not None]  # type: ignore[misc]
        self._apply_filters()

    def set_collection_filter(self, collection_id: Optional[int]) -> None:
        self._active_collection = collection_id
        self._apply_filters()

    def set_tag_filter(self, tag: Optional[str]) -> None:
        if tag:
            self._active_tags = [tag]
        else:
            self._active_tags = []
        self._apply_filters()

    def refresh_tags(self) -> None:
        selected = {item.text() for item in self._tag_list.selectedItems()}
        self._tag_list.clear()
        for tag in self._db.get_all_tags():
            item = QListWidgetItem(tag)
            self._tag_list.addItem(item)
            if tag in selected:
                item.setSelected(True)

    def _apply_filters(self) -> None:
        year_from = self._year_from.value() or None
        year_to = self._year_to.value() or None
        journal = self._journal_filter.text().strip() or None
        needs_review = self._needs_review_cb.isChecked()

        sidebar_tags = [item.text() for item in self._tag_list.selectedItems()]
        combined_tags = list(set(self._active_tags + sidebar_tags)) or None

        filtered_ids = self._db.search_filter(
            self._last_result_ids,
            year_from=year_from,
            year_to=year_to,
            journal=journal,
            tags=combined_tags,
            collection_id=self._active_collection,
            needs_review_only=needs_review,
        )

        papers = self._db.get_papers_by_ids(filtered_ids)
        self._model.set_papers(papers)
        count = len(papers)
        self._result_count_label.setText(f"{count} paper{'s' if count != 1 else ''}")

    def _on_double_click(self, index: QModelIndex) -> None:
        paper = self._model.paper_at(index.row())
        if paper and paper.file_path:
            os.startfile(paper.file_path)

    def _on_row_changed(self, current: QModelIndex, previous: QModelIndex) -> None:
        paper = self._model.paper_at(current.row())
        self.paper_selected.emit(paper)

    def reload_current_paper(self, paper_id: int) -> None:
        """Refresh a paper row in-place (e.g. after tag edit)."""
        for row in range(self._model.rowCount()):
            p = self._model.paper_at(row)
            if p and p.id == paper_id:
                refreshed = self._db.get_paper(paper_id)
                if refreshed:
                    self._model._papers[row] = refreshed
                    top = self._model.index(row, 0)
                    bot = self._model.index(row, self._model.columnCount() - 1)
                    self._model.dataChanged.emit(top, bot)
                break

    def _on_context_menu(self, pos) -> None:
        index = self._table.indexAt(pos)
        if not index.isValid():
            return
        paper = self._model.paper_at(index.row())
        if paper is None:
            return

        menu = QMenu(self)
        act_library = menu.addAction("Remove from library")
        act_disk    = menu.addAction("Delete from library and disk")
        chosen = menu.exec(self._table.viewport().mapToGlobal(pos))

        if chosen == act_library:
            self._delete_paper(paper, delete_file=False)
        elif chosen == act_disk:
            self._delete_paper(paper, delete_file=True)

    def _delete_paper(self, paper, *, delete_file: bool) -> None:
        title_snippet = (paper.title or paper.file_path)[:80]
        verb = "Delete from library and disk" if delete_file else "Remove from library"
        reply = QMessageBox.question(
            self,
            verb,
            f"{verb}?\n\n{title_snippet}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        paper_id = paper.id
        self._db.delete_paper(paper_id)
        self._indexer.delete_document(paper_id)

        if delete_file:
            try:
                Path(paper.file_path).unlink(missing_ok=True)
            except OSError:
                pass

        # Remove from the visible model without a full reload
        self._model.beginResetModel()
        self._model._papers = [p for p in self._model._papers if p.id != paper_id]
        self._model.endResetModel()

        # Also drop from the cached result id list so re-filtering stays consistent
        self._last_result_ids = [i for i in self._last_result_ids if i != paper_id]

        self.paper_selected.emit(None)
        self.paper_deleted.emit(paper_id)
