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

"""The programs' icons, drawn here (no image files): the Duplicate Remover is two
copies of a book, one of them crossed out; the Metadata Review a book under a
magnifying glass. Also the Windows taskbar identity, so the taskbar shows these
icons instead of Python's."""

from __future__ import annotations

import sys

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

SIZES = (16, 24, 32, 48, 64, 128, 256)
DEDUP, REVIEW = "dedup", "review"


def _book(p: QPainter, x: float, y: float, w: float, h: float, cover: str, spine: str) -> None:
    """A book seen from the front: cover, darker spine, white pages edge and a title band."""
    path = QPainterPath()
    path.addRoundedRect(QRectF(x, y, w, h), w * 0.08, w * 0.08)
    p.fillPath(path, QColor(cover))
    p.fillRect(QRectF(x, y + h * 0.02, w * 0.14, h * 0.96), QColor(spine))
    p.fillRect(QRectF(x + w * 0.94, y + h * 0.04, w * 0.06, h * 0.92), QColor("#f5f1e6"))
    p.fillRect(QRectF(x + w * 0.28, y + h * 0.2, w * 0.52, h * 0.1), QColor(255, 255, 255, 200))
    p.fillRect(QRectF(x + w * 0.28, y + h * 0.36, w * 0.36, h * 0.06), QColor(255, 255, 255, 150))


def _draw(p: QPainter, kind: str) -> None:
    """On a 256 x 256 canvas."""
    if kind == DEDUP:
        _book(p, 30, 26, 128, 176, "#90a4ae", "#607d8b")  # the copy that goes
        _book(p, 98, 62, 128, 176, "#1e88e5", "#1565c0")  # the copy that stays
        p.setPen(QPen(QColor("#e53935"), 22, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(QPointF(42, 48), QPointF(86, 92))
        p.drawLine(QPointF(86, 48), QPointF(42, 92))
    else:
        _book(p, 40, 24, 140, 196, "#43a047", "#2e7d32")
        p.setPen(QPen(QColor("#37474f"), 26, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(QPointF(196, 196), QPointF(236, 236))  # handle
        p.setPen(QPen(QColor("#37474f"), 16))
        p.setBrush(QColor(255, 255, 255, 140))
        p.drawEllipse(QPointF(160, 160), 52, 52)


def app_icon(kind: str) -> QIcon:
    icon = QIcon()
    for size in SIZES:
        pix = QPixmap(size, size)
        pix.fill(Qt.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing)
        p.scale(size / 256, size / 256)
        _draw(p, kind)
        p.end()
        icon.addPixmap(pix)
    return icon


def set_taskbar_identity(kind: str) -> None:
    """Windows groups a program's windows on the taskbar by this id: without it both
    programs show under pythonw.exe, with Python's icon. Call before the first window."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(f"CalibreDedup.{kind}")
    except (AttributeError, OSError):
        pass
