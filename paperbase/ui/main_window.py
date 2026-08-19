import threading
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, QTimer, pyqtSlot
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QPushButton,
    QSizePolicy, QSplitter, QVBoxLayout, QWidget,
)

from paperbase.core.categoriser import EmbeddingCategoriser
from paperbase.core.db import Database
from paperbase.core.indexer import Indexer
from paperbase.models.paper import Paper
from paperbase.ui import theme
from paperbase.ui.categorisation_dialog import CategorizationDialog
from paperbase.ui.collection_tree import CollectionTree
from paperbase.ui.glass import CanvasBackdrop, GlassPanel
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
        self._cat_dialog: Optional[CategorizationDialog] = None

        self._categoriser = EmbeddingCategoriser()
        self._categoriser.update_settings(
            categories=settings.categories,
            threshold=settings.category_threshold,
            tag_count=settings.tag_count,
        )
        self.setWindowTitle("PaperBase")
        self.setMinimumSize(1100, 680)
        self._build_ui()

    def start_deferred_load(self) -> None:
        """Everything that is not needed to paint the first frame.

        Called by main.py after show(). Ordered by how soon the user needs it: the list,
        then the sidebar, then the embedding model, which is only needed once an import
        starts and costs seconds of CPU and disk to load.
        """
        QTimer.singleShot(0, self._populate_initial)
        QTimer.singleShot(120, self._collection_tree.refresh)
        QTimer.singleShot(2000, self._preload_categoriser)

    def _populate_initial(self) -> None:
        self._search_panel.run_search("")
        self._search_panel.refresh_tags()
        self._refresh_status()

    def _preload_categoriser(self) -> None:
        if self._settings.auto_categorise and self._settings.categories:
            threading.Thread(target=self._categoriser.load_model, daemon=True).start()

    def _build_ui(self) -> None:
        """Three rows of floating glass on a painted canvas: commands, work, status.

        Nothing is flush with anything. The 20px window margin and the 20px row gap are
        the same measure as the splitter's handle width, so the black between the panels
        reads as one continuous field showing through rather than as gutters.
        """
        central = CanvasBackdrop()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.setSpacing(20)

        # ---- Command bar: chrome glass, every control visible, no overflow menu ----
        command_bar = GlassPanel(chrome=True)
        command_bar.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        commands = QHBoxLayout()
        commands.setContentsMargins(0, 0, 0, 0)
        commands.setSpacing(theme.SPACE)

        self._search_bar = QLineEdit()
        self._search_bar.setPlaceholderText("Search papers…")
        self._search_bar.setMinimumWidth(400)
        self._search_bar.returnPressed.connect(self._run_search)
        commands.addWidget(self._search_bar, 1)

        search_btn = QPushButton("Search")
        search_btn.clicked.connect(self._run_search)
        commands.addWidget(search_btn)

        # Two groups, held apart by space rather than by the old separator rule: the
        # query on the left, the library actions on the right.
        commands.addSpacing(theme.SPACE * 2)

        import_btn = QPushButton("Import")
        import_btn.setObjectName("primary")
        import_btn.clicked.connect(self._open_import)
        commands.addWidget(import_btn)

        categorise_btn = QPushButton("Categorise")
        categorise_btn.setToolTip("Auto-categorise and tag all papers using text embeddings")
        categorise_btn.clicked.connect(self._open_categorise)
        commands.addWidget(categorise_btn)

        # Spelled out rather than a gear glyph: Tahoma has no U+2699, and the fallback
        # draws it small enough to read as an empty button.
        settings_btn = QPushButton("Settings")
        settings_btn.clicked.connect(self._open_settings)
        commands.addWidget(settings_btn)

        command_bar.content_layout.addLayout(commands)
        main_layout.addWidget(command_bar)

        # ---- The work: three islands, one accent each, canvas between them ----
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(20)
        splitter.setChildrenCollapsible(False)

        # Left: collection tree
        self._collection_tree = CollectionTree(self._db)
        self._collection_tree.setMinimumWidth(160)
        self._collection_tree.collection_selected.connect(self._on_collection_selected)
        self._collection_tree.tag_selected.connect(self._on_tag_selected)
        self._collection_tree.papers_added_to_collection.connect(self._on_papers_added_to_collection)
        library_panel = GlassPanel("Library", accent=theme.ACCENT_LIME)
        library_panel.set_content(self._collection_tree)
        splitter.addWidget(library_panel)

        # Centre: search + results
        self._search_panel = SearchPanel(self._db, self._indexer)
        self._search_panel.paper_selected.connect(self._on_paper_selected)
        self._search_panel.paper_deleted.connect(self._on_paper_deleted)
        self._search_panel.import_requested.connect(self._open_import)
        papers_panel = GlassPanel("Papers", accent=theme.ACCENT_CYAN)
        # The count belongs to the panel's header, opposite its title: it describes the
        # whole panel, and putting it above the table would cost a row of the results.
        papers_panel.add_header_widget(self._search_panel.count_label)
        papers_panel.set_content(self._search_panel)
        splitter.addWidget(papers_panel)

        # Right: detail panel
        self._detail_panel = PaperDetail(self._db, user_email=self._settings.user_email)
        self._detail_panel.paper_changed.connect(self._on_paper_changed)
        self._detail_panel.setMinimumWidth(260)
        paper_panel = GlassPanel("Paper", accent=theme.ACCENT_MAGENTA)
        paper_panel.set_content(self._detail_panel)
        splitter.addWidget(paper_panel)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 4)
        splitter.setStretchFactor(2, 2)
        main_layout.addWidget(splitter, 1)

        # ---- Status strip: an island too, and a slim one ----
        self._status_panel = GlassPanel()
        self._status_panel.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        # Half the vertical padding of a working panel: this is a strip carrying one
        # line, and a full 2u pad above and below it would read as an empty surface.
        self._status_panel.layout().setContentsMargins(
            theme.SPACE * 2, theme.SPACE, theme.SPACE * 2, theme.SPACE
        )

        self._status_label = QLabel()
        self._status_label.setTextFormat(Qt.TextFormat.RichText)
        self._status_label.setStyleSheet(
            f"color: {theme.TEXT_SECONDARY}; background: transparent;"
        )
        self._status_panel.content_layout.addWidget(self._status_label)
        main_layout.addWidget(self._status_panel)

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

    def _on_papers_added_to_collection(self, paper_ids: list[int]) -> None:
        for pid in paper_ids:
            self._search_panel.reload_current_paper(pid)
        self._collection_tree.refresh()

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
                categoriser=self._categoriser,
                parent=self,
            )
            self._import_dialog.import_finished.connect(self.refresh_all)
            self._import_dialog.settings_changed.connect(
                lambda: self._settings.save(self._settings_path)
            )
        self._import_dialog.show()
        self._import_dialog.raise_()

    def _open_categorise(self) -> None:
        if not self._settings.is_configured():
            self._open_settings()
            if not self._settings.is_configured():
                return

        if self._cat_dialog is None:
            state_file = Path(self._settings.library_root) / "categorisation_state.json"
            self._cat_dialog = CategorizationDialog(
                db=self._db,
                categoriser=self._categoriser,
                state_file=state_file,
                parent=self,
            )
            self._cat_dialog.finished.connect(self.refresh_all)
        self._cat_dialog.show()
        self._cat_dialog.raise_()

    def _open_settings(self) -> None:
        dlg = SettingsDialog(self._settings, self)
        if dlg.exec():
            self._settings.save(self._settings_path)
            self._detail_panel.set_user_email(self._settings.user_email)
            self._categoriser.update_settings(
                categories=self._settings.categories,
                threshold=self._settings.category_threshold,
                tag_count=self._settings.tag_count,
            )
            # If categories were just configured for the first time, preload the model.
            self._preload_categoriser()
            # ImportDialog is cached; invalidate it so it picks up the new categoriser state.
            self._import_dialog = None
            self._cat_dialog = None
            self._refresh_status()

    def _refresh_status(self) -> None:
        total = self._db.get_paper_count()
        review = self._db.get_needs_review_count()
        idx_docs = self._indexer.document_count()
        # Rich text, so the separators' air has to be non-breaking: HTML collapses a run
        # of ordinary spaces and the three figures would run together.
        gap = "&nbsp;&nbsp;&nbsp;·&nbsp;&nbsp;&nbsp;"
        needs_review = f"{review:,} need review"
        if review > 0:
            # Amber is the needs-review hue everywhere in the application. The count and
            # the word carry the same information for anyone who cannot read the colour.
            needs_review = f'<span style="color: {theme.ACCENT_AMBER};">{needs_review}</span>'
        self._status_label.setText(
            f"{total:,} papers{gap}{needs_review}{gap}index {idx_docs:,}"
        )

    @pyqtSlot()
    def refresh_all(self) -> None:
        """Called after bulk import completes to refresh UI."""
        self._search_panel.run_search(self._search_bar.text())
        self._search_panel.refresh_tags()
        self._collection_tree.refresh()
        self._refresh_status()
