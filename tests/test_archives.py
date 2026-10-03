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

import zipfile
from pathlib import Path

import pytest

from calibre_dedup.ai import AICache
from calibre_dedup.archives import Unpack, ask_once, plan_unpack, prepare
from calibre_dedup.executor import plan_actions
from calibre_dedup.extract import TextExtractor
from calibre_dedup.models import Action, Book, Identity, Plan, PlanItem
from calibre_dedup.review import ReviewAction, ReviewItem, review_actions
from calibre_dedup import planner
from calibre_dedup.selection import actionable, checkable, needs_review, revert
from tests.test_bridge import bridge  # noqa: F401  (a fixture)
from tests.test_review import book as make_book


def files(*pairs):
    return [{"name": n, "size": s} for n, s in pairs]


@pytest.fixture
def archive(tmp_path):
    path = tmp_path / "book.zip"
    path.write_bytes(b"x")
    return str(path)


# --- the rules (checked on 327 real RAR books: all clear) ---------------------------------
def test_book_files_are_added_leftovers_ignored(archive):
    u = plan_unpack("ZIP", archive, files(("Book.epub", 300_000), ("Book.pdf", 600_000), ("Book.txt", 225_000),
                                          ("Book.doc", 400_000), ("trama.txt", 1_361), ("Cover.jpg", 80_000),
                                          ("dir/Thumbs.db", 5_632)), {"ZIP": archive})
    assert not u.problem and sorted(u.add) == ["EPUB", "PDF", "TXT"] and u.sizes["PDF"] == 600_000
    assert u.ignored == ["Book.doc", "trama.txt", "Cover.jpg", "Thumbs.db"]


def test_formats_the_book_has_are_skipped(archive):
    u = plan_unpack("ZIP", archive, files(("Book.epub", 3), ("Book.pdf", 6)), {"ZIP": archive, "EPUB": "x.epub"})
    assert u.add == {"PDF": "Book.pdf"} and u.present == ["EPUB"]


@pytest.mark.parametrize("members, problem", [
    ((("One.epub", 300_000), ("Two.epub", 300_000)), "2 EPUB files (different books?)"),
    ((("Book.doc", 3), ("Cover.jpg", 3)), "nothing Calibre can read inside"),
    ((("Book.pdf", 3), ("More.rar", 3)), "an archive inside (More.rar)"),
])
def test_unclear_archives_are_left_untouched(archive, members, problem):
    u = plan_unpack("ZIP", archive, files(*members), {"ZIP": archive})
    assert u.problem == problem and u.note == f"ZIP not unpacked: {problem}"


# --- the analysis: nothing is written, the book is read from the files inside ---------------
def zip_book(tmp_path, **members) -> Book:
    path = tmp_path / "lib" / "Book.zip"
    path.parent.mkdir()
    with zipfile.ZipFile(path, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return make_book(title="Book", path="lib", formats={"ZIP": str(path)})


def test_yes_reads_the_extracted_files_and_leaves_the_archive_as_it_is(tmp_path):
    book = zip_book(tmp_path, **{"Book.epub": b"E" * 50, "Book.doc": b"D"})
    before = Path(book.formats["ZIP"]).read_bytes()
    extractor = TextExtractor(tmp_path)
    try:
        seen, (u,) = prepare(book, extractor, lambda b, a: True)
        assert u.unpack and u.planned and u.note == "ZIP unpacked for the analysis: adds EPUB"
        assert set(seen.formats) == {"EPUB"} and Path(seen.formats["EPUB"]).read_bytes() == b"E" * 50
        assert AICache.aliases[seen.formats["EPUB"]] == f"{book.formats['ZIP']}::Book.epub"
        assert Path(book.formats["ZIP"]).read_bytes() == before  # nothing written
    finally:
        extractor.close()
    assert not Path(seen.formats["EPUB"]).exists()  # the run's folder is gone


def test_no_keeps_the_book_as_it_is(tmp_path):
    book = zip_book(tmp_path, **{"Book.epub": b"E"})
    extractor = TextExtractor(tmp_path)
    try:
        seen, (u,) = prepare(book, extractor, lambda b, a: False)
    finally:
        extractor.close()
    assert seen is book and not u.unpack and u.note == "ZIP not unpacked (your choice): holds EPUB"


def test_a_damaged_archive_is_flagged_and_never_asked(tmp_path):
    book = zip_book(tmp_path, **{"Book.epub": b"E"})
    Path(book.formats["ZIP"]).write_bytes(b"not a zip")
    asked = []
    extractor = TextExtractor(tmp_path)
    try:
        seen, (u,) = prepare(book, extractor, lambda b, a: asked.append(a) or True)
    finally:
        extractor.close()
    assert seen is book and asked == [] and u.problem.startswith("can't be opened")


def test_asked_once_before_the_analysis_for_all_the_books():
    books = [make_book(formats={"RAR": "a.rar"}), make_book(formats={"EPUB": "b.epub", "ZIP": "b.zip"}),
             make_book(formats={"EPUB": "c.epub"})]
    asked = []
    answer = ask_once(books, lambda n: asked.append(n) or True)
    assert asked == [2] and answer(books[0], None) is True
    assert ask_once(books[2:], lambda n: asked.append(n) or True) is None and asked == [2]  # no archive: no question
    assert ask_once(books, None) is None


# --- Execute: what is sent to Calibre -----------------------------------------------------
def unpack(fmt="RAR", on=True) -> Unpack:
    return Unpack(fmt, "b.rar", 10, 20, add={"EPUB": "b.epub"}, sizes={"EPUB": 5}, unpack=on, planned=on)


def test_an_unpack_runs_on_a_ticked_book_left_in_the_source():
    item = PlanItem(make_book(id=7, title="T", last_modified="2026-10-03 09:00:00+00:00"), Action.LEAVE,
                    "not proven", Identity())
    item.archives = [unpack()]
    plan = Plan("s", "t", "x", items=[item])
    assert checkable(item) and actionable(plan) == []  # unticked: left as it is
    item.selected = True  # as the analysis ticks it (planner._review_files)
    assert actionable(plan) == [item]
    assert plan_actions(plan) == [{"op": "unpack", "src_id": 7, "title": "T", "unpack": [
        {"format": "RAR", "size": 10, "mtime": 20, "add": {"EPUB": "b.epub"}, "sizes": {"EPUB": 5}, "remove": True}],
        "stamp": "2026-10-03 09:00:00+00:00"}]
    item.archives[0].unpack = False  # right-click: keep the archive
    assert actionable(plan) == [] and not checkable(item)
    revert(item)
    assert item.archives[0].unpack


def test_a_book_trashed_whole_keeps_its_archive():
    item = PlanItem(make_book(id=7, title="T"), Action.TRASH, "dup", Identity(),
                    match=make_book(id=9, title="T"), add_formats=["EPUB"])
    item.archives = [unpack()]
    (action,) = plan_actions(Plan("s", "t", "x", items=[item]))
    assert action["op"] == "trash" and action["unpack"][0]["remove"] is False


def test_review_unpacks_a_ticked_book_unless_it_is_trashed():
    kept = ReviewItem(make_book(id=1, title="T", formats={"RAR": "b.rar"}), None, "")
    kept.archives = [unpack()]
    kept.tick_cleanup()  # as the scan does
    trashed = ReviewItem(make_book(id=2, title="T", formats={"RAR": "b.rar"}), None, "")
    trashed.archives = [unpack()]
    trashed.set_action(ReviewAction.TRASH)
    ops = review_actions([kept, trashed], {"title"})
    assert kept.selected and kept.checkable
    assert [(a["op"], "unpack" in a) for a in ops] == [("unpack", True), ("trash", False)]
    kept.selected = False  # unticked: left as it is
    assert [a["op"] for a in review_actions([kept], {"title"})] == []


def test_files_of_different_formats_with_different_names_are_left_packed(archive, tmp_path):
    u = plan_unpack("ZIP", archive, files(("Foundation.epub", 3), ("I, Robot.pdf", 6)), {"ZIP": archive})
    assert not u.problem and u.doubt == "files with different names (Foundation.epub, I, Robot.pdf): different books?"
    assert not plan_unpack("ZIP", archive, files(("Foundation.epub", 3), ("Asimov - Foundation.pdf", 6)),
                           {"ZIP": archive}).doubt
    book = zip_book(tmp_path, **{"Foundation.epub": b"E", "Robot.pdf": b"P"})
    extractor = TextExtractor(tmp_path)
    try:
        seen, (u,) = prepare(book, extractor, lambda b, a: True)  # "unpack" was answered: not this one
    finally:
        extractor.close()
    assert seen is book and u.asked and not u.unpack and u.note.startswith("ZIP not unpacked: files with different")
    item = PlanItem(book, Action.MOVE, "not in target", Identity(), archives=[u])
    planner._review_files(item, False)
    assert needs_review(item) and item.selected  # the move is no doubt: only the archive stays packed
    u.unpack = True  # right-click: unpack it after all
    assert not needs_review(item)


# --- the Calibre side (bridge_script), with a fake library ----------------------------------
class FakeLibrary:
    def __init__(self, fmt, path):
        self.files = {fmt: str(path)}
        self.log = []

    def format_abspath(self, book_id, fmt):
        return self.files.get(fmt)

    def formats(self, book_id):
        return tuple(self.files)

    def add_format(self, book_id, fmt, path, replace=False, run_hooks=True):
        assert not replace and not run_hooks  # never replaced, never converted
        self.log.append(("add", fmt, Path(path).read_bytes()))
        self.files[fmt] = path

    def remove_formats(self, formats):
        (gone,) = formats.values()
        self.log.append(("remove", *gone))
        for fmt in gone:
            del self.files[fmt]


def zipped(tmp_path, **members) -> Path:
    path = tmp_path / "b.zip"  # ZIP needs no Calibre; RAR and 7Z go through the same code
    with zipfile.ZipFile(path, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return path


def spec_for(path: Path, add: dict, sizes: dict) -> dict:
    st = path.stat()
    return {"format": "ZIP", "size": st.st_size, "mtime": int(st.st_mtime), "add": add, "sizes": sizes,
            "remove": True}


def test_execute_copies_the_record_then_adds_then_removes_the_archive(bridge, tmp_path, monkeypatch):
    path = zipped(tmp_path, **{"b.epub": b"EPUB!", "b.pdf": b"PDF"})
    db = FakeLibrary("ZIP", path)
    db.files["PDF"] = "old.pdf"  # the book got a PDF since the analysis: skipped, never replaced
    monkeypatch.setattr(bridge, "copy_verified", lambda src, book_id, dest: db.log.append(("copy",)) or 42)
    spec = spec_for(path, {"EPUB": "b.epub", "PDF": "b.pdf"}, {"EPUB": 5, "PDF": 3})
    msg = bridge.take_out(db, 1, None, {"unpack": [spec]}, str(tmp_path))
    assert db.log == [("copy",), ("add", "EPUB", b"EPUB!"), ("remove", "ZIP")]
    assert msg == "record copied to trash (id 42); ZIP unpacked: added EPUB; ZIP removed from source"
    assert not (tmp_path / "unpack_1_ZIP").exists()


def test_execute_refuses_an_archive_changed_since_the_analysis(bridge, tmp_path, monkeypatch):
    path = zipped(tmp_path, **{"b.epub": b"EPUB!"})
    db = FakeLibrary("ZIP", path)
    monkeypatch.setattr(bridge, "copy_verified", lambda *a: pytest.fail("nothing may be written"))
    spec = spec_for(path, {"EPUB": "b.epub"}, {"EPUB": 5})
    spec["size"] += 1
    with pytest.raises(RuntimeError, match="changed since the analysis"):
        bridge.take_out(db, 1, None, {"unpack": [spec]}, str(tmp_path))
    assert db.log == [] and "ZIP" in db.files
