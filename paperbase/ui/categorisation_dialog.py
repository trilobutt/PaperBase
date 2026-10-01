import time
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSlot
from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit, QProgressBar,
    QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from paperbase.core.categoriser import CategorizationWorker, EmbeddingCategoriser
from paperbase.core.db import Database
from paperbase.core.vectors import VectorStore
from paperbase.ui import theme
from paperbase.ui.glass import CanvasBackdrop, GlassPanel, accent_glow

# Embedding throughput on the CPU build: roughly an hour for 150,000 papers on 4 to 6 cores
# (CLAUDE.md, Auto-Categorisation). Only the before-you-start estimates use it; a running
# embedding stage quotes its own measured rate instead.
_EMBED_PAPERS_PER_SECOND = 40

_SEP = "   ·   "


def _duration(seconds: float) -> str:
    """A span in words, to the minute: an hour-long estimate quoted to the second is false
    precision, and a figure that ticks every second reads as more certain than it is."""
    minutes = round(seconds / 60)
    if minutes < 1:
        return "under a minute"
    if minutes < 60:
        return f"about {minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"about {hours} h {minutes} min" if minutes else f"about {hours} h"


class CategorizationDialog(QDialog):
    def __init__(
        self,
        db: Database,
        categoriser: EmbeddingCategoriser,
        store: VectorStore,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Auto-Categorise Papers")
        self.setMinimumWidth(620)
        self.setMinimumHeight(440)
        self._db = db
        self._categoriser = categoriser
        self._store = store
        self._worker: Optional[CategorizationWorker] = None

        # The run's state, rendered into one status line by _render_run_status. Held here
        # rather than written straight from each slot because the facts arrive from four
        # slots in any order: a batch in flight when Pause is pressed still reports its
        # progress afterwards, and must not overwrite "Paused" with running text.
        self._stage: Optional[str] = None      # None while the model loads
        self._done = 0
        self._total = 0
        self._paused = False
        self._stopping = False
        self._keywords_only = False
        self._rate_start: Optional[float] = None
        self._rate_done = 0
        self._eta = ""

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

        self._status_label = QLabel(self._idle_text())
        self._status_label.setWordWrap(True)
        # Two lines held open whatever the line says: the running text grows and shrinks
        # as counts and the ETA come and go, and a label that rewrapped between one and two
        # lines would bounce the bar and the log beneath it every batch.
        self._status_label.setMinimumHeight(self._status_label.fontMetrics().lineSpacing() * 2)
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
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

        self._reset_btn = QPushButton("Rebuild vectors")
        self._reset_btn.setToolTip(
            "Discard the stored vectors so the next run embeds every paper from scratch"
        )
        self._reset_btn.clicked.connect(self._rebuild_vectors)

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

    def _idle_text(self) -> str:
        """What Start is about to do, said before it is pressed: the first run is the
        hours-long one, and the user deserves that figure before committing to it."""
        if not self._categoriser.has_categories:
            return (
                "Nothing to assign yet. Add a taxonomy file, a "
                "taxa.txt beside it, or categories in Settings, then press Start."
            )
        paper_ids = self._db.get_all_paper_ids()
        if not paper_ids:
            return "The library is empty, so there is nothing to categorise yet."
        pending = len(self._store.missing(paper_ids))
        if pending == 0:
            return (
                "Every paper already has a stored vector, so a run skips stage 1 and goes "
                "straight to assigning collections, taxa, and tags."
            )
        return (
            f"Two stages: embedding the {pending:,} papers without a stored vector takes "
            f"{_duration(pending / _EMBED_PAPERS_PER_SECOND)} and is kept for later runs, "
            "then assigning collections, taxa, and tags takes a fraction of that."
        )

    def _start(self) -> None:
        if not self._categoriser.has_categories:
            self._status_label.setText(self._idle_text())
            self._log.appendPlainText(
                "No taxonomy labels, taxa, or categories configured. Add them in Settings first."
            )
            return

        self._stage = None
        self._done = self._total = 0
        self._paused = False
        self._stopping = False
        self._keywords_only = False
        self._rate_start = None
        self._eta = ""
        self._progress_bar.setValue(0)

        self._worker = CategorizationWorker(
            db=self._db,
            categoriser=self._categoriser,
            store=self._store,
            parent=self,
        )
        self._worker.progress.connect(self._on_progress)
        self._worker.stage_changed.connect(self._on_stage)
        self._worker.log_message.connect(self._log.appendPlainText)
        self._worker.finished_all.connect(self._on_finished)
        self._worker.start()

        self._start_btn.setEnabled(False)
        self._pause_btn.setText("Pause")
        self._pause_btn.setEnabled(True)
        self._stop_btn.setEnabled(True)
        self._reset_btn.setEnabled(False)
        self._render_run_status()

    def _toggle_pause(self) -> None:
        if self._worker is None:
            return
        self._paused = not self._paused
        if self._paused:
            self._worker.request_pause()
            self._pause_btn.setText("Resume")
        else:
            self._worker.request_resume()
            self._pause_btn.setText("Pause")
            # Measure the rate afresh from here, or the paused minutes count as working
            # time and the ETA inflates by however long the user was away.
            if self._rate_start is not None:
                self._rate_start = time.monotonic()
                self._rate_done = self._done
        self._render_run_status()

    def _stop(self) -> None:
        if self._worker:
            self._worker.request_stop()
        self._stopping = True
        self._paused = False
        self._pause_btn.setEnabled(False)
        self._stop_btn.setEnabled(False)
        self._render_run_status()

    def _rebuild_vectors(self) -> None:
        stored = len(self._store)
        if stored == 0:
            self._log.appendPlainText(
                "No vectors are stored yet, so there is nothing to rebuild."
            )
            return

        paper_count = len(self._db.get_all_paper_ids())
        cost = _duration(paper_count / _EMBED_PAPERS_PER_SECOND)
        answer = QMessageBox.question(
            self,
            "Rebuild vectors",
            f"Discard the {stored:,} stored paper vectors?\n\n"
            f"The next run reads all {paper_count:,} papers again to rebuild them, which "
            f"takes {cost}. Collections and tags already assigned are left as they are.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        self._store.clear()
        self._log.appendPlainText(
            f"Discarded {stored:,} vectors. The next run embeds all {paper_count:,} "
            "papers again."
        )
        # The last run's ending ("Complete", with a vector count) is no longer true, and
        # neither is a full bar standing for it.
        self._progress_bar.setValue(0)
        self._status_label.setText(self._idle_text())

    def _render_run_status(self) -> None:
        """Compose the status line from the run's state, and hold the glow to match it.

        The glow is the running state and is off whenever nothing is moving: paused, or
        stopping. A stage change is still running, so it never flickers between stages.
        """
        counts = ""
        if self._total:
            pct = int(self._done / self._total * 100)
            counts = f"{_SEP}{self._done:,} of {self._total:,} papers ({pct}%)"

        if self._stopping:
            text = "Stopping once the current batch finishes…"
        elif self._stage is None:
            text = "Loading the embedding model…"
        elif self._keywords_only and self._stage == "embedding":
            text = (
                "No embedding model, so stage 1 is skipped. This run adds keywords and taxa "
                "only, and assigns no collections."
            )
        elif self._keywords_only:
            text = f"Keywords and taxa only, no collections (no embedding model){counts}"
        elif self._stage == "embedding":
            if not self._total:
                text = f"Stage 1 of 2{_SEP}Embedding{_SEP}finding papers without a vector…"
            else:
                text = f"Stage 1 of 2{_SEP}Embedding{counts}"
                if not self._paused and self._done < self._total:
                    text += (
                        f"{_SEP}{self._eta} left" if self._eta
                        else f"{_SEP}estimating time left…"
                    )
        else:
            text = f"Stage 2 of 2{_SEP}Assigning collections, taxa, and tags{counts or '…'}"

        if self._paused:
            text = f"Paused{_SEP}{text}"
        self._status_label.setText(text)
        self._bar_glow.setEnabled(not (self._paused or self._stopping))

    @pyqtSlot(str)
    def _on_stage(self, stage: str) -> None:
        """Carry the run from one stage to the next without the bar reading as a stall.

        The bar restarts from zero for each stage and the status line leads with the stage
        number, so a bar that crawled through an hour of embedding and then refills in
        seconds reads as a second, shorter pass rather than the first one leaping ahead.
        """
        if stage == "done":
            # The ending belongs to _on_finished, the only slot that knows whether Stop
            # was pressed; keep the last working stage for it to report.
            return
        if stage == "embedding" and not self._categoriser.is_loaded:
            # The worker has finished loading by the time it announces a stage, so this is
            # settled: the run goes on without a model and produces keywords only.
            self._keywords_only = True
        self._stage = stage
        self._done = self._total = 0
        self._rate_start = None
        self._eta = ""
        self._progress_bar.setValue(0)
        self._render_run_status()

    @pyqtSlot(int, int)
    def _on_progress(self, done: int, total: int) -> None:
        self._done, self._total = done, total
        self._progress_bar.setValue(int(done / total * 100) if total > 0 else 0)

        # Only the embedding stage carries an ETA: it is the one measured in hours, and
        # its rate is the run's own, measured from its first report onwards.
        if self._stage == "embedding":
            now = time.monotonic()
            if self._rate_start is None:
                self._rate_start, self._rate_done = now, done
            elif done > self._rate_done and now > self._rate_start:
                rate = (done - self._rate_done) / (now - self._rate_start)
                self._eta = _duration((total - done) / rate)
        self._render_run_status()

    @pyqtSlot()
    def _on_finished(self) -> None:
        self._start_btn.setEnabled(True)
        self._pause_btn.setText("Pause")
        self._pause_btn.setEnabled(False)
        self._stop_btn.setEnabled(False)
        self._reset_btn.setEnabled(True)
        self._bar_glow.setEnabled(False)

        # Stop pressed during the last batch lets the run finish anyway; call it complete.
        finished_anyway = (
            self._stage == "assigning" and self._total > 0 and self._done >= self._total
        )
        if self._stopping and not finished_anyway:
            # The bar stays where the run stopped: filling it would say "finished".
            if self._stage == "assigning":
                text = (
                    "Stopped in stage 2 of 2. Stored vectors are kept, and the next run "
                    "assigns collections and tags again from the start of the library."
                )
            elif stored := len(self._store):
                text = (
                    f"Stopped in stage 1 of 2 with {stored:,} vectors stored. They are "
                    "kept, so the next run embeds only the papers still without one."
                )
            else:
                text = "Stopped in stage 1 of 2 before any vectors were stored."
        elif self._keywords_only:
            self._progress_bar.setValue(100)
            text = (
                "Finished with keywords only. No collections were assigned, because the "
                "embedding model is not available."
            )
        else:
            self._progress_bar.setValue(100)
            text = (
                f"Complete. {len(self._store):,} papers have a stored vector, so the next "
                "run embeds only papers added since."
            )
        self._status_label.setText(text)
