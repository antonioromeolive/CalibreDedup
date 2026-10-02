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

"""Files Calibre can't open: detected, proposed for the trash library (whole book, or
only those formats with the record copied as it is), and turned into bridge actions."""

from pathlib import Path

from calibre_dedup.ai import AICache
from calibre_dedup.executor import plan_actions
import pytest

from calibre_dedup.extract import Excerpt, TextExtractor, unreadable_formats
from calibre_dedup.models import Action
from calibre_dedup.planner import build_plan
from calibre_dedup.review import ReviewAction, Reviewer, review_actions, scan_library
from calibre_dedup.selection import SelectionStore, actionable, revert
from tests.test_planner import libs, make_library  # noqa: F401  (libs is a fixture)
from tests.test_review import REPLY, FakeProvider

WORD = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(600)
PDF = b"%PDF-1.4\n" + bytes(600)


def put(library: str, book_no: int, fmt: str, data: bytes) -> None:
    """Write the book's file for `fmt` (make_library names them book.<fmt>)."""
    folder = Path(library, f"a/b ({book_no})")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"book.{fmt.lower()}").write_bytes(data)


# --- detection -----------------------------------------------------------------------
def test_unreadable_formats(tmp_path):
    def f(name, data):
        (tmp_path / name).write_bytes(data)
        return str(tmp_path / name)
    formats = {"PDF": f("a.pdf", WORD), "DOC": f("b.doc", WORD), "EPUB": f("c.epub", b"PK\x03\x04" + bytes(50)),
               "ORIGINAL_EPUB": f("d.epub", b"x"), "MOBI": str(tmp_path / "missing.mobi")}
    bad = unreadable_formats(formats)
    assert set(bad) == {"PDF", "DOC"}
    assert "really a Word 97-2003 document" in bad["PDF"] and "can't read DOC" in bad["DOC"]


def test_a_file_that_fails_to_open_is_remembered(tmp_path, monkeypatch):
    (tmp_path / "x.mobi").write_bytes(b"x" * 100)
    extractor = TextExtractor(tmp_path)
    monkeypatch.setattr(extractor, "_convert", lambda path: (_ for _ in ()).throw(
        RuntimeError("ebook-convert failed: Traceback...\nValueError: No plugin to handle input format: mobi")))
    extractor.excerpt({"MOBI": str(tmp_path / "x.mobi")}, "start")
    assert extractor.failed_formats({"MOBI": str(tmp_path / "x.mobi"), "EPUB": "y"}) == {
        "MOBI": "Calibre can't read the MOBI file (ValueError: No plugin to handle input format: mobi)"}
    extractor.close()


# --- the analysis ----------------------------------------------------------------------
def test_a_book_with_no_readable_file_is_proposed_for_the_trash_unticked(libs):
    src, tgt, trash = libs(source=[{"title": "Ernani", "formats": ["PDF"]}, {"title": "Dune Messiah"}], target=[])
    put(src, 1, "PDF", WORD)
    plan = build_plan(src, tgt, trash)
    item = plan.items[0]
    assert item.action is Action.TRASH and item.match is None and item.unreadable
    assert not item.selected and "no file Calibre can open" in item.reason
    assert [a["op"] for a in plan_actions(plan, False)] == ["move"]  # unticked: only Dune Messiah
    item.selected = True
    assert plan_actions(plan, False)[0] == {"op": "trash", "src_id": 1, "title": "Ernani", "add_formats": [],
                                            "no_target": True}


def test_with_the_setting_on_it_is_ticked(libs):
    src, tgt, trash = libs(source=[{"title": "Ernani", "formats": ["PDF"]}], target=[])
    put(src, 1, "PDF", WORD)
    item = build_plan(src, tgt, trash, trash_unreadable=True).items[0]
    assert item.action is Action.TRASH and item.selected


def test_some_bad_formats_the_book_is_decided_on_the_others(libs):
    src, tgt, trash = libs(
        source=[{"title": "Dune", "isbn": "9780441013593", "formats": ["PDF", "MOBI", "LIT"]},
                {"title": "Dune Messiah", "formats": ["EPUB", "PDF"]}],
        target=[{"title": "Dune", "isbn": "9780441013593", "formats": ["EPUB"]}])
    put(src, 1, "PDF", PDF)
    put(src, 1, "MOBI", bytes(60) + b"BOOKMOBI")
    put(src, 1, "LIT", WORD)  # a fake LIT: never merged into the match
    put(src, 2, "EPUB", b"PK\x03\x04" + bytes(50))
    put(src, 2, "PDF", WORD)
    plan = build_plan(src, tgt, trash, trash_unreadable=True)
    dup, new = plan.items
    assert dup.action is Action.TRASH and dup.add_formats == ["MOBI"] and set(dup.bad_formats) == {"LIT"}
    assert set(dup.source.formats) == {"PDF", "MOBI", "LIT"}  # the record itself
    assert new.action is Action.MOVE and new.bad_formats_to_trash == ["PDF"] and "unreadable" in new.reason
    ops = plan_actions(plan, False)
    assert "trash_formats" not in ops[0]  # the whole book goes to the trash library anyway
    assert ops[1]["op"] == "move" and ops[1]["trash_formats"] == ["PDF"]


def test_bad_formats_go_even_when_the_book_itself_is_not_ticked(libs):
    src, tgt, trash = libs(source=[{"title": "Dune Messiah", "formats": ["EPUB", "PDF"]}], target=[])
    put(src, 1, "EPUB", b"PK\x03\x04" + bytes(50))
    put(src, 1, "PDF", WORD)
    plan = build_plan(src, tgt, trash, trash_unreadable=True)
    plan.items[0].selected = False
    assert plan_actions(plan, False) == [{"op": "trash_formats", "src_id": 1, "title": "Dune Messiah",
                                          "trash_formats": ["PDF"]}]
    plan.items[0].trash_bad = False
    assert actionable(plan) == [] and plan_actions(plan, False) == []


def test_ticks_are_remembered_against_the_analysis_own(libs, tmp_path):
    src, tgt, trash = libs(source=[{"title": "Ernani", "formats": ["PDF"]}], target=[])
    put(src, 1, "PDF", WORD)
    store = SelectionStore(tmp_path / "sel.json")
    plan = build_plan(src, tgt, trash)
    store.save(plan)
    assert not (tmp_path / "sel.json").read_text().count("selected")  # unticked is its default
    plan.items[0].selected = True  # the user ticks it
    store.save(plan)
    again = build_plan(src, tgt, trash)
    store.apply(again)
    assert again.items[0].selected
    revert(again.items[0])
    assert not again.items[0].selected


# --- the review ------------------------------------------------------------------------
class ReadsEpubOnly:
    """An extractor that fails on anything but EPUB, like ebook-convert on a broken file."""
    pdf_pages, text_chars = 6, 12000

    def __init__(self):
        self.failed = {}

    @staticmethod
    def pick_format(formats):
        return next(iter(sorted(formats.items(), key=lambda kv: kv[0] != "MOBI")), None)

    def excerpt(self, formats, part):
        fmt, path = self.pick_format(formats)
        if fmt != "EPUB":
            self.failed[path] = f"Calibre can't read the {fmt} file (broken)"
            return Excerpt(source=f"{fmt} extraction failed")
        return Excerpt(text="Frontespizio", source="EPUB start")

    def failed_formats(self, formats):
        return {f: self.failed[p] for f, p in formats.items() if p in self.failed}

    def embedded_cover(self, formats):
        return None


def test_review_reads_the_next_format_when_one_fails_and_keeps_it(tmp_path):
    lib = make_library(tmp_path / "lib", [{"title": "Dune", "formats": ["EPUB", "MOBI"]}])
    put(lib, 1, "EPUB", b"PK\x03\x04" + bytes(50))
    put(lib, 1, "MOBI", bytes(60) + b"BOOKMOBI")
    item = scan_library(lib, "", Reviewer(FakeProvider(REPLY), ReadsEpubOnly(), AICache(tmp_path / "c.json"))).items[0]
    assert item.found is not None and set(item.bad_formats) == {"MOBI"} and not item.broken
    assert item.action is ReviewAction.UPDATE and "unreadable" in item.note
    ops = review_actions([item], {"year"})
    assert ops[0]["op"] == "set" and "trash_formats" not in ops[0]  # only Calibre's converter failed on it
    item.trash_bad = True  # the user moves it to the trash library anyway
    assert review_actions([item], {"year"})[0]["trash_formats"] == ["MOBI"]
    item.selected = False  # nothing to write: the formats go on their own, then the tag
    assert [a["op"] for a in review_actions([item], {"year"})] == ["trash_formats", "tag"]
    item.trash_bad = False
    assert [a["op"] for a in review_actions([item], {"year"})] == ["tag"]


def test_review_trashes_a_book_only_when_none_of_its_files_opens(tmp_path):
    lib = make_library(tmp_path / "lib", [{"title": "Ernani", "formats": ["PDF", "EPUB"]},
                                          {"title": "Otello", "formats": ["PDF", "DOC"]}])
    put(lib, 1, "PDF", WORD)  # a Word document named .pdf and an empty EPUB: nothing opens them
    put(lib, 1, "EPUB", b"")
    put(lib, 2, "PDF", WORD)
    put(lib, 2, "DOC", WORD)  # Word opens it
    text = FakeProvider(REPLY)
    fake, doc = scan_library(lib, "", Reviewer(text, ReadsEpubOnly(), AICache(tmp_path / "c.json"))).items
    assert fake.broken and fake.action is ReviewAction.TRASH and fake.selected and text.calls == []
    assert doc.broken and doc.action is ReviewAction.KEEP and "another program may open it" in doc.note
    assert review_actions([fake, doc], {"year"}) == [{"src_id": 1, "title": "Ernani", "op": "trash", "no_target": True}]


def test_review_takes_out_only_the_files_that_open_nowhere(tmp_path):
    lib = make_library(tmp_path / "lib", [{"title": "Dune", "formats": ["EPUB", "PDF", "DOC"]}])
    put(lib, 1, "EPUB", b"PK\x03\x04" + bytes(50))
    put(lib, 1, "PDF", WORD)  # not a PDF: opens nowhere
    put(lib, 1, "DOC", WORD)  # Calibre doesn't read it, Word does
    item = scan_library(lib, "", Reviewer(FakeProvider(REPLY), ReadsEpubOnly(), AICache(tmp_path / "c.json"))).items[0]
    assert set(item.bad_formats) == {"PDF", "DOC"} and item.bad_formats_to_trash == ["PDF"]
    item.trash_bad = True
    assert item.bad_formats_to_trash == ["DOC", "PDF"]


@pytest.mark.parametrize("tag", ["", "New"])
def test_in_one_library_a_copy_whose_epub_is_fake_does_not_replace_a_real_one(tmp_path, monkeypatch, tag):
    monkeypatch.setattr("calibre_dedup.planner.MAX_LIBRARY_PATH", 10_000)
    library = make_library(tmp_path / "library", [  # the first looks better until its EPUB is opened
        {"title": "Dune", "publisher": "Ace", "year": 1965, "formats": ["EPUB", "MOBI"], "tags": ["New"],
         "comments": "A novel"},
        {"title": "Dune", "publisher": "Ace", "year": 1965, "formats": ["EPUB"], "tags": ["New"]},
    ])
    put(library, 1, "EPUB", WORD)
    plan = build_plan(library, library, str(tmp_path / "trash"), tag=tag)
    first, second = plan.items
    assert first.action is Action.LEAVE and "EPUB" in first.bad_formats
    assert second.action is Action.LEAVE and second.match is not None
    assert "can't be opened (found when it was analyzed): neither is handled" in second.reason
