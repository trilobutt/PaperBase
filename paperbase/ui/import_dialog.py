from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QDialog, QFileDialog, QGroupBox, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QPlainTextEdit, QProgressBar, QPushButton,
    QTabWidget, QVBoxLayout, QWidget,
)

from paperbase.core.db import Database
from paperbase.core.importer import ImportWorker
from paperbase.core.indexer import Indexer
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
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Import Papers")
        self.setMinimumSize(640, 520)
        self.setWindowFlag(Qt.WindowType.Window)  # non-modal independent window

        self._db = db
        self._indexer = indexer
        self._library_root = library_root
        self._user_email = user_email
        self._settings = settings
        self._state_file = state_file
        self._worker: Optional[ImportWorker] = None
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        self._tabs = QTabWidget()

        # ---- Tab 1: Drop PDFs ----
        pdf_tab = QWidget()
        pdf_layout = QVBoxLayout(pdf_tab)
        self._pdf_list = QListWidget()
        self._pdf_list.setAcceptDrops(True)
        self._pdf_list.setDragDropMode(QListWidget.DragDropMode.DropOnly)
        self._pdf_list.setToolTip("Drag and drop PDF files here")
        pdf_layout.addWidget(self._pdf_list)
        pdf_btn_row = QHBoxLayout()
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
        doi_layout.addWidget(QLabel("One DOI per line:"))
        self._doi_text = QPlainTextEdit()
        self._doi_text.setPlaceholderText("10.1234/example\n10.5678/another")
        doi_layout.addWidget(self._doi_text)
        self._tabs.addTab(doi_tab, "Paste DOIs")

        # ---- Tab 3: Paste URLs ----
        url_tab = QWidget()
        url_layout = QVBoxLayout(url_tab)
        url_layout.addWidget(QLabel("One URL per line (PDF links or article pages):"))
        self._url_text = QPlainTextEdit()
        self._url_text.setPlaceholderText("https://example.com/article/10.1234/example")
        url_layout.addWidget(self._url_text)
        self._tabs.addTab(url_tab, "Paste URLs")

        layout.addWidget(self._tabs)

        # ---- Progress section ----
        progress_group = QGroupBox("Progress")
        pg_layout = QVBoxLayout(progress_group)

        self._progress_bar = QProgressBar()
        self._progress_bar.setTextVisible(True)
        pg_layout.addWidget(self._progress_bar)

        counts_row = QHBoxLayout()
        self._lbl_queued  = QLabel("Queued: 0")
        self._lbl_done    = QLabel("Done: 0")
        self._lbl_ok      = QLabel("OK: 0")
        self._lbl_review  = QLabel("Needs review: 0")
        self._lbl_failed  = QLabel("Failed: 0")
        self._lbl_dupes   = QLabel("Duplicates: 0")
        for lbl in (self._lbl_queued, self._lbl_done, self._lbl_ok,
                    self._lbl_review, self._lbl_failed, self._lbl_dupes):
            counts_row.addWidget(lbl)
        counts_row.addStretch()
        pg_layout.addLayout(counts_row)

        self._log_view = QListWidget()
        self._log_view.setMaximumHeight(120)
        self._log_view.setFont(
            __import__("PyQt6.QtGui", fromlist=["QFont"]).QFont("Consolas", 9)
        )
        pg_layout.addWidget(self._log_view)

        layout.addWidget(progress_group)

        # ---- Buttons ----
        btn_row = QHBoxLayout()
        self._start_btn = QPushButton("Start Import")
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
        layout.addLayout(btn_row)

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
        self._lbl_queued.setText(f"Queued: {self._pdf_list.count()}")

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
        self._lbl_queued.setText(f"Queued: {len(items)}")

        self._worker = ImportWorker(
            mode=mode,
            items=items,
            db=self._db,
            indexer=self._indexer,
            library_root=self._library_root,
            user_email=self._user_email,
            state_file=self._state_file,
            folder_pattern=self._settings.folder_pattern,
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
        self._worker.start()

    @pyqtSlot(int, int, int, int, int, int)
    def _on_progress(self, done: int, total: int, ok: int, review: int, failed: int, dupes: int) -> None:
        self._progress_bar.setValue(done)
        self._lbl_done.setText(f"Done: {done}")
        self._lbl_ok.setText(f"OK: {ok}")
        self._lbl_review.setText(f"Needs review: {review}")
        self._lbl_failed.setText(f"Failed: {failed}")
        self._lbl_dupes.setText(f"Duplicates: {dupes}")

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
        self._on_log("Import complete.")
        self.import_finished.emit()

    def _toggle_pause(self) -> None:
        if self._worker is None:
            return
        if self._pause_btn.text() == "Pause":
            self._worker.request_pause()
            self._pause_btn.setText("Resume")
        else:
            self._worker.request_resume()
            self._pause_btn.setText("Pause")

    def _stop_import(self) -> None:
        if self._worker:
            self._worker.request_stop()
        self._stop_btn.setEnabled(False)

    def closeEvent(self, event) -> None:
        # Non-modal: just hide, don't destroy. Worker continues in background.
        event.ignore()
        self.hide()
