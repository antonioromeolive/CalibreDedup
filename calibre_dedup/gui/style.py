"""Button styles shared by the main window and the settings dialog."""

from __future__ import annotations

from PySide6.QtWidgets import QPushButton

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
