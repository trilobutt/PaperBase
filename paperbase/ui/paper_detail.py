import json
import os
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QPlainTextEdit, QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget,
)

from paperbase.core.db import Database
from paperbase.models.paper import Paper


class TagChip(QPushButton):
    removed = pyqtSignal(str)

    def __init__(self, tag: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(f"  {tag}  ✕", parent)
        self._tag = tag
        self.setFlat(True)
        self.setStyleSheet(
            "QPushButton { background: #e0e0e0; border-radius: 10px; padding: 2px 6px;"
            " font-size: 12px; } QPushButton:hover { background: #c0c0c0; }"
        )
        self.clicked.connect(lambda: self.removed.emit(self._tag))


class PaperDetail(QWidget):
    paper_changed = pyqtSignal(int)   # paper_id changed — tells results list to re-fetch

    def __init__(self, db: Database, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._db = db
        self._paper: Optional[Paper] = None
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner = QWidget()
        self._form_layout = QVBoxLayout(inner)
        scroll.setWidget(inner)
        outer.addWidget(scroll)

        # Needs-review badge
        self._review_badge = QLabel("⚠ Needs Review")
        self._review_badge.setStyleSheet(
            "background: #ffcc00; color: #333; font-weight: bold; padding: 4px 8px;"
            " border-radius: 4px;"
        )
        self._review_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._review_badge.hide()
        self._form_layout.addWidget(self._review_badge)

        # Dismiss review button
        self._dismiss_btn = QPushButton("Mark as Reviewed")
        self._dismiss_btn.hide()
        self._dismiss_btn.clicked.connect(self._dismiss_review)
        self._form_layout.addWidget(self._dismiss_btn)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._title_edit = QLineEdit()
        self._title_edit.editingFinished.connect(lambda: self._save_field("title", self._title_edit.text()))
        form.addRow("Title:", self._title_edit)

        self._authors_edit = QLineEdit()
        self._authors_edit.setPlaceholderText("Lastname, Firstname; Lastname2, Firstname2")
        self._authors_edit.editingFinished.connect(self._save_authors)
        form.addRow("Authors:", self._authors_edit)

        self._journal_edit = QLineEdit()
        self._journal_edit.editingFinished.connect(lambda: self._save_field("journal", self._journal_edit.text()))
        form.addRow("Journal:", self._journal_edit)

        self._year_spin = QSpinBox()
        self._year_spin.setRange(0, 2100)
        self._year_spin.setSpecialValueText("Unknown")
        self._year_spin.editingFinished.connect(lambda: self._save_field(
            "year", self._year_spin.value() if self._year_spin.value() > 0 else None
        ))
        form.addRow("Year:", self._year_spin)

        self._doi_edit = QLineEdit()
        self._doi_edit.editingFinished.connect(lambda: self._save_field("doi", self._doi_edit.text() or None))
        form.addRow("DOI:", self._doi_edit)

        self._volume_edit = QLineEdit()
        self._volume_edit.editingFinished.connect(lambda: self._save_field("volume", self._volume_edit.text()))
        form.addRow("Volume:", self._volume_edit)

        self._issue_edit = QLineEdit()
        self._issue_edit.editingFinished.connect(lambda: self._save_field("issue", self._issue_edit.text()))
        form.addRow("Issue:", self._issue_edit)

        self._pages_edit = QLineEdit()
        self._pages_edit.editingFinished.connect(lambda: self._save_field("pages", self._pages_edit.text()))
        form.addRow("Pages:", self._pages_edit)

        self._abstract_edit = QPlainTextEdit()
        self._abstract_edit.setMaximumHeight(120)
        self._abstract_edit.focusOutEvent = self._abstract_focus_out  # type: ignore[method-assign]
        form.addRow("Abstract:", self._abstract_edit)

        self._form_layout.addLayout(form)

        # Tags section
        tags_label = QLabel("Tags:")
        tags_label.setStyleSheet("font-weight: bold; margin-top: 8px;")
        self._form_layout.addWidget(tags_label)

        self._tags_container = QWidget()
        # Use a wrapping HBoxLayout; chips are added dynamically
        self._tags_flow = QHBoxLayout(self._tags_container)
        self._tags_flow.setContentsMargins(0, 0, 0, 0)
        self._tags_flow.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._form_layout.addWidget(self._tags_container)

        add_tag_row = QHBoxLayout()
        self._tag_input = QLineEdit()
        self._tag_input.setPlaceholderText("Add tag…")
        self._tag_input.returnPressed.connect(self._add_tag)
        add_tag_btn = QPushButton("+")
        add_tag_btn.setFixedWidth(30)
        add_tag_btn.clicked.connect(self._add_tag)
        add_tag_row.addWidget(self._tag_input)
        add_tag_row.addWidget(add_tag_btn)
        self._form_layout.addLayout(add_tag_row)

        # Open PDF button
        self._open_btn = QPushButton("Open PDF")
        self._open_btn.clicked.connect(self._open_pdf)
        self._form_layout.addWidget(self._open_btn)
        self._form_layout.addStretch()

        self._set_enabled(False)

    def _abstract_focus_out(self, event) -> None:
        self._save_field("abstract", self._abstract_edit.toPlainText())
        QPlainTextEdit.focusOutEvent(self._abstract_edit, event)

    def _set_enabled(self, enabled: bool) -> None:
        for w in (self._title_edit, self._authors_edit, self._journal_edit,
                  self._year_spin, self._doi_edit, self._volume_edit,
                  self._issue_edit, self._pages_edit, self._abstract_edit,
                  self._tag_input, self._open_btn):
            w.setEnabled(enabled)

    def show_paper(self, paper: Paper) -> None:
        self._paper = paper
        self._set_enabled(True)

        self._title_edit.setPlaceholderText(Path(paper.file_path).name)
        self._title_edit.setText(paper.title)
        self._authors_edit.setText("; ".join(
            f"{a}" for a in paper.authors
        ))
        self._journal_edit.setText(paper.journal)
        self._year_spin.setValue(paper.year or 0)
        self._doi_edit.setText(paper.doi or "")
        self._volume_edit.setText(paper.volume)
        self._issue_edit.setText(paper.issue)
        self._pages_edit.setText(paper.pages)
        self._abstract_edit.setPlainText(paper.abstract)

        if paper.needs_review:
            self._review_badge.show()
            self._dismiss_btn.show()
        else:
            self._review_badge.hide()
            self._dismiss_btn.hide()

        self._refresh_tags()

    def clear(self) -> None:
        self._paper = None
        self._set_enabled(False)
        self._title_edit.clear()
        self._authors_edit.clear()
        self._journal_edit.clear()
        self._year_spin.setValue(0)
        self._doi_edit.clear()
        self._volume_edit.clear()
        self._issue_edit.clear()
        self._pages_edit.clear()
        self._abstract_edit.clear()
        self._review_badge.hide()
        self._dismiss_btn.hide()
        self._refresh_tags()

    def _refresh_tags(self) -> None:
        # Clear existing chips
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
        # Split by semicolon; each entry is stored as-is (family, given)
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
        self._review_badge.hide()
        self._dismiss_btn.hide()
        self.paper_changed.emit(self._paper.id)

    def _open_pdf(self) -> None:
        if self._paper and self._paper.file_path:
            os.startfile(self._paper.file_path)  # type: ignore[attr-defined]
