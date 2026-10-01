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

from calibre_dedup.ai import AICache, ReviewMetadata
from calibre_dedup.covers import BAD_COVER_TAG, generic_covers
from calibre_dedup.models import Book
from calibre_dedup.planner import AIResolver
from calibre_dedup.review import ReviewAction, ReviewItem, Reviewer, _mark_generic, review_actions, scan_library
from tests.test_planner import make_library
from tests.test_review import REPLY, FakeExtractor, FakeProvider

LOGO = b"word 2000 logo" * 100


def book(tmp_path, i, title, cover: bytes | None, tags=(), author=None) -> Book:
    folder = tmp_path / f"b{i}"
    folder.mkdir()
    if cover is not None:
        (folder / "cover.jpg").write_bytes(cover)
    return Book(i, title, [author or f"Author{'XYZW'[i % 4]} Surname{'abcd'[i % 4]}"], None, None, set(), {}, "",
                f"b{i}", str(tmp_path), has_cover=cover is not None, tags=set(tags))


def test_the_same_image_on_three_titles_and_authors_is_generic(tmp_path):
    books = [book(tmp_path, 1, "I Malavoglia", LOGO, author="Giovanni Verga"),
             book(tmp_path, 2, "Il Paradiso Perduto", LOGO, author="John Milton"),
             book(tmp_path, 3, "Dei delitti e delle pene", LOGO, author="Cesare Beccaria"),
             book(tmp_path, 4, "Dune", b"real cover"), book(tmp_path, 5, "Emma", None)]
    generic = generic_covers(books)
    assert sorted(generic) == sorted(str(tmp_path / f"b{i}" / "cover.jpg") for i in (1, 2, 3))
    assert set(generic.values()) == {3}


def test_copies_of_one_book_sharing_their_cover_are_not_generic(tmp_path):
    books = [book(tmp_path, 1, "Dune", LOGO), book(tmp_path, 2, "DUNE", LOGO), book(tmp_path, 3, "Emma", LOGO)]
    assert generic_covers(books) == {}  # two titles only


def test_one_authors_books_with_messy_titles_or_a_series_cover_are_not_generic(tmp_path):
    books = [book(tmp_path, i, t, LOGO, author="Ernest Hemingway") for i, t in enumerate(
        ("Il vecchio e il mare", "ITABOOK 0052 - Hemingway, Ernest", "Ernest Hemingway", "Videssos 01"), 1)]
    assert generic_covers(books) == {}


def test_a_library_read_twice_counts_each_book_once(tmp_path):
    books = [book(tmp_path, i, t, LOGO) for i, t in enumerate(("A", "B", "C"), 1)]
    assert set(generic_covers(books + books).values()) == {3}


def test_a_generic_cover_is_never_proof(tmp_path):
    a, b = book(tmp_path, 1, "Storia sociale dell'arte", LOGO), book(tmp_path, 2, "Storia sociale dell'arte 2", LOGO)

    class Vision:
        profile = type("P", (), {"kind": "fake", "base_url": "", "model": "v"})()

        def chat(self, *args, **kw):
            raise AssertionError("the AI is not asked")

    resolver = AIResolver(None, None, AICache(tmp_path / "c.json"), Vision())
    assert resolver.same_cover(a, b) == (True, "identical cover files", False)
    resolver.generic = {str(tmp_path / "b1" / "cover.jpg"): 34}
    same, note, called = resolver.same_cover(a, b)
    assert (same, called) == (False, False)
    assert note.startswith("generic cover (the same image on 34 books) on ") and note.endswith(": not proof")


def test_review_tags_generic_covers_except_books_going_to_the_trash(tmp_path):
    lib = make_library(tmp_path / "lib", [{"title": t, "authors": [f"Autore {t}"], "text": "x", "cover": True}
                                          for t in ("Uno", "Due", "Tre", "Quattro")])
    for i in (1, 2, 3):
        (tmp_path / "lib" / f"a/b ({i})" / "cover.jpg").write_bytes(LOGO)
    (tmp_path / "lib" / "a/b (4)" / "cover.jpg").write_bytes(b"real")
    items = scan_library(lib, "", Reviewer(FakeProvider(REPLY), FakeExtractor(), AICache(tmp_path / "c.json"))).items
    assert [i.generic_cover for i in items] == [3, 3, 3, 0]
    assert "generic cover (the same image on 3 books)" in items[0].note
    items[1].set_action(ReviewAction.TRASH)
    tag = [a for a in review_actions(items, {"year"}) if a.get("tag") == BAD_COVER_TAG]
    assert tag == [{"op": "tag", "src_ids": [1, 3], "tag": BAD_COVER_TAG}]


def test_a_book_already_tagged_bad_cover_is_not_tagged_again(tmp_path):
    it = ReviewItem(book(tmp_path, 1, "Uno", LOGO, tags=[BAD_COVER_TAG]), ReviewMetadata())
    _mark_generic(it, 34)
    assert it.generic_cover == 0 and "generic" not in it.note
    assert not [a for a in review_actions([it], set()) if a.get("tag") == BAD_COVER_TAG]
