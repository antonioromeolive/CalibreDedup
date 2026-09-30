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

import pytest

pytest.importorskip("PySide6")
from calibre_dedup.gui.main_window import has_no_duplicate  # noqa: E402
from calibre_dedup.models import Action, Book, Identity, PlanItem  # noqa: E402

OTHER = Book(99, "Dune", ["Frank Herbert"], None, None, set(), {}, "u99", "p99", "L")


def item(action, title="Dune", authors=("Frank Herbert",), match=None):
    book = Book(1, title, list(authors), None, None, set(), {"EPUB": "x"}, "u1", "p1", "L")
    return PlanItem(book, action, "reason", Identity(title=title or None, authors=list(authors)), match=match)


def test_no_duplicate_means_left_with_nothing_to_compare_or_only_different_editions():
    assert has_no_duplicate(item(Action.LEAVE))


@pytest.mark.parametrize("it", [
    item(Action.LEAVE, match=OTHER),              # undecided pair: to review
    item(Action.LEAVE, title="", authors=()),     # title/authors unreadable: to review
    item(Action.MOVE),
    item(Action.TRASH, match=OTHER),
])
def test_everything_else_is_not_counted_as_no_duplicate(it):
    assert not has_no_duplicate(it)


def test_a_different_edition_left_in_place_still_counts_as_no_duplicate():
    it = item(Action.LEAVE, match=OTHER)
    it.different = True
    assert has_no_duplicate(it)


def test_time_left_goes_before_the_title():
    from calibre_dedup.gui.main_window import with_eta
    assert with_eta("Book 12 of 1500: Analyzing Dune: part 1", "about 3 h 10 min left") == \
        "Book 12 of 1500 · about 3 h 10 min left: Analyzing Dune: part 1"
    assert with_eta("Book 12 of 1500: Analyzing Dune", "") == "Book 12 of 1500: Analyzing Dune"
    assert with_eta("Reading libraries", "about 1 h 00 min left") == "Reading libraries"
