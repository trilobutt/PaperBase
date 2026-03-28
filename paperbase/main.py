"""
PaperBase entry point.

First-run wizard collects library root, email, and confirmation of folder
pattern, then opens MainWindow with import running in background.
"""
import sys
from pathlib import Path

import qasync
from PyQt6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QLabel, QLineEdit, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)
from platformdirs import user_data_dir

from paperbase.core.db import Database
from paperbase.core.indexer import Indexer
from paperbase.ui.main_window import MainWindow
from paperbase.ui.settings_dialog import Settings


def _data_dir() -> Path:
    p = Path(user_data_dir("PaperBase", "PaperBase"))
    p.mkdir(parents=True, exist_ok=True)
    return p


class FirstRunWizard(QDialog):
    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("PaperBase — First Run Setup")
        self.setMinimumWidth(500)
        self._settings = settings
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        intro = QLabel(
            "<b>Welcome to PaperBase.</b><br>"
            "Please configure your library before continuing."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()

        # Library root
        root_widget = QWidget()
        from PyQt6.QtWidgets import QHBoxLayout
        rhl = QHBoxLayout(root_widget)
        rhl.setContentsMargins(0, 0, 0, 0)
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
        note.setStyleSheet("color: grey; font-size: 11px;")

        layout.addLayout(form)
        layout.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(self._validate)
        layout.addWidget(buttons)

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
    app = QApplication(sys.argv)
    app.setApplicationName("PaperBase")
    app.setOrganizationName("PaperBase")

    data = _data_dir()
    settings_path = data / "settings.json"
    db_path = data / "paperbase.db"
    index_dir = data / "index"

    settings = Settings.load(settings_path)

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
