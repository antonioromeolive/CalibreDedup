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

"""The same text, whatever the files: same_text.py and the planner's use of it."""

from calibre_dedup.models import Action
from calibre_dedup.planner import build_plan
from calibre_dedup.same_text import SAME_TEXT, fingerprint, share, words
from tests.test_planner import FakeResolver, LibraryResolver, libs, prose  # noqa: F401  (a fixture)


def test_copies_of_one_text_are_made_uniform():
    # apostrophes of any kind, Windows-1252's read as Latin-1, accents right, wrong or mis-encoded, old accents
    assert words("C'era perché più d\x92una cosa") == words("C’era perchè piů d’una cosa") == \
        words("C`era perche' più d'una cosa")
    assert words("una pa-\nrola") == ["una", "parola"]


def test_the_same_text_in_two_files_is_the_same():
    text = prose("one")
    other = text.replace(" ", "  ").replace(".", ". \n")  # another layout
    assert share(fingerprint(text), fingerprint(other)) == 1.0
    assert share(fingerprint(text), fingerprint(prose("two"))) < 0.2


def test_an_excerpt_or_an_abridged_text_is_not_the_same():
    whole = prose("one", 4000)
    assert share(fingerprint(whole), fingerprint(whole[: len(whole) // 3])) < SAME_TEXT


def test_text_without_language_or_too_short_has_no_fingerprint():
    garbage = " ".join("0 A 3 -7 0 JM B6 :RL && 1 M (: 0 J8 &" for _ in range(300))
    assert fingerprint(garbage) is None
    assert fingerprint(prose("one", 100)) is None


def test_same_text_in_another_format_is_a_duplicate(libs):
    txt = prose("Dune").encode()
    src, tgt, trash = libs(source=[{"title": "Dune", "files": {"TXT": txt}}],
                           target=[{"title": "Dune", "publisher": "Ace", "prose": "Dune"}])
    item = build_plan(src, tgt, trash).items[0]
    assert item.action is Action.TRASH and "same text (100%)" in item.reason


def test_one_format_each_and_formats_the_target_lacks_are_merged(libs):
    src, tgt, trash = libs(source=[{"title": "Dune", "prose": "Dune", "formats": ["EPUB", "MOBI"]}],
                           target=[{"title": "Dune", "prose": "Dune"}])
    item = build_plan(src, tgt, trash).items[0]
    assert item.action is Action.TRASH and "same text" in item.reason and item.add_formats == ["MOBI"]


def test_a_pair_still_undecided_at_the_end_is_compared_by_text(libs):
    # Only Calibre's years differ, the AI finds none: undecided, unless the texts are the same.
    src, tgt, trash = libs(source=[{"title": "Dune", "publisher": "Ace", "year": 1965, "prose": "Dune"}],
                           target=[{"title": "Dune", "publisher": "Ace", "year": 2005, "prose": "Dune"}])
    item = build_plan(src, tgt, trash, LibraryResolver({}), recheck_years=True).items[0]
    assert item.action is Action.TRASH and "same text (100%)" in item.reason



def test_a_different_text_leaves_the_pair_undecided(libs):
    src, tgt, trash = libs(source=[{"title": "Dune", "publisher": "Ace", "year": 1965, "prose": "one"}],
                           target=[{"title": "Dune", "publisher": "Ace", "year": 2005, "prose": "two"}])
    item = build_plan(src, tgt, trash, LibraryResolver({}), recheck_years=True).items[0]
    assert item.action is Action.LEAVE and "same text" not in item.reason


def test_the_same_text_overrules_metadata_that_differs(libs):
    # Another publisher, the same text: the same book. Its files are the "other edition's": Trash only.
    src, tgt, trash = libs(source=[{"title": "Dune", "publisher": "Ace", "prose": "Dune", "formats": ["EPUB", "MOBI"]}],
                           target=[{"title": "Dune", "publisher": "Gollancz", "prose": "Dune"}])
    item = build_plan(src, tgt, trash).items[0]
    assert item.action is Action.TRASH and item.selected and not item.review and item.add_formats == []
    assert "same text (100%); metadata differs:" in item.reason


def test_metadata_that_differs_with_another_text_is_another_book(libs):
    src, tgt, trash = libs(source=[{"title": "Dune", "publisher": "Ace", "prose": "one"}],
                           target=[{"title": "Dune", "publisher": "Gollancz", "prose": "two"}])
    assert build_plan(src, tgt, trash).items[0].action is Action.MOVE


def test_the_same_text_needs_no_review_of_the_authors(libs):
    # "Giunti & Giunti Editore" / "Giunti Demetra": matched by the surname only, but the text is the same.
    src, tgt, trash = libs(source=[{"title": "Dune", "authors": ["Herbert"], "prose": "Dune"}],
                           target=[{"title": "Dune", "authors": ["Frank Herbert"], "prose": "Dune"}])
    item = build_plan(src, tgt, trash, author_variants=True).items[0]
    assert item.action is Action.TRASH and "same text (100%)" in item.reason and "(surname only)" in item.reason
    assert item.selected and not item.review
