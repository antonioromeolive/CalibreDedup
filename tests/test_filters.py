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

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from calibre_dedup.ai import ReviewMetadata  # noqa: E402
from calibre_dedup.config import Settings  # noqa: E402
from calibre_dedup.gui import main_window as dedup, review_window as review  # noqa: E402
from calibre_dedup.gui.filters import CHANGED_CSS, DEFAULT_CSS, FilterButton, status_keys  # noqa: E402
from calibre_dedup.models import Action, Book, Identity, PlanItem  # noqa: E402
from calibre_dedup.review import ReviewAction, ReviewItem  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def book(bid, **kw) -> Book:
    return Book(bid, f"T{bid}", ["A"], None, None, set(), {"EPUB": "x.epub"}, f"u{bid}", "", "lib", **kw)


def shown(window) -> list[int]:
    p = window.proxy
    return sorted(p.mapToSource(p.index(r, 0)).row() for r in range(p.rowCount()))


def test_button_is_highlighted_when_not_on_its_default(app):
    entries = [("a", "A", ""), ("b", "B", ""), ("c", "C", ""), ("d", "D", "")]
    b = FilterButton("Books", "", entries)
    assert b.text() == "Books" and b.is_default() and b.styleSheet() == DEFAULT_CSS
    b.set_selected({"a"})
    assert b.text() == "Books" and not b.is_default() and b.styleSheet() == CHANGED_CSS
    assert "Showing: A" in b.toolTip()
    b.set_selected({"a", "b", "c", "d"})  # everything ticked filters nothing, like nothing ticked
    assert b.is_default()
    b.set_shown({"a", "b"})  # hidden entries are never selected
    assert b.selected() == {"a", "b"}
    with_default = FilterButton("Books", "", entries, default={"a"})
    assert with_default.selected() == {"a"} and with_default.is_default()
    with_default.set_selected(set())  # showing everything is not the default here
    assert not with_default.is_default()


def test_status_keys():
    assert status_keys(True, "") == {"checked"}
    assert status_keys(False, "OK: updated") == {"unchecked", "done"}
    assert status_keys(True, "FAILED: locked") == {"checked", "failed"}


def test_review_lists_combine_any_within_and_all_between(app):
    w = review.ReviewWindow(Settings(), dedup.QtLogHandler())
    changed = ReviewItem(book(1), ReviewMetadata(title="Other", authors=["A"]))  # Update, ticked
    unread = ReviewItem(book(2), None, "no text")
    same = ReviewItem(book(3), ReviewMetadata(title="T3", authors=["A"]))  # nothing to change: "other"
    generic = ReviewItem(book(4), ReviewMetadata(title="T4", authors=["A"]), generic_cover=34)
    w.model.reset([changed, unread, same, generic])
    rows = {id(it): w.model.items.index(it) for it in (changed, unread, same, generic)}
    assert shown(w) == [rows[id(changed)]]  # default: with differences
    assert w.showing_label.text() == "Showing 1 of 4 books"
    w.book_filter.set_selected({"unread", "generic"})
    assert shown(w) == sorted([rows[id(unread)], rows[id(generic)]])
    w.book_filter.set_selected({k for k, _, _ in review.BOOK_ENTRIES})  # every entry: every book
    assert len(shown(w)) == 4
    w.action_filter.set_selected({ReviewAction.KEEP.value})
    assert rows[id(changed)] not in shown(w) and len(shown(w)) == 3
    w.status_filter.set_selected({"checked"})
    assert shown(w) == []  # kept books are never checked
    w._clear_filters()
    assert len(shown(w)) == 4 and w.showing_label.text() == "Showing 4 of 4 books"


def test_dedup_same_library_starts_without_the_books_with_no_duplicate(app):
    w = dedup.MainWindow(Settings(), dedup.QtLogHandler())
    unique = PlanItem(book(1), Action.LEAVE, "no duplicate", Identity(title="T1", authors=["A"]))
    undecided = PlanItem(book(2), Action.LEAVE, "edition unknown", Identity(title="T2", authors=["A"]),
                         match=book(3))
    trash = PlanItem(book(4), Action.TRASH, "dup", Identity(title="T4", authors=["A"]), match=book(5))
    assert dedup.action_key(unique) == dedup.LEAVE_UNIQUE and dedup.action_key(undecided) == "leave"
    assert dedup.action_key(trash) == "trash"
    w._set_action_entries(True)
    assert dedup.LEAVE_UNIQUE not in w.action_filter.selected()
    assert Action.MOVE.value not in w.action_filter.selected()  # one library: nothing moves
    assert w.action_filter.is_default()  # hiding them is the default here: not highlighted
    w._clear_filters()
    assert w.action_filter.selected() == set() and not w.action_filter.is_default()


def test_review_ask_the_ai_with_one_profile_leaves_the_choice_above(app):
    from calibre_dedup.config import ProviderProfile
    s = Settings(profiles=[ProviderProfile(name="G", vision=True), ProviderProfile(name="T")],
                 text_profile="T", image_profile="G")
    w = review.ReviewWindow(s, dedup.QtLogHandler())
    choices = w._ask_choices()
    assert choices[0] == ("With the AIs selected above (T + G)", None)
    assert [name for _, name in choices[1:]] == ["G", "T"]
    assert "text and covers" in choices[1][0] and "text only" in choices[2][0]
    vision, text = w._ask_settings("G"), w._ask_settings("T")
    assert (vision.text_profile, vision.image_profile) == ("G", "G")
    assert (text.text_profile, text.image_profile) == ("T", "")
    above = w._ask_settings(None)
    assert (above.text_profile, above.image_profile) == ("T", "G")
    assert (s.text_profile, s.image_profile) == ("T", "G")  # the window's own settings are untouched
