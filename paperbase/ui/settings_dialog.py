import json
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QPushButton, QScrollArea, QSizePolicy, QSpinBox, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from paperbase.core.db import Database
from paperbase.ui import theme
from paperbase.ui.glass import CanvasBackdrop, GlassPanel

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
        # Appearance
        self.reduce_motion: bool = False       # collapse every animation to an instant swap

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
            "reduce_motion": self.reduce_motion,
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
                s.reduce_motion = bool(data.get("reduce_motion", False))
            except Exception:
                pass
        return s


class SettingsDialog(QDialog):
    # Asked for by the Library maintenance group, answered by MainWindow: the scan
    # dialog outlives this modal one, so this dialog cannot be the thing that owns it.
    backfill_requested = pyqtSignal()

    def __init__(
        self, settings: Settings, db: Database, parent: Optional[QWidget] = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(560)
        self._settings = settings
        self._db = db
        self._build_ui()

    def _build_ui(self) -> None:
        """Glass groups floating on the canvas, the same measure as the main window.

        The 20px margin and 20px gap are what the window uses between its own panels, so
        a dialog opening over it reads as another island on the same field rather than as
        a box pasted on top.
        """
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        backdrop = CanvasBackdrop(self)
        root.addWidget(backdrop)

        outer = QVBoxLayout(backdrop)
        outer.setContentsMargins(20, 20, 20, 20)
        outer.setSpacing(20)

        # Wrap everything in a scroll area so the dialog stays usable at low resolutions.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(scroll.Shape.NoFrame)
        scroll.viewport().setAutoFillBackground(False)
        inner = QWidget()
        layout = QVBoxLayout(inner)
        # A hair of top margin: the first group's title lives in the box's own margin
        # band, and flush against the viewport edge it reads as cropped even when it is not.
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(20)
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)

        # ---- General settings ----
        general_box = QGroupBox("General")
        form = QFormLayout(general_box)
        form.setVerticalSpacing(theme.SPACE)
        form.setHorizontalSpacing(theme.SPACE * 2)

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
        pattern_note.setObjectName("FieldNote")
        pattern_note.setWordWrap(True)
        form.addRow("", pattern_note)

        layout.addWidget(general_box)

        # ---- Auto-categorisation settings ----
        # Lime, because this group configures the categoriser, and the categoriser owns
        # lime everywhere it appears: the Library panel it writes into and its own dialog.
        cat_box = QGroupBox("Auto-Categorisation")
        cat_box.setProperty("accent", "lime")
        cat_layout = QVBoxLayout(cat_box)
        cat_layout.setSpacing(theme.SPACE * 2)

        cat_form = QFormLayout()
        cat_form.setVerticalSpacing(theme.SPACE)
        cat_form.setHorizontalSpacing(theme.SPACE * 2)

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
        cat_label.setObjectName("FieldNote")
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

        # ---- Appearance ----
        appearance_box = QGroupBox("Appearance")
        app_form = QFormLayout(appearance_box)
        app_form.setVerticalSpacing(theme.SPACE)
        app_form.setHorizontalSpacing(theme.SPACE * 2)

        self._reduce_motion_check = QCheckBox("Reduce motion")
        self._reduce_motion_check.setChecked(self._settings.reduce_motion)
        app_form.addRow("Motion:", self._reduce_motion_check)

        motion_note = QLabel(
            "Panel hover lift and the cross-fade between result states become instant. "
            "Nothing is hidden by this: every state the motion carries is carried by the "
            "change itself."
        )
        motion_note.setObjectName("FieldNote")
        motion_note.setWordWrap(True)
        app_form.addRow("", motion_note)

        layout.addWidget(appearance_box)

        # ---- Library maintenance ----
        # Amber, the import region's hue: fingerprinting is the import pipeline's own
        # maintenance job, and coverage short of the whole library is a caution.
        maint_box = QGroupBox("Library maintenance")
        maint_box.setProperty("accent", "amber")
        maint_form = QFormLayout(maint_box)
        maint_form.setVerticalSpacing(theme.SPACE)
        maint_form.setHorizontalSpacing(theme.SPACE * 2)

        # Read once, when the dialog is built: this states what was true on opening, and
        # a figure that moved while the user was typing in another field would be noise.
        hashed, total = self._db.get_hash_coverage()
        complete = total > 0 and hashed >= total

        coverage_row = QWidget()
        coverage_col = QVBoxLayout(coverage_row)
        coverage_col.setContentsMargins(0, 0, 0, 0)
        coverage_col.setSpacing(theme.SPACE)

        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.setSpacing(theme.SPACE * 2)

        if total == 0:
            coverage_text = "No papers in the library yet."
        elif complete:
            coverage_text = f"All {total:,} papers fingerprinted."
        else:
            coverage_text = f"{hashed:,} of {total:,} papers fingerprinted."
        # No word wrap: all three of its strings are one short sentence, and wrapping lets
        # the layout hand it a narrow width and break it across two lines next to a button.
        coverage_label = QLabel(coverage_text)
        status_row.addWidget(coverage_label)

        # The remedy sits beside the state it answers, so the row reads as one sentence.
        # The stretch goes after the button, not into the label: pushed to the far right
        # edge of a 700px row the button reads as unrelated chrome.
        scan_btn = QPushButton("Scan library…")
        scan_btn.setEnabled(total > 0)
        scan_btn.clicked.connect(self._request_backfill)
        status_row.addWidget(scan_btn)
        status_row.addStretch(1)
        coverage_col.addLayout(status_row)

        if total > 0 and not complete:
            # The caution state, in the vocabulary needs-review already uses on the paper
            # form: amber on the value itself and an amber left edge on the consequence.
            # Never a banner across the group, which is the shape a reader skips.
            coverage_label.setStyleSheet(f"color: {theme.ACCENT_AMBER};")
            caution = QLabel(
                "Papers without a fingerprint cannot be recognised as duplicates by "
                "their contents."
            )
            caution.setWordWrap(True)
            caution.setStyleSheet(
                f"color: {theme.TEXT_PRIMARY};"
                f"border-left: 2px solid {theme.ACCENT_AMBER};"
                f"padding-left: {theme.SPACE}px;"
            )
            coverage_col.addWidget(caution)

        maint_form.addRow("Fingerprints:", coverage_row)

        scan_note = QLabel(
            "Reads every PDF once and records a fingerprint, so the same file is never "
            "imported twice. Papers imported before fingerprinting was added have none "
            "until the scan runs. The scan can be stopped and resumed, and changes "
            "nothing else about a paper."
        )
        scan_note.setObjectName("FieldNote")
        scan_note.setWordWrap(True)
        maint_form.addRow("", scan_note)

        layout.addWidget(maint_box)
        layout.addStretch(1)

        # ---- Dialog buttons: a chrome strip, matching the window's command bar ----
        button_bar = GlassPanel(chrome=True)
        button_bar.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok_button is not None:
            ok_button.setObjectName("primary")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        button_bar.content_layout.addWidget(buttons)
        outer.addWidget(button_bar)

    def _request_backfill(self) -> None:
        """Hand the scan to the window, saving and closing on the way out.

        The scan window is non-modal and runs for as long as the library is large;
        leaving a modal Settings dialog in front of it would hold the application shut
        for the length of the run.
        """
        self.backfill_requested.emit()
        self._accept()

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
        self._settings.reduce_motion = self._reduce_motion_check.isChecked()
        # Applied here rather than by the caller: every animation in the application asks
        # theme at the moment it would start, so the box takes effect on OK without a
        # restart and without a settings object reaching the widgets that animate.
        theme.set_reduced_motion(self._settings.reduce_motion)

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
