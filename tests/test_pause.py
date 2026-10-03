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

"""Pause and Continue between two books (pause.py), in the analysis and the review."""

import threading
import time

from calibre_dedup.pause import Pause
from calibre_dedup.planner import build_plan
from tests.test_planner import libs  # noqa: F401  (a fixture)


def _wait_for(condition, seconds=5.0):
    end = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < end, "timed out"
        time.sleep(0.01)


def test_a_paused_analysis_waits_between_two_books_and_goes_on(libs):
    src, tgt, trash = libs(source=[{"title": "Dune"}, {"title": "Dune Messiah"}, {"title": "Emma"}], target=[])
    waits = []
    pause = Pause(on_wait=waits.append)
    seen = []

    def on_item(item):
        seen.append(item.source.title)
        if len(seen) == 1:
            pause.pause()  # pressed while the first book is analyzed

    result = {}
    run = threading.Thread(target=lambda: result.setdefault("plan", build_plan(
        src, tgt, trash, on_item=on_item, pause=pause)))
    run.start()
    _wait_for(lambda: waits == [True])
    assert len(seen) == 1 and run.is_alive()  # waiting before the second book
    pause.resume()
    run.join(5)
    assert waits == [True, False] and len(result["plan"].items) == 3 and not result["plan"].stopped


def test_stop_ends_a_pause(libs):
    src, tgt, trash = libs(source=[{"title": "Dune"}, {"title": "Emma"}], target=[])
    pause, cancel = Pause(), threading.Event()
    pause.pause()
    result = {}
    run = threading.Thread(target=lambda: result.setdefault("plan", build_plan(
        src, tgt, trash, cancel=cancel, pause=pause)))
    run.start()
    time.sleep(0.3)
    assert run.is_alive()
    cancel.set()
    run.join(5)
    assert result["plan"].stopped and result["plan"].items == []
