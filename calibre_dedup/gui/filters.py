# Copyright (c) 2026 Antonio Romeo <antonioromeo@ilve.it>
# Author: Antonio Romeo (with Claude Code et al.)
# SPDX-License-Identifier: MIT
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

"""The list filters of both windows: drop-down buttons (Actions, Books, Status) whose
entries are ticked in any combination. Within a button a book is shown when it
matches ANY ticked entry; nothing ticked is no filter. Between buttons, and with the
search box, a book must pass them all."""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QMenu, QPushButton, QToolButton

Entry = tuple[str, str, str]  # key, label, tooltip

# The same size in both looks (same border width, padding, font), so nothing in the row moves.
BUTTON_CSS = ("QToolButton {{ padding: 5px 24px 5px 14px; border: 2px solid {border}; border-radius: 4px; "
              "background: {background}; min-width: 80px; }}"
              "QToolButton::menu-indicator {{ subcontrol-origin: padding; subcontrol-position: right center; "
              "right: 8px; }}")
DEFAULT_CSS = BUTTON_CSS.format(border="palette(mid)", background="palette(button)")
CHANGED_CSS = BUTTON_CSS.format(border="#2186c4", background="rgba(33, 134, 196, 70)")
SHOWING_SAMPLE = "Showing 999,999 of 999,999 books"  # sizes the "Showing" text, so that it never moves


class _StayOpenMenu(QMenu):
    """Ticking an entry keeps the list open, so that several can be ticked in a row."""

    def mouseReleaseEvent(self, event):
        action = self.activeAction()
        if action is not None and action.isEnabled() and action.isCheckable():
            action.trigger()
            return
        super().mouseReleaseEvent(event)


class FilterButton(QToolButton):
    changed = Signal()

    def __init__(self, name: str, tooltip: str, entries: list[Entry],
                 counts: Callable[[], dict[str, int]] | None = None, default: set[str] | None = None):
        """`counts`: books per entry, shown next to each when the list opens. `default`:
        the entries ticked at the start; the button is highlighted when the ticks differ."""
        super().__init__()
        self.name = name
        self._tip = tooltip
        self._labels = {key: label for key, label, _ in entries}
        self._counts = counts
        self._default = set(default or ())
        self.setText(name)
        self.setPopupMode(QToolButton.InstantPopup)
        menu = _StayOpenMenu(self)
        menu.setToolTipsVisible(True)
        everything = menu.addAction("All")
        everything.setToolTip("No filter: untick everything below.")
        everything.triggered.connect(lambda: self.set_selected(set()))
        menu.addSeparator()
        self._actions = {}
        for key, label, tip in entries:
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setToolTip(tip)
            action.toggled.connect(self._toggled)
            self._actions[key] = action
        menu.aboutToShow.connect(self._show_counts)
        self.setMenu(menu)
        self.set_selected(self._default)

    def selected(self) -> set[str]:
        """The ticked entries (of those shown); empty: no filter."""
        return {k for k, a in self._actions.items() if a.isChecked() and a.isVisible()}

    def set_selected(self, keys: set[str]) -> None:
        for key, action in self._actions.items():
            action.blockSignals(True)
            action.setChecked(key in keys)
            action.blockSignals(False)
        self._toggled()

    def set_shown(self, keys: set[str]) -> None:
        """Show only these entries (e.g. no Move when source and target are one library)."""
        for key, action in self._actions.items():
            action.setVisible(key in keys)
        self._toggled()

    def set_default(self, keys: set[str]) -> None:
        """Tick these entries, and take them as the default from now on."""
        self._default = set(keys)
        self.set_selected(self._default)

    def is_default(self) -> bool:
        shown = {k for k, a in self._actions.items() if a.isVisible()}

        def effect(keys: set[str]) -> set[str]:  # everything ticked filters nothing, like nothing ticked
            keys = keys & shown
            return set() if keys == shown else keys

        return effect(self.selected()) == effect(self._default)

    def _toggled(self, *_) -> None:
        self._refresh()
        self.changed.emit()

    def _summary(self) -> str:
        shown = [k for k, a in self._actions.items() if a.isVisible()]
        on = [k for k in shown if self._actions[k].isChecked()]
        if not on or len(on) == len(shown):
            return "all"
        return ", ".join(self._labels[k] for k in on)

    def _refresh(self) -> None:
        default = self.is_default()
        self.setStyleSheet(DEFAULT_CSS if default else CHANGED_CSS)
        self.setToolTip(f"{self._tip}\n\nShowing: {self._summary()}" + (" (the default)" if default else ""))

    def _show_counts(self) -> None:
        if self._counts is None:
            return
        counts = self._counts()
        for key, action in self._actions.items():
            action.setText(f"{self._labels[key]}  ({counts.get(key, 0):,})")


STATUS_ENTRIES: list[Entry] = [
    ("checked", "Checked", "Ticked: Execute will act on them."),
    ("unchecked", "Not checked", "Not ticked: Execute leaves them as they are."),
    ("done", "Done", "Executed successfully in this session."),
    ("failed", "Failed", "Execute tried and failed: the reason is in the Result column."),
]
STATUS_TIP = "Status: show the books in any of the states ticked here. Nothing ticked: all books."


def status_keys(checked: bool, status: str) -> set[str]:
    keys = {"checked" if checked else "unchecked"}
    if status.startswith("OK"):
        keys.add("done")
    elif status.startswith("FAILED"):
        keys.add("failed")
    return keys


SEARCH_WIDTH = 360


def filter_row(search: QLineEdit, buttons: list[FilterButton], clear: QPushButton, showing: QLabel) -> QHBoxLayout:
    """Search, the lists, Clear filters and "Showing …", at fixed widths: ticking an
    entry or a growing count never moves anything in the row."""
    search.setMaximumWidth(SEARCH_WIDTH)
    showing.setFixedWidth(showing.fontMetrics().horizontalAdvance(SHOWING_SAMPLE) + 12)
    row = QHBoxLayout()
    row.addWidget(search, 1)
    for button in buttons:
        row.addWidget(button)
    row.addWidget(clear)
    row.addSpacing(8)
    row.addWidget(showing)
    row.addStretch(1)
    return row


def showing_text(shown: int, total: int) -> str:
    return f"Showing {shown:,} of {total:,} books"
