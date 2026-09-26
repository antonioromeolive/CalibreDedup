"""Styles shared by the main window and the settings dialog: buttons, inactive settings."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication, QComboBox, QPushButton, QWidget

GREEN = ("#218739", "#2ea043", "#176b2c", "#196b2d")  # colour, hover, pressed, border
BLUE = ("#1769aa", "#2186c4", "#0f4f7f", "#125a91")
RED = ("#c62828", "#d84343", "#8e1c1c", "#a01f1f")


def button_css(color: str, hover: str, pressed: str, border: str) -> str:
    """Coloured when usable, grey when not, amber while it is the one working
    (property running=true: "Analyzing…", "Executing…", "Testing…")."""
    return (
        f"QPushButton {{ background: {color}; color: white; font-weight: bold; "
        f"padding: 6px 12px; border: 1px solid {border}; border-radius: 3px; }}"
        f"QPushButton:hover {{ background: {hover}; }}"
        f"QPushButton:pressed {{ background: {pressed}; }}"
        "QPushButton:disabled { background: #7f8c96; color: #d9dee2; border-color: #6b757d; }"
        'QPushButton[running="true"], QPushButton[running="true"]:disabled '
        "{ background: #e69500; color: white; border-color: #b87600; }"
    )


def set_running(button: QPushButton, running: bool) -> None:
    if button.property("running") != running:
        button.setProperty("running", running)
        button.style().unpolish(button)  # re-evaluate the [running="true"] selector
        button.style().polish(button)


# --- "inactive" marking -----------------------------------------------------------
# A choice or setting that won't take effect (e.g. Image AI "None", or a cover check
# with no Image AI): amber italic. Dark amber on a light background, light amber on
# a dark one (Windows dark mode), so it stays readable in both.
AMBER = {"light": "#b26a00", "dark": "#ffb74d"}


def amber(widget: QWidget) -> QColor:
    dark = widget.palette().color(QPalette.Base).lightness() < 128
    return QColor(AMBER["dark" if dark else "light"])


def mark_inactive(widget: QWidget, inactive: bool) -> None:
    """Amber italic text while `inactive`, the normal look otherwise. Done with the
    font and palette: a stylesheet would drop the native look of a combo box."""
    font = widget.font()
    font.setItalic(inactive)
    widget.setFont(font)
    palette = widget.palette()
    for role in (QPalette.Text, QPalette.ButtonText, QPalette.WindowText):
        palette.setColor(role, amber(widget) if inactive else QApplication.palette().color(role))
    widget.setPalette(palette)
    if isinstance(widget, QComboBox):  # the open list keeps its own look (see style_none_item)
        # Set explicitly: a child only overrides what its own font/palette set,
        # the rest is inherited from the combo.
        view = widget.view()
        font = view.font()
        font.setItalic(False)
        view.setFont(font)
        palette = view.palette()
        for role in (QPalette.Text, QPalette.ButtonText, QPalette.WindowText):
            palette.setColor(role, QApplication.palette().color(role))
        view.setPalette(palette)


def style_none_item(combo: QComboBox, row: int = 0) -> None:
    """Show a "None" entry of the open list in amber italic."""
    font = QFont(combo.font())
    font.setItalic(True)
    combo.setItemData(row, font, Qt.FontRole)
    combo.setItemData(row, amber(combo), Qt.ForegroundRole)
