import json
from pathlib import Path
from typing import Optional

from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QLabel,
    QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

SETTINGS_VERSION = 1


class Settings:
    def __init__(self) -> None:
        self.library_root: str = ""
        self.user_email: str = ""
        self.folder_pattern: str = "{journal}/{year}/{author} ({year}) {title}.pdf"
        self.last_import_dir: str = ""

    def is_configured(self) -> bool:
        return bool(self.library_root and self.user_email)

    def save(self, path: Path) -> None:
        data = {
            "version": SETTINGS_VERSION,
            "library_root": self.library_root,
            "user_email": self.user_email,
            "folder_pattern": self.folder_pattern,
            "last_import_dir": self.last_import_dir,
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
            except Exception:
                pass
        return s


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(480)
        self._settings = settings
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        form = QFormLayout()

        # Library root
        root_row = QWidget()
        root_hl = __import__("PyQt6.QtWidgets", fromlist=["QHBoxLayout"]).QHBoxLayout(root_row)
        root_hl.setContentsMargins(0, 0, 0, 0)
        self._root_edit = QLineEdit(self._settings.library_root)
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse_root)
        root_hl.addWidget(self._root_edit)
        root_hl.addWidget(browse_btn)
        form.addRow("Library root:", root_row)

        # Email
        self._email_edit = QLineEdit(self._settings.user_email)
        self._email_edit.setPlaceholderText("your@email.com")
        form.addRow("Email (Crossref/Unpaywall):", self._email_edit)

        # Folder pattern
        self._pattern_edit = QLineEdit(self._settings.folder_pattern)
        form.addRow("Folder pattern:", self._pattern_edit)

        note = QLabel(
            "Pattern tokens: {journal} {year} {author} {title}\n"
            "Unresolved tokens are replaced with 'Unknown' or 'Unsorted'."
        )
        note.setStyleSheet("color: grey; font-size: 11px;")

        layout.addLayout(form)
        layout.addWidget(note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _browse_root(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select Library Root",
                                                 self._root_edit.text() or str(Path.home()))
        if path:
            self._root_edit.setText(path)

    def _accept(self) -> None:
        self._settings.library_root = self._root_edit.text().strip()
        self._settings.user_email = self._email_edit.text().strip()
        self._settings.folder_pattern = self._pattern_edit.text().strip()
        self.accept()
