"""
PaperBase entry point.

First-run wizard collects library root, email, and confirmation of folder
pattern, then opens MainWindow with import running in background.
"""
import logging
import os
import sys
from pathlib import Path

import qasync
from PyQt6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QVBoxLayout,
    QWidget,
)
from platformdirs import user_data_dir

from paperbase.core.db import Database
from paperbase.core.indexer import Indexer
from paperbase.ui import theme
from paperbase.ui.glass import CanvasBackdrop, GlassPanel, display_font
from paperbase.ui.main_window import MainWindow
from paperbase.ui.settings_dialog import Settings


def _data_dir() -> Path:
    """Runtime data directory. PAPERBASE_DATA_DIR overrides it, for fixtures and tests."""
    override = os.environ.get("PAPERBASE_DATA_DIR", "").strip()
    if override:
        p = Path(override).expanduser()
        if not p.is_absolute():
            raise SystemExit(f"PAPERBASE_DATA_DIR must be an absolute path, got: {override}")
    else:
        p = Path(user_data_dir("PaperBase", "PaperBase"))
    p.mkdir(parents=True, exist_ok=True)
    return p


class FirstRunWizard(QDialog):
    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("PaperBase — First Run Setup")
        self.setMinimumWidth(560)
        self._settings = settings
        self._build_ui()

    def _build_ui(self) -> None:
        """The first screen anyone sees: a name, a promise, two fields, one action.

        One panel floating in black, and the black doing most of the work. The heading
        is display type rather than a bolded sentence because this screen sets the
        expectation for every screen after it, and it has the room to.
        """
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        backdrop = CanvasBackdrop(self)
        root.addWidget(backdrop)

        outer = QVBoxLayout(backdrop)
        outer.setContentsMargins(20, 20, 20, 20)
        outer.setSpacing(20)

        panel = GlassPanel()
        panel.content_layout.setSpacing(theme.SPACE * 2)

        # Name and promise are one block: a two-pixel gap between them, and three units
        # of air below, so they read as a single voice rather than as two labels.
        masthead = QVBoxLayout()
        masthead.setContentsMargins(0, 0, 0, 0)
        masthead.setSpacing(2)

        heading = QLabel("PaperBase")
        heading.setObjectName("DisplayHeading")
        heading.setFont(display_font(26))
        masthead.addWidget(heading)

        subtitle = QLabel("Point it at a folder and it will do the rest.")
        subtitle.setObjectName("DisplaySubtitle")
        subtitle.setWordWrap(True)
        masthead.addWidget(subtitle)

        panel.content_layout.addLayout(masthead)
        panel.content_layout.addSpacing(theme.SPACE)

        form = QFormLayout()
        form.setVerticalSpacing(theme.SPACE)
        form.setHorizontalSpacing(theme.SPACE * 2)

        # Library root
        root_widget = QWidget()
        rhl = QHBoxLayout(root_widget)
        rhl.setContentsMargins(0, 0, 0, 0)
        rhl.setSpacing(theme.SPACE)
        self._root_edit = QLineEdit()
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse)
        rhl.addWidget(self._root_edit)
        rhl.addWidget(browse_btn)
        form.addRow("Library root folder:", root_widget)

        self._email_edit = QLineEdit()
        self._email_edit.setPlaceholderText("your@email.com")
        form.addRow("Your email (for APIs):", self._email_edit)

        note = QLabel(
            "Your email is sent in the User-Agent string to Crossref and Unpaywall "
            "for polite API access. It is not shared with any other service."
        )
        note.setWordWrap(True)
        note.setObjectName("FieldNote")

        panel.content_layout.addLayout(form)
        panel.content_layout.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok_button is not None:
            # The action names what it does. "OK" on a first screen reads as dismissal.
            ok_button.setText("Continue")
            ok_button.setObjectName("primary")
        buttons.accepted.connect(self._validate)
        panel.content_layout.addWidget(buttons)

        outer.addWidget(panel)
        outer.addStretch(1)

    def _browse(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select Library Root",
                                                 str(Path.home()))
        if path:
            self._root_edit.setText(path)

    def _validate(self) -> None:
        root = self._root_edit.text().strip()
        email = self._email_edit.text().strip()
        if not root:
            QMessageBox.warning(self, "Required", "Please select a library root folder.")
            return
        if not email or "@" not in email:
            QMessageBox.warning(self, "Required", "Please enter a valid email address.")
            return
        self._settings.library_root = root
        self._settings.user_email = email
        self.accept()


def main() -> None:
    debug = "--debug" in sys.argv
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.WARNING,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    app = QApplication(sys.argv)
    app.setApplicationName("PaperBase")
    app.setOrganizationName("PaperBase")
    theme.apply_theme(app)

    data = _data_dir()
    settings_path = data / "settings.json"
    db_path = data / "paperbase.db"
    index_dir = data / "index"

    settings = Settings.load(settings_path)
    # Before any widget is built: GlassPanel and StackFader read this when they animate,
    # and the wizard below is already a surface that would otherwise animate.
    theme.set_reduced_motion(settings.reduce_motion)

    # First-run wizard
    if not settings.is_configured():
        wizard = FirstRunWizard(settings)
        if wizard.exec() != QDialog.DialogCode.Accepted:
            sys.exit(0)
        settings.save(settings_path)

    # Open database + indexer
    db = Database(db_path)
    db.open()

    indexer = Indexer(index_dir)
    indexer.open()

    window = MainWindow(db, indexer, settings, settings_path)
    window.show()
    window.start_deferred_load()

    # Use qasync event loop so asyncio coroutines work in the Qt event loop
    import asyncio
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    with loop:
        loop.run_forever()

    indexer.close()
    db.close()


if __name__ == "__main__":
    main()
