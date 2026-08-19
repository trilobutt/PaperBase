"""Glass, glow and lift: the three primitives the whole interface is built from.

Qt has no backdrop blur and no ``box-shadow``, so the depth model in ``theme.py`` cannot be
expressed in the stylesheet alone. It is built once here and reused:

- :class:`CanvasBackdrop` paints the window's ground (one gradient, two wide glows) so the
  black reads as deep space rather than as an off switch.
- :class:`GlassPanel` is the floating island: object-name styling from the global sheet,
  a drop shadow for lift, a region accent that is visible at rest and blooms on hover.
- :func:`panel_shadow` gives lift to anything that is not a full panel, and
  :func:`accent_glow` gives a live control a bloom in its region's hue.
- :class:`StackFader` cross-fades a ``QStackedWidget``, so a switch between two unrelated
  full-panel states is followed rather than cut.
- :class:`EmptyState` is the designed absence: local glow, display heading, one action.
- :func:`display_font` is the ``FONT_UI`` stack at display size, for the one heading that
  lives outside this module.

Every colour is a ``theme`` constant. The single literal is the shadow's ``#00000099``,
written as ``QColor(0, 0, 0, 0x99)`` because Qt reads a nine-digit hex string as
``#AARRGGBB`` and would silently make that shadow fully transparent.
"""

from html import escape
from typing import Optional

from PyQt6.QtCore import (
    QEasingCurve,
    QEvent,
    QObject,
    QPointF,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    QVariantAnimation,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QColor,
    QEnterEvent,
    QFont,
    QLinearGradient,
    QPainter,
    QPaintEvent,
    QPixmap,
    QRadialGradient,
    QResizeEvent,
)
from PyQt6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from . import theme

# --------------------------------------------------------------------------------------
# Shared paint helpers
# --------------------------------------------------------------------------------------

# Alpha falls off as (1 - t) ** 2.2. A linear ramp reads as a hard ring at the outer edge;
# this one dissolves into the canvas with nothing to catch the eye. Qt interpolates
# linearly between stops, so a coarse set of them lays visible Mach bands across a wide
# bloom: 33 keeps every segment shorter than the eye can pick out on near-black.
_GLOW_STEPS = 32
_GLOW_STOPS: tuple[float, ...] = tuple(i / _GLOW_STEPS for i in range(_GLOW_STEPS + 1))
_GLOW_FALLOFF = 2.2

# Lift. The panel's shadow at rest and at full hover: deeper, softer and pushed further
# down, so hovering reads as the panel rising rather than as a colour swap.
_SHADOW_COLOUR = QColor(0, 0, 0, 0x99)      # #00000099
_SHADOW_ALPHA_HOVER = 0xC0
_SHADOW_BLUR_LIFT = 12.0
_SHADOW_DY_LIFT = 3.0

# The region accent never becomes a solid 1px wire around the panel: at full hover the
# border is the accent hue at this alpha, which brightens the edge without shouting.
_BORDER_ACCENT_ALPHA = 160


def _parse_colour(value: str) -> QColor:
    """Turn a theme token (``#RRGGBB`` or ``rgba(r, g, b, a)``) into a ``QColor``."""
    text = value.strip()
    if text.startswith("rgba(") or text.startswith("rgb("):
        parts = [p.strip() for p in text[text.index("(") + 1 : text.rindex(")")].split(",")]
        channels = [int(round(float(p))) for p in parts]
        if len(channels) == 3:
            channels.append(255)
        return QColor(*channels)
    return QColor(text)


def _rgba(colour: QColor) -> str:
    """Render a ``QColor`` as a QSS ``rgba()`` literal (QSS alpha is 0-255, never 0-1)."""
    return f"rgba({colour.red()}, {colour.green()}, {colour.blue()}, {colour.alpha()})"


def _mix(start: QColor, end: QColor, t: float) -> QColor:
    """Linear blend between two colours, alpha included."""
    t = min(1.0, max(0.0, t))
    return QColor(
        round(start.red() + (end.red() - start.red()) * t),
        round(start.green() + (end.green() - start.green()) * t),
        round(start.blue() + (end.blue() - start.blue()) * t),
        round(start.alpha() + (end.alpha() - start.alpha()) * t),
    )


def _glow(centre: QPointF, radius: float, colour: QColor, peak: float) -> QRadialGradient:
    """A wide, diffuse bloom. ``peak`` is the alpha at the centre, 0-1."""
    gradient = QRadialGradient(centre, max(radius, 1.0))
    for stop in _GLOW_STOPS:
        step = QColor(colour)
        step.setAlphaF(min(1.0, max(0.0, peak * (1.0 - stop) ** _GLOW_FALLOFF)))
        gradient.setColorAt(stop, step)
    return gradient


def _ui_font(point_size: int, *, bold: bool = False, tracking: float = 100.0) -> QFont:
    """A font from the ``FONT_UI`` stack. Tracking is a percentage; below 100 tightens.

    Pair it with :func:`_type_qss`. The global sheet declares ``font-size`` on ``QWidget``
    and a stylesheet outranks ``setFont``, so size and weight only stick when they are
    declared on the widget's own sheet; tracking has no QSS property and only comes from
    here.
    """
    families = [
        part.strip().strip("'\"")
        for part in theme.FONT_UI.split(",")
        if part.strip().strip("'\"") not in ("sans-serif", "serif", "monospace")
    ]
    font = QFont()
    font.setFamilies(families)
    font.setPointSize(point_size)
    font.setBold(bold)
    font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, tracking)
    return font


def _type_qss(colour: str, point_size: int, *, bold: bool = False) -> str:
    """Widget-level type declarations that survive the global sheet."""
    weight = "bold" if bold else "normal"
    return (
        f"color: {colour}; background: transparent; "
        f"font-size: {point_size}pt; font-weight: {weight};"
    )


def display_font(point_size: int, *, bold: bool = True, tracking: float = 96.0) -> QFont:
    """The ``FONT_UI`` stack at display size, tracking pulled in as the size grows.

    Pair it with the ``QLabel#DisplayHeading`` rule in ``theme``: the stylesheet outranks
    ``setFont`` for size and weight, and tracking has no QSS property at all, so the two
    together are what a display heading actually needs.
    """
    return _ui_font(point_size, bold=bold, tracking=tracking)


def panel_shadow(widget: QWidget, *, blur: int = 28, dy: int = 6) -> None:
    """Give ``widget`` the standard lift without making it a full :class:`GlassPanel`."""
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(blur)
    effect.setOffset(0, dy)
    effect.setColor(QColor(_SHADOW_COLOUR))
    widget.setGraphicsEffect(effect)


def accent_glow(
    widget: QWidget, colour: str, *, blur: int = 34, alpha: int = 150
) -> QGraphicsDropShadowEffect:
    """A soft bloom of ``colour`` around ``widget``, returned so it can be switched off.

    A drop shadow with no offset is Qt's only route to a glow on a live widget, and the
    only one that costs nothing while it is disabled. The caller owns the state: the
    effect starts enabled and is switched with ``setEnabled``, so the bloom marks a
    control that is genuinely doing something rather than sitting there lit at rest.
    """
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(blur)
    effect.setOffset(0, 0)
    tint = _parse_colour(colour)
    tint.setAlpha(alpha)
    effect.setColor(tint)
    widget.setGraphicsEffect(effect)
    return effect


# --------------------------------------------------------------------------------------
# The ground
# --------------------------------------------------------------------------------------
class CanvasBackdrop(QWidget):
    """The window's background: one gradient, two glows, nothing else.

    The gradient runs ``CANVAS`` at the top-left to ``CANVAS_DEEP`` at the bottom-right, so
    the window edges fall away and the working area sits in the lighter part of the field.
    Over it, an ``ANCHOR`` bloom centred outside the top-left corner gives the black a
    direction, and a much fainter ``ACCENT_CYAN`` bloom sits under the top edge of the
    results panel, tying the region's hue into the ground it floats on. Both are far too
    diffuse to read as shapes; at 7% the cyan is atmosphere, not decoration over data.

    The composite is cached to a pixmap and rebuilt only on resize: three full-window
    alpha blends per paint event is real cost during a window drag, and the output is
    identical every time.
    """

    # Anchor bloom: centre pushed outside the corner so no hotspot is ever visible.
    _ANCHOR_CENTRE = (-0.06, -0.12)
    _ANCHOR_RADIUS = 0.9
    _ANCHOR_PEAK = 0.22

    # Cyan bloom: the centre column of the 1:4:2 splitter, at the panels' top edge.
    _CYAN_CENTRE = (0.43, 0.15)
    _CYAN_RADIUS = 0.6
    _CYAN_PEAK = 0.07

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAutoFillBackground(False)
        self._cache: Optional[QPixmap] = None

    def resizeEvent(self, event: QResizeEvent) -> None:
        self._cache = None
        super().resizeEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        if self._cache is None or self._cache.size() != self._backing_size():
            self._cache = self._render()
        # Drawn whole: the painter is already clipped to the exposed region, and the
        # source-rect overload would need device pixels rather than logical ones.
        painter = QPainter(self)
        painter.drawPixmap(0, 0, self._cache)
        painter.end()

    def _backing_size(self) -> QSize:
        ratio = self.devicePixelRatioF()
        return QSize(max(1, round(self.width() * ratio)), max(1, round(self.height() * ratio)))

    def _render(self) -> QPixmap:
        width = max(1, self.width())
        height = max(1, self.height())
        pixmap = QPixmap(self._backing_size())
        pixmap.setDevicePixelRatio(self.devicePixelRatioF())
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(0.0, 0.0, float(width), float(height))

        ground = QLinearGradient(rect.topLeft(), rect.bottomRight())
        ground.setColorAt(0.0, _parse_colour(theme.CANVAS))
        ground.setColorAt(1.0, _parse_colour(theme.CANVAS_DEEP))
        painter.fillRect(rect, ground)

        painter.fillRect(
            rect,
            _glow(
                QPointF(width * self._ANCHOR_CENTRE[0], height * self._ANCHOR_CENTRE[1]),
                width * self._ANCHOR_RADIUS,
                _parse_colour(theme.ANCHOR),
                self._ANCHOR_PEAK,
            ),
        )
        painter.fillRect(
            rect,
            _glow(
                QPointF(width * self._CYAN_CENTRE[0], height * self._CYAN_CENTRE[1]),
                width * self._CYAN_RADIUS,
                _parse_colour(theme.ACCENT_CYAN),
                self._CYAN_PEAK,
            ),
        )
        painter.end()
        return pixmap


# --------------------------------------------------------------------------------------
# The island
# --------------------------------------------------------------------------------------
class GlassPanel(QFrame):
    """A floating glass island carrying one idea.

    ``title`` is drawn in the panel's region accent at bold body size: the hue identifies
    the place before a word is read, and it is present at rest, so nothing about the
    region's identity depends on a cursor being there. Hover adds to it rather than
    revealing it: the hairline brightens toward the accent and the shadow deepens and
    drops, which reads as the panel rising off the canvas.

    ``header_layout`` holds the title on the left and a right-aligned slot for controls
    (see :meth:`add_header_widget`); ``content_layout`` is the body. Callers that want the
    heavier chrome fill (the command bar, dialog headers) pass ``chrome=True``.
    """

    def __init__(
        self,
        title: str = "",
        *,
        accent: str = theme.ACCENT_CYAN,
        chrome: bool = False,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("GlassPanel")
        self.accent = accent
        self._accent_colour = _parse_colour(accent)
        self._border_rest = _parse_colour(theme.PANEL_BORDER)
        self._border_hover = QColor(self._accent_colour)
        self._border_hover.setAlpha(_BORDER_ACCENT_ALPHA)
        self._hover = 0.0

        if chrome:
            self.setProperty("chrome", True)

        self._shadow = QGraphicsDropShadowEffect(self)
        self._shadow.setBlurRadius(28)
        self._shadow.setOffset(0, 6)
        self._shadow.setColor(QColor(_SHADOW_COLOUR))
        self.setGraphicsEffect(self._shadow)

        pad = theme.SPACE * 2
        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(pad, pad, pad, pad)
        outer = self._outer

        self.header_layout = QHBoxLayout()
        self.header_layout.setContentsMargins(0, 0, 0, 0)
        self.header_layout.setSpacing(theme.SPACE)

        self.title_label = QLabel(title, self)
        self.title_label.setFont(_ui_font(9, bold=True, tracking=104.0))
        self.title_label.setStyleSheet(_type_qss(accent, 9, bold=True))
        self.header_layout.addWidget(self.title_label)
        self.header_layout.addStretch(1)

        self._header_visible = bool(title)
        self.title_label.setVisible(self._header_visible)
        # A panel with no title (the command bar, the status strip) must not pay for the
        # header gap: an empty header row is zero-height but the spacing around it is not.
        outer.setSpacing(theme.SPACE if self._header_visible else 0)
        outer.addLayout(self.header_layout)

        self.content_layout = QVBoxLayout()
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(theme.SPACE)
        outer.addLayout(self.content_layout, 1)

        self._animation = QVariantAnimation(self)
        self._animation.setDuration(theme.MOTION_HOVER)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.valueChanged.connect(self._apply_hover)

    # -- composition -------------------------------------------------------------------
    def set_content(self, widget: QWidget) -> None:
        """Put ``widget`` in the panel body, filling it."""
        self.content_layout.addWidget(widget, 1)

    def add_header_widget(self, widget: QWidget) -> None:
        """Add a control to the header's right-aligned slot (a count, a small button)."""
        self.header_layout.addWidget(widget)
        if not self._header_visible:
            self._header_visible = True
            self._outer.setSpacing(theme.SPACE)

    # -- hover -------------------------------------------------------------------------
    def enterEvent(self, event: QEnterEvent) -> None:
        self._animate_to(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        self._animate_to(0.0)
        super().leaveEvent(event)

    def _animate_to(self, target: float) -> None:
        self._animation.stop()
        if theme.reduced_motion():
            self._apply_hover(target)
            return
        self._animation.setStartValue(self._hover)
        self._animation.setEndValue(target)
        self._animation.start()

    def _apply_hover(self, value: object) -> None:
        progress = min(1.0, max(0.0, float(value)))  # type: ignore[arg-type]
        self._hover = progress
        self._shadow.setBlurRadius(28 + _SHADOW_BLUR_LIFT * progress)
        self._shadow.setOffset(0, 6 + _SHADOW_DY_LIFT * progress)
        rest_alpha = _SHADOW_COLOUR.alpha()
        lifted = QColor(_SHADOW_COLOUR)
        lifted.setAlpha(round(rest_alpha + (_SHADOW_ALPHA_HOVER - rest_alpha) * progress))
        self._shadow.setColor(lifted)

        if progress <= 0.001:
            # Cleared rather than pinned at the rest colour, so the global sheet's bright
            # top hairline (PANEL_EDGE_TOP) comes back exactly as it was.
            self.setStyleSheet("")
            return
        border = _mix(self._border_rest, self._border_hover, progress)
        self.setStyleSheet(f"QFrame#GlassPanel {{ border-color: {_rgba(border)}; }}")


# --------------------------------------------------------------------------------------
# Continuity
# --------------------------------------------------------------------------------------
class StackFader(QObject):
    """Fade a ``QStackedWidget``'s incoming page up over ``MOTION_BASE``.

    A stack swapping a table for an empty state is a cut between two unrelated
    full-panel images, and a cut gives the eye nothing to follow. Qt raises the new page
    instantly, so the fade is carried by the page arriving rather than by the old one
    leaving: a ``QGraphicsOpacityEffect`` on the incoming widget, animated 0 to 1, then
    torn down so nothing pays for an effect at rest.

    Instantiate it on the stack and forget it; it parents itself there and hooks
    ``currentChanged`` for the stack's lifetime. Under ``reduced_motion`` the page change
    is left exactly as Qt made it, which is the same swap with the duration at zero.
    """

    def __init__(self, stack: QStackedWidget) -> None:
        super().__init__(stack)
        self._stack = stack
        self._faded: Optional[QWidget] = None
        self._animation = QPropertyAnimation(self)
        self._animation.setPropertyName(b"opacity")
        self._animation.setDuration(theme.MOTION_BASE)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.finished.connect(self._settle)
        stack.currentChanged.connect(self._fade_in)

    def _fade_in(self, index: int) -> None:
        self._settle()
        if theme.reduced_motion():
            return
        widget = self._stack.widget(index)
        if widget is None:
            return
        effect = QGraphicsOpacityEffect(widget)
        effect.setOpacity(0.0)
        widget.setGraphicsEffect(effect)
        self._faded = widget
        self._animation.setTargetObject(effect)
        self._animation.start()

    def _settle(self) -> None:
        """Stop, un-target, and remove the effect. Idempotent, and the only teardown."""
        self._animation.stop()
        self._animation.setTargetObject(None)
        if self._faded is not None:
            # Deletes the effect Qt owns. Order matters: the animation must be off its
            # target first, or it spends a frame writing to a destroyed object.
            self._faded.setGraphicsEffect(None)
            self._faded = None


# --------------------------------------------------------------------------------------
# The designed absence
# --------------------------------------------------------------------------------------
class EmptyState(QWidget):
    """A nothing-here surface with atmosphere: glow, display heading, one line, one action.

    The glow is ``ANCHOR`` rather than any accent, so this reads the same wherever it is
    dropped and never borrows a hue that belongs to another region. It is painted behind
    the heading's own rectangle, which keeps the bloom tied to the type as the panel
    resizes instead of drifting to the geometric centre of an oddly-shaped panel.
    """

    action_clicked = pyqtSignal()

    _GLOW_PEAK = 0.42
    _GLOW_CORE_PEAK = 0.20
    # One measure for the whole block: heading and body share it, so their centres
    # coincide whatever the panel width, and the copy never runs to an unreadable line.
    _MEASURE = 440

    def __init__(
        self,
        heading: str,
        body: str,
        action_text: Optional[str] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setAutoFillBackground(False)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        pad = theme.SPACE * 3
        layout = QVBoxLayout(self)
        layout.setContentsMargins(pad, pad, pad, pad)
        layout.setSpacing(0)
        layout.addStretch(1)

        # The copy is centred by a row of stretches around a nested column, never by an
        # alignment flag on the labels themselves: a flag makes the layout hand a wrapped
        # QLabel its single-line sizeHint, and the heading then overlaps the body.
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        # 1:6:1 rather than stretch-column-stretch at equal weight: the column has to
        # claim most of the width and be capped by _MEASURE, not collapse to whatever a
        # wrapped QLabel guesses its own sizeHint should be.
        row.addStretch(1)
        column = QVBoxLayout()
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        row.addLayout(column, 6)
        row.addStretch(1)
        layout.addLayout(row)

        # Display type: large, tracking pulled in, and leading dropped below 1.0 (only
        # reachable through rich text, since QLabel has no line-height of its own).
        self.heading_label = QLabel(self)
        self.heading_label.setFont(_ui_font(22, bold=True, tracking=97.0))
        # The padding is not decoration: a document laid out below 100% leading reserves
        # less height than the glyphs actually ink, and without it the descenders clip.
        self.heading_label.setStyleSheet(
            _type_qss(theme.TEXT_PRIMARY, 22, bold=True) + " padding-bottom: 7px;"
        )
        self.heading_label.setTextFormat(Qt.TextFormat.RichText)
        self.heading_label.setText(
            f'<div style="line-height: 90%">{escape(heading)}</div>'
        )
        self.heading_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.heading_label.setWordWrap(True)
        self.heading_label.setMaximumWidth(self._MEASURE)
        # An explicit minimum overrides minimumSizeHint, which for a rich-text label is
        # wide enough to push a splitter pane out of its stretch factors. The empty state
        # wraps harder in a narrow panel instead of widening it.
        self.heading_label.setMinimumWidth(1)
        column.addWidget(self.heading_label)

        column.addSpacing(theme.SPACE)

        self.body_label = QLabel(body, self)
        self.body_label.setFont(_ui_font(10))
        self.body_label.setStyleSheet(_type_qss(theme.TEXT_SECONDARY, 10))
        self.body_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.body_label.setWordWrap(True)
        self.body_label.setMaximumWidth(self._MEASURE)
        self.body_label.setMinimumWidth(1)
        column.addWidget(self.body_label)

        self.action_button: Optional[QPushButton] = None
        if action_text:
            # Three units below the copy: the action is its own group, not a third line.
            column.addSpacing(theme.SPACE * 3)
            self.action_button = QPushButton(action_text, self)
            self.action_button.setObjectName("primary")
            self.action_button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.action_button.clicked.connect(self.action_clicked)
            panel_shadow(self.action_button, blur=18, dy=4)
            column.addWidget(self.action_button, 0, Qt.AlignmentFlag.AlignHCenter)

        layout.addStretch(1)

    def paintEvent(self, event: QPaintEvent) -> None:
        centre = QPointF(self.heading_label.geometry().center())
        radius = max(self.width() * 0.55, 200.0)
        anchor = _parse_colour(theme.ANCHOR)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect())
        painter.fillRect(rect, _glow(centre, radius, anchor, self._GLOW_PEAK))
        painter.fillRect(rect, _glow(centre, radius * 0.45, anchor, self._GLOW_CORE_PEAK))
        painter.end()
