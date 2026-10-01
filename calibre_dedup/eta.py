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

"""Estimated time left for a long run (analysis, review scan).

Books go at very different speeds: without AI or from the cache in milliseconds,
with an AI call in seconds or minutes, and the library mixes them (books read by
an earlier run come from the cache, wherever they are). So the rate is the whole
run's: time since the first book / books done. A window over the last minutes was
tried: it follows every stretch (minutes during cached books, hours during scanned
PDFs) and was further off (loc-ita2 review of 2026-10-01: 56-73% mean error against
39-58%). Its cost: after a long stretch of cached books it stays too low, until the
new books outweigh them. The shown estimate changes at most every REFRESH seconds.
Cheap enough to call for every book: one comparison, sometimes one division.
"""

from __future__ import annotations

import time

REFRESH = 10.0  # seconds between two changes of the shown estimate
MIN_ELAPSED = 20.0  # no estimate before this (the first books say little)
MIN_DONE = 3


class Eta:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.reset()

    def reset(self) -> None:
        self._start: tuple[float, int] | None = None  # (time, books done) at the first book
        self._done = self._total = 0
        self._shown, self._shown_at = "", float("-inf")

    def update(self, done: int, total: int) -> None:
        """Called for every book. The clock starts at the first call, so what came
        before the first book (reading the library, questions) isn't counted."""
        if self._start is not None and done < self._done:  # a new run: start again
            self.reset()
        if self._start is None:
            self._start = (self.clock(), done)
        self._done, self._total = done, total

    def seconds_left(self) -> float | None:
        if self._start is None:
            return None
        t0, d0 = self._start
        elapsed, books = self.clock() - t0, self._done - d0
        if elapsed < MIN_ELAPSED or books < MIN_DONE:
            return None
        if self._done >= self._total:
            return 0.0
        return (self._total - self._done) * elapsed / books

    def text(self) -> str:
        """"about 2 h 10 min left", refreshed at most every REFRESH seconds; "" when unknown."""
        now = self.clock()
        if now - self._shown_at >= REFRESH:
            left = self.seconds_left()
            self._shown = "" if left is None else f"about {format_duration(left)} left"
            self._shown_at = now
        return self._shown


def format_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return "under 1 min"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} h {minutes:02d} min"
    days, hours = divmod(hours, 24)
    return f"{days} d {hours} h"
