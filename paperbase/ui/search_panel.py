import os
import subprocess
from collections import OrderedDict
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, QAbstractTableModel, QByteArray, QMimeData, QModelIndex, QObject, QPoint, QUrl, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMenu, QMessageBox, QSpinBox,
    QStackedWidget, QTableView, QVBoxLayout, QWidget,
)

from paperbase.core.db import Database
from paperbase.core.indexer import Indexer
from paperbase.models.paper import Paper
from paperbase.ui import theme
from paperbase.ui.glass import EmptyState, StackFader

_COLUMNS = ["Title", "Authors", "Journal", "Year", "Score"]
# Column index -> search_filter sort key. Authors sorts on the raw JSON array, which puts
# it in first-author order because the array is ["Lastname, Firstname", ...].
_SORT_KEYS = ["title", "authors", "journal", "year", "rank"]


class PaperTableModel(QAbstractTableModel):
    """Id-backed model. Rows are fetched from SQLite a block at a time as the view asks
    for them, so a 150,000-row result costs the same as a 200-row one until scrolled."""

    sort_requested = pyqtSignal(int, int)   # column, Qt.SortOrder value

    BLOCK = 200
    CACHE_LIMIT = 3000                      # papers, hard bound; ~10 MB at slim width

    def __init__(self, db: Database, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._db = db
        self._ids: list[int] = []
        self._scores: dict[int, float] = {}
        self._cache: "OrderedDict[int, Paper]" = OrderedDict()

    def set_ids(self, ids: list[int], scores: Optional[dict[int, float]] = None) -> None:
        self.beginResetModel()
        self._ids = ids
        self._scores = scores or {}
        self._cache.clear()
        self.endResetModel()

    def paper_at(self, row: int) -> Optional[Paper]:
        if not 0 <= row < len(self._ids):
            return None
        paper_id = self._ids[row]
        cached = self._cache.get(paper_id)
        if cached is not None:
            self._cache.move_to_end(paper_id)
            return cached
        self._fetch_block(row)
        return self._cache.get(paper_id)

    def replace_paper(self, paper: Paper) -> Optional[int]:
        """Update one cached row after an edit. Returns its row index, or None."""
        if paper.id is None or paper.id not in self._ids:
            return None
        self._cache[paper.id] = paper
        self._cache.move_to_end(paper.id)
        return self._ids.index(paper.id)

    def drop_id(self, paper_id: int) -> None:
        if paper_id in self._ids:
            row = self._ids.index(paper_id)
            self.beginRemoveRows(QModelIndex(), row, row)
            self._ids.pop(row)
            self._cache.pop(paper_id, None)
            self.endRemoveRows()

    def _fetch_block(self, row: int) -> None:
        start = (row // self.BLOCK) * self.BLOCK
        block = self._ids[start : start + self.BLOCK]
        for paper in self._db.get_papers_slim_by_ids(block):
            if paper.id is not None:
                self._cache[paper.id] = paper
        while len(self._cache) > self.CACHE_LIMIT:
            self._cache.popitem(last=False)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._ids)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(_COLUMNS)

    def headerData(self, section: int, orientation: Qt.Orientation,
                   role: int = Qt.ItemDataRole.DisplayRole) -> object:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return _COLUMNS[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> object:
        if not index.isValid() or role != Qt.ItemDataRole.DisplayRole:
            return None
        p = self.paper_at(index.row())
        if p is None:
            return None
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
        if col == 4:
            s = self._scores.get(p.id or -1)
            return f"{s:.0f}" if s is not None else ""
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        return super().flags(index) | Qt.ItemFlag.ItemIsDragEnabled

    def mimeTypes(self) -> list[str]:
        return ["application/x-paperbase-paper-ids"]

    def mimeData(self, indexes: list[QModelIndex]) -> QMimeData:
        rows = sorted({idx.row() for idx in indexes})
        ids = [str(self._ids[r]) for r in rows if 0 <= r < len(self._ids)]
        mime = QMimeData()
        mime.setData("application/x-paperbase-paper-ids", QByteArray(",".join(ids).encode()))
        return mime

    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        # Sorting 150k rows in Python is the cost this model exists to avoid; the panel
        # re-runs the query with an ORDER BY instead.
        self.sort_requested.emit(column, order.value)


class SearchPanel(QWidget):
    paper_selected  = pyqtSignal(object)   # Paper or None
    paper_deleted   = pyqtSignal(int)      # paper_id
    import_requested = pyqtSignal()        # the empty library's one action

    def __init__(self, db: Database, indexer: Indexer, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._db = db
        self._indexer = indexer
        self._active_collection: Optional[int] = None
        self._active_tags: list[str] = []
        self._build_ui()

    def _build_ui(self) -> None:
        """Filters on the left, results on the right.

        Both are plain children of the glass Results panel that hosts this widget: the
        panel supplies the surface, so nothing in here paints one of its own. The results
        side is a stack rather than a bare table, because three of its four states are an
        absence and an empty grid is not a designed answer to any of them.
        """
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(theme.SPACE * 2)

        # ---- Filter sidebar: four groups, 1u inside a group, 3u between them ---------
        # The ratio is the whole structure: compressing inside a group is free, and the
        # space between groups is what makes the four of them legible without dividers.
        sidebar = QWidget()
        sidebar.setFixedWidth(200)
        pad = theme.SPACE * 2
        sbl = QVBoxLayout(sidebar)
        sbl.setContentsMargins(pad, pad, pad, pad)
        sbl.setSpacing(theme.SPACE * 3)

        years = QVBoxLayout()
        years.setContentsMargins(0, 0, 0, 0)
        years.setSpacing(theme.SPACE)
        years.addWidget(self._group_label("Years"))
        # Prefix rather than a label beside each box: the two stay self-identifying at
        # any value, the "any" case included, without spending a column on labels.
        self._year_from = QSpinBox()
        self._year_from.setRange(0, 2100)
        self._year_from.setPrefix("From ")
        self._year_from.setSpecialValueText("From any")
        self._year_from.valueChanged.connect(self._apply_filters)
        years.addWidget(self._year_from)
        self._year_to = QSpinBox()
        self._year_to.setRange(0, 2100)
        self._year_to.setPrefix("To ")
        self._year_to.setSpecialValueText("To any")
        self._year_to.valueChanged.connect(self._apply_filters)
        years.addWidget(self._year_to)
        sbl.addLayout(years)

        journal = QVBoxLayout()
        journal.setContentsMargins(0, 0, 0, 0)
        journal.setSpacing(theme.SPACE)
        journal.addWidget(self._group_label("Journal"))
        self._journal_filter = QLineEdit()
        self._journal_filter.setPlaceholderText("contains…")
        self._journal_filter.textChanged.connect(self._apply_filters)
        journal.addWidget(self._journal_filter)
        sbl.addLayout(journal)

        flags = QVBoxLayout()
        flags.setContentsMargins(0, 0, 0, 0)
        flags.setSpacing(theme.SPACE)
        flags.addWidget(self._group_label("Flags"))
        self._needs_review_cb = QCheckBox("Needs review only")
        self._needs_review_cb.stateChanged.connect(self._apply_filters)
        flags.addWidget(self._needs_review_cb)
        self._books_only_cb = QCheckBox("Books only")
        self._books_only_cb.stateChanged.connect(self._apply_filters)
        flags.addWidget(self._books_only_cb)
        sbl.addLayout(flags)

        tags = QVBoxLayout()
        tags.setContentsMargins(0, 0, 0, 0)
        tags.setSpacing(theme.SPACE)
        tags.addWidget(self._group_label("Tags"))
        self._tag_list = QListWidget()
        self._tag_list.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        self._tag_list.itemSelectionChanged.connect(self._apply_filters)
        tags.addWidget(self._tag_list)
        # The tag list takes the slack rather than a trailing stretch, so the column ends
        # level with the table instead of leaving dead space under a short list.
        sbl.addLayout(tags, 1)

        row.addWidget(sidebar)

        # ---- Results: the count lives in the host panel's header slot ----------------
        self._result_count_label = QLabel("Loading library…")
        self._result_count_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self._result_count_label.setStyleSheet(
            f"color: {theme.TEXT_SECONDARY}; background: transparent;"
        )

        # Set before the table so _on_sort_requested has something to read: setSortingEnabled
        # below makes Qt call model.sort() immediately, which now emits sort_requested
        # synchronously rather than just reordering an in-memory list.
        self._last_query = ""
        self._last_result_ids: Optional[list[int]] = None  # None = all papers
        self._last_scores: dict[int, float] = {}  # paper_id -> normalised 0-100
        self._sort_column = "date_added"
        self._sort_descending = True
        # Nothing is listed until the deferred load calls run_search: until then the
        # panel holds its launch surface rather than flashing a half-populated grid.
        self._loaded = False

        self._model = PaperTableModel(self._db)
        self._model.sort_requested.connect(self._on_sort_requested)
        self._table = QTableView()
        self._table.setModel(self._model)
        self._table.horizontalHeader().setSortIndicatorShown(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(False)
        # Title takes the slack so the five columns always add up to the panel's width.
        # Fixed widths for all five summed past the panel the redesign gave the table,
        # which put Year under a horizontal scrollbar and Score off the edge entirely:
        # the two columns the sort keys expose were the two that could not be seen.
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._table.setColumnWidth(1, 150)
        self._table.setColumnWidth(2, 150)
        self._table.setColumnWidth(3, 62)
        self._table.setColumnWidth(4, 62)
        self._table.verticalHeader().setVisible(False)
        # Dense is correct for a table of 130,000 rows; the airiness lives in the panel
        # around it rather than between the rows.
        self._table.verticalHeader().setDefaultSectionSize(28)
        self._table.setAlternatingRowColors(True)
        self._table.setDragEnabled(True)
        self._table.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self._table.selectionModel().currentRowChanged.connect(self._on_row_changed)
        self._table.doubleClicked.connect(self._on_double_click)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_context_menu)

        # The table and the four ways there is nothing to put in it. Each absence says
        # what happened and, where there is one, carries the action that resolves it.
        self._stack = QStackedWidget()
        self._stack.addWidget(self._table)

        self._loading_state = EmptyState(
            "Reading the library",
            "Counting the shelves and opening the index.",
        )
        self._stack.addWidget(self._loading_state)

        self._empty_library_state = EmptyState(
            "Nothing in the library yet",
            "Point Import at a folder of PDFs. PaperBase reads the DOIs, fetches the "
            "metadata, and files everything where it belongs.",
            "Import papers",
        )
        self._empty_library_state.action_clicked.connect(self.import_requested)
        self._stack.addWidget(self._empty_library_state)

        self._no_results_state = EmptyState(
            "No papers match that",
            "Try fewer terms, or end one with an asterisk: mycorrhiz* matches every ending.",
        )
        self._stack.addWidget(self._no_results_state)

        self._no_filtered_state = EmptyState(
            "No papers match these filters",
            "The year range or tag selection is narrowing this to nothing.",
            "Clear filters",
        )
        self._no_filtered_state.action_clicked.connect(self._clear_filters)
        self._stack.addWidget(self._no_filtered_state)

        # The first 200ms after launch is a designed surface rather than an empty grid:
        # the deferred load's run_search is what swaps this out.
        self._stack.setCurrentWidget(self._loading_state)

        # After the first page is chosen, so the window opens on the loading state
        # already there rather than fading it up: every later change is a change the
        # user caused, and those are the ones worth following.
        self._stack_fader = StackFader(self._stack)

        # Last, and deliberately: enabling this makes Qt call model.sort() straight
        # away, which emits sort_requested and runs _apply_filters against every widget
        # built above. Anything created after this line would not exist yet when it did.
        self._table.setSortingEnabled(True)
        # It leaves an arrow on Title; nothing is sorted by title yet.
        self._sync_sort_indicator()

        row.addWidget(self._stack, 1)
        layout.addLayout(row)

    @property
    def count_label(self) -> QLabel:
        """The result count, for the host panel's header slot (GlassPanel.add_header_widget)."""
        return self._result_count_label

    def _group_label(self, text: str) -> QLabel:
        """A filter group's heading: bold at body size, one step back from its controls."""
        label = QLabel(text)
        label.setObjectName("FilterGroupLabel")
        return label

    # ------------------------------------------------------------------

    def run_search(self, query: str) -> None:
        self._loaded = True
        self._last_query = query
        if query.strip():
            results = self._indexer.search(query)
            self._last_result_ids = [r.paper_id for r in results]
            # Normalise BM25 scores to 0-100 relative to the top hit
            if results:
                max_score = max(r.score for r in results)
                self._last_scores = (
                    {r.paper_id: (r.score / max_score) * 100 for r in results}
                    if max_score > 0
                    else {r.paper_id: 0.0 for r in results}
                )
            else:
                self._last_scores = {}
        else:
            # None signals search_filter to query all papers without an IN clause.
            self._last_result_ids = None
            self._last_scores = {}
        # A new query re-establishes relevance order; an explicit column sort overrides it.
        self._sort_column = "rank" if self._last_result_ids is not None else "date_added"
        self._sort_descending = self._last_result_ids is None
        self._sync_sort_indicator()
        self._apply_filters()

    def _sync_sort_indicator(self) -> None:
        """Point the header arrow at the order actually in effect, or at nothing.

        Qt leaves the indicator wherever it was last put, so without this the header
        claims a Title sort while the rows are in date_added order. Relevance maps to the
        Score column; date_added maps to no visible column and so shows no arrow at all.
        Signals are blocked because setSortIndicator re-enters sortByColumn.
        """
        header = self._table.horizontalHeader()
        section = _SORT_KEYS.index(self._sort_column) if self._sort_column in _SORT_KEYS else -1
        order = (Qt.SortOrder.DescendingOrder if self._sort_descending
                 else Qt.SortOrder.AscendingOrder)
        blocked = header.blockSignals(True)
        header.setSortIndicator(section, order)
        header.blockSignals(blocked)

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
        document_type = "book" if self._books_only_cb.isChecked() else None

        sidebar_tags = [item.text() for item in self._tag_list.selectedItems()]
        combined_tags = list(set(self._active_tags + sidebar_tags)) or None

        if not self._loaded:
            # setSortingEnabled below the widget tree makes Qt call model.sort() during
            # construction, which lands here before MainWindow has painted anything. The
            # query it would run is the whole library, which is exactly the work
            # start_deferred_load exists to move behind the first frame.
            self._show_state(0)
            return

        filtered_ids = self._db.search_filter(
            self._last_result_ids,
            year_from=year_from,
            year_to=year_to,
            journal=journal,
            tags=combined_tags,
            collection_id=self._active_collection,
            needs_review_only=needs_review,
            document_type=document_type,
            sort_column=self._sort_column,
            descending=self._sort_descending,
        )

        scores = self._last_scores if self._last_scores else None
        self._model.set_ids(filtered_ids, scores)
        # QItemSelectionModel::reset() drops the selection on a model reset without
        # emitting anything, so currentRowChanged never fires and the detail panel would
        # go on showing a paper the results no longer list.
        self.paper_selected.emit(None)
        count = len(filtered_ids)
        if self._loaded:
            self._result_count_label.setText(f"{count:,} paper{'s' if count != 1 else ''}")
        self._show_state(count)

    def _show_state(self, count: int) -> None:
        """Pick the surface for what the query and the filters actually produced.

        Three different nothings, and they want three different answers: an unfilled
        library, a query that matched nothing, and filters that threw away everything the
        query found. get_paper_count is only reached on the last row of results going
        away, so the common path costs nothing.
        """
        if not self._loaded:
            self._stack.setCurrentWidget(self._loading_state)
        elif count:
            self._stack.setCurrentWidget(self._table)
        elif self._db.get_paper_count() == 0:
            self._stack.setCurrentWidget(self._empty_library_state)
        elif self._last_result_ids is not None and not self._last_result_ids:
            self._stack.setCurrentWidget(self._no_results_state)
        else:
            self._stack.setCurrentWidget(self._no_filtered_state)

    def _clear_filters(self) -> None:
        """Reset the sidebar and re-run. Signals are blocked so one search runs, not six."""
        controls = (
            self._year_from,
            self._year_to,
            self._journal_filter,
            self._needs_review_cb,
            self._books_only_cb,
            self._tag_list,
        )
        for control in controls:
            control.blockSignals(True)
        self._year_from.setValue(0)
        self._year_to.setValue(0)
        self._journal_filter.clear()
        self._needs_review_cb.setChecked(False)
        self._books_only_cb.setChecked(False)
        self._tag_list.clearSelection()
        for control in controls:
            control.blockSignals(False)
        self._apply_filters()

    def _on_sort_requested(self, column: int, order_value: int) -> None:
        self._sort_column = _SORT_KEYS[column]
        self._sort_descending = order_value == Qt.SortOrder.DescendingOrder.value
        self._apply_filters()

    def _on_double_click(self, index: QModelIndex) -> None:
        paper = self._model.paper_at(index.row())
        if paper and paper.file_path:
            os.startfile(paper.file_path)

    def _on_row_changed(self, current: QModelIndex, previous: QModelIndex) -> None:
        if not current.isValid():
            self.paper_selected.emit(None)
            return
        slim = self._model.paper_at(current.row())
        if slim is None:
            self.paper_selected.emit(None)
            return
        # The listing model holds slim papers (no abstract/keywords); fetch the
        # full row for the detail panel. PK lookup is <5 ms.
        paper = self._db.get_paper(slim.id) if slim.id is not None else slim
        self.paper_selected.emit(paper)

    def reload_current_paper(self, paper_id: int) -> None:
        """Refresh a paper row in-place (e.g. after a tag edit)."""
        refreshed = self._db.get_paper(paper_id)
        if refreshed is None:
            return
        row = self._model.replace_paper(refreshed)
        if row is None:
            return
        top = self._model.index(row, 0)
        bot = self._model.index(row, self._model.columnCount() - 1)
        self._model.dataChanged.emit(top, bot)

    def _on_context_menu(self, pos: QPoint) -> None:
        index = self._table.indexAt(pos)
        if not index.isValid():
            return
        paper = self._model.paper_at(index.row())
        if paper is None:
            return

        menu = QMenu(self)
        act_open_folder = menu.addAction("Open containing folder")
        act_copy_file   = menu.addAction("Copy PDF file")
        act_copy_text   = menu.addAction("Copy full text")
        menu.addSeparator()
        act_library = menu.addAction("Remove from library")
        act_disk    = menu.addAction("Delete from library and disk")
        chosen = menu.exec(self._table.viewport().mapToGlobal(pos))

        if chosen == act_open_folder:
            self._open_containing_folder(paper.file_path)
        elif chosen == act_copy_file:
            self._copy_pdf_file(paper.file_path)
        elif chosen == act_copy_text:
            self._copy_full_text(paper.file_path)
        elif chosen == act_library:
            self._delete_paper(paper, delete_file=False)
        elif chosen == act_disk:
            self._delete_paper(paper, delete_file=True)

    def _open_containing_folder(self, file_path: str) -> None:
        subprocess.Popen(["explorer", "/select," + file_path])

    def _copy_pdf_file(self, file_path: str) -> None:
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(file_path)])
        QApplication.clipboard().setMimeData(mime)

    def _copy_full_text(self, file_path: str) -> None:
        try:
            import fitz
            with fitz.open(file_path) as doc:
                text = "\n".join(page.get_text() for page in doc)
            QApplication.clipboard().setText(text)
        except Exception as e:
            logger.error("Failed to extract full text from %s: %s", file_path, e)

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
        self._model.drop_id(paper_id)

        # Drop from the cached result id list so re-filtering stays consistent.
        # If _last_result_ids is None (show-all), re-filtering will naturally exclude
        # the deleted paper via the DB query, so no update needed.
        if self._last_result_ids is not None:
            self._last_result_ids = [i for i in self._last_result_ids if i != paper_id]

        self.paper_selected.emit(None)
        self.paper_deleted.emit(paper_id)
