import asyncio
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QMimeData, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QFocusEvent
from PyQt6.QtWidgets import (
    QApplication, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QPlainTextEdit, QPushButton, QScrollArea, QSpinBox, QStackedWidget, QVBoxLayout,
    QWidget,
)

from paperbase.core.db import Database
from paperbase.models.paper import Paper
from paperbase.ui import theme
from paperbase.ui.glass import EmptyState, panel_shadow

logger = logging.getLogger(__name__)

# Three units between the form's sections against one unit inside them. That ratio is
# the whole of the grouping: nothing is hidden, and the eye still reads three blocks
# rather than eleven identical rows.
_GROUP_GAP = theme.SPACE * 3

# The narrowest a field may be compressed to. Without an explicit floor each input
# reports its own sizeHint-derived minimum, which puts the form's minimum width past
# the panel's and has the scroll area clip the fields rather than shrink them.
_FIELD_MIN = 80


class TagChip(QPushButton):
    removed = pyqtSignal(str)

    def __init__(self, tag: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(f"  {tag}  ✕", parent)
        self._tag = tag
        self.setObjectName("TagChip")
        self.setFlat(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(f"Remove the tag “{tag}”")
        self.clicked.connect(lambda: self.removed.emit(self._tag))


class PaperDetail(QWidget):
    paper_changed = pyqtSignal(int)   # paper_id changed — tells results list to re-fetch

    def __init__(self, db: Database, user_email: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._db = db
        self._user_email = user_email
        self._paper: Optional[Paper] = None
        self._build_ui()

    def set_user_email(self, email: str) -> None:
        self._user_email = email

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Two pages: the designed absence and the editor. The absence is a real surface
        # rather than the whole form greyed out, which reads as breakage.
        self._stack = QStackedWidget()
        outer.addWidget(self._stack)

        self._empty_state = EmptyState(
            "No paper selected",
            "Pick one from the list to read and edit its metadata.",
        )
        self._stack.addWidget(self._empty_state)

        self._editor = QWidget()
        editor = QVBoxLayout(self._editor)
        editor.setContentsMargins(0, 0, 0, 0)
        editor.setSpacing(theme.SPACE)
        self._stack.addWidget(self._editor)

        # ---- Needs review: a chip under the panel title, not a banner across it ----
        self._review_row = QWidget()
        review_hl = QHBoxLayout(self._review_row)
        review_hl.setContentsMargins(0, 0, 0, 0)
        review_hl.setSpacing(theme.SPACE)
        self._review_chip = QLabel("Needs review")
        self._review_chip.setObjectName("ReviewChip")
        self._review_chip.setToolTip(
            "The marked fields were guessed rather than resolved from a source"
        )
        review_hl.addWidget(self._review_chip)
        self._dismiss_btn = QPushButton("Mark as Reviewed")
        self._dismiss_btn.clicked.connect(self._dismiss_review)
        review_hl.addWidget(self._dismiss_btn)
        review_hl.addStretch(1)
        self._review_row.hide()
        editor.addWidget(self._review_row)

        # ---- The form, in three named groups ----
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        # Off, as before: the tag row is a single QHBoxLayout (Qt has no flow layout), so
        # any paper with three long tags would otherwise raise a horizontal bar across
        # the whole form at ordinary panel widths.
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        inner = QWidget()
        column = QVBoxLayout(inner)
        # A gutter on the right so the fields never run into the scrollbar.
        column.setContentsMargins(0, 0, theme.SPACE, 0)
        column.setSpacing(0)
        scroll.setWidget(inner)
        editor.addWidget(scroll, 1)

        # Identity: what the paper is.
        self._add_group_label(column, "Identity", first=True)
        identity = self._new_form(column)

        self._title_edit = QLineEdit()
        self._title_edit.editingFinished.connect(lambda: self._save_field("title", self._title_edit.text()))
        self._add_row(identity, "Title:", self._title_edit)

        self._authors_edit = QLineEdit()
        self._authors_edit.setPlaceholderText("Lastname, Firstname; Lastname2, Firstname2")
        self._authors_edit.editingFinished.connect(self._save_authors)
        self._add_row(identity, "Authors:", self._authors_edit)

        # Publication: where and when it appeared.
        self._add_group_label(column, "Publication")
        publication = self._new_form(column)

        self._journal_edit = QLineEdit()
        self._journal_edit.editingFinished.connect(lambda: self._save_field("journal", self._journal_edit.text()))
        self._add_row(publication, "Journal/Publisher:", self._journal_edit)

        self._year_spin = QSpinBox()
        self._year_spin.setRange(0, 2100)
        self._year_spin.setSpecialValueText("Unknown")
        self._year_spin.editingFinished.connect(lambda: self._save_field(
            "year", self._year_spin.value() if self._year_spin.value() > 0 else None
        ))
        self._add_row(publication, "Year:", self._year_spin)

        self._volume_edit = QLineEdit()
        self._volume_edit.editingFinished.connect(
            lambda: self._save_field("volume", self._volume_edit.text()))
        self._add_row(publication, "Volume:", self._volume_edit)

        self._issue_edit = QLineEdit()
        self._issue_edit.editingFinished.connect(
            lambda: self._save_field("issue", self._issue_edit.text()))
        self._add_row(publication, "Issue:", self._issue_edit)

        self._pages_edit = QLineEdit()
        self._pages_edit.editingFinished.connect(
            lambda: self._save_field("pages", self._pages_edit.text()))
        self._add_row(publication, "Pages:", self._pages_edit)

        # Identifiers: the two keys that can fetch everything above.
        self._add_group_label(column, "Identifiers")
        identifiers = self._new_form(column)

        doi_row = QWidget()
        doi_hl = QHBoxLayout(doi_row)
        doi_hl.setContentsMargins(0, 0, 0, 0)
        doi_hl.setSpacing(theme.SPACE)
        self._doi_edit = QLineEdit()
        self._doi_edit.editingFinished.connect(lambda: self._save_field("doi", self._doi_edit.text() or None))
        # No fixed width: at this theme's button padding a 60px box clips its own
        # label, and the QLineEdit beside it is the widget that should absorb the slack.
        self._doi_lookup_btn = QPushButton("Lookup")
        self._doi_lookup_btn.setToolTip("Fetch metadata from Crossref using this DOI")
        self._doi_lookup_btn.clicked.connect(self._lookup_by_doi)
        doi_hl.addWidget(self._doi_edit)
        doi_hl.addWidget(self._doi_lookup_btn)
        self._add_row(identifiers, "DOI:", doi_row)

        isbn_row = QWidget()
        isbn_hl = QHBoxLayout(isbn_row)
        isbn_hl.setContentsMargins(0, 0, 0, 0)
        isbn_hl.setSpacing(theme.SPACE)
        self._isbn_edit = QLineEdit()
        self._isbn_edit.setPlaceholderText("9780000000000")
        self._isbn_edit.editingFinished.connect(lambda: self._save_field("isbn", self._isbn_edit.text() or None))
        self._isbn_lookup_btn = QPushButton("Lookup")
        self._isbn_lookup_btn.setToolTip("Fetch metadata from Open Library / Google Books using this ISBN")
        self._isbn_lookup_btn.clicked.connect(self._lookup_by_isbn)
        isbn_hl.addWidget(self._isbn_edit)
        isbn_hl.addWidget(self._isbn_lookup_btn)
        self._add_row(identifiers, "ISBN:", isbn_row)

        # Abstract and tags: the two blocks that want the panel's full width.
        self._add_group_label(column, "Abstract")
        self._abstract_edit = QPlainTextEdit()
        self._abstract_edit.setMinimumHeight(140)
        self._abstract_edit.focusOutEvent = self._abstract_focus_out  # type: ignore[method-assign]
        column.addWidget(self._abstract_edit)

        self._add_group_label(column, "Tags")
        self._tags_container = QWidget()
        self._tags_flow = QHBoxLayout(self._tags_container)
        self._tags_flow.setContentsMargins(0, 0, 0, 0)
        self._tags_flow.setSpacing(theme.SPACE // 2)
        self._tags_flow.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        # A single row of chips is as wide as the tags happen to be (Qt has no flow
        # layout, per CLAUDE.md), and without a floor of its own that width becomes the
        # whole form's minimum: three ordinary tags then push every field in the panel
        # past the viewport, where the disabled horizontal scrollbar silently clips them.
        # The chips give first instead, which costs the tail of one row rather than the
        # right-hand edge of every field above it.
        self._tags_container.setMinimumWidth(1)
        column.addWidget(self._tags_container)
        column.addSpacing(theme.SPACE)

        add_tag_row = QHBoxLayout()
        add_tag_row.setSpacing(theme.SPACE)
        self._tag_input = QLineEdit()
        self._tag_input.setPlaceholderText("Add tag…")
        self._tag_input.returnPressed.connect(self._add_tag)
        add_tag_btn = QPushButton("+")
        add_tag_btn.setFixedWidth(34)
        add_tag_btn.setToolTip("Add this tag to the paper")
        add_tag_btn.clicked.connect(self._add_tag)
        add_tag_row.addWidget(self._tag_input)
        add_tag_row.addWidget(add_tag_btn)
        column.addLayout(add_tag_row)
        column.addStretch(1)

        # ---- Pinned actions: outside the scroll area, so they never scroll away ----
        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setStyleSheet(f"background-color: {theme.PANEL_BORDER};")
        editor.addWidget(divider)

        actions = QWidget()
        actions_column = QVBoxLayout(actions)
        actions_column.setContentsMargins(0, 0, 0, 0)
        actions_column.setSpacing(theme.SPACE)

        file_row = QHBoxLayout()
        file_row.setContentsMargins(0, 0, 0, 0)
        file_row.setSpacing(theme.SPACE)
        self._copy_pdf_btn = QPushButton("Copy PDF")
        self._copy_pdf_btn.setToolTip("Copy PDF file to clipboard (paste into Explorer)")
        self._copy_pdf_btn.clicked.connect(self._copy_pdf)
        self._copy_text_btn = QPushButton("Copy text")
        self._copy_text_btn.setToolTip("Copy full extracted text to clipboard")
        self._copy_text_btn.clicked.connect(self._copy_full_text)
        self._open_folder_btn = QPushButton("Open folder")
        self._open_folder_btn.setToolTip("Open containing folder with file selected")
        self._open_folder_btn.clicked.connect(self._open_folder)
        file_row.addWidget(self._copy_pdf_btn)
        file_row.addWidget(self._copy_text_btn)
        file_row.addWidget(self._open_folder_btn)
        actions_column.addLayout(file_row)

        # The panel's primary action, given the width and the lift to say so. Stacked
        # above its own row rather than squeezed in beside three secondary buttons,
        # which at this panel's width would leave all four equally cramped.
        self._open_btn = QPushButton("Open PDF")
        self._open_btn.setObjectName("primary")
        self._open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._open_btn.clicked.connect(self._open_pdf)
        panel_shadow(self._open_btn, blur=18, dy=4)
        actions_column.addWidget(self._open_btn)
        editor.addWidget(actions)

        for field in (self._title_edit, self._authors_edit, self._journal_edit,
                      self._year_spin, self._doi_edit, self._isbn_edit,
                      self._volume_edit, self._issue_edit, self._pages_edit,
                      self._tag_input):
            field.setMinimumWidth(_FIELD_MIN)

        # The fields a needs-review flag actually implicates. Marking every field would
        # carry no information; these four are what the guessing pipeline fills in.
        self._review_marked: tuple[QWidget, ...] = (
            self._title_edit, self._authors_edit, self._journal_edit, self._year_spin,
        )

        self._stack.setCurrentWidget(self._empty_state)
        self._set_enabled(False)

    def _add_group_label(self, column: QVBoxLayout, title: str, *, first: bool = False) -> None:
        """Lead a section with a bold label, three units clear of the section above it."""
        if not first:
            column.addSpacing(_GROUP_GAP)
        label = QLabel(title)
        label.setObjectName("FieldGroupLabel")
        column.addWidget(label)
        column.addSpacing(theme.SPACE)

    def _new_form(self, column: QVBoxLayout) -> QFormLayout:
        """A section's field grid: labels on the same left rail as the section heading."""
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setHorizontalSpacing(theme.SPACE)
        form.setVerticalSpacing(theme.SPACE)
        column.addLayout(form)
        return form

    def _add_row(self, form: QFormLayout, text: str, field: QWidget) -> None:
        """A labelled field. The label is one step back from the value it names: the
        data is the brightest thing in the panel, and the chrome around it is not."""
        label = QLabel(text)
        label.setObjectName("FieldLabel")
        form.addRow(label, field)

    def _set_review_marks(self, marked: bool) -> None:
        """Amber left edge on the implicated fields; Qt re-reads the property on polish."""
        for widget in self._review_marked:
            if widget.property("review") == marked:
                continue
            widget.setProperty("review", marked)
            widget.style().unpolish(widget)
            widget.style().polish(widget)

    def _abstract_focus_out(self, event: QFocusEvent) -> None:
        self._save_field("abstract", self._abstract_edit.toPlainText())
        QPlainTextEdit.focusOutEvent(self._abstract_edit, event)

    def _set_enabled(self, enabled: bool) -> None:
        for w in (self._title_edit, self._authors_edit, self._journal_edit,
                  self._year_spin, self._doi_edit, self._doi_lookup_btn,
                  self._isbn_edit, self._isbn_lookup_btn,
                  self._volume_edit, self._issue_edit, self._pages_edit,
                  self._abstract_edit, self._tag_input, self._open_btn,
                  self._copy_pdf_btn, self._copy_text_btn, self._open_folder_btn):
            w.setEnabled(enabled)

    def show_paper(self, paper: Paper) -> None:
        self._paper = paper
        self._stack.setCurrentWidget(self._editor)
        self._set_enabled(True)

        self._title_edit.setPlaceholderText(Path(paper.file_path).name)
        self._title_edit.setText(paper.title)
        self._authors_edit.setText("; ".join(paper.authors))
        self._journal_edit.setText(paper.journal)
        self._year_spin.setValue(paper.year or 0)
        self._doi_edit.setText(paper.doi or "")
        self._isbn_edit.setText(paper.isbn or "")
        self._volume_edit.setText(paper.volume)
        self._issue_edit.setText(paper.issue)
        self._pages_edit.setText(paper.pages)
        self._abstract_edit.setPlainText(paper.abstract)

        # setText leaves the cursor at the end, so a title or journal longer than its box
        # opens scrolled to its last few words. The beginning is the part that identifies
        # the paper, and it is the part that has to be on screen without being asked for.
        for field in (self._title_edit, self._authors_edit, self._journal_edit,
                      self._doi_edit, self._isbn_edit, self._volume_edit,
                      self._issue_edit, self._pages_edit):
            field.setCursorPosition(0)

        self._review_row.setVisible(paper.needs_review)
        self._set_review_marks(paper.needs_review)

        self._refresh_tags()

    def clear(self) -> None:
        self._paper = None
        self._stack.setCurrentWidget(self._empty_state)
        self._set_enabled(False)
        self._title_edit.clear()
        self._authors_edit.clear()
        self._journal_edit.clear()
        self._year_spin.setValue(0)
        self._doi_edit.clear()
        self._isbn_edit.clear()
        self._volume_edit.clear()
        self._issue_edit.clear()
        self._pages_edit.clear()
        self._abstract_edit.clear()
        self._review_row.hide()
        self._set_review_marks(False)
        self._refresh_tags()

    def _refresh_tags(self) -> None:
        while self._tags_flow.count():
            item = self._tags_flow.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not self._paper:
            return

        for tag in self._paper.tags:
            chip = TagChip(tag, self._tags_container)
            chip.removed.connect(self._remove_tag)
            self._tags_flow.addWidget(chip)

    def _save_field(self, field: str, value: object) -> None:
        if not self._paper or self._paper.id is None:
            return
        self._db.update_paper_field(self._paper.id, field, value)
        setattr(self._paper, field, value)

    def _save_authors(self) -> None:
        if not self._paper or self._paper.id is None:
            return
        raw = self._authors_edit.text()
        authors = [a.strip() for a in raw.split(";") if a.strip()]
        self._db.update_paper_field(self._paper.id, "authors", json.dumps(authors))
        self._paper.authors = authors

    def _add_tag(self) -> None:
        if not self._paper or self._paper.id is None:
            return
        tag = self._tag_input.text().strip()
        if not tag or tag in self._paper.tags:
            return
        self._paper.tags.append(tag)
        self._db.update_paper_field(self._paper.id, "tags", json.dumps(self._paper.tags))
        self._tag_input.clear()
        self._refresh_tags()
        self.paper_changed.emit(self._paper.id)

    def _remove_tag(self, tag: str) -> None:
        if not self._paper or self._paper.id is None:
            return
        if tag in self._paper.tags:
            self._paper.tags.remove(tag)
            self._db.update_paper_field(self._paper.id, "tags", json.dumps(self._paper.tags))
            self._refresh_tags()
            self.paper_changed.emit(self._paper.id)

    def _dismiss_review(self) -> None:
        if not self._paper or self._paper.id is None:
            return
        self._db.update_paper_field(self._paper.id, "needs_review", 0)
        self._paper.needs_review = False
        self._review_row.hide()
        self._set_review_marks(False)
        self.paper_changed.emit(self._paper.id)

    def _open_pdf(self) -> None:
        if self._paper and self._paper.file_path:
            os.startfile(self._paper.file_path)  # type: ignore[attr-defined]

    def _copy_pdf(self) -> None:
        if not self._paper or not self._paper.file_path:
            return
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(self._paper.file_path)])
        QApplication.clipboard().setMimeData(mime)

    def _copy_full_text(self) -> None:
        if not self._paper or not self._paper.file_path:
            return
        try:
            import fitz
            with fitz.open(self._paper.file_path) as doc:
                text = "\n".join(page.get_text() for page in doc)
            QApplication.clipboard().setText(text)
        except Exception as e:
            logger.error("Failed to extract full text from %s: %s", self._paper.file_path, e)

    def _open_folder(self) -> None:
        if not self._paper or not self._paper.file_path:
            return
        subprocess.Popen(["explorer", "/select," + self._paper.file_path])

    # ------------------------------------------------------------------
    # Metadata lookup
    # ------------------------------------------------------------------

    def _lookup_by_doi(self) -> None:
        doi = self._doi_edit.text().strip()
        if not doi or not self._paper:
            return
        # Save the DOI first so it's in the DB even if lookup partially fails
        self._save_field("doi", doi)
        asyncio.ensure_future(self._do_doi_lookup(doi))

    def _lookup_by_isbn(self) -> None:
        isbn = self._isbn_edit.text().strip()
        if not isbn or not self._paper:
            return
        self._save_field("isbn", isbn)
        asyncio.ensure_future(self._do_isbn_lookup(isbn))

    def _set_lookup_busy(self, busy: bool) -> None:
        self._doi_lookup_btn.setEnabled(not busy)
        self._isbn_lookup_btn.setEnabled(not busy)
        if busy:
            self._doi_lookup_btn.setText("…")
            self._isbn_lookup_btn.setText("…")
        else:
            self._doi_lookup_btn.setText("Lookup")
            self._isbn_lookup_btn.setText("Lookup")

    async def _do_doi_lookup(self, doi: str) -> None:
        import httpx

        from paperbase.core.metadata import RateLimiter, resolve_metadata
        from paperbase.core.scraper import scrape_landing_page
        self._set_lookup_busy(True)
        try:
            rl = RateLimiter()
            async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
                paper = await resolve_metadata(doi, self._user_email, rl, client)
                if paper is None:
                    logger.warning("DOI lookup returned no result for %s", doi)
                    return
                # Crossref often omits abstracts even when the publisher page has one.
                # Fall back to scraping the DOI landing page for the abstract.
                if not paper.abstract:
                    try:
                        scrape = await scrape_landing_page(f"https://doi.org/{doi}", client)
                        if scrape.metadata and scrape.metadata.abstract:
                            paper.abstract = scrape.metadata.abstract
                    except Exception as scrape_err:
                        logger.debug("Abstract scrape fallback failed for %s: %s", doi, scrape_err)
                self._apply_lookup_result(paper)
        except Exception as e:
            logger.error("DOI lookup failed: %s", e)
        finally:
            self._set_lookup_busy(False)

    async def _do_isbn_lookup(self, isbn: str) -> None:
        import httpx

        from paperbase.core.metadata import RateLimiter, resolve_book_metadata
        self._set_lookup_busy(True)
        try:
            rl = RateLimiter()
            async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
                paper = await resolve_book_metadata(isbn, rl, client)
                if paper is None:
                    logger.warning("ISBN lookup returned no result for %s", isbn)
                    return
                self._apply_lookup_result(paper)
        except Exception as e:
            logger.error("ISBN lookup failed: %s", e)
        finally:
            self._set_lookup_busy(False)

    def _apply_lookup_result(self, fetched: Paper) -> None:
        """Write all non-empty fields from fetched Paper into the current paper and DB."""
        if not self._paper or self._paper.id is None:
            return

        updates: dict[str, object] = {}

        if fetched.title:
            updates["title"] = fetched.title
        if fetched.authors:
            updates["authors"] = json.dumps(fetched.authors)
        if fetched.journal:
            updates["journal"] = fetched.journal
        if fetched.year:
            updates["year"] = fetched.year
        if fetched.doi:
            updates["doi"] = fetched.doi
        if fetched.isbn:
            updates["isbn"] = fetched.isbn
        if fetched.volume:
            updates["volume"] = fetched.volume
        if fetched.issue:
            updates["issue"] = fetched.issue
        if fetched.pages:
            updates["pages"] = fetched.pages
        if fetched.abstract:
            updates["abstract"] = fetched.abstract
        if fetched.keywords:
            updates["keywords"] = json.dumps(fetched.keywords)
        if fetched.document_type and fetched.document_type != "article":
            updates["document_type"] = fetched.document_type
        updates["metadata_source"] = fetched.metadata_source
        updates["needs_review"] = 0

        for field, value in updates.items():
            self._db.update_paper_field(self._paper.id, field, value)
            # Keep in-memory paper in sync
            if field == "authors":
                self._paper.authors = fetched.authors
            elif field == "keywords":
                self._paper.keywords = fetched.keywords
            else:
                setattr(self._paper, field, value)

        # Refresh UI from the updated in-memory paper
        self.show_paper(self._paper)
        self.paper_changed.emit(self._paper.id)
