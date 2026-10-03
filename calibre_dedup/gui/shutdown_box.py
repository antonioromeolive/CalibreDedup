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

"""The "Shut down the PC when done" box of both windows (see shutdown.py): one switch for
every window running, asked for confirmation, and shown in each by a red banner across the
top and a marked window title while it is on."""

from __future__ import annotations

import logging
import time
from typing import Callable

from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtWidgets import QCheckBox, QLabel, QMainWindow, QMessageBox

from ..shutdown import NOTHING, OTHER_SHUTS_DOWN, SHUT_DOWN, WAIT, Instance, decide, shut_down_computer

log = logging.getLogger(__name__)
COUNTDOWN = 60  # seconds to cancel, once the work is done
CHECK_MS = 2000  # how often the other windows (and the shared switch) are looked at
TITLE_MARK = "⚠ SHUTDOWN WHEN DONE — "
BANNER_CSS = ("QLabel { background: #c62828; color: white; font-weight: bold; font-size: 11pt; "
              "padding: 6px; border-radius: 3px; }")
BOX_ON_CSS = "QCheckBox { color: #c62828; font-weight: bold; }"
OTHERS_CLOSE_SECONDS = 20  # how long the other windows are given to close


class ShutdownWhenDone(QObject):
    """`window` closes itself when another window shuts the computer down (it must be idle);
    its `_close_pending` skips its "Close?" question. `status(text)` shows what is awaited."""

    def __init__(self, window: QMainWindow, program: str, status: Callable[[str], None]):
        super().__init__(window)
        self.window, self.status = window, status
        self.box = QCheckBox("Shut down the PC when done")
        self.box.setToolTip(
            "When the analysis or execution running now (or the next one) ends, shut the computer down.\n"
            "Other windows of Merge and Dedup or Metadata Review still working are waited for; the last\n"
            "one to finish shuts down, after a minute you can cancel. Idle windows close themselves.")
        try:
            self.instance: Instance | None = Instance(program)
        except OSError as e:
            log.warning("Shut down when done unavailable: %s", e)
            self.instance = None
            self.box.setEnabled(False)
        self.box.toggled.connect(self._toggled)
        # Very visible while on: a banner the window puts across its top, and its title.
        self.banner = QLabel()
        self.banner.setStyleSheet(BANNER_CSS)
        self.banner.setAlignment(Qt.AlignCenter)
        self.banner.setVisible(False)
        self._title = ""
        self._syncing = False  # the switch changed in another window: no question
        self._timer = QTimer(self, interval=CHECK_MS)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self._countdown: QMessageBox | None = None
        self._left = 0
        self._closing_since = 0.0  # asked the others to close: when

    def set_busy(self, busy: bool, activity: str = "") -> None:
        if self.instance is not None:
            self.instance.update(busy=busy, activity=activity if busy else "")
            if busy:
                self._cancel_countdown("a new run started")

    def run_ended(self) -> None:
        """A run of this window ended (analysis, execution, the AI asked again)."""
        if self.instance is not None and self.box.isChecked():
            self.instance.update(ended=time.time())
            log.info("Run ended: shutting down when no other window is working")
            self._tick()

    def _toggled(self, on: bool) -> None:
        if self.instance is None:
            return
        if on and not self._syncing and QMessageBox.question(
                self.window, "Shut down the PC when done",
                "Shut the computer down when the work is done?\n\n"
                "This is turned on in every Merge and Dedup and Metadata Review window running. When the "
                "analysis or execution running now (or the next one) ends, and no other window is still "
                "working, the computer shuts down after a minute you can cancel. Idle windows close "
                "themselves; other programs with unsaved work may still ask.") != QMessageBox.Yes:
            self.box.blockSignals(True)
            self.box.setChecked(False)
            self.box.blockSignals(False)
            return
        if not self._syncing:
            self.instance.set_armed(on)  # the other windows follow
            log.info("Shut down when done turned %s (in every window)", "on" if on else "off")
        # Only a run that ends from now on: ticked when idle, the computer waits for the next one.
        self.instance.update(shutdown=on, ended=0.0)
        if not on:
            self._cancel_countdown("unticked")
            self.status("")
        self._show_armed(on)

    def _show_armed(self, on: bool) -> None:
        self.box.setStyleSheet(BOX_ON_CSS if on else "")
        self.banner.setText("⚠  The PC will SHUT DOWN when all the work is done (every window)  ⚠"
                            if on else "")
        self.banner.setVisible(on)
        title = self.window.windowTitle()
        if on and not title.startswith(TITLE_MARK):
            self.window.setWindowTitle(TITLE_MARK + title)
        elif not on and title.startswith(TITLE_MARK):
            self.window.setWindowTitle(title[len(TITLE_MARK):])

    def _sync(self) -> None:
        """Follow the switch as another window set it."""
        armed = self.instance.armed()
        if armed != self.box.isChecked():
            self._syncing = True
            try:
                self.box.setChecked(armed)
            finally:
                self._syncing = False

    def _tick(self) -> None:
        inst = self.instance
        if inst is None:
            return
        if self._closing_since:  # shutting down: _finish_shutdown follows the other windows
            return
        self._sync()
        if inst.shutdown_requested() and not inst.state.busy:
            log.info("Another window is shutting the computer down: closing")
            self._timer.stop()
            self.window._close_pending = True
            self.window.close()
            return
        action, who = decide(inst.state, inst.others())
        if action != SHUT_DOWN:
            self._cancel_countdown("another window is working")
        if action == WAIT:
            self.status("Shut down when done: waiting for " + ", ".join(o.describe() for o in who))
        elif action == OTHER_SHUTS_DOWN:
            self.status("Shut down when done: the window finishing last will do it")
        elif action == SHUT_DOWN and self._countdown is None:
            self._start_countdown()
        elif action == NOTHING:
            pass

    def _start_countdown(self) -> None:
        self._left = COUNTDOWN
        box = QMessageBox(QMessageBox.Warning, "Shut down", "", QMessageBox.Cancel, self.window)
        box.setModal(False)
        box.finished.connect(lambda _: self._cancel_countdown("cancelled", untick=True))  # Cancel, or closed
        self._countdown = box
        timer = QTimer(box, interval=1000)
        timer.timeout.connect(self._count)
        timer.start()
        self._show_left()
        box.show()
        log.info("Work done: the computer shuts down in %d s unless cancelled", COUNTDOWN)

    def _show_left(self) -> None:
        if self._countdown is not None:
            self._countdown.setText(f"The work is done: the computer shuts down in {self._left} s.")

    def _count(self) -> None:
        self._left -= 1
        self._show_left()
        if self._left <= 0 and self._countdown is not None:
            box, self._countdown = self._countdown, None
            box.blockSignals(True)
            box.close()
            box.deleteLater()
            self.instance.request_shutdown()  # the idle windows close themselves
            self._closing_since = time.monotonic()
            self.status("Shutting down: closing the other windows…")
            self._finish_shutdown()

    def _finish_shutdown(self) -> None:
        others = self.instance.others()
        if others and time.monotonic() - self._closing_since < OTHERS_CLOSE_SECONDS:
            QTimer.singleShot(1000, self._finish_shutdown)
            return
        self._timer.stop()
        self.instance.clear_request()
        self.instance.set_armed(False)  # not on in the next session
        shut_down_computer()
        self.window._close_pending = True
        self.window.close()

    def _cancel_countdown(self, why: str, untick: bool = False) -> None:
        if self._countdown is None:
            return
        box, self._countdown = self._countdown, None
        box.blockSignals(True)
        box.close()
        box.deleteLater()
        log.info("Shut down cancelled: %s", why)
        if untick:
            self.box.setChecked(False)

    def close(self) -> None:
        self._timer.stop()
        if self.instance is not None:
            self.instance.close()
