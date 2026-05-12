import json
from pathlib import Path
from typing import Optional

from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QPushButton, QScrollArea, QSpinBox, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

SETTINGS_VERSION = 2


class Settings:
    def __init__(self) -> None:
        self.library_root: str = ""
        self.user_email: str = ""
        self.folder_pattern: str = "{journal}/{year}/{author} ({year}) {title}.pdf"
        self.last_import_dir: str = ""
        self.secondary_dest: str = ""
        # Auto-categorisation
        self.categories: list[dict] = []        # [{"name": "...", "description": "..."}]
        self.auto_categorise: bool = True       # run categoriser on each new import
        self.category_threshold: float = 0.35  # min cosine similarity to assign a category
        self.tag_count: int = 5                # keywords to extract per paper

    def is_configured(self) -> bool:
        return bool(self.library_root and self.user_email)

    def save(self, path: Path) -> None:
        data = {
            "version": SETTINGS_VERSION,
            "library_root": self.library_root,
            "user_email": self.user_email,
            "folder_pattern": self.folder_pattern,
            "last_import_dir": self.last_import_dir,
            "secondary_dest": self.secondary_dest,
            "categories": self.categories,
            "auto_categorise": self.auto_categorise,
            "category_threshold": self.category_threshold,
            "tag_count": self.tag_count,
        }
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "Settings":
        s = cls()
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                s.library_root = data.get("library_root", "")
                s.user_email = data.get("user_email", "")
                s.folder_pattern = data.get("folder_pattern", s.folder_pattern)
                s.last_import_dir = data.get("last_import_dir", "")
                s.secondary_dest = data.get("secondary_dest", "")
                s.categories = data.get("categories", [])
                s.auto_categorise = data.get("auto_categorise", True)
                s.category_threshold = float(data.get("category_threshold", 0.35))
                s.tag_count = int(data.get("tag_count", 5))
            except Exception:
                pass
        return s


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(560)
        self._settings = settings
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)

        # Wrap everything in a scroll area so the dialog stays usable at low resolutions.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(scroll.Shape.NoFrame)
        inner = QWidget()
        layout = QVBoxLayout(inner)
        scroll.setWidget(inner)
        outer.addWidget(scroll)

        # ---- General settings ----
        general_box = QGroupBox("General")
        form = QFormLayout(general_box)

        root_row = QWidget()
        root_hl = QHBoxLayout(root_row)
        root_hl.setContentsMargins(0, 0, 0, 0)
        self._root_edit = QLineEdit(self._settings.library_root)
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse_root)
        root_hl.addWidget(self._root_edit)
        root_hl.addWidget(browse_btn)
        form.addRow("Library root:", root_row)

        self._email_edit = QLineEdit(self._settings.user_email)
        self._email_edit.setPlaceholderText("your@email.com")
        form.addRow("Email (Crossref/Unpaywall):", self._email_edit)

        self._pattern_edit = QLineEdit(self._settings.folder_pattern)
        form.addRow("Folder pattern:", self._pattern_edit)

        sec_row = QWidget()
        sec_hl = QHBoxLayout(sec_row)
        sec_hl.setContentsMargins(0, 0, 0, 0)
        self._secondary_dest_edit = QLineEdit(self._settings.secondary_dest)
        self._secondary_dest_edit.setPlaceholderText("Leave blank to disable")
        sec_browse_btn = QPushButton("Browse…")
        sec_browse_btn.clicked.connect(self._browse_secondary_dest)
        sec_clear_btn = QPushButton("Clear")
        sec_clear_btn.clicked.connect(self._secondary_dest_edit.clear)
        sec_hl.addWidget(self._secondary_dest_edit)
        sec_hl.addWidget(sec_browse_btn)
        sec_hl.addWidget(sec_clear_btn)
        form.addRow("Secondary copy destination:", sec_row)

        pattern_note = QLabel(
            "Pattern tokens: {journal} {year} {author} {title}\n"
            "Unresolved tokens are replaced with 'Unknown' or 'Unsorted'."
        )
        pattern_note.setStyleSheet("color: #706860; font-size: 8pt;")
        form.addRow("", pattern_note)

        layout.addWidget(general_box)

        # ---- Auto-categorisation settings ----
        cat_box = QGroupBox("Auto-Categorisation")
        cat_layout = QVBoxLayout(cat_box)

        cat_form = QFormLayout()

        self._auto_cat_check = QCheckBox("Run on each new import")
        self._auto_cat_check.setChecked(self._settings.auto_categorise)
        cat_form.addRow("Auto-categorise:", self._auto_cat_check)

        self._threshold_spin = QDoubleSpinBox()
        self._threshold_spin.setRange(0.1, 0.9)
        self._threshold_spin.setSingleStep(0.05)
        self._threshold_spin.setDecimals(2)
        self._threshold_spin.setValue(self._settings.category_threshold)
        self._threshold_spin.setToolTip(
            "Minimum cosine similarity (0–1) required to assign a paper to a category.\n"
            "Lower values assign more liberally; higher values are more conservative.\n"
            "0.35 is a reasonable default for academic abstracts."
        )
        cat_form.addRow("Assignment threshold:", self._threshold_spin)

        self._tag_count_spin = QSpinBox()
        self._tag_count_spin.setRange(1, 20)
        self._tag_count_spin.setValue(self._settings.tag_count)
        cat_form.addRow("Keywords per paper:", self._tag_count_spin)

        cat_layout.addLayout(cat_form)

        # Category table
        cat_label = QLabel(
            "Categories — each becomes a top-level collection. The description is used "
            "to calibrate the embedding; richer descriptions improve accuracy."
        )
        cat_label.setWordWrap(True)
        cat_label.setStyleSheet("color: #706860; font-size: 8pt;")
        cat_layout.addWidget(cat_label)

        self._cat_table = QTableWidget(0, 2)
        self._cat_table.setHorizontalHeaderLabels(["Name", "Description (optional)"])
        self._cat_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self._cat_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self._cat_table.setMinimumHeight(160)
        self._cat_table.verticalHeader().setVisible(False)

        for cat in self._settings.categories:
            self._append_category_row(cat.get("name", ""), cat.get("description", ""))

        cat_layout.addWidget(self._cat_table)

        tbl_btns = QWidget()
        tbl_btn_hl = QHBoxLayout(tbl_btns)
        tbl_btn_hl.setContentsMargins(0, 0, 0, 0)
        add_btn = QPushButton("Add category")
        add_btn.clicked.connect(self._add_category_row)
        remove_btn = QPushButton("Remove selected")
        remove_btn.clicked.connect(self._remove_selected_category)
        tbl_btn_hl.addWidget(add_btn)
        tbl_btn_hl.addWidget(remove_btn)
        tbl_btn_hl.addStretch()
        cat_layout.addWidget(tbl_btns)

        layout.addWidget(cat_box)

        # ---- Dialog buttons ----
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    # ------------------------------------------------------------------
    # Category table helpers
    # ------------------------------------------------------------------

    def _append_category_row(self, name: str = "", description: str = "") -> None:
        row = self._cat_table.rowCount()
        self._cat_table.insertRow(row)
        self._cat_table.setItem(row, 0, QTableWidgetItem(name))
        self._cat_table.setItem(row, 1, QTableWidgetItem(description))

    def _add_category_row(self) -> None:
        self._append_category_row()
        self._cat_table.editItem(self._cat_table.item(self._cat_table.rowCount() - 1, 0))

    def _remove_selected_category(self) -> None:
        rows = sorted(
            {idx.row() for idx in self._cat_table.selectedIndexes()}, reverse=True
        )
        for row in rows:
            self._cat_table.removeRow(row)

    # ------------------------------------------------------------------
    # Browse helpers
    # ------------------------------------------------------------------

    def _browse_root(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Select Library Root", self._root_edit.text() or str(Path.home())
        )
        if path:
            self._root_edit.setText(path)

    def _browse_secondary_dest(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Select Secondary Copy Destination",
            self._secondary_dest_edit.text() or str(Path.home()),
        )
        if path:
            self._secondary_dest_edit.setText(path)

    # ------------------------------------------------------------------

    def _accept(self) -> None:
        self._settings.library_root = self._root_edit.text().strip()
        self._settings.user_email = self._email_edit.text().strip()
        self._settings.folder_pattern = self._pattern_edit.text().strip()
        self._settings.secondary_dest = self._secondary_dest_edit.text().strip()
        self._settings.auto_categorise = self._auto_cat_check.isChecked()
        self._settings.category_threshold = self._threshold_spin.value()
        self._settings.tag_count = self._tag_count_spin.value()

        categories: list[dict] = []
        for row in range(self._cat_table.rowCount()):
            name_item = self._cat_table.item(row, 0)
            desc_item = self._cat_table.item(row, 1)
            name = (name_item.text().strip() if name_item else "")
            if not name:
                continue
            desc = (desc_item.text().strip() if desc_item else "")
            categories.append({"name": name, "description": desc})
        self._settings.categories = categories

        self.accept()
