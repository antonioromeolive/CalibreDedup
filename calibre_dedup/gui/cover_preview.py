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

"""The cover(s) of the selected row, beside the table: one book (calibre-review), or
a book and its match stacked (Merge and Dedup), to compare them at a glance."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QPixmap
from PySide6.QtWidgets import QLabel, QSizePolicy, QVBoxLayout, QWidget

from ..models import Book


def cover_file(book: Book) -> Path | None:
    """Calibre's cover.jpg of the book, if it has one."""
    path = Path(book.library, book.path, "cover.jpg")
    return path if path.is_file() else None


class _Panel(QWidget):
    def __init__(self):
        super().__init__()
        self.caption = QLabel()
        font = QFont(self.caption.font())
        font.setBold(True)
        self.caption.setFont(font)
        self.caption.setWordWrap(True)
        self.caption.setAlignment(Qt.AlignHCenter)
        self.image = QLabel()
        self.image.setAlignment(Qt.AlignCenter)
        self.image.setWordWrap(True)
        # The picture follows the panel's size; it must never push the window wider.
        self.image.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.image.setMinimumSize(1, 1)
        self.pixmap = QPixmap()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self.caption)
        layout.addWidget(self.image, 1)

    def show_book(self, caption: str, book: Book | None, empty: str):
        self.caption.setText(caption)
        self.caption.setVisible(bool(caption))
        path = cover_file(book) if book is not None else None
        self.pixmap = QPixmap(str(path)) if path else QPixmap()
        self.setToolTip(book.label() if book is not None else "")
        if self.pixmap.isNull():
            self.image.setPixmap(QPixmap())
            self.image.setText(empty if book is None else f"{book.label()}\n\n(no cover)")
        else:
            self.image.setText("")
            self.fit()

    def fit(self):
        if not self.pixmap.isNull():
            size = self.image.size()
            self.image.setPixmap(self.pixmap.scaled(size, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fit()


class CoverPreview(QWidget):
    """`show_books([(caption, book or None), …])`: one panel per entry, stacked."""

    def __init__(self, empty: str = "No book selected"):
        super().__init__()
        self.empty = empty
        self.setMinimumWidth(140)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._panels: list[_Panel] = []
        self.clear()

    def show_books(self, books: list[tuple[str, Book | None]], empty: str = "") -> None:
        while len(self._panels) < len(books):
            panel = _Panel()
            self._panels.append(panel)
            self._layout.addWidget(panel, 1)
        for i, panel in enumerate(self._panels):
            panel.setVisible(i < len(books))
            if i < len(books):
                panel.show_book(*books[i], empty or self.empty)

    def clear(self) -> None:
        self.show_books([("", None)])
