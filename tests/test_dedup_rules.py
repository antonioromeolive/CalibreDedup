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

"""The duplicate rules: with no edition data two copies are the same book unless their
covers differ; identical files; a text in another language or far longer is another
book; titles made from file names; the best copy first, between two libraries too."""

from pathlib import Path

import pytest

from calibre_dedup import planner
from calibre_dedup.ai import AIMetadata
from calibre_dedup.models import Action
from calibre_dedup.planner import SKIP_COVER, build_plan
from tests.test_planner import CoverResolver, FakeResolver, actions, libs, make_library  # noqa: F401  (a fixture)

ISBN = "9780441013593"
NO_DATA = dict(source=[{"title": "Children of Dune", "cover": True, "formats": ["EPUB", "MOBI"]}],
               target=[{"title": "Children of Dune", "cover": True}])


def blind(same=None) -> CoverResolver:
    resolver = CoverResolver(same)
    resolver.vision = None  # no Image AI
    return resolver


# --- no edition data: the same book unless the covers differ -------------------------
@pytest.mark.parametrize("same,action", [(None, Action.TRASH), (True, Action.TRASH), (False, Action.LEAVE)])
def test_with_no_edition_data_the_covers_decide(libs, same, action):
    src, tgt, trash = libs(**NO_DATA)
    item = build_plan(src, tgt, trash, CoverResolver(same), cover_check=True).items[0]
    assert item.action is action
    if same is None:  # the AI is unsure: nothing tells them apart, so they are the same book
        assert item.no_edition and not item.by_cover and item.add_formats == ["MOBI"]
        assert "nothing tells them apart" in item.reason and "AI cover check: unsure" in item.reason
    elif same:
        assert item.by_cover and not item.no_edition
    else:
        assert "another cover" in item.reason and item.match is not None


def test_edition_data_on_one_copy_only_is_no_edition_data(libs):
    src, tgt, trash = libs(source=[{"title": "Children of Dune", "cover": True}],
                           target=[{"title": "Children of Dune", "publisher": "Ace", "year": 1991, "cover": True}])
    item = build_plan(src, tgt, trash, CoverResolver(None), cover_check=True).items[0]
    assert item.action is Action.TRASH and item.no_edition


def test_edition_data_that_cant_be_compared_needs_the_same_cover(libs):
    src, tgt, trash = libs(source=[{"title": "Dune (2nd Edition)", "publisher": "Ace", "cover": True}],
                           target=[{"title": "Dune", "publisher": "Ace", "year": 1965, "cover": True}])
    item = build_plan(src, tgt, trash, CoverResolver(None), cover_check=True).items[0]
    assert item.action is Action.LEAVE and "edition not comparable (edition 2 vs 1965)" in item.reason
    item = build_plan(src, tgt, trash, CoverResolver(True), cover_check=True).items[0]
    assert item.action is Action.TRASH and item.by_cover


def test_without_an_image_ai_or_the_cover_check_the_covers_say_nothing(libs):
    src, tgt, trash = libs(**NO_DATA)
    resolver = blind()
    item = build_plan(src, tgt, trash, resolver, cover_check=True).items[0]
    assert item.action is Action.LEAVE and item.skipped == [SKIP_COVER] and resolver.cover_calls == []
    item = build_plan(src, tgt, trash, CoverResolver(None)).items[0]  # cover check off
    assert item.action is Action.LEAVE and item.skipped == []


def test_identical_cover_files_need_no_ai(libs, tmp_path):
    src, tgt, trash = libs(**NO_DATA)
    (tmp_path / "tgt" / "a/b (1)" / "cover.jpg").write_bytes((tmp_path / "src" / "a/b (1)" / "cover.jpg").read_bytes())
    for resolver in (None, blind()):
        item = build_plan(src, tgt, trash, resolver, cover_check=True).items[0]
        assert item.action is Action.TRASH and item.by_cover and "identical cover files" in item.reason


def test_a_generic_cover_is_not_a_real_cover(libs, monkeypatch, tmp_path):
    src, tgt, trash = libs(**NO_DATA)
    cover = tmp_path / "tgt" / "a/b (1)" / "cover.jpg"
    monkeypatch.setattr(planner, "generic_covers", lambda books, cancel, cache: {str(cover): 5})
    resolver = CoverResolver(True)
    item = build_plan(src, tgt, trash, resolver, cover_check=True).items[0]
    assert item.action is Action.TRASH and item.no_edition and resolver.cover_calls == []
    assert "generic cover (the same image on 5 books)" in item.reason


def test_the_rule_applies_within_one_library(tmp_path, monkeypatch):
    monkeypatch.setattr("calibre_dedup.planner.MAX_LIBRARY_PATH", 10_000)
    lib = make_library(tmp_path / "lib", [{"title": "Emma", "authors": ["Jane Austen"], "cover": True},
                                          {"title": "Emma", "authors": ["Jane Austen"], "cover": True,
                                           "formats": ["MOBI"]}])
    plan = build_plan(lib, lib, str(tmp_path / "trash"), CoverResolver(None), cover_check=True)
    assert actions(plan) == [("Emma", Action.LEAVE), ("Emma", Action.TRASH)]  # the EPUB copy is kept
    assert plan.items[1].no_edition and plan.items[1].add_formats == ["MOBI"]


# --- years that differ only in Calibre -------------------------------------------------
YEARS = dict(source=[{"title": "Mary's meadow", "year": 2011, "publisher": "Mondadori", "cover": True,
                      "formats": ["EPUB", "MOBI"]}],
             target=[{"title": "Mary's Meadow", "year": 1986, "publisher": "Mondadori", "cover": True}])


@pytest.mark.parametrize("same,action", [(True, Action.TRASH), (False, Action.MOVE), (None, Action.LEAVE)])
def test_years_the_books_dont_confirm_are_settled_by_the_covers(libs, same, action):
    src, tgt, trash = libs(**YEARS)
    item = build_plan(src, tgt, trash, CoverResolver(same), cover_check=True, recheck_years=True).items[0]
    assert item.action is action
    if same:
        assert item.by_cover and item.add_formats == []  # the metadata differs: nothing added to the other


# --- identical files -------------------------------------------------------------------
EPUB = b"PK\x03\x04" + b"A" * 700


def test_the_same_file_is_the_same_book_whatever_the_titles(libs):
    src, tgt, trash = libs(source=[{"title": "ITABOOK 0052 - Hemingway", "authors": ["Hemingway"],
                                    "files": {"EPUB": EPUB}}],
                           target=[{"title": "Il vecchio e il mare", "authors": ["Ernest Hemingway"],
                                    "files": {"EPUB": EPUB}}])
    item = build_plan(src, tgt, trash).items[0]  # no AI needed
    assert item.action is Action.TRASH and "identical EPUB file" in item.reason
    assert item.match.title == "Il vecchio e il mare"


def test_a_file_of_the_same_size_is_read_to_tell(libs):
    src, tgt, trash = libs(source=[{"title": "Dune Messiah", "files": {"EPUB": EPUB}}],
                           target=[{"title": "Children of Dune", "files": {"EPUB": b"PK\x03\x04" + b"B" * 700}}])
    item = build_plan(src, tgt, trash).items[0]
    assert item.action is Action.MOVE and "identical" not in item.reason


def test_a_file_shared_by_three_books_is_a_placeholder(libs):
    page = b"%PDF-1.4 file not found" * 30
    src, tgt, trash = libs(source=[{"title": "Uno", "authors": ["Anna Uno"], "files": {"PDF": page}}],
                           target=[{"title": "Due", "authors": ["Bruno Due"], "files": {"PDF": page}},
                                   {"title": "Tre", "authors": ["Carla Tre"], "files": {"PDF": page}}])
    assert build_plan(src, tgt, trash).items[0].action is Action.MOVE


# --- another language, another length ------------------------------------------------
class Reader:
    """The extractor's text_profile, canned per library ('src', 'tgt') or per book ('tgt/b (2)')."""

    def __init__(self, languages: dict, lengths: dict | None = None):
        self.languages, self.lengths = languages, lengths or {}

    def text_profile(self, formats):
        folder = Path(next(iter(formats.values()))).parent
        keys = (f"{folder.parents[1].name}/{folder.name}", folder.parents[1].name)
        return (next((self.languages[k] for k in keys if k in self.languages), None),
                next((self.lengths[k] for k in keys if k in self.lengths), None))


def reading(resolver, languages, lengths=None):
    resolver.extractor = Reader(languages, lengths)
    return resolver


def test_a_copy_in_another_language_is_another_book(libs):
    src, tgt, trash = libs(source=[{"title": "Dune", "isbn": ISBN}], target=[{"title": "Dune", "isbn": ISBN}])
    item = build_plan(src, tgt, trash, reading(FakeResolver({}), {"src": "ita", "tgt": "eng"})).items[0]
    assert item.action is Action.MOVE and item.different and item.match.title == "Dune"
    assert "in another language (English text, this one Italian)" in item.reason
    item = build_plan(src, tgt, trash, reading(FakeResolver({}), {"src": "ita", "tgt": "ita"})).items[0]
    assert item.action is Action.TRASH


def test_the_copy_in_the_same_language_is_the_duplicate(libs):
    src, tgt, trash = libs(source=[{"title": "Dune", "isbn": ISBN}],
                           target=[{"title": "Dune", "isbn": ISBN}, {"title": "Dune", "isbn": ISBN}])
    resolver = reading(FakeResolver({}), {"src": "ita", "tgt/b (1)": "eng", "tgt/b (2)": "ita"})
    item = build_plan(src, tgt, trash, resolver).items[0]
    assert item.action is Action.TRASH and item.match.id == 2


def test_a_copy_three_times_longer_is_another_content(libs):
    src, tgt, trash = libs(**NO_DATA)
    resolver = reading(CoverResolver(None), {"src": "eng", "tgt": "eng"}, {"src": 100_000, "tgt": 30_000})
    item = build_plan(src, tgt, trash, resolver, cover_check=True).items[0]
    assert item.action is Action.LEAVE and "3.3 times longer: another content" in item.reason
    resolver = reading(CoverResolver(None), {"src": "eng", "tgt": "eng"}, {"src": 100_000, "tgt": 60_000})
    assert build_plan(src, tgt, trash, resolver, cover_check=True).items[0].action is Action.TRASH


def test_the_length_does_not_overrule_a_proof(libs):
    src, tgt, trash = libs(source=[{"title": "Dune", "isbn": ISBN}], target=[{"title": "Dune", "isbn": ISBN}])
    resolver = reading(FakeResolver({}), {}, {"src": 100_000, "tgt": 10_000})
    assert build_plan(src, tgt, trash, resolver).items[0].action is Action.TRASH


# --- titles made from file names ------------------------------------------------------
def test_a_file_name_title_is_never_moved_nor_read_by_the_ai(libs):
    src, tgt, trash = libs(
        source=[{"title": "ITABOOK 0052 - Hemingway", "authors": ["Ernest Hemingway"], "isbn": ISBN},
                {"title": "scan_0012", "authors": ["Ernest Hemingway"]}],
        target=[{"title": "Il vecchio e il mare", "authors": ["Ernest Hemingway"], "isbn": ISBN}])
    for resolver in (None, FakeResolver({"ITABOOK 0052 - Hemingway": AIMetadata(title="Il vecchio e il mare")})):
        plan = build_plan(src, tgt, trash, resolver)
        assert actions(plan) == [("ITABOOK 0052 - Hemingway", Action.LEAVE), ("scan_0012", Action.LEAVE)]
        for item in plan.items:
            assert item.reason.startswith("not moved: the title looks like a file name; fix it with the Metadata "
                                          "Review first") and item.file_name_title and not item.selected
        assert resolver is None or resolver.calls == []  # the Metadata Review reads the real title
    # an identical file still proves it a duplicate (test_the_same_file_is_the_same_book_whatever_the_titles)


def test_a_target_book_with_a_file_name_title_holds_back_the_books_by_its_author(libs):
    src, tgt, trash = libs(
        source=[{"title": "Il vecchio e il mare", "authors": ["Ernest Hemingway"], "isbn": ISBN},
                {"title": "Fiesta", "authors": ["Ernest Hemingway"]},
                {"title": "Emma", "authors": ["Jane Austen"]}],
        target=[{"title": "ITABOOK 0052 - Hemingway", "authors": ["Ernest Hemingway"], "isbn": ISBN}])
    resolver = FakeResolver({"ITABOOK 0052 - Hemingway": AIMetadata(title="Il vecchio e il mare")})
    plan = build_plan(src, tgt, trash, resolver)
    assert actions(plan) == [("Il vecchio e il mare", Action.LEAVE), ("Fiesta", Action.LEAVE), ("Emma", Action.MOVE)]
    held = plan.items[0]
    assert held.reason.startswith("not moved: the target's 'ITABOOK 0052 - Hemingway — Ernest Hemingway', by the "
                                  "same author, has a file name for title: it may be this book; fix that title with "
                                  "the Metadata Review first")
    assert held.match.title == "ITABOOK 0052 - Hemingway" and resolver.calls == []


# --- unproven duplicates: unticked, to review -------------------------------------------
def test_an_unproven_duplicate_starts_unticked_to_review(libs):
    from calibre_dedup.executor import plan_actions
    from calibre_dedup.selection import mark_reviewed, needs_review
    src, tgt, trash = libs(**NO_DATA)
    plan = build_plan(src, tgt, trash, CoverResolver(None), cover_check=True)
    item = plan.items[0]
    assert item.action is Action.TRASH and item.add_formats == ["MOBI"]  # Merge & Trash, once checked
    assert item.review == planner.REVIEW_NO_EDITION and needs_review(item) and not item.selected
    assert plan_actions(plan) == []  # unticked: nothing is trashed, nothing added to the target copy
    assert "1 to review before ticking" in planner.run_summary(plan)[0]
    item.selected = True  # the user compared the copies
    assert not needs_review(item) and [a["op"] for a in plan_actions(plan)] == ["trash"]
    item.selected = False
    assert needs_review(item) and mark_reviewed([item]) == 1 and not needs_review(item) and not item.selected
    for same, review in ((True, planner.REVIEW_COVER), (False, "")):  # the same cover: also to review
        item = build_plan(src, tgt, trash, CoverResolver(same), cover_check=True).items[0]
        assert item.review == review and item.selected is (not review and item.action is not Action.LEAVE)


def test_a_proven_duplicate_is_ticked(libs):
    src, tgt, trash = libs(source=[{"title": "Dune", "isbn": ISBN, "formats": ["EPUB", "MOBI"]}],
                           target=[{"title": "Dune", "isbn": ISBN}])
    item = build_plan(src, tgt, trash).items[0]
    assert item.action is Action.TRASH and item.add_formats == ["MOBI"] and item.selected and not item.review


def test_authors_matched_by_the_surname_or_the_ai_are_to_review(libs):
    src, tgt, trash = libs(source=[{"title": "Dune", "authors": ["Herbert"], "isbn": ISBN}],
                           target=[{"title": "Dune", "authors": ["Frank Herbert"], "isbn": ISBN}])
    item = build_plan(src, tgt, trash, author_variants=True).items[0]
    assert item.action is Action.TRASH and not item.selected
    assert item.review == "authors matched by the surname only: check they are the same person"


# --- records with no file ---------------------------------------------------------------
def test_an_empty_record_goes_to_the_trash_and_is_no_copy(libs):
    from calibre_dedup.executor import plan_actions
    src, tgt, trash = libs(source=[{"title": "Dune", "formats": []}, {"title": "Emma", "authors": ["Jane Austen"]}],
                           target=[{"title": "Emma", "authors": ["Jane Austen"], "formats": []}])
    plan = build_plan(src, tgt, trash)
    assert actions(plan) == [("Dune", Action.TRASH), ("Emma", Action.MOVE)]  # the target's empty Emma is no copy
    empty = plan.items[0]
    assert empty.reason == planner.EMPTY_REASON and empty.selected and empty.match is None
    assert plan_actions(plan)[0] == {"op": "trash", "src_id": 1, "title": "Dune", "add_formats": [],
                                     "no_target": True, "stamp": empty.source.last_modified}


# --- the best copy first ---------------------------------------------------------------
def test_between_two_libraries_the_best_copy_is_moved(libs):
    src, tgt, trash = libs(source=[{"title": "Dune", "isbn": ISBN, "formats": ["PDF"]},
                                   {"title": "Dune", "isbn": ISBN, "formats": ["EPUB"], "comments": "A novel"}],
                           target=[])
    plan = build_plan(src, tgt, trash)
    assert actions(plan) == [("Dune", Action.TRASH), ("Dune", Action.MOVE)]  # listed in the library's order
    assert plan.items[0].match.id == 2 and plan.items[0].match_planned
