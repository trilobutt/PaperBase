from PyQt6.QtWidgets import QApplication

# Colour palette
# BASE_BG    #1c1917  main window / deep panel background
# SURFACE    #242018  widget panel surface
# RAISED     #3a3530  toolbar / button fill (mid)
# RAISED_HI  #4e4840  raised bevel lighter edge (top-left)
# RAISED_LO  #141210  raised bevel darker edge (bottom-right)
# INSET_BG   #161310  input / list background (recessed below surface)
# BORDER     #4a4540  standard border
# TEXT       #e0d8cc  primary text
# TEXT_DIM   #706860  muted / secondary text
# ACCENT     #F26822  primary orange accent (selections, focus, primary actions)
# ACCENT_DK  #c44f10  pressed accent
# ALT_ROW    #1e1b17  alternating table row
# HOVER      #38332e  hover surface highlight

_STYLESHEET = """
/* ================================================================
   Base
   ================================================================ */
QWidget {
    background-color: #242018;
    color: #e0d8cc;
    font-family: "Segoe UI", sans-serif;
    font-size: 9pt;
}

QMainWindow {
    background-color: #1c1917;
}

QDialog {
    background-color: #242018;
}

/* ================================================================
   Toolbar
   ================================================================ */
QToolBar {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #3d3830, stop:1 #2e2a25);
    border: none;
    border-bottom: 1px solid #141210;
    padding: 3px 6px;
    spacing: 5px;
}

QToolBar QWidget {
    background: transparent;
}

QToolBar::separator {
    background-color: #4a4540;
    width: 1px;
    margin: 4px 4px;
}

/* ================================================================
   Push buttons  (raised bevel: light top-left, dark bottom-right)
   ================================================================ */
QPushButton {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #484038, stop:1 #36322c);
    color: #e0d8cc;
    border-style: solid;
    border-width: 1px;
    border-color: #141210 #141210 #141210 #141210;
    border-top-color: #5c5248;
    border-left-color: #5c5248;
    border-radius: 2px;
    padding: 3px 10px;
    min-height: 20px;
}

QPushButton:hover {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #575048, stop:1 #46403a);
    border-top-color: #6e6660;
    border-left-color: #6e6660;
}

QPushButton:pressed {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #28241f, stop:1 #343028);
    border-top-color: #141210;
    border-left-color: #141210;
    border-bottom-color: #5c5248;
    border-right-color: #5c5248;
}

QPushButton:disabled {
    background: #27231e;
    color: #504840;
    border-color: #2e2a25;
}

/* Primary action button — set objectName="primary" to activate */
QPushButton[objectName="primary"] {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #f87c38, stop:1 #d05618);
    color: #ffffff;
    border-top-color: #ff9958;
    border-left-color: #ff9958;
    border-bottom-color: #903410;
    border-right-color: #903410;
    font-weight: bold;
}

QPushButton[objectName="primary"]:hover {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #ff8840, stop:1 #e06020);
}

QPushButton[objectName="primary"]:pressed {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #b84808, stop:1 #d05618);
    border-top-color: #903410;
    border-left-color: #903410;
    border-bottom-color: #ff9958;
    border-right-color: #ff9958;
}

/* ================================================================
   Input fields  (inset bevel: dark top-left, lighter bottom-right)
   ================================================================ */
QLineEdit,
QSpinBox,
QDoubleSpinBox,
QPlainTextEdit,
QTextEdit {
    background-color: #161310;
    color: #e0d8cc;
    border-style: solid;
    border-width: 1px;
    border-color: #4a4540;
    border-top-color: #0c0a08;
    border-left-color: #0c0a08;
    border-bottom-color: #5c5248;
    border-right-color: #5c5248;
    border-radius: 2px;
    padding: 2px 4px;
    selection-background-color: #F26822;
    selection-color: #ffffff;
}

QLineEdit:focus,
QSpinBox:focus,
QDoubleSpinBox:focus,
QPlainTextEdit:focus,
QTextEdit:focus {
    border-top-color: #0c0a08;
    border-left-color: #0c0a08;
    border-bottom-color: #F26822;
    border-right-color: #F26822;
}

QLineEdit:disabled,
QSpinBox:disabled,
QDoubleSpinBox:disabled,
QPlainTextEdit:disabled,
QTextEdit:disabled {
    background-color: #1e1b17;
    color: #504840;
}

QSpinBox::up-button,
QSpinBox::down-button,
QDoubleSpinBox::up-button,
QDoubleSpinBox::down-button {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #484038, stop:1 #36322c);
    border-left: 1px solid #0c0a08;
    width: 14px;
    subcontrol-origin: border;
}

QSpinBox::up-button,
QDoubleSpinBox::up-button {
    subcontrol-position: top right;
    border-top: none;
    border-bottom: 1px solid #4a4540;
}

QSpinBox::down-button,
QDoubleSpinBox::down-button {
    subcontrol-position: bottom right;
    border-top: 1px solid #4a4540;
    border-bottom: none;
}

QSpinBox::up-button:hover,
QDoubleSpinBox::up-button:hover,
QSpinBox::down-button:hover,
QDoubleSpinBox::down-button:hover {
    background: #F26822;
}

/* ================================================================
   Table views  (inset)
   ================================================================ */
QTableView,
QTableWidget {
    background-color: #161310;
    alternate-background-color: #1e1b17;
    color: #e0d8cc;
    border-style: solid;
    border-width: 1px;
    border-top-color: #0c0a08;
    border-left-color: #0c0a08;
    border-bottom-color: #5c5248;
    border-right-color: #5c5248;
    gridline-color: #2a2620;
    selection-background-color: #F26822;
    selection-color: #ffffff;
    outline: none;
}

QTableView::item:hover,
QTableWidget::item:hover {
    background-color: #38332e;
}

QTableView::item:selected,
QTableWidget::item:selected {
    background-color: #F26822;
    color: #ffffff;
}

QHeaderView {
    background-color: #2e2a25;
}

QHeaderView::section {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #3d3830, stop:1 #2c2822);
    color: #e0d8cc;
    border: none;
    border-right: 1px solid #4a4540;
    border-bottom: 1px solid #0c0a08;
    padding: 3px 6px;
    font-weight: bold;
}

QHeaderView::section:hover {
    background: #484038;
}

QHeaderView::section:first {
    border-left: none;
}

/* ================================================================
   Tree view
   ================================================================ */
QTreeView {
    background-color: #1c1917;
    color: #e0d8cc;
    border-style: solid;
    border-width: 1px;
    border-top-color: #0c0a08;
    border-left-color: #0c0a08;
    border-bottom-color: #5c5248;
    border-right-color: #5c5248;
    selection-background-color: #F26822;
    selection-color: #ffffff;
    outline: none;
}

QTreeView::item:hover {
    background-color: #38332e;
}

QTreeView::item:selected {
    background-color: #F26822;
    color: #ffffff;
}

QTreeView::branch {
    background-color: #1c1917;
}

QTreeView::branch:has-children:!has-siblings:closed,
QTreeView::branch:closed:has-children:has-siblings {
    border-image: none;
    image: none;
}

/* ================================================================
   List widget
   ================================================================ */
QListWidget {
    background-color: #161310;
    color: #e0d8cc;
    border-style: solid;
    border-width: 1px;
    border-top-color: #0c0a08;
    border-left-color: #0c0a08;
    border-bottom-color: #5c5248;
    border-right-color: #5c5248;
    selection-background-color: #F26822;
    selection-color: #ffffff;
    outline: none;
}

QListWidget::item:hover {
    background-color: #38332e;
}

QListWidget::item:selected {
    background-color: #F26822;
    color: #ffffff;
}

/* ================================================================
   Scroll bars  (traditional, always visible, 14 px wide)
   ================================================================ */
QScrollBar:vertical {
    background-color: #1c1917;
    border-left: 1px solid #141210;
    width: 14px;
    margin: 14px 0 14px 0;
}

QScrollBar::handle:vertical {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #4e4840, stop:1 #3a3530);
    border: 1px solid #141210;
    border-top-color: #5c5248;
    border-left-color: #5c5248;
    min-height: 20px;
    margin: 0;
}

QScrollBar::handle:vertical:hover {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #F26822, stop:1 #c85010);
    border-top-color: #ff9958;
}

QScrollBar::add-line:vertical {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #484038, stop:1 #36322c);
    border: 1px solid #141210;
    border-top-color: #5c5248;
    border-left-color: #5c5248;
    height: 14px;
    subcontrol-position: bottom;
    subcontrol-origin: margin;
}

QScrollBar::sub-line:vertical {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #484038, stop:1 #36322c);
    border: 1px solid #141210;
    border-top-color: #5c5248;
    border-left-color: #5c5248;
    height: 14px;
    subcontrol-position: top;
    subcontrol-origin: margin;
}

QScrollBar::add-line:vertical:hover,
QScrollBar::sub-line:vertical:hover {
    background: #F26822;
}

QScrollBar:horizontal {
    background-color: #1c1917;
    border-top: 1px solid #141210;
    height: 14px;
    margin: 0 14px 0 14px;
}

QScrollBar::handle:horizontal {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #4e4840, stop:1 #3a3530);
    border: 1px solid #141210;
    border-top-color: #5c5248;
    border-left-color: #5c5248;
    min-width: 20px;
    margin: 0;
}

QScrollBar::handle:horizontal:hover {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #F26822, stop:1 #c85010);
    border-top-color: #ff9958;
}

QScrollBar::add-line:horizontal {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #484038, stop:1 #36322c);
    border: 1px solid #141210;
    border-top-color: #5c5248;
    border-left-color: #5c5248;
    width: 14px;
    subcontrol-position: right;
    subcontrol-origin: margin;
}

QScrollBar::sub-line:horizontal {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #484038, stop:1 #36322c);
    border: 1px solid #141210;
    border-top-color: #5c5248;
    border-left-color: #5c5248;
    width: 14px;
    subcontrol-position: left;
    subcontrol-origin: margin;
}

QScrollBar::add-line:horizontal:hover,
QScrollBar::sub-line:horizontal:hover {
    background: #F26822;
}

QScrollBar::add-page,
QScrollBar::sub-page {
    background: none;
}

/* ================================================================
   Splitter
   ================================================================ */
QSplitter::handle {
    background-color: #141210;
}

QSplitter::handle:horizontal {
    width: 3px;
}

QSplitter::handle:vertical {
    height: 3px;
}

QSplitter::handle:hover {
    background-color: #F26822;
}

/* ================================================================
   Status bar
   ================================================================ */
QStatusBar {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #2a2620, stop:1 #1c1917);
    color: #706860;
    border-top: 1px solid #141210;
    font-size: 8pt;
}

/* ================================================================
   Menu
   ================================================================ */
QMenu {
    background-color: #2a2620;
    color: #e0d8cc;
    border: 1px solid #4a4540;
    padding: 2px 0;
}

QMenu::item {
    padding: 4px 20px 4px 8px;
}

QMenu::item:selected {
    background-color: #F26822;
    color: #ffffff;
}

QMenu::item:disabled {
    color: #504840;
}

QMenu::separator {
    background-color: #4a4540;
    height: 1px;
    margin: 3px 6px;
}

/* ================================================================
   Group box
   ================================================================ */
QGroupBox {
    background-color: #242018;
    border: 1px solid #4a4540;
    border-top-color: #0c0a08;
    border-left-color: #0c0a08;
    border-bottom-color: #5c5248;
    border-right-color: #5c5248;
    border-radius: 3px;
    margin-top: 10px;
    padding-top: 10px;
    font-weight: bold;
    color: #e0d8cc;
}

QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 8px;
    top: -1px;
    padding: 0 5px;
    color: #F26822;
    background-color: #242018;
}

/* ================================================================
   Checkbox
   ================================================================ */
QCheckBox {
    color: #e0d8cc;
    spacing: 6px;
    background: transparent;
}

QCheckBox::indicator {
    width: 13px;
    height: 13px;
    background-color: #161310;
    border-style: solid;
    border-width: 1px;
    border-top-color: #0c0a08;
    border-left-color: #0c0a08;
    border-bottom-color: #5c5248;
    border-right-color: #5c5248;
    border-radius: 1px;
}

QCheckBox::indicator:checked {
    background-color: #F26822;
    border-color: #c44f10;
}

QCheckBox::indicator:hover {
    border-top-color: #0c0a08;
    border-left-color: #0c0a08;
    border-bottom-color: #F26822;
    border-right-color: #F26822;
}

/* ================================================================
   Labels
   ================================================================ */
QLabel {
    background: transparent;
    color: #e0d8cc;
}

/* ================================================================
   Scroll area
   ================================================================ */
QScrollArea {
    border: none;
    background: transparent;
}

QScrollArea > QWidget > QWidget {
    background: transparent;
}

/* ================================================================
   Dialog button box
   ================================================================ */
QDialogButtonBox QPushButton {
    min-width: 72px;
}

/* ================================================================
   Tooltip
   ================================================================ */
QToolTip {
    background-color: #2e2a25;
    color: #e0d8cc;
    border: 1px solid #F26822;
    padding: 3px 6px;
}
"""


def apply_theme(app: QApplication) -> None:
    app.setStyleSheet(_STYLESHEET)
