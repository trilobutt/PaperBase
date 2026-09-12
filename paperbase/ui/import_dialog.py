from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QDialog, QFileDialog, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QPlainTextEdit, QProgressBar, QPushButton,
    QSizePolicy, QTabWidget, QVBoxLayout, QWidget,
)

from paperbase.core.categoriser import EmbeddingCategoriser
from paperbase.core.db import Database
from paperbase.core.importer import ImportWorker
from paperbase.core.indexer import Indexer
from paperbase.ui import theme
from paperbase.ui.glass import CanvasBackdrop, GlassPanel, accent_glow
from paperbase.ui.settings_dialog import Settings

MAX_LOG_LINES = 20


class ImportDialog(QDialog):
    import_finished = pyqtSignal()
    settings_changed = pyqtSignal()

    def __init__(
        self,
        db: Database,
        indexer: Indexer,
        library_root: Path,
        user_email: str,
        settings: Optional[Settings] = None,
        state_file: Optional[Path] = None,
        categoriser: Optional[EmbeddingCategoriser] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Import Papers")
        self.setMinimumSize(720, 640)
        self.setWindowFlag(Qt.WindowType.Window)  # non-modal independent window

        self._db = db
        self._indexer = indexer
        self._library_root = library_root
        self._user_email = user_email
        self._settings = settings
        self._state_file = state_file
        self._categoriser = categoriser
        self._worker: Optional[ImportWorker] = None
        self._build_ui()

    def _build_ui(self) -> None:
        """Three islands on the canvas: what to import, how it is going, the controls.

        Amber throughout, because import is the amber region everywhere in the
        application: the needs-review state this dialog produces wears the same hue in
        the paper form, and in the window's status strip.
        """
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        backdrop = CanvasBackdrop(self)
        root.addWidget(backdrop)

        layout = QVBoxLayout(backdrop)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(20)

        # ---- Source: three tabs, one job each ----
        source_panel = GlassPanel("Add papers", accent=theme.ACCENT_AMBER)
        self._tabs = QTabWidget()

        # ---- Tab 1: Drop PDFs ----
        pdf_tab = QWidget()
        pdf_layout = QVBoxLayout(pdf_tab)
        pdf_layout.setContentsMargins(0, theme.SPACE, 0, 0)
        pdf_layout.setSpacing(theme.SPACE)
        # Said on the surface, not only in the tooltip: the drop target is an empty black
        # rectangle until something is in it, and the other two tabs already label theirs.
        pdf_layout.addWidget(QLabel("Drag PDFs onto the list, or add them from disk:"))
        self._pdf_list = QListWidget()
        self._pdf_list.setAcceptDrops(True)
        self._pdf_list.setDragDropMode(QListWidget.DragDropMode.DropOnly)
        self._pdf_list.setToolTip("Drag and drop PDF files here")
        pdf_layout.addWidget(self._pdf_list)
        pdf_btn_row = QHBoxLayout()
        pdf_btn_row.setSpacing(theme.SPACE)
        add_files_btn = QPushButton("Add Files…")
        add_files_btn.clicked.connect(self._browse_pdfs)
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self._pdf_list.clear)
        pdf_btn_row.addWidget(add_files_btn)
        pdf_btn_row.addWidget(clear_btn)
        pdf_btn_row.addStretch()
        pdf_layout.addLayout(pdf_btn_row)
        self._tabs.addTab(pdf_tab, "Drop PDFs")

        # ---- Tab 2: Paste DOIs ----
        doi_tab = QWidget()
        doi_layout = QVBoxLayout(doi_tab)
        doi_layout.setContentsMargins(0, theme.SPACE, 0, 0)
        doi_layout.setSpacing(theme.SPACE)
        doi_layout.addWidget(QLabel("One DOI per line:"))
        self._doi_text = QPlainTextEdit()
        self._doi_text.setPlaceholderText("10.1234/example\n10.5678/another")
        doi_layout.addWidget(self._doi_text)
        self._tabs.addTab(doi_tab, "Paste DOIs")

        # ---- Tab 3: Paste URLs ----
        url_tab = QWidget()
        url_layout = QVBoxLayout(url_tab)
        url_layout.setContentsMargins(0, theme.SPACE, 0, 0)
        url_layout.setSpacing(theme.SPACE)
        url_layout.addWidget(QLabel("One URL per line (PDF links or article pages):"))
        self._url_text = QPlainTextEdit()
        self._url_text.setPlaceholderText("https://example.com/article/10.1234/example")
        url_layout.addWidget(self._url_text)
        self._tabs.addTab(url_tab, "Paste URLs")

        source_panel.set_content(self._tabs)

        # One amber line, and only while it is true. Without a fingerprint, a file the
        # library already holds under a different name imports a second time and nobody
        # is told; this is the one place that gap costs the user anything, so it is the
        # one place it is said. Read once here, and it gates nothing.
        hashed, total = self._db.get_hash_coverage()
        missing = total - hashed
        if missing > 0:
            subject = "paper has" if missing == 1 else "papers have"
            gap_note = QLabel(
                f"{missing:,} {subject} no fingerprint yet, so identical files may "
                "import twice. Settings → Scan library."
            )
            gap_note.setWordWrap(True)
            gap_note.setStyleSheet(f"color: {theme.ACCENT_AMBER};")
            source_panel.content_layout.addWidget(gap_note)

        layout.addWidget(source_panel, 1)

        # ---- Progress: the amber region's live surface ----
        progress_panel = GlassPanel("Progress", accent=theme.ACCENT_AMBER)

        # A slim band of pure amber on an inset groove, and no text on the bar: the
        # counts row under it says the same thing in words, and says it more precisely.
        self._progress_bar = QProgressBar()
        self._progress_bar.setProperty("accent", "amber")
        self._progress_bar.setTextVisible(False)
        # Lit only while the worker is actually moving, so the bloom is a state rather
        # than decoration. The button labels carry that state in words as well.
        self._bar_glow = accent_glow(self._progress_bar, theme.ACCENT_AMBER)
        self._bar_glow.setEnabled(False)
        progress_panel.content_layout.addWidget(self._progress_bar)

        counts_row = QHBoxLayout()
        counts_row.setSpacing(theme.SPACE * 2)
        self._lbl_done    = QLabel("0 of 0")
        self._lbl_ok      = QLabel("Imported 0")
        self._lbl_dupes   = QLabel("Already held 0")
        self._lbl_failed  = QLabel("Failed 0")
        self._lbl_review  = QLabel("Needs review 0")
        self._lbl_review.setToolTip(
            "Imported papers whose metadata could not be confirmed. Counted in Imported."
        )
        for lbl in (self._lbl_done, self._lbl_ok, self._lbl_dupes,
                    self._lbl_failed, self._lbl_review):
            counts_row.addWidget(lbl)
        counts_row.addStretch()
        progress_panel.content_layout.addLayout(counts_row)

        # Fixed-width, because it is fixed-width output: DOIs and paths line up.
        self._log_view = QListWidget()
        self._log_view.setObjectName("LogView")
        # Read-only output: selectable by click, never a tab stop. See the same note in
        # categorisation_dialog.
        self._log_view.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self._log_view.setMaximumHeight(120)
        progress_panel.content_layout.addWidget(self._log_view)

        layout.addWidget(progress_panel)

        # ---- Controls: a chrome strip, matching the window's command bar ----
        control_bar = GlassPanel(chrome=True)
        control_bar.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 0, 0, 0)
        btn_row.setSpacing(theme.SPACE)
        self._start_btn = QPushButton("Start Import")
        self._start_btn.setObjectName("primary")
        self._start_btn.clicked.connect(self._start_import)
        self._pause_btn = QPushButton("Pause")
        self._pause_btn.setEnabled(False)
        self._pause_btn.clicked.connect(self._toggle_pause)
        self._stop_btn = QPushButton("Stop")
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._stop_import)
        btn_row.addWidget(self._start_btn)
        btn_row.addWidget(self._pause_btn)
        btn_row.addWidget(self._stop_btn)
        btn_row.addStretch()
        control_bar.content_layout.addLayout(btn_row)
        layout.addWidget(control_bar)

    # ------------------------------------------------------------------

    def _browse_pdfs(self) -> None:
        start_dir = ""
        if self._settings and self._settings.last_import_dir:
            start_dir = self._settings.last_import_dir
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Select PDFs", start_dir, "PDF Files (*.pdf)"
        )
        if paths:
            last_dir = str(Path(paths[-1]).parent)
            if self._settings:
                self._settings.last_import_dir = last_dir
                self.settings_changed.emit()
        for p in paths:
            if not self._pdf_list.findItems(p, Qt.MatchFlag.MatchExactly):
                self._pdf_list.addItem(QListWidgetItem(p))

    def _start_import(self) -> None:
        tab = self._tabs.currentIndex()
        if tab == 0:
            items = [self._pdf_list.item(i).text() for i in range(self._pdf_list.count())]
            mode = "pdfs"
        elif tab == 1:
            items = [l.strip() for l in self._doi_text.toPlainText().splitlines() if l.strip()]
            mode = "dois"
        else:
            items = [l.strip() for l in self._url_text.toPlainText().splitlines() if l.strip()]
            mode = "urls"

        if not items:
            return

        self._progress_bar.setMaximum(len(items))
        self._progress_bar.setValue(0)

        secondary_dest = (
            Path(self._settings.secondary_dest)
            if self._settings and self._settings.secondary_dest
            else None
        )
        self._worker = ImportWorker(
            mode=mode,
            items=items,
            db=self._db,
            indexer=self._indexer,
            library_root=self._library_root,
            user_email=self._user_email,
            state_file=self._state_file,
            folder_pattern=self._settings.folder_pattern,
            secondary_dest=secondary_dest,
            categoriser=self._categoriser if self._settings and self._settings.auto_categorise else None,
        )
        self._worker.progress.connect(self._on_progress)
        self._worker.log_message.connect(self._on_log)
        self._worker.item_failed.connect(
            lambda label, reason: self._on_log(f"FAILED {label}: {reason}")
        )
        self._worker.finished_all.connect(self._on_finished)

        self._start_btn.setEnabled(False)
        self._pause_btn.setEnabled(True)
        self._stop_btn.setEnabled(True)
        self._bar_glow.setEnabled(True)
        self._worker.start()

    @pyqtSlot(int, int, int, int, int, int)
    def _on_progress(self, done: int, total: int, imported: int, review: int,
                     failed: int, dupes: int) -> None:
        self._progress_bar.setMaximum(total)
        self._progress_bar.setValue(done)
        self._lbl_done.setText(f"{done:,} of {total:,}")
        self._lbl_ok.setText(f"Imported {imported:,}")
        self._lbl_dupes.setText(f"Already held {dupes:,}")
        self._lbl_failed.setText(f"Failed {failed:,}")
        self._lbl_review.setText(f"Needs review {review:,}")

    @pyqtSlot(str)
    def _on_log(self, text: str) -> None:
        self._log_view.addItem(text)
        if self._log_view.count() > MAX_LOG_LINES:
            self._log_view.takeItem(0)
        self._log_view.scrollToBottom()

    def _on_finished(self) -> None:
        self._start_btn.setEnabled(True)
        self._pause_btn.setEnabled(False)
        self._stop_btn.setEnabled(False)
        self._bar_glow.setEnabled(False)
        self._on_log("Import complete.")
        self.import_finished.emit()

    def _toggle_pause(self) -> None:
        if self._worker is None:
            return
        if self._pause_btn.text() == "Pause":
            self._worker.request_pause()
            self._pause_btn.setText("Resume")
            self._bar_glow.setEnabled(False)
        else:
            self._worker.request_resume()
            self._pause_btn.setText("Pause")
            self._bar_glow.setEnabled(True)

    def _stop_import(self) -> None:
        if self._worker:
            self._worker.request_stop()
        self._stop_btn.setEnabled(False)
        self._bar_glow.setEnabled(False)

    def closeEvent(self, event) -> None:
        # Non-modal: just hide, don't destroy. Worker continues in background.
        event.ignore()
        self.hide()
