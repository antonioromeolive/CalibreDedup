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

"""Pause and Continue for a long run (an analysis, a review): the run stops between two
books, after the current one, and waits. Stop ends the wait (the run then stops as
usual). The time paused is left out of the run's duration in the performance log."""

from __future__ import annotations

import logging
import threading
from typing import Callable

from . import perf

log = logging.getLogger(__name__)


class Pause:
    """`on_wait(waiting)`: called on the run's thread when it starts waiting (True), and
    when it goes on (False), e.g. to tell the window "Paused"."""

    def __init__(self, on_wait: Callable[[bool], None] | None = None):
        self._go = threading.Event()
        self._go.set()
        self.on_wait = on_wait

    @property
    def paused(self) -> bool:
        return not self._go.is_set()

    def pause(self) -> None:
        self._go.clear()

    def resume(self) -> None:
        self._go.set()

    def wait(self, cancel: threading.Event | None = None) -> None:
        """Called by the run before each book: while paused, wait for Continue or Stop."""
        if self._go.is_set():
            return
        log.info("Paused")
        perf.pause()
        if self.on_wait is not None:
            self.on_wait(True)
        try:
            while not self._go.wait(0.2):
                if cancel is not None and cancel.is_set():
                    break
        finally:
            perf.resume()
            if self.on_wait is not None:
                self.on_wait(False)
        log.info("Stopped while paused" if cancel is not None and cancel.is_set() else "Continued")


def wait_if_paused(pause: Pause | None, cancel: threading.Event | None) -> None:
    if pause is not None:
        pause.wait(cancel)
