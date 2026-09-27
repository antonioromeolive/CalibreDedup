# Copyright (c) 2026 Antonio Romeo <antonioromeo@ilve.it>
# Author: Antonio Romeo
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

from calibre_dedup.eta import Eta, format_duration


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def run(eta: Eta, clock: Clock, books: range, seconds_per_book: float, total: int):
    for done in books:
        clock.now += seconds_per_book
        eta.update(done, total)


def test_no_estimate_at_the_start():
    clock = Clock()
    eta = Eta(clock)
    run(eta, clock, range(1, 4), 1, 1000)
    assert eta.seconds_left() is None and eta.text() == ""


def test_steady_rate():
    clock = Clock()
    eta = Eta(clock)
    run(eta, clock, range(1, 101), 2, 1000)  # 2 s per book, 900 left
    assert abs(eta.seconds_left() - 1800) < 60
    assert eta.text() == "about 30 min left"


def test_rate_follows_the_last_minutes_not_the_whole_run():
    clock = Clock()
    eta = Eta(clock)
    run(eta, clock, range(1, 5001), 0.01, 6000)  # cached books: fast
    run(eta, clock, range(5001, 5201), 10, 6000)  # then AI books: 10 s each, for 2000 s
    assert abs(eta.seconds_left() - 800 * 10) < 800  # not the fast average of the whole run


def test_shown_text_changes_at_most_every_ten_seconds():
    clock = Clock()
    eta = Eta(clock)
    run(eta, clock, range(1, 101), 2, 1000)
    first = eta.text()
    run(eta, clock, range(101, 104), 3, 1000)  # 9 s later, slower: not shown yet
    assert eta.text() == first
    clock.now += 2
    assert eta.text() != "" and eta._shown_at == clock.now


def test_a_new_run_starts_again():
    clock = Clock()
    eta = Eta(clock)
    run(eta, clock, range(1, 101), 2, 1000)
    eta.update(0, 500)
    assert eta.seconds_left() is None


def test_durations():
    assert format_duration(30) == "under 1 min"
    assert format_duration(125) == "2 min"
    assert format_duration(3 * 3600 + 600) == "3 h 10 min"
    assert format_duration(50 * 3600) == "2 d 2 h"
