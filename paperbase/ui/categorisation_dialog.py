from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSlot
from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QProgressBar,
    QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from paperbase.core.categoriser import CategorizationWorker, EmbeddingCategoriser
from paperbase.core.db import Database
from paperbase.ui import theme
from paperbase.ui.glass import CanvasBackdrop, GlassPanel, accent_glow


class CategorizationDialog(QDialog):
    def __init__(
        self,
        db: Database,
        categoriser: EmbeddingCategoriser,
        state_file: Path,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Auto-Categorise Papers")
        self.setMinimumWidth(620)
        self.setMinimumHeight(440)
        self._db = db
        self._categoriser = categoriser
        self._state_file = state_file
        self._worker: Optional[CategorizationWorker] = None
        self._build_ui()

    def _build_ui(self) -> None:
        """Two islands on the canvas: the run, and the controls that drive it.

        Lime, because this writes into collections and tags, and lime is the hue those
        wear in the window's Library panel and in the Settings group that configures it.
        """
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        backdrop = CanvasBackdrop(self)
        root.addWidget(backdrop)

        layout = QVBoxLayout(backdrop)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(20)

        run_panel = GlassPanel("Categorise library", accent=theme.ACCENT_LIME)

        self._status_label = QLabel("Ready. Press Start to begin.")
        run_panel.content_layout.addWidget(self._status_label)

        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setProperty("accent", "lime")
        self._progress_bar.setTextVisible(False)
        # The bloom is the running state, and it is off at rest: the status line above
        # says the same thing in words for anyone who cannot read the colour.
        self._bar_glow = accent_glow(self._progress_bar, theme.ACCENT_LIME)
        self._bar_glow.setEnabled(False)
        run_panel.content_layout.addWidget(self._progress_bar)

        self._log = QPlainTextEdit()
        self._log.setObjectName("LogView")
        self._log.setReadOnly(True)
        # Click to select text, but never take focus on open: read-only output wearing
        # the focus ring puts the brightest edge in the dialog around its emptiest panel,
        # and pushes it off Start, which is the control the user actually came for.
        self._log.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self._log.setMaximumBlockCount(200)
        run_panel.content_layout.addWidget(self._log, 1)

        layout.addWidget(run_panel, 1)

        # ---- Controls: a chrome strip, matching the window's command bar ----
        control_bar = GlassPanel(chrome=True)
        control_bar.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        btn_layout = QHBoxLayout()
        btn_layout.setContentsMargins(0, 0, 0, 0)
        btn_layout.setSpacing(theme.SPACE)

        self._start_btn = QPushButton("Start")
        self._start_btn.setObjectName("primary")
        self._start_btn.clicked.connect(self._start)

        self._pause_btn = QPushButton("Pause")
        self._pause_btn.clicked.connect(self._toggle_pause)
        self._pause_btn.setEnabled(False)

        self._stop_btn = QPushButton("Stop")
        self._stop_btn.clicked.connect(self._stop)
        self._stop_btn.setEnabled(False)

        self._reset_btn = QPushButton("Reset progress")
        self._reset_btn.setToolTip(
            "Clear saved progress so all papers are re-categorised from scratch on the next run"
        )
        self._reset_btn.clicked.connect(self._reset_state)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)

        btn_layout.addWidget(self._start_btn)
        btn_layout.addWidget(self._pause_btn)
        btn_layout.addWidget(self._stop_btn)
        btn_layout.addStretch()
        btn_layout.addWidget(self._reset_btn)
        btn_layout.addWidget(close_btn)
        control_bar.content_layout.addLayout(btn_layout)
        layout.addWidget(control_bar)

    def _start(self) -> None:
        if not self._categoriser.has_categories:
            self._log.appendPlainText(
                "No categories configured. Add categories in Settings first."
            )
            return

        self._worker = CategorizationWorker(
            db=self._db,
            categoriser=self._categoriser,
            state_file=self._state_file,
            parent=self,
        )
        self._worker.progress.connect(self._on_progress)
        self._worker.log_message.connect(self._log.appendPlainText)
        self._worker.finished_all.connect(self._on_finished)
        self._worker.start()

        self._start_btn.setEnabled(False)
        self._pause_btn.setEnabled(True)
        self._stop_btn.setEnabled(True)
        self._reset_btn.setEnabled(False)
        self._bar_glow.setEnabled(True)
        self._status_label.setText("Running…")

    def _toggle_pause(self) -> None:
        if self._worker is None:
            return
        if self._pause_btn.text() == "Pause":
            self._worker.request_pause()
            self._pause_btn.setText("Resume")
            self._bar_glow.setEnabled(False)
            self._status_label.setText("Paused.")
        else:
            self._worker.request_resume()
            self._pause_btn.setText("Pause")
            self._bar_glow.setEnabled(True)
            self._status_label.setText("Running…")

    def _stop(self) -> None:
        if self._worker:
            self._worker.request_stop()
        self._pause_btn.setEnabled(False)
        self._stop_btn.setEnabled(False)
        self._bar_glow.setEnabled(False)
        self._status_label.setText("Stopping…")

    def _reset_state(self) -> None:
        if self._state_file.exists():
            self._state_file.unlink()
        self._log.appendPlainText(
            "Progress reset. All papers will be re-categorised on the next run."
        )

    @pyqtSlot(int, int)
    def _on_progress(self, done: int, total: int) -> None:
        pct = int(done / total * 100) if total > 0 else 0
        self._progress_bar.setValue(pct)
        self._status_label.setText(f"{done:,} / {total:,} papers ({pct}%)")

    @pyqtSlot()
    def _on_finished(self) -> None:
        self._start_btn.setEnabled(True)
        self._pause_btn.setEnabled(False)
        self._stop_btn.setEnabled(False)
        self._reset_btn.setEnabled(True)
        self._bar_glow.setEnabled(False)
        self._status_label.setText("Categorisation complete.")
        self._progress_bar.setValue(100)
