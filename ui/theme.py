"""Forced light Fusion theme for the UI.

Applies a deterministic light palette via Qt Fusion so the app renders the
same regardless of the user's system theme (e.g. GNOME dark, Plasma Breeze
Dark).  Also exposes ``STATUS_LED_QSS`` — small stylesheet snippets shared by
panels that show a connection-state indicator.
"""
from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication


# Stylesheet snippets for the connection-status LED.  All four share the same
# circle geometry; only the fill colour changes so widgets can swap without
# reflow.
_LED_GEOM = (
    "border-radius: 7px; "
    "min-width: 14px; min-height: 14px; "
    "max-width: 14px; max-height: 14px;"
)

STATUS_LED_QSS: dict[str, str] = {
    "off":  f"background-color: #888; {_LED_GEOM}",
    "ok":   f"background-color: #2ECC71; {_LED_GEOM}",
    "warn": f"background-color: #F1C40F; {_LED_GEOM}",
    "err":  f"background-color: #E74C3C; {_LED_GEOM}",
}


def apply_light_theme(app: QApplication) -> None:
    """Force a Fusion light palette regardless of the system theme.

    Call once after :class:`QApplication` construction.
    """
    app.setStyle("Fusion")

    palette = QPalette()

    # Window chrome / panels.
    palette.setColor(QPalette.ColorRole.Window, QColor("#F0F0F0"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#202020"))

    # Inputs (line edits, list views, etc).
    palette.setColor(QPalette.ColorRole.Base, QColor("#FFFFFF"))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#F7F7F7"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#202020"))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor("#808080"))

    # Tooltips.
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor("#FFFFE1"))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor("#202020"))

    # Buttons.
    palette.setColor(QPalette.ColorRole.Button, QColor("#E0E0E0"))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor("#202020"))
    palette.setColor(QPalette.ColorRole.BrightText, QColor("#FF0000"))

    # Selection / highlight.
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#2A82DA"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#FFFFFF"))

    # Hyperlinks.
    palette.setColor(QPalette.ColorRole.Link, QColor("#2A82DA"))
    palette.setColor(QPalette.ColorRole.LinkVisited, QColor("#7E57C2"))

    # Disabled-state overrides — Fusion's default "greyed out" doesn't always
    # render well on a forced light palette, so spell these out.
    disabled_text = QColor("#A0A0A0")
    palette.setColor(
        QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, disabled_text
    )
    palette.setColor(
        QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, disabled_text
    )
    palette.setColor(
        QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, disabled_text
    )
    palette.setColor(
        QPalette.ColorGroup.Disabled,
        QPalette.ColorRole.HighlightedText,
        disabled_text,
    )
    palette.setColor(
        QPalette.ColorGroup.Disabled,
        QPalette.ColorRole.Highlight,
        QColor("#C0C0C0"),
    )

    app.setPalette(palette)
