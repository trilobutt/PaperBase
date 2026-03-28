from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSlot
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QPushButton,
    QSplitter, QStatusBar, QToolBar, QVBoxLayout, QWidget,
)

from paperbase.core.db import Database
from paperbase.core.indexer import Indexer
from paperbase.models.paper import Paper
from paperbase.ui.collection_tree import CollectionTree
from paperbase.ui.import_dialog import ImportDialog
from paperbase.ui.paper_detail import PaperDetail
from paperbase.ui.search_panel import SearchPanel
from paperbase.ui.settings_dialog import Settings, SettingsDialog


class MainWindow(QMainWindow):
    def __init__(
        self,
        db: Database,
        indexer: Indexer,
        settings: Settings,
        settings_path: Path,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._db = db
        self._indexer = indexer
        self._settings = settings
        self._settings_path = settings_path
        self._import_dialog: Optional[ImportDialog] = None
        self.setWindowTitle("PaperBase")
        self.setMinimumSize(1100, 680)
        self._build_ui()
        self._refresh_status()

    def _build_ui(self) -> None:
        # ---- Toolbar ----
        toolbar = QToolBar()
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        self._search_bar = QLineEdit()
        self._search_bar.setPlaceholderText("Search papers…")
        self._search_bar.setMinimumWidth(400)
        self._search_bar.returnPressed.connect(self._run_search)
        toolbar.addWidget(self._search_bar)

        search_btn = QPushButton("Search")
        search_btn.clicked.connect(self._run_search)
        toolbar.addWidget(search_btn)

        toolbar.addSeparator()

        import_btn = QPushButton("Import")
        import_btn.clicked.connect(self._open_import)
        toolbar.addWidget(import_btn)

        settings_btn = QPushButton("⚙")
        settings_btn.setToolTip("Settings")
        settings_btn.clicked.connect(self._open_settings)
        toolbar.addWidget(settings_btn)

        # ---- Central splitter ----
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left: collection tree
        self._collection_tree = CollectionTree(self._db)
        self._collection_tree.setMinimumWidth(160)
        self._collection_tree.collection_selected.connect(self._on_collection_selected)
        self._collection_tree.tag_selected.connect(self._on_tag_selected)
        splitter.addWidget(self._collection_tree)

        # Centre: search + results
        self._search_panel = SearchPanel(self._db, self._indexer)
        self._search_panel.paper_selected.connect(self._on_paper_selected)
        self._search_panel.paper_deleted.connect(self._on_paper_deleted)
        splitter.addWidget(self._search_panel)

        # Right: detail panel
        self._detail_panel = PaperDetail(self._db)
        self._detail_panel.paper_changed.connect(self._on_paper_changed)
        self._detail_panel.setMinimumWidth(260)
        splitter.addWidget(self._detail_panel)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 4)
        splitter.setStretchFactor(2, 2)
        main_layout.addWidget(splitter)

        # ---- Status bar ----
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)

        # Show all papers on startup
        self._search_panel.run_search("")

    # ------------------------------------------------------------------

    def _run_search(self) -> None:
        self._search_panel.run_search(self._search_bar.text())

    def _on_paper_selected(self, paper: Optional[Paper]) -> None:
        if paper:
            self._detail_panel.show_paper(paper)
        else:
            self._detail_panel.clear()

    def _on_collection_selected(self, collection_id: Optional[int]) -> None:
        self._search_panel.set_collection_filter(collection_id)

    def _on_tag_selected(self, tag: Optional[str]) -> None:
        self._search_panel.set_tag_filter(tag)

    def _on_paper_changed(self, paper_id: int) -> None:
        self._search_panel.reload_current_paper(paper_id)
        self._collection_tree.refresh()
        self._refresh_status()

    def _on_paper_deleted(self, paper_id: int) -> None:
        self._detail_panel.clear()
        self._collection_tree.refresh()
        self._refresh_status()

    def _open_import(self) -> None:
        if not self._settings.is_configured():
            self._open_settings()
            if not self._settings.is_configured():
                return

        if self._import_dialog is None:
            state_file = Path(self._settings.library_root) / "import_state.json"
            self._import_dialog = ImportDialog(
                db=self._db,
                indexer=self._indexer,
                library_root=Path(self._settings.library_root),
                user_email=self._settings.user_email,
                settings=self._settings,
                state_file=state_file,
                parent=self,
            )
            self._import_dialog.import_finished.connect(self.refresh_all)
            self._import_dialog.settings_changed.connect(
                lambda: self._settings.save(self._settings_path)
            )
        self._import_dialog.show()
        self._import_dialog.raise_()

    def _open_settings(self) -> None:
        dlg = SettingsDialog(self._settings, self)
        if dlg.exec():
            self._settings.save(self._settings_path)
            self._refresh_status()

    def _refresh_status(self) -> None:
        total = self._db.get_paper_count()
        review = self._db.get_needs_review_count()
        idx_docs = self._indexer.document_count()
        self._status_bar.showMessage(
            f"{total:,} papers  |  {review} needs review  |  Index: {idx_docs:,} docs"
        )

    @pyqtSlot()
    def refresh_all(self) -> None:
        """Called after bulk import completes to refresh UI."""
        self._search_panel.run_search(self._search_bar.text())
        self._search_panel.refresh_tags()
        self._collection_tree.refresh()
        self._refresh_status()
