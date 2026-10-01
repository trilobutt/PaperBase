"""BackfillDialog: the surface for ``HashBackfillWorker`` and the duplicate report it yields.

Amber throughout. Fingerprinting is the import family's maintenance job, and incomplete
coverage is a caution, so it wears the hue import and needs-review already wear everywhere
else in the application. The one exception is the clean end state, which resolves to lime:
that is the success hue in the Library panel and in the categoriser's own dialog.

Two islands on the canvas and a chrome control strip:

- **Fingerprint library** is the run: a status line in words, a slim amber bar, and the
  worker's log.
- **Duplicates** is the report, and every one of its states is designed. Before a scan it
  says so; after a scan it either confirms the library holds no repeated file or lists the
  groups it found, each row carrying the full path because the path is what tells the user
  which copy to keep.

Nothing is deleted without the user pressing Remove duplicates and confirming it. The
button keeps one copy per group (see ``core.dedupe.choose_keeper``) and says so.
"""

from typing import Optional

from PyQt6.QtCore import Qt, pyqtSlot
from PyQt6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QScrollArea, QSizePolicy, QStackedWidget,
    QVBoxLayout, QWidget,
)

from paperbase.core.backfill import HashBackfillWorker
from paperbase.core.db import Database
from paperbase.core.dedupe import DedupeWorker
from paperbase.core.indexer import Indexer
from paperbase.models.paper import Paper
from paperbase.ui import theme
from paperbase.ui.glass import (
    CanvasBackdrop, EmptyState, GlassPanel, StackFader, accent_glow,
)

# A library of 130,000 papers can carry more duplicate groups than there is any use in
# building widgets for. The report is a working list, not an archive: the first slice is
# what the user acts on, and the count of the rest is stated rather than hidden.
MAX_GROUPS_SHOWN = 200

# EmptyState builds its heading at this size and weight (see glass.EmptyState). A widget
# stylesheet replaces rather than merges, so recolouring the heading has to restate both,
# or the global sheet's body size takes over and the display heading silently shrinks.
_EMPTY_HEADING_PT = 22


def _plural(count: int, singular: str, plural: str) -> str:
    """``3 groups``/``1 group``. Proper plurals: this copy is read, not logged."""
    return f"{count:,} {singular if count == 1 else plural}"


def _first_author(paper: Paper) -> str:
    """The family name of the first author, which is how the user recognises a paper."""
    if not paper.authors:
        return "Unknown"
    return paper.authors[0].split(",")[0].strip() or "Unknown"


class BackfillDialog(QDialog):
    """Runs :class:`HashBackfillWorker` and reports what its fingerprints found."""

    _PAGE_UNSCANNED = 0
    _PAGE_RESULT = 1

    def __init__(
        self, db: Database, indexer: Indexer, parent: Optional[QWidget] = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Fingerprint Library")
        self.setMinimumSize(720, 700)
        self._db = db
        self._indexer = indexer
        self._worker: Optional[HashBackfillWorker] = None
        self._dedupe_worker: Optional[DedupeWorker] = None
        self._groups: list[tuple[str, list[int]]] = []
        self._stop_requested = False
        self._build_ui()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        backdrop = CanvasBackdrop(self)
        root.addWidget(backdrop)

        layout = QVBoxLayout(backdrop)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(20)

        # The worker logs three lines over an entire run (start, unreadable count, stop),
        # so the run panel needs room for its status and its bar and no more. The report
        # is what the user opened this for, and it takes the rest.
        layout.addWidget(self._build_run_panel(), 1)
        layout.addWidget(self._build_report_panel(), 3)
        layout.addWidget(self._build_control_bar())

    def _build_run_panel(self) -> GlassPanel:
        panel = GlassPanel("Fingerprint library", accent=theme.ACCENT_AMBER)

        self._status_label = QLabel(
            "Ready. Each PDF is read once and fingerprinted, and nothing else about a "
            "paper changes."
        )
        self._status_label.setWordWrap(True)
        panel.content_layout.addWidget(self._status_label)

        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setProperty("accent", "amber")
        self._progress_bar.setTextVisible(False)
        # The bloom is the running state and it is off at rest: the status line above
        # says the same thing in words for anyone who cannot read the colour.
        self._bar_glow = accent_glow(self._progress_bar, theme.ACCENT_AMBER)
        self._bar_glow.setEnabled(False)
        panel.content_layout.addWidget(self._progress_bar)

        self._log = QPlainTextEdit()
        self._log.setObjectName("LogView")
        self._log.setReadOnly(True)
        # Click to select, never a tab stop: read-only output wearing the focus ring puts
        # the brightest edge in the dialog around its emptiest panel, and pushes it off
        # Start, which is the control the user actually came for.
        self._log.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self._log.setMaximumBlockCount(200)
        # Capped rather than left to expand: this worker writes three lines over an entire
        # run, and an empty box 300px tall is the largest thing on the panel earning the
        # least. Measured in line spacing so it holds five lines at any DPI.
        self._log.setMaximumHeight(self._log.fontMetrics().lineSpacing() * 5 + theme.SPACE * 2)
        panel.content_layout.addWidget(self._log, 1)

        return panel

    def _build_report_panel(self) -> GlassPanel:
        panel = GlassPanel("Duplicates", accent=theme.ACCENT_AMBER)

        self._stack = QStackedWidget()
        # A swap between two unrelated full-panel images is a cut; the fade gives the eye
        # something to follow, and collapses to the same instant swap under reduced motion.
        StackFader(self._stack)

        self._stack.addWidget(
            EmptyState(
                "Nothing scanned yet",
                "Start a scan and any paper stored twice under a different name is "
                "listed here.",
            )
        )
        # The result page is built per run, so it can say what actually happened rather
        # than picking between two pre-written outcomes.
        self._stack.addWidget(QWidget())
        self._stack.setCurrentIndex(self._PAGE_UNSCANNED)

        panel.content_layout.addWidget(self._stack, 1)
        return panel

    def _build_control_bar(self) -> GlassPanel:
        bar = GlassPanel(chrome=True)
        bar.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(theme.SPACE)

        self._start_btn = QPushButton("Start")
        self._start_btn.setObjectName("primary")
        self._start_btn.clicked.connect(self._start)

        # No Pause. The worker resumes from the database on its own, so Stop keeps its
        # progress: the same affordance with one fewer state to explain.
        self._stop_btn = QPushButton("Stop")
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._stop)

        # Keeps one copy per group and deletes the rest from library and disk, so it is
        # red and stays disabled until a scan has actually found something.
        self._dedupe_btn = QPushButton("Remove duplicates")
        self._dedupe_btn.setObjectName("danger")
        self._dedupe_btn.setEnabled(False)
        self._dedupe_btn.clicked.connect(self._remove_duplicates)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)

        row.addWidget(self._start_btn)
        row.addWidget(self._stop_btn)
        row.addStretch()
        row.addWidget(self._dedupe_btn)
        row.addWidget(close_btn)
        bar.content_layout.addLayout(row)
        return bar

    # ------------------------------------------------------------------
    # The run
    # ------------------------------------------------------------------

    def showEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().showEvent(event)
        self._load_existing_report()

    def _load_existing_report(self) -> None:
        """Show the duplicates already in the database. Fingerprints persist across runs, so
        the report must not depend on a scan having finished in this dialog's lifetime. The
        dialog is cached by MainWindow, so this re-reads on every show, but never while a
        scan or removal is running."""
        busy = (self._worker is not None and self._worker.isRunning()) or (
            self._dedupe_worker is not None and self._dedupe_worker.isRunning()
        )
        if busy:
            return
        covered, total = self._db.get_hash_coverage()
        if not covered:
            return
        groups = self._db.get_duplicate_hash_groups()
        self._groups = groups
        self._dedupe_btn.setEnabled(bool(groups))
        self._progress_bar.setValue(int(covered / total * 100))
        status = f"{covered:,} of {total:,} papers already fingerprinted."
        if groups:
            status += f" {_plural(len(groups), 'group', 'groups')} of identical files, listed below."
        elif covered == total:
            status += " No two of them hold the same file."
        self._status_label.setText(status)
        self._show_result(groups, covered)

    def _start(self) -> None:
        self._stop_requested = False
        self._worker = HashBackfillWorker(self._db, parent=self)
        self._worker.progress.connect(self._on_progress)
        self._worker.log_message.connect(self._log.appendPlainText)
        self._worker.finished_all.connect(self._on_finished)
        self._worker.start()

        self._start_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._bar_glow.setEnabled(True)
        self._progress_bar.setValue(0)
        self._status_label.setText("Reading the library…")

    def _stop(self) -> None:
        if self._worker is not None:
            self._worker.request_stop()
        self._stop_requested = True
        self._stop_btn.setEnabled(False)
        self._bar_glow.setEnabled(False)
        self._status_label.setText("Stopping…")

    @pyqtSlot(int, int, int)
    def _on_progress(self, done: int, total: int, unreadable: int) -> None:
        pct = int(done / total * 100) if total > 0 else 0
        self._progress_bar.setValue(pct)
        text = f"{done:,} of {total:,} fingerprinted"
        if unreadable:
            text += f"   ·   {unreadable:,} unreadable"
        self._status_label.setText(text)

    @pyqtSlot(int, int)
    def _on_finished(self, hashed: int, unreadable: int) -> None:
        self._start_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._bar_glow.setEnabled(False)

        groups = self._db.get_duplicate_hash_groups()
        self._groups = groups
        self._dedupe_btn.setEnabled(bool(groups))
        covered, total = self._db.get_hash_coverage()
        # The bar settles on what the library actually carries, which is the same number
        # the status line states. Filling it to 100% on a run where every file was
        # unreadable would put "finished and fine" in the loudest channel on the panel
        # while the words underneath said the opposite.
        self._progress_bar.setValue(int(covered / total * 100) if total else 0)
        self._status_label.setText(
            self._summary(covered, total, len(groups), unreadable)
        )
        self._show_result(groups, covered)

    def _summary(self, covered: int, total: int, groups: int, unreadable: int) -> str:
        """The run's outcome in one line: coverage, then what it found, then what it could
        not read. Coverage comes first because a partial scan's findings are partial."""
        if self._stop_requested:
            parts = [
                "Stopped. Every fingerprint written so far is kept, and starting again "
                "resumes from here."
            ]
        elif total == 0:
            parts = ["Done. The library is empty."]
        elif covered == total:
            parts = [f"Done. All {total:,} papers fingerprinted."]
        else:
            parts = [f"Done. {covered:,} of {total:,} papers fingerprinted."]

        if groups:
            parts.append(f"{_plural(groups, 'group', 'groups')} of identical files, listed below.")
        elif covered:
            parts.append("No two of them hold the same file.")
        if unreadable:
            parts.append(f"{_plural(unreadable, 'file', 'files')} could not be read.")
        return " ".join(parts)

    # ------------------------------------------------------------------
    # Removing duplicates
    # ------------------------------------------------------------------

    def _remove_duplicates(self) -> None:
        extra = sum(len(ids) - 1 for _digest, ids in self._groups)
        reply = QMessageBox.warning(
            self,
            "Remove duplicates",
            f"Keep one copy in each of {_plural(len(self._groups), 'group', 'groups')} and "
            f"permanently delete the other {extra:,} from the library and from disk?\n\n"
            "Tags and collections of the removed copies are merged onto the one kept. "
            "This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self._dedupe_btn.setEnabled(False)
        self._start_btn.setEnabled(False)
        self._bar_glow.setEnabled(True)
        self._progress_bar.setValue(0)
        self._status_label.setText("Removing duplicates…")
        self._dedupe_worker = DedupeWorker(self._db, self._indexer, self._groups, parent=self)
        self._dedupe_worker.progress.connect(self._on_dedupe_progress)
        self._dedupe_worker.finished_all.connect(self._on_dedupe_finished)
        self._dedupe_worker.start()

    @pyqtSlot(int, int)
    def _on_dedupe_progress(self, done: int, total: int) -> None:
        self._progress_bar.setValue(int(done / total * 100) if total else 0)
        self._status_label.setText(f"Removed duplicates in {done:,} of {total:,} groups")

    @pyqtSlot(int, int, int)
    def _on_dedupe_finished(self, removed: int, deleted: int, skipped: int) -> None:
        self._start_btn.setEnabled(True)
        self._bar_glow.setEnabled(False)
        text = f"Done. {_plural(removed, 'duplicate', 'duplicates')} removed from the library"
        if deleted != removed:
            text += f", {removed - deleted:,} of them with the file left on disk"
        text += "."
        if skipped:
            text += (
                f" {_plural(skipped, 'copy', 'copies')} left alone because the file no "
                "longer matched its fingerprint."
            )
        groups = self._db.get_duplicate_hash_groups()
        self._groups = groups
        self._dedupe_btn.setEnabled(bool(groups))
        covered, _total = self._db.get_hash_coverage()
        self._progress_bar.setValue(100)
        self._status_label.setText(text)
        self._show_result(groups, covered)

    # ------------------------------------------------------------------
    # The report
    # ------------------------------------------------------------------

    def _show_result(self, groups: list[tuple[str, list[int]]], covered: int) -> None:
        """Swap in a freshly built result page and fade to it."""
        if groups:
            page: QWidget = self._build_groups_page(groups)
        elif covered:
            page = self._empty_state(
                "No duplicates",
                "Every fingerprinted paper in the library holds a file of its own.",
                theme.ACCENT_LIME,
            )
        else:
            # Nothing was hashed, so nothing was compared. Saying "no duplicates" here is
            # the exact false comfort this feature exists to remove.
            page = self._empty_state(
                "Nothing to compare",
                "No file could be read, so no fingerprints were recorded and no two "
                "papers were compared.",
                theme.ACCENT_AMBER,
            )

        old = self._stack.widget(self._PAGE_RESULT)
        if old is not None:
            self._stack.removeWidget(old)
            old.deleteLater()
        self._stack.insertWidget(self._PAGE_RESULT, page)
        self._stack.setCurrentIndex(self._PAGE_RESULT)

    def _empty_state(self, heading: str, body: str, hue: str) -> EmptyState:
        state = EmptyState(heading, body)
        state.heading_label.setStyleSheet(
            f"color: {hue}; background: transparent; "
            f"font-size: {_EMPTY_HEADING_PT}pt; font-weight: bold; padding-bottom: 7px;"
        )
        return state

    def _build_groups_page(self, groups: list[tuple[str, list[int]]]) -> QWidget:
        shown = groups[:MAX_GROUPS_SHOWN]
        ids = [paper_id for _digest, group in shown for paper_id in group]
        papers = {paper.id: paper for paper in self._db.get_papers_by_ids(ids)}

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(theme.SPACE)

        caption = QLabel(
            "Each group lists every copy found, with its full path. Remove duplicates "
            "keeps one copy per group (reviewed metadata first, then the most reliable "
            "source, then a filed copy over one in Unsorted, then the oldest) and deletes "
            "the rest from the library and from disk."
        )
        caption.setObjectName("FieldNote")
        caption.setWordWrap(True)
        layout.addWidget(caption)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.viewport().setAutoFillBackground(False)

        host = QWidget()
        host_layout = QVBoxLayout(host)
        host_layout.setContentsMargins(0, 0, 0, 0)
        host_layout.setSpacing(theme.SPACE * 2)
        for number, (_digest, group) in enumerate(shown, start=1):
            host_layout.addWidget(self._group_block(number, group, papers))
        host_layout.addStretch(1)
        scroll.setWidget(host)
        layout.addWidget(scroll, 1)

        if len(groups) > len(shown):
            more = QLabel(
                f"Showing the first {len(shown):,} groups of {len(groups):,}. "
                f"Clear some of these and run the scan again to see the rest."
            )
            more.setObjectName("FieldNote")
            more.setWordWrap(True)
            layout.addWidget(more)

        return page

    def _group_block(
        self, number: int, group: list[int], papers: dict[Optional[int], Paper]
    ) -> QWidget:
        """One group of identical files: an amber edge, a heading, and one row per copy.

        The amber left edge is the vocabulary needs-review already uses on a field whose
        value is in doubt, borrowed here for a set of files that are in doubt.
        """
        block = QFrame()
        block.setObjectName("DuplicateGroup")
        block.setStyleSheet(
            f"QFrame#DuplicateGroup {{ border: none; "
            f"border-left: 2px solid {theme.ACCENT_AMBER}; }}"
        )
        layout = QVBoxLayout(block)
        layout.setContentsMargins(theme.SPACE * 2, 0, 0, 0)
        layout.setSpacing(theme.SPACE)

        heading = QLabel(f"Group {number}   ·   {_plural(len(group), 'copy', 'copies')}")
        heading.setStyleSheet(
            f"color: {theme.ACCENT_AMBER}; background: transparent; font-weight: bold;"
        )
        layout.addWidget(heading)

        for paper_id in group:
            layout.addLayout(self._paper_row(paper_id, papers.get(paper_id)))

        return block

    def _paper_row(self, paper_id: int, paper: Optional[Paper]) -> QVBoxLayout:
        row = QVBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(2)

        if paper is None:
            missing = QLabel(f"#{paper_id}   ·   no longer in the library")
            missing.setObjectName("FieldNote")
            row.addWidget(missing)
            return row

        title = QLabel(paper.title or "Untitled")
        title.setStyleSheet("background: transparent; font-weight: bold;")
        title.setWordWrap(True)
        row.addWidget(title)

        year = str(paper.year) if paper.year else "No year"
        meta = QLabel(f"#{paper_id}   ·   {year}   ·   {_first_author(paper)}")
        meta.setObjectName("FieldNote")
        row.addWidget(meta)

        # A read-only line edit rather than a label: the path is the field that decides
        # which copy to keep, so it has to be selectable and must never be elided. This
        # one scrolls sideways instead of losing its tail.
        path = QLineEdit(paper.file_path)
        path.setReadOnly(True)
        # Scrolled to the end, not the start. Every copy in a group shares its leading
        # folders by definition, so the head of the path is the half that cannot tell them
        # apart; the filename can, and dragging left still reaches the root.
        path.setCursorPosition(len(paper.file_path))
        row.addWidget(path)

        return row
