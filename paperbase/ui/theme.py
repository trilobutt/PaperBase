"""Design tokens and the global Qt stylesheet.

The identity is a deep black canvas with translucent glass panels floating on it as
islands, one accent hue per region, and light coming from colour rather than grey fill.
Every colour, radius, spacing step and motion duration in the application is a module
constant here; widget files reference them by name and never hardcode a hex value.

Structural rules this file depends on:

- ``QWidget`` carries the canvas black as its base. Nothing else paints a solid panel
  colour globally: a panel colour on the base selector would repaint every anonymous
  layout container and destroy the floating-island layout.
- ``.QWidget`` (the dot form matches plain ``QWidget`` instances and not subclasses)
  is transparent, so the row containers, scroll-area viewports and layout holders that
  sit inside a glass panel let the panel show through.
- Panels are styled by object name (``#GlassPanel``), never by class. A panel carrying
  chrome (the command bar, a dialog header) sets the dynamic property ``chrome`` to
  ``True`` for the heavier ``PANEL_FILL_HI`` fill.
- Qt has no ``box-shadow``: panel lift comes from ``QGraphicsDropShadowEffect`` in
  ``paperbase.ui.glass``, not from here.
- Qt has no ``prefers-reduced-motion`` either, so ``reduced_motion()`` is the switch
  every animation is gated on.
"""

import os
from string import Template

from PyQt6.QtWidgets import QApplication

# --------------------------------------------------------------------------------------
# Ground
# --------------------------------------------------------------------------------------
CANVAS = "#0A0A0C"        # root; the majority of pixels on screen
CANVAS_DEEP = "#060608"   # gradient far end (window edges)
ANCHOR = "#1A1046"        # deep indigo; gradient toward, and the base of every glow

# --------------------------------------------------------------------------------------
# Glass (QSS alpha is an integer 0-255, never a 0-1 float)
# --------------------------------------------------------------------------------------
PANEL_FILL = "rgba(255, 255, 255, 14)"
PANEL_FILL_HI = "rgba(255, 255, 255, 22)"   # dialogs and the command bar
PANEL_BORDER = "rgba(255, 255, 255, 38)"
PANEL_EDGE_TOP = "rgba(255, 255, 255, 58)"  # bright top hairline on chrome only
INSET_FILL = "rgba(0, 0, 0, 120)"           # inputs and lists: recessed, darker than host
INSET_BORDER = "rgba(255, 255, 255, 26)"

# --------------------------------------------------------------------------------------
# Accents (each owns a region; never reuse one for a second region)
# --------------------------------------------------------------------------------------
ACCENT_CYAN = "#22E1FF"     # results/search region, primary action, focus, selection
ACCENT_LIME = "#8FE84A"     # collections/tags region, success
ACCENT_MAGENTA = "#FF5CB0"  # paper-detail region, tag chips
ACCENT_AMBER = "#FFB03A"    # import region, needs-review state
ACCENT_RED = "#FF5A5A"      # destructive actions, errors

# --------------------------------------------------------------------------------------
# Text
# --------------------------------------------------------------------------------------
TEXT_PRIMARY = "#EDEDF2"
TEXT_SECONDARY = "#B9BAC7"   # deliberately above the #888-#AAA band, which fails the floor
TEXT_ON_ACCENT = "#0A0A0C"   # near-black; verified against all five accents

# --------------------------------------------------------------------------------------
# Type
# --------------------------------------------------------------------------------------
FONT_UI = "'Tahoma', 'Segoe UI', sans-serif"
FONT_MONO = "'Consolas', 'Cascadia Mono', monospace"

# --------------------------------------------------------------------------------------
# Form
# --------------------------------------------------------------------------------------
SPACE = 8            # one unit. Panel gaps 24 (3u), panel padding 16 (2u), intra-group 8
RADIUS_PANEL = 14
RADIUS_CONTROL = 8
RADIUS_INPUT = 6

# --------------------------------------------------------------------------------------
# Motion (ms)
# --------------------------------------------------------------------------------------
MOTION_HOVER = 140
MOTION_BASE = 220
MOTION_CELEBRATE = 600

# --------------------------------------------------------------------------------------
# Private derivations. Popups (menus, tooltips, combo lists) are top-level windows with
# nothing of ours behind them, so translucent glass would composite against the desktop.
# They get a solid backing at the value glass over canvas resolves to. The accent shades
# are lighter/darker steps of ACCENT_CYAN and ACCENT_RED for hover and press, not new hues.
# --------------------------------------------------------------------------------------
_POPUP_BACKING = "#12121A"
_CYAN_HI = "#6BECFF"
_CYAN_LO = "#12B4CE"
_MAGENTA_HI = "#FF8FCB"
_MAGENTA_LO = "#D93A8C"
_RED_HI = "#FF8080"
_RED_LO = "#D93F3F"
_HOVER_LIFT = "rgba(255, 255, 255, 18)"     # translucent lift, never a colour swap
_HOVER_FILL = "rgba(255, 255, 255, 34)"
_HOVER_BORDER = "rgba(255, 255, 255, 72)"
_PRESS_FILL = "rgba(0, 0, 0, 90)"
_DISABLED_FILL = "rgba(255, 255, 255, 8)"
_DISABLED_BORDER = "rgba(255, 255, 255, 20)"
_DISABLED_TEXT = "rgba(185, 186, 199, 120)"
_ALT_ROW = "rgba(255, 255, 255, 8)"
_GRID_LINE = "rgba(255, 255, 255, 18)"
_FOCUS_RING = "rgba(34, 225, 255, 120)"
_TRACK = "rgba(0, 0, 0, 90)"

_QSS = """
/* ======================================================================
   Base. The canvas is the dominant colour of the interface.
   ====================================================================== */
QWidget {
    background-color: ${CANVAS};
    color: ${TEXT_PRIMARY};
    font-family: ${FONT_UI};
    font-size: 9pt;
}

/* Plain QWidget instances only: anonymous layout containers, scroll-area
   viewports and content holders inside a glass panel. */
.QWidget {
    background: transparent;
}

QMainWindow,
QDialog {
    background-color: ${CANVAS};
}

QSplitter,
QStackedWidget,
QScrollArea,
QTabWidget,
QToolBar,
QStatusBar,
QLabel,
QCheckBox,
QRadioButton {
    background: transparent;
}

QScrollArea {
    border: none;
}

QToolBar {
    border: none;
    padding: ${SPACE}px;
    spacing: ${SPACE}px;
}

QStatusBar {
    color: ${TEXT_SECONDARY};
    border: none;
}

/* A group heading inside a panel (the filter column, the paper form's sections).
   Bold at body size and one step back from the controls it leads: the structure
   comes from the weight and the space around it, never from shrinking the type. */
QLabel#FilterGroupLabel,
QLabel#FieldGroupLabel {
    color: ${TEXT_SECONDARY};
    font-weight: bold;
}

/* A field's own label, inside a group. Same colour as the group heading and a
   step below it in weight, so the value in the input is the brightest thing in
   the panel and the two levels of label are told apart by weight alone. */
QLabel#FieldLabel {
    color: ${TEXT_SECONDARY};
}

/* The needs-review state, worn beside the panel title as a small amber pill. A
   banner across the top of the form would be read as decoration and skipped; this
   is one chip and a matching edge on the fields whose values are actually in
   doubt, which is a state the eye can attach to something. */
QLabel#ReviewChip {
    background-color: ${ACCENT_AMBER};
    color: ${TEXT_ON_ACCENT};
    border: 1px solid ${ACCENT_AMBER};
    border-radius: 11px;
    padding: 2px 10px;
    font-weight: bold;
}

/* Display type: the largest heading in the application, carried by the first-run
   screen. Size and weight live here because a stylesheet outranks setFont;
   tracking is pulled in by glass.display_font, since QSS has no letter-spacing. */
QLabel#DisplayHeading {
    color: ${TEXT_PRIMARY};
    font-size: 26pt;
    font-weight: bold;
}

QLabel#DisplaySubtitle {
    color: ${TEXT_SECONDARY};
    font-size: 12pt;
}

/* An explanatory line under a field, a table or a form. Secondary in colour and
   never in size: it is the only place some of this is said, and shrinking it
   below the body size fails the legibility floor. */
QLabel#FieldNote {
    color: ${TEXT_SECONDARY};
}

/* Why an entry was refused, under the field it refused. */
QLabel#FieldError {
    color: ${ACCENT_RED};
}

/* Fixed-width output: the import and categorisation logs, where the columns are
   the medium. The only monospace in the application outside them. */
#LogView {
    font-family: ${FONT_MONO};
}

/* ======================================================================
   Glass panels. Styled by object name so no widget class is claimed.
   ====================================================================== */
#GlassPanel {
    background-color: ${PANEL_FILL};
    border: 1px solid ${PANEL_BORDER};
    border-top-color: ${PANEL_EDGE_TOP};
    border-radius: ${RADIUS_PANEL}px;
}

/* Chrome carries the identity harder: command bar, dialog headers. */
#GlassPanel[chrome="true"] {
    background-color: ${PANEL_FILL_HI};
}

/* ======================================================================
   Inputs. Recessed: darker than their host panel, tighter radius, no lift.
   ====================================================================== */
QLineEdit,
QPlainTextEdit,
QTextEdit,
QSpinBox,
QDoubleSpinBox,
QComboBox {
    background-color: ${INSET_FILL};
    color: ${TEXT_PRIMARY};
    border: 1px solid ${INSET_BORDER};
    border-radius: ${RADIUS_INPUT}px;
    padding: 5px 8px;
    selection-background-color: ${ACCENT_CYAN};
    selection-color: ${TEXT_ON_ACCENT};
}

QLineEdit:hover,
QPlainTextEdit:hover,
QTextEdit:hover,
QSpinBox:hover,
QDoubleSpinBox:hover,
QComboBox:hover {
    border-color: ${PANEL_BORDER};
}

QLineEdit:focus,
QPlainTextEdit:focus,
QTextEdit:focus,
QSpinBox:focus,
QDoubleSpinBox:focus,
QComboBox:focus {
    border-color: ${ACCENT_CYAN};
}

QLineEdit:disabled,
QPlainTextEdit:disabled,
QTextEdit:disabled,
QSpinBox:disabled,
QDoubleSpinBox:disabled,
QComboBox:disabled {
    color: ${DISABLED_TEXT};
    border-color: ${DISABLED_BORDER};
}

/* Needs review, marked on the field itself. Only the left edge, so hover and
   focus still read on the other three. */
QLineEdit[review="true"],
QSpinBox[review="true"] {
    border-left-color: ${ACCENT_AMBER};
}

/* A hand-typed taxon that taxa.txt does not hold. Red, since the entry was refused, and
   on the whole border, since nothing was saved. */
QLineEdit[invalid="true"] {
    border-color: ${ACCENT_RED};
}

/* The spin-box stepper sub-controls and the combo-box drop-down are
   deliberately left unstyled: giving them any rule switches them to CSS box
   layout and collapses their arrow glyphs to invisible. */

/* ======================================================================
   Buttons. Glass, with a lift in fill and border on hover.
   ====================================================================== */
QPushButton {
    background-color: ${PANEL_FILL_HI};
    color: ${TEXT_PRIMARY};
    border: 1px solid ${PANEL_BORDER};
    border-radius: ${RADIUS_CONTROL}px;
    padding: 6px 16px;
    min-height: 22px;
}

QPushButton:hover {
    background-color: ${HOVER_FILL};
    border-color: ${HOVER_BORDER};
}

QPushButton:pressed {
    background-color: ${PRESS_FILL};
    border-color: ${PANEL_BORDER};
}

QPushButton:focus {
    border-color: ${ACCENT_CYAN};
}

QPushButton:disabled {
    background-color: ${DISABLED_FILL};
    color: ${TEXT_SECONDARY};
    border-color: ${DISABLED_BORDER};
}

QPushButton#primary {
    background-color: ${ACCENT_CYAN};
    color: ${TEXT_ON_ACCENT};
    border-color: ${ACCENT_CYAN};
    font-weight: bold;
}

QPushButton#primary:hover {
    background-color: ${CYAN_HI};
    border-color: ${CYAN_HI};
}

QPushButton#primary:pressed {
    background-color: ${CYAN_LO};
    border-color: ${CYAN_LO};
}

QPushButton#primary:disabled {
    background-color: ${DISABLED_FILL};
    color: ${TEXT_SECONDARY};
    border-color: ${DISABLED_BORDER};
}

QPushButton#danger {
    background-color: ${ACCENT_RED};
    color: ${TEXT_ON_ACCENT};
    border-color: ${ACCENT_RED};
    font-weight: bold;
}

QPushButton#danger:hover {
    background-color: ${RED_HI};
    border-color: ${RED_HI};
}

QPushButton#danger:pressed {
    background-color: ${RED_LO};
    border-color: ${RED_LO};
}

QPushButton#danger:disabled {
    background-color: ${DISABLED_FILL};
    color: ${TEXT_SECONDARY};
    border-color: ${DISABLED_BORDER};
}

/* Tag chips: the paper panel's one piece of personality, in that region's
   magenta. Filling with the accent on hover makes the trailing glyph read as the
   removal it is; the glyph itself is present at rest, so the affordance never
   depends on a cursor being there. */
QPushButton#TagChip {
    background-color: ${PANEL_FILL_HI};
    color: ${TEXT_PRIMARY};
    border: 1px solid ${ACCENT_MAGENTA};
    border-radius: 11px;
    padding: 2px 6px;
    min-height: 16px;
}

QPushButton#TagChip:hover {
    background-color: ${ACCENT_MAGENTA};
    color: ${TEXT_ON_ACCENT};
    border-color: ${ACCENT_MAGENTA};
}

QPushButton#TagChip:pressed {
    background-color: ${MAGENTA_LO};
    border-color: ${MAGENTA_LO};
    color: ${TEXT_ON_ACCENT};
}

QPushButton#TagChip:focus {
    background-color: ${HOVER_FILL};
    border-color: ${MAGENTA_HI};
}

QDialogButtonBox QPushButton {
    min-width: 84px;
}

/* ======================================================================
   Item views. Dense inside, recessed against the panel around them.
   ====================================================================== */
QTableView,
QTableWidget,
QTreeView,
QListView,
QListWidget {
    background-color: ${INSET_FILL};
    alternate-background-color: ${ALT_ROW};
    color: ${TEXT_PRIMARY};
    border: 1px solid ${INSET_BORDER};
    border-radius: ${RADIUS_INPUT}px;
    gridline-color: ${GRID_LINE};
    selection-background-color: ${ACCENT_CYAN};
    selection-color: ${TEXT_ON_ACCENT};
    outline: none;
}

QTableView:focus,
QTableWidget:focus,
QTreeView:focus,
QListView:focus,
QListWidget:focus {
    border-color: ${FOCUS_RING};
}

QTableView::item,
QTableWidget::item,
QTreeView::item,
QListView::item,
QListWidget::item {
    padding: 3px 6px;
    border: none;
}

QTableView::item:hover,
QTableWidget::item:hover,
QTreeView::item:hover,
QListView::item:hover,
QListWidget::item:hover {
    background-color: ${HOVER_LIFT};
}

QTableView::item:selected,
QTableWidget::item:selected,
QTreeView::item:selected,
QListView::item:selected,
QListWidget::item:selected {
    background-color: ${ACCENT_CYAN};
    color: ${TEXT_ON_ACCENT};
}

QTreeView::branch {
    background: transparent;
}

QHeaderView {
    background: transparent;
    border: none;
}

QHeaderView::section {
    background-color: ${PANEL_FILL};
    color: ${TEXT_SECONDARY};
    font-weight: bold;
    border: none;
    border-right: 1px solid ${GRID_LINE};
    border-bottom: 1px solid ${PANEL_BORDER};
    padding: 6px 10px;
}

QHeaderView::section:hover {
    background-color: ${PANEL_FILL_HI};
    color: ${TEXT_PRIMARY};
}

QHeaderView::section:last {
    border-right: none;
}

QTableCornerButton::section {
    background-color: ${PANEL_FILL};
    border: none;
    border-bottom: 1px solid ${PANEL_BORDER};
}

/* ======================================================================
   Scroll bars. Traditional and always visible: 14px, a glass handle with
   real mass, never an overlay that hides itself.
   ====================================================================== */
QScrollBar:vertical {
    background-color: ${TRACK};
    border: none;
    border-radius: ${RADIUS_CONTROL}px;
    width: 14px;
    margin: 0;
}

QScrollBar:horizontal {
    background-color: ${TRACK};
    border: none;
    border-radius: ${RADIUS_CONTROL}px;
    height: 14px;
    margin: 0;
}

QScrollBar::handle:vertical {
    background-color: ${PANEL_FILL_HI};
    border: 1px solid ${PANEL_BORDER};
    border-radius: ${RADIUS_CONTROL}px;
    min-height: 32px;
    margin: 3px;
}

QScrollBar::handle:horizontal {
    background-color: ${PANEL_FILL_HI};
    border: 1px solid ${PANEL_BORDER};
    border-radius: ${RADIUS_CONTROL}px;
    min-width: 32px;
    margin: 3px;
}

QScrollBar::handle:vertical:hover,
QScrollBar::handle:horizontal:hover {
    background-color: ${ACCENT_CYAN};
    border-color: ${ACCENT_CYAN};
}

QScrollBar::handle:vertical:pressed,
QScrollBar::handle:horizontal:pressed {
    background-color: ${CYAN_LO};
    border-color: ${CYAN_LO};
}

/* No stepper buttons: styling them would collapse their arrows, so the
   track is given over entirely to the handle. */
QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical,
QScrollBar::add-line:horizontal,
QScrollBar::sub-line:horizontal {
    background: none;
    border: none;
    width: 0;
    height: 0;
}

QScrollBar::add-page,
QScrollBar::sub-page {
    background: none;
}

/* ======================================================================
   Splitter. The gap between panels is canvas, not chrome.
   ====================================================================== */
QSplitter::handle {
    background-color: transparent;
}

QSplitter::handle:horizontal {
    width: 20px;
}

QSplitter::handle:vertical {
    height: 20px;
}

QSplitter::handle:horizontal:hover {
    background-color: ${ACCENT_CYAN};
    margin: 0 9px;
}

QSplitter::handle:vertical:hover {
    background-color: ${ACCENT_CYAN};
    margin: 9px 0;
}

/* ======================================================================
   Popups. Top-level windows, so a solid backing rather than glass.
   ====================================================================== */
QMenuBar {
    background: transparent;
    border: none;
}

QMenuBar::item {
    background: transparent;
    padding: 6px 10px;
    border-radius: ${RADIUS_CONTROL}px;
}

QMenuBar::item:selected {
    background-color: ${HOVER_LIFT};
}

QMenu {
    background-color: ${POPUP_BACKING};
    color: ${TEXT_PRIMARY};
    border: 1px solid ${PANEL_BORDER};
    border-radius: ${RADIUS_CONTROL}px;
    padding: 6px;
}

QMenu::item {
    padding: 6px 24px 6px 12px;
    border-radius: ${RADIUS_INPUT}px;
}

QMenu::item:selected {
    background-color: ${ACCENT_CYAN};
    color: ${TEXT_ON_ACCENT};
}

QMenu::item:disabled {
    color: ${DISABLED_TEXT};
}

QMenu::separator {
    background-color: ${PANEL_BORDER};
    height: 1px;
    margin: 6px 8px;
}

QComboBox QAbstractItemView {
    background-color: ${POPUP_BACKING};
    color: ${TEXT_PRIMARY};
    border: 1px solid ${PANEL_BORDER};
    border-radius: ${RADIUS_INPUT}px;
    padding: 4px;
    selection-background-color: ${ACCENT_CYAN};
    selection-color: ${TEXT_ON_ACCENT};
    outline: none;
}

QToolTip {
    background-color: ${POPUP_BACKING};
    color: ${TEXT_PRIMARY};
    border: 1px solid ${PANEL_BORDER};
    border-radius: ${RADIUS_INPUT}px;
    padding: 6px 10px;
}

/* ======================================================================
   Group boxes. Glass, so dialogs need no per-dialog styling.
   ====================================================================== */
QGroupBox {
    background-color: ${PANEL_FILL};
    border: 1px solid ${PANEL_BORDER};
    border-top-color: ${PANEL_EDGE_TOP};
    border-radius: ${RADIUS_PANEL}px;
    /* The title is drawn in this margin band (subcontrol-origin: margin below), so the
       band has to clear the full height of 9pt bold type. At 14px the ascenders were
       clipped by whatever sat above the box, which for the first group in a scroll area
       is the viewport edge. */
    margin-top: 20px;
    padding: 20px 16px 16px 16px;
    font-weight: bold;
    color: ${TEXT_PRIMARY};
}

QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 14px;
    padding: 0 6px;
    color: ${TEXT_SECONDARY};
    background: transparent;
}

/* A group that belongs to a region wears that region's hue in its title, the
   same way a GlassPanel does. Set the dynamic property `accent` on the box. */
QGroupBox[accent="amber"]::title {
    color: ${ACCENT_AMBER};
}

QGroupBox[accent="lime"]::title {
    color: ${ACCENT_LIME};
}

QGroupBox[accent="cyan"]::title {
    color: ${ACCENT_CYAN};
}

/* ======================================================================
   Check boxes and radio buttons. A filled accent indicator: colour carries
   the state, so it reads without relying on a glyph.
   ====================================================================== */
QCheckBox,
QRadioButton {
    spacing: ${SPACE}px;
}

QCheckBox::indicator,
QRadioButton::indicator {
    width: 14px;
    height: 14px;
    background-color: ${INSET_FILL};
    border: 1px solid ${INSET_BORDER};
    border-radius: 3px;
}

QRadioButton::indicator {
    border-radius: 8px;
}

QCheckBox::indicator:hover,
QRadioButton::indicator:hover,
QCheckBox:focus::indicator,
QRadioButton:focus::indicator {
    border-color: ${ACCENT_CYAN};
}

QCheckBox::indicator:checked,
QRadioButton::indicator:checked {
    background-color: ${ACCENT_CYAN};
    border-color: ${ACCENT_CYAN};
}

QCheckBox::indicator:disabled,
QRadioButton::indicator:disabled {
    background-color: ${DISABLED_FILL};
    border-color: ${DISABLED_BORDER};
}

QCheckBox:disabled,
QRadioButton:disabled {
    color: ${TEXT_SECONDARY};
}

/* ======================================================================
   Tabs.
   ====================================================================== */
QTabWidget::pane {
    background: transparent;
    border: none;
    border-top: 1px solid ${PANEL_BORDER};
    top: -1px;
}

QTabBar {
    background: transparent;
}

QTabBar::tab {
    background: transparent;
    color: ${TEXT_SECONDARY};
    border: none;
    border-bottom: 2px solid transparent;
    padding: 8px 16px;
    margin-right: 4px;
}

QTabBar::tab:hover {
    background-color: ${HOVER_LIFT};
    color: ${TEXT_PRIMARY};
}

QTabBar::tab:selected {
    color: ${TEXT_PRIMARY};
    border-bottom-color: ${ACCENT_CYAN};
    font-weight: bold;
}

QTabBar::tab:focus {
    border-bottom-color: ${ACCENT_CYAN};
}

/* ======================================================================
   Progress. Visible mass on an inset groove.
   ====================================================================== */
QProgressBar {
    background-color: ${INSET_FILL};
    color: ${TEXT_PRIMARY};
    border: 1px solid ${INSET_BORDER};
    border-radius: ${RADIUS_CONTROL}px;
    min-height: 12px;
    text-align: center;
}

QProgressBar::chunk {
    background-color: ${ACCENT_CYAN};
    border-radius: ${RADIUS_CONTROL}px;
}

/* A region's progress: no text on the bar (the counts row beside it says the
   same thing in words), so the bar can be a slim band of pure colour instead. */
QProgressBar[accent="amber"],
QProgressBar[accent="lime"] {
    min-height: 10px;
    max-height: 10px;
}

QProgressBar[accent="amber"]::chunk {
    background-color: ${ACCENT_AMBER};
}

QProgressBar[accent="lime"]::chunk {
    background-color: ${ACCENT_LIME};
}
"""

_TOKENS: dict[str, str] = {
    "CANVAS": CANVAS,
    "CANVAS_DEEP": CANVAS_DEEP,
    "ANCHOR": ANCHOR,
    "PANEL_FILL": PANEL_FILL,
    "PANEL_FILL_HI": PANEL_FILL_HI,
    "PANEL_BORDER": PANEL_BORDER,
    "PANEL_EDGE_TOP": PANEL_EDGE_TOP,
    "INSET_FILL": INSET_FILL,
    "INSET_BORDER": INSET_BORDER,
    "ACCENT_CYAN": ACCENT_CYAN,
    "ACCENT_LIME": ACCENT_LIME,
    "ACCENT_MAGENTA": ACCENT_MAGENTA,
    "ACCENT_AMBER": ACCENT_AMBER,
    "ACCENT_RED": ACCENT_RED,
    "TEXT_PRIMARY": TEXT_PRIMARY,
    "TEXT_SECONDARY": TEXT_SECONDARY,
    "TEXT_ON_ACCENT": TEXT_ON_ACCENT,
    "FONT_UI": FONT_UI,
    "FONT_MONO": FONT_MONO,
    "SPACE": str(SPACE),
    "RADIUS_PANEL": str(RADIUS_PANEL),
    "RADIUS_CONTROL": str(RADIUS_CONTROL),
    "RADIUS_INPUT": str(RADIUS_INPUT),
    "POPUP_BACKING": _POPUP_BACKING,
    "CYAN_HI": _CYAN_HI,
    "CYAN_LO": _CYAN_LO,
    "MAGENTA_HI": _MAGENTA_HI,
    "MAGENTA_LO": _MAGENTA_LO,
    "RED_HI": _RED_HI,
    "RED_LO": _RED_LO,
    "HOVER_LIFT": _HOVER_LIFT,
    "HOVER_FILL": _HOVER_FILL,
    "HOVER_BORDER": _HOVER_BORDER,
    "PRESS_FILL": _PRESS_FILL,
    "DISABLED_FILL": _DISABLED_FILL,
    "DISABLED_BORDER": _DISABLED_BORDER,
    "DISABLED_TEXT": _DISABLED_TEXT,
    "ALT_ROW": _ALT_ROW,
    "GRID_LINE": _GRID_LINE,
    "FOCUS_RING": _FOCUS_RING,
    "TRACK": _TRACK,
}

STYLESHEET: str = Template(_QSS).substitute(_TOKENS)


def _env_reduced_motion() -> bool | None:
    """The environment's answer, or ``None`` when it has not been given one."""
    raw = os.environ.get("PAPERBASE_REDUCED_MOTION", "").strip().lower()
    if not raw:
        return None
    return raw in ("1", "true", "yes")


# Read once at import. Qt has no prefers-reduced-motion, so these two are the only things
# standing between the application and an unmet accessibility floor.
_ENV_REDUCED_MOTION: bool | None = _env_reduced_motion()
_SETTING_REDUCED_MOTION: bool = False


def set_reduced_motion(enabled: bool) -> None:
    """Push ``Settings.reduce_motion`` into the switch every animation reads.

    Module state rather than an injected object, and deliberately: the animations are
    built in widget constructors several layers below anything holding a ``Settings``
    (``GlassPanel``, ``StackFader``), so threading one down to them would put a
    constructor argument on every panel in the application to carry a single boolean.
    Exactly two call sites write it, ``main.main`` at startup and
    ``SettingsDialog._accept`` when the box is ticked, and both are one-way.
    """
    global _SETTING_REDUCED_MOTION
    _SETTING_REDUCED_MOTION = enabled


def reduced_motion() -> bool:
    """True when animations must collapse to instant state changes.

    ``PAPERBASE_REDUCED_MOTION`` (``1``/``true``/``yes``, any case) overrides the setting
    in either direction whenever it holds a non-empty value; it exists so a test run can
    force either answer regardless of what is in ``settings.json``. Every animation reads
    this at the moment it would start, so the setting takes effect without a restart.
    """
    if _ENV_REDUCED_MOTION is not None:
        return _ENV_REDUCED_MOTION
    return _SETTING_REDUCED_MOTION


def apply_theme(app: QApplication) -> None:
    """Install the global stylesheet. Call immediately after ``QApplication()``."""
    # Fusion first: the native Windows style ignores parts of the sheet, so the glass
    # treatment only lands consistently on top of Fusion.
    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET)
