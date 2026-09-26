"""Estimated time left for a long run (analysis, review scan).

Books go at very different speeds: without AI or from the cache in milliseconds,
with an AI call in seconds or minutes. The rate is therefore measured over the
last few minutes only (it follows a stretch of AI books, or of cached ones), and
the shown estimate changes at most every REFRESH seconds, so it doesn't jump.
Cheap enough to call for every book: one comparison, sometimes one division.
"""

from __future__ import annotations

import time
from collections import deque

WINDOW = 300.0  # seconds of progress the rate is measured over
SAMPLE_EVERY = 5.0  # seconds between two samples kept for the window
REFRESH = 10.0  # seconds between two changes of the shown estimate
MIN_ELAPSED = 20.0  # no estimate before this (the first books say little)
MIN_DONE = 3


class Eta:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.reset()

    def reset(self) -> None:
        self._samples: deque[tuple[float, int]] = deque()  # (time, books done)
        self._started = self.clock()
        self._done = self._total = 0
        self._shown, self._shown_at = "", float("-inf")

    def update(self, done: int, total: int) -> None:
        """Called for every book; keeps a sample only every SAMPLE_EVERY seconds."""
        now = self.clock()
        if self._samples and done < self._samples[-1][1]:  # a new run: start again
            self.reset()
        self._done, self._total = done, total
        if not self._samples or now - self._samples[-1][0] >= SAMPLE_EVERY:
            self._samples.append((now, done))
            while len(self._samples) > 2 and now - self._samples[1][0] >= WINDOW:
                self._samples.popleft()

    def seconds_left(self) -> float | None:
        now = self.clock()
        if now - self._started < MIN_ELAPSED or self._done < MIN_DONE or not self._samples:
            return None
        if self._done >= self._total:
            return 0.0
        t0, d0 = self._samples[0]
        if now - t0 < MIN_ELAPSED:  # the window was just restarted
            t0, d0 = self._started, 0
        books = self._done - d0
        if books <= 0:
            return None  # nothing finished in the whole window: can't tell
        return (self._total - self._done) * (now - t0) / books

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
