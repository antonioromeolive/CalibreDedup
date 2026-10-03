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

"""Analyzing only the books with a tag (Merge and Dedup and calibre-review)."""

import pytest

from calibre_dedup.config import Settings
from calibre_dedup.executor import plan_actions
from calibre_dedup.library import library_tags
from calibre_dedup.models import Action
from calibre_dedup.planner import build_plan
from calibre_dedup.selection import SelectionStore, override
from calibre_dedup.session import analysis_signature, changed_settings

from test_planner import actions, libs, make_library  # noqa: F401 (libs is a fixture)


@pytest.fixture
def one_library(tmp_path, monkeypatch):
    monkeypatch.setattr("calibre_dedup.planner.MAX_LIBRARY_PATH", 10_000)
    return lambda books: (make_library(tmp_path / "library", books), str(tmp_path / "trash"))


def test_only_tagged_source_books_are_analyzed_against_the_whole_target(libs):
    src, tgt, trash = libs(
        source=[{"title": "Dune", "isbn": "9780441013593", "tags": ["New"]},
                {"title": "Emma", "authors": ["Jane Austen"]},  # no tag: not analyzed
                {"title": "Persuasion", "authors": ["Jane Austen"], "tags": ["new", "Other"]}],
        target=[{"title": "Dune", "isbn": "9780441013593"}],
    )
    plan = build_plan(src, tgt, trash, tag=" NEW ")
    assert actions(plan) == [("Dune", Action.TRASH), ("Persuasion", Action.MOVE)]
    assert plan.total_books == 2 and plan.tag == "NEW"
    assert actions(build_plan(src, tgt, trash)) == [  # no tag: every book
        ("Dune", Action.TRASH), ("Emma", Action.MOVE), ("Persuasion", Action.MOVE)]


def test_one_library_tagged_copy_is_trashed_into_a_better_untagged_one(one_library):
    library, trash = one_library([
        {"title": "Dune", "publisher": "Ace", "year": 1965, "formats": ["EPUB"]},
        {"title": "Dune", "publisher": "Ace", "year": 1965, "formats": ["MOBI"], "tags": ["New"]},
        {"title": "Emma", "authors": ["Jane Austen"]},
    ])
    plan = build_plan(library, library, trash, tag="New")
    assert actions(plan) == [("Dune", Action.TRASH)]
    item = plan.items[0]
    assert item.source.id == 2 and item.match.id == 1 and item.add_formats == ["MOBI"]
    stamp = "2026-01-01 00:00:00+00:00"  # both copies' last_modified, checked on Execute
    assert plan_actions(plan) == [{"op": "trash", "src_id": 2, "title": "Dune", "add_formats": ["MOBI"],
                                   "target_id": 1, "target_stamp": stamp, "stamp": stamp}]


def test_one_library_better_tagged_copy_is_left_and_the_untagged_one_untouched(one_library):
    library, trash = one_library([
        {"title": "Dune", "publisher": "Ace", "year": 1965, "formats": ["PDF"]},
        {"title": "Dune", "publisher": "Ace", "year": 1965, "formats": ["EPUB"], "tags": ["New"]},
    ])
    plan = build_plan(library, library, trash, tag="New")
    assert actions(plan) == [("Dune", Action.LEAVE)]
    item = plan.items[0]
    assert item.match.id == 1 and not item.add_formats and not item.selected
    assert "one to keep" in item.reason and "adding" not in item.reason
    assert plan_actions(plan) == []  # nothing happens to either copy
    override(item, Action.TRASH, same_library=True)  # the user can still trash it into the other
    assert item.action is Action.TRASH and item.add_formats == ["EPUB"]


def test_one_library_tagged_books_are_still_matched_with_each_other(one_library):
    library, trash = one_library([
        {"title": "Dune", "publisher": "Ace", "year": 1965, "formats": ["EPUB"], "tags": ["New"]},
        {"title": "Dune", "publisher": "Ace", "year": 1965, "formats": ["MOBI"], "tags": ["New"]},
        {"title": "Emma", "authors": ["Jane Austen"], "isbn": "9780141439587", "tags": ["New"]},
        {"title": "Emma", "authors": ["Jane Austen"], "isbn": "9780141439587"},  # untagged, same rank: it stays
    ])
    plan = build_plan(library, library, trash, tag="New")
    assert actions(plan) == [("Dune", Action.LEAVE), ("Dune", Action.TRASH), ("Emma", Action.TRASH)]
    assert plan.items[1].match.id == 1 and plan.items[2].match.id == 4


def test_tagged_choices_are_remembered_apart(libs, tmp_path):
    src, tgt, trash = libs(
        source=[{"title": "Dune", "tags": ["New"]}, {"title": "Emma", "authors": ["Jane Austen"]}],
        target=[],
    )
    store = SelectionStore(tmp_path / "selections.json")
    full = build_plan(src, tgt, trash)
    full.items[1].selected = False  # Emma unticked in the whole-library analysis
    store.save(full)
    tagged = build_plan(src, tgt, trash, tag="New")
    tagged.items[0].selected = False
    store.save(tagged)  # doesn't replace the whole library's choices
    again = build_plan(src, tgt, trash)
    assert store.apply(again) == 1
    assert [i.selected for i in again.items] == [True, False]
    again_tagged = build_plan(src, tgt, trash, tag="new")
    assert store.apply(again_tagged) == 1 and not again_tagged.items[0].selected


def test_changing_the_tag_or_its_mode_asks_to_analyze_again():
    before = analysis_signature(Settings(only_tag="New"))
    assert changed_settings(before, analysis_signature(Settings(only_tag=" new "))) == []
    assert changed_settings(before, analysis_signature(Settings(only_tag=""))) == ["tag filter"]
    assert changed_settings(before, analysis_signature(Settings(only_tag="New", only_tag_exclude=True))) == [
        "tag filter"]
    no_tag = analysis_signature(Settings())
    assert changed_settings(no_tag, analysis_signature(Settings(only_tag_exclude=True))) == []  # no tag: no filter


def test_except_tag_analyzes_the_other_source_books(libs):
    src, tgt, trash = libs(
        source=[{"title": "Dune", "isbn": "9780441013593", "tags": ["Done"]},
                {"title": "Emma", "authors": ["Jane Austen"]},
                {"title": "Persuasion", "authors": ["Jane Austen"], "tags": ["done"]}],
        target=[{"title": "Dune", "isbn": "9780441013593"}],
    )
    plan = build_plan(src, tgt, trash, tag="DONE", tag_exclude=True)
    assert actions(plan) == [("Emma", Action.MOVE)]
    assert (plan.total_books, plan.tag_exclude) == (1, True)
    assert not build_plan(src, tgt, trash, tag="", tag_exclude=True).tag_exclude  # no tag: every book


def test_one_library_except_tag_keeps_the_tagged_books_untouched(one_library):
    library, trash = one_library([
        {"title": "Dune", "publisher": "Ace", "year": 1965, "formats": ["PDF"], "tags": ["Done"]},
        {"title": "Dune", "publisher": "Ace", "year": 1965, "formats": ["EPUB"]},  # better: left
        {"title": "Emma", "publisher": "Penguin", "year": 1990, "formats": ["EPUB"], "tags": ["Done"]},
        {"title": "Emma", "publisher": "Penguin", "year": 1990, "formats": ["MOBI"]},  # worse: trashed into it
    ])
    plan = build_plan(library, library, trash, tag="Done", tag_exclude=True)
    assert [(i.source.id, i.action) for i in plan.items] == [(2, Action.LEAVE), (4, Action.TRASH)]
    assert plan.items[0].match.id == 1 and "is tagged 'Done'" in plan.items[0].reason
    assert plan.items[1].match.id == 3


def test_only_and_except_remember_their_ticks_apart(libs, tmp_path):
    src, tgt, trash = libs(
        source=[{"title": "Dune", "tags": ["New"]}, {"title": "Emma", "authors": ["Jane Austen"]}],
        target=[],
    )
    store = SelectionStore(tmp_path / "selections.json")
    other = build_plan(src, tgt, trash, tag="New", tag_exclude=True)
    other.items[0].selected = False  # Emma
    store.save(other)
    assert store.apply(build_plan(src, tgt, trash, tag="New")) == 0
    assert store.apply(build_plan(src, tgt, trash)) == 0
    again = build_plan(src, tgt, trash, tag="New", tag_exclude=True)
    assert store.apply(again) == 1 and not again.items[0].selected


def test_command_line_tag_options():
    import argparse
    from calibre_dedup.session import tag_option
    ap = argparse.ArgumentParser()
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--tag")
    group.add_argument("--except-tag")
    assert tag_option(ap.parse_args([]), "Saved", True) == ("Saved", True)  # as in the GUI
    assert tag_option(ap.parse_args(["--tag", "New"]), "Saved", True) == ("New", False)
    assert tag_option(ap.parse_args(["--except-tag", "Done"]), "", False) == ("Done", True)
    assert tag_option(ap.parse_args(["--tag", ""]), "Saved", True) == ("", False)  # all books
    with pytest.raises(SystemExit):
        ap.parse_args(["--tag", "A", "--except-tag", "B"])


def test_library_tags_lists_each_tag_once(tmp_path):
    library = make_library(tmp_path / "library", [
        {"title": "A", "tags": ["New", "sf"]}, {"title": "B", "tags": ["New"]}, {"title": "C"}])
    assert library_tags(library) == ["New", "sf"]
    assert library_tags(tmp_path / "nowhere") == []


def test_review_scans_only_the_tagged_books(tmp_path, monkeypatch):
    from calibre_dedup import review
    library = make_library(tmp_path / "library", [
        {"title": "A", "tags": ["New"]}, {"title": "B"}, {"title": "C", "tags": ["New", review.REVIEWED_TAG]}])
    monkeypatch.setattr(review, "check_libraries", lambda *a: None)
    monkeypatch.setattr(review, "_scan_book", lambda reviewer, book, unpack: review.ReviewItem(book, None))
    reviewer = type("R", (), {"provider": None, "vision": None, "stats": {},
                              "disabled_reason": "", "image_disabled_reason": ""})()
    result = review.scan_library(library, str(tmp_path / "trash"), reviewer, tag="new")
    assert [i.book.title for i in result.items] == ["A"]
    assert (result.total_books, result.skipped, result.tag) == (1, 1, "new")
    others = review.scan_library(library, str(tmp_path / "trash"), reviewer, tag="new", tag_exclude=True)
    assert [i.book.title for i in others.items] == ["B"]
    assert "not tagged 'new'" in review.summary(others)
