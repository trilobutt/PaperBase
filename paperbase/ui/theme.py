from PyQt6.QtWidgets import QApplication

# Colour palette
# BASE_BG    #0D0D0D  root window / deep-black canvas
# SURFACE    #0D1B2A  panel / card / dialog surface (deep dark blue)
# RAISED     #163046  toolbar / button fill (raised above surface)
# RAISED_HI  #1E4264  raised bevel lighter edge (top-left)
# RAISED_LO  #060F1A  raised bevel darker edge (bottom-right)
# INSET_BG   #07111C  input / list background (recessed below surface)
# INSET_SH   #030A12  inset bevel shadow (top-left darker)
# INSET_LF   #172D44  inset bevel lift (bottom-right lighter)
# BORDER     #1A3048  standard border / divider
# TEXT       #e0d8cc  primary text
# TEXT_DIM   #6888A0  muted / secondary text (blue-grey)
# ACCENT     #F26822  primary orange accent (selections, focus, primary actions)
# ACCENT_DK  #c44f10  pressed accent
# ALT_ROW    #0A1826  alternating table row
# HOVER      #142840  hover surface highlight

_STYLESHEET = """
/* ================================================================
   Base
   ================================================================ */
QWidget {
    background-color: #0D1B2A;
    color: #e0d8cc;
    font-family: "Segoe UI", sans-serif;
    font-size: 9pt;
}

QMainWindow {
    background-color: #0D0D0D;
}

QDialog {
    background-color: #0D1B2A;
}

/* ================================================================
   Toolbar
   ================================================================ */
QToolBar {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #1A3A58, stop:1 #102030);
    border: none;
    border-bottom: 1px solid #060F1A;
    padding: 3px 6px;
    spacing: 5px;
}

QToolBar QWidget {
    background: transparent;
}

QToolBar::separator {
    background-color: #1A3048;
    width: 1px;
    margin: 4px 4px;
}

/* ================================================================
   Push buttons  (raised bevel: light top-left, dark bottom-right)
   ================================================================ */
QPushButton {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #1E4264, stop:1 #122840);
    color: #e0d8cc;
    border-style: solid;
    border-width: 1px;
    border-color: #060F1A #060F1A #060F1A #060F1A;
    border-top-color: #1E4264;
    border-left-color: #1E4264;
    border-radius: 2px;
    padding: 3px 10px;
    min-height: 20px;
}

QPushButton:hover {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #244E6C, stop:1 #1A3A58);
    border-top-color: #2A5A7A;
    border-left-color: #2A5A7A;
}

QPushButton:pressed {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #08121C, stop:1 #101E30);
    border-top-color: #060F1A;
    border-left-color: #060F1A;
    border-bottom-color: #1E4264;
    border-right-color: #1E4264;
}

QPushButton:disabled {
    background: #0C1E2E;
    color: #3A5070;
    border-color: #102234;
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
    background-color: #07111C;
    color: #e0d8cc;
    border-style: solid;
    border-width: 1px;
    border-color: #1A3048;
    border-top-color: #030A12;
    border-left-color: #030A12;
    border-bottom-color: #172D44;
    border-right-color: #172D44;
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
    border-top-color: #030A12;
    border-left-color: #030A12;
    border-bottom-color: #F26822;
    border-right-color: #F26822;
}

QLineEdit:disabled,
QSpinBox:disabled,
QDoubleSpinBox:disabled,
QPlainTextEdit:disabled,
QTextEdit:disabled {
    background-color: #0A1826;
    color: #3A5070;
}

QSpinBox::up-button,
QSpinBox::down-button,
QDoubleSpinBox::up-button,
QDoubleSpinBox::down-button {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #1E4264, stop:1 #122840);
    border-left: 1px solid #030A12;
    width: 14px;
    subcontrol-origin: border;
}

QSpinBox::up-button,
QDoubleSpinBox::up-button {
    subcontrol-position: top right;
    border-top: none;
    border-bottom: 1px solid #1A3048;
}

QSpinBox::down-button,
QDoubleSpinBox::down-button {
    subcontrol-position: bottom right;
    border-top: 1px solid #1A3048;
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
    background-color: #07111C;
    alternate-background-color: #0A1826;
    color: #e0d8cc;
    border-style: solid;
    border-width: 1px;
    border-top-color: #030A12;
    border-left-color: #030A12;
    border-bottom-color: #172D44;
    border-right-color: #172D44;
    gridline-color: #0F2035;
    selection-background-color: #F26822;
    selection-color: #ffffff;
    outline: none;
}

QTableView::item:hover,
QTableWidget::item:hover {
    background-color: #142840;
}

QTableView::item:selected,
QTableWidget::item:selected {
    background-color: #F26822;
    color: #ffffff;
}

QHeaderView {
    background-color: #102234;
}

QHeaderView::section {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #1A3A58, stop:1 #0C1C2E);
    color: #e0d8cc;
    border: none;
    border-right: 1px solid #1A3048;
    border-bottom: 1px solid #030A12;
    padding: 3px 6px;
    font-weight: bold;
}

QHeaderView::section:hover {
    background: #1E4264;
}

QHeaderView::section:first {
    border-left: none;
}

/* ================================================================
   Tree view
   ================================================================ */
QTreeView {
    background-color: #0D1B2A;
    color: #e0d8cc;
    border-style: solid;
    border-width: 1px;
    border-top-color: #030A12;
    border-left-color: #030A12;
    border-bottom-color: #172D44;
    border-right-color: #172D44;
    selection-background-color: #F26822;
    selection-color: #ffffff;
    outline: none;
}

QTreeView::item:hover {
    background-color: #142840;
}

QTreeView::item:selected {
    background-color: #F26822;
    color: #ffffff;
}

QTreeView::branch {
    background-color: #0D1B2A;
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
    background-color: #07111C;
    color: #e0d8cc;
    border-style: solid;
    border-width: 1px;
    border-top-color: #030A12;
    border-left-color: #030A12;
    border-bottom-color: #172D44;
    border-right-color: #172D44;
    selection-background-color: #F26822;
    selection-color: #ffffff;
    outline: none;
}

QListWidget::item:hover {
    background-color: #142840;
}

QListWidget::item:selected {
    background-color: #F26822;
    color: #ffffff;
}

/* ================================================================
   Scroll bars  (traditional, always visible, 14 px wide)
   ================================================================ */
QScrollBar:vertical {
    background-color: #0D0D0D;
    border-left: 1px solid #060F1A;
    width: 14px;
    margin: 14px 0 14px 0;
}

QScrollBar::handle:vertical {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #1E4060, stop:1 #163046);
    border: 1px solid #060F1A;
    border-top-color: #1E4264;
    border-left-color: #1E4264;
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
                    stop:0 #1E4264, stop:1 #122840);
    border: 1px solid #060F1A;
    border-top-color: #1E4264;
    border-left-color: #1E4264;
    height: 14px;
    subcontrol-position: bottom;
    subcontrol-origin: margin;
}

QScrollBar::sub-line:vertical {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #1E4264, stop:1 #122840);
    border: 1px solid #060F1A;
    border-top-color: #1E4264;
    border-left-color: #1E4264;
    height: 14px;
    subcontrol-position: top;
    subcontrol-origin: margin;
}

QScrollBar::add-line:vertical:hover,
QScrollBar::sub-line:vertical:hover {
    background: #F26822;
}

QScrollBar:horizontal {
    background-color: #0D0D0D;
    border-top: 1px solid #060F1A;
    height: 14px;
    margin: 0 14px 0 14px;
}

QScrollBar::handle:horizontal {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #1E4060, stop:1 #163046);
    border: 1px solid #060F1A;
    border-top-color: #1E4264;
    border-left-color: #1E4264;
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
                    stop:0 #1E4264, stop:1 #122840);
    border: 1px solid #060F1A;
    border-top-color: #1E4264;
    border-left-color: #1E4264;
    width: 14px;
    subcontrol-position: right;
    subcontrol-origin: margin;
}

QScrollBar::sub-line:horizontal {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #1E4264, stop:1 #122840);
    border: 1px solid #060F1A;
    border-top-color: #1E4264;
    border-left-color: #1E4264;
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
    background-color: #060F1A;
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
                    stop:0 #0F2035, stop:1 #0D0D0D);
    color: #6888A0;
    border-top: 1px solid #060F1A;
    font-size: 8pt;
}

/* ================================================================
   Menu
   ================================================================ */
QMenu {
    background-color: #0F2035;
    color: #e0d8cc;
    border: 1px solid #1A3048;
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
    color: #3A5070;
}

QMenu::separator {
    background-color: #1A3048;
    height: 1px;
    margin: 3px 6px;
}

/* ================================================================
   Group box
   ================================================================ */
QGroupBox {
    background-color: #0D1B2A;
    border: 1px solid #1A3048;
    border-top-color: #030A12;
    border-left-color: #030A12;
    border-bottom-color: #172D44;
    border-right-color: #172D44;
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
    background-color: #0D1B2A;
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
    background-color: #07111C;
    border-style: solid;
    border-width: 1px;
    border-top-color: #030A12;
    border-left-color: #030A12;
    border-bottom-color: #172D44;
    border-right-color: #172D44;
    border-radius: 1px;
}

QCheckBox::indicator:checked {
    background-color: #F26822;
    border-color: #c44f10;
}

QCheckBox::indicator:hover {
    border-top-color: #030A12;
    border-left-color: #030A12;
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
    background-color: #102234;
    color: #e0d8cc;
    border: 1px solid #F26822;
    padding: 3px 6px;
}
"""


def apply_theme(app: QApplication) -> None:
    app.setStyleSheet(_STYLESHEET)
