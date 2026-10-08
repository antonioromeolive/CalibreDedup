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


"""bridge_script runs inside Calibre (calibre-debug): its Calibre imports are stubbed here."""

import importlib
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest


@pytest.fixture
def bridge(monkeypatch):
    for name in ("calibre", "calibre.db", "calibre.db.copy_to_library", "calibre.library", "calibre.utils",
                 "calibre.utils.date"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    sys.modules["calibre.db.copy_to_library"].copy_one_book = None
    sys.modules["calibre.library"].db = None
    date = sys.modules["calibre.utils.date"]
    date.as_local_time = date.local_tz = None
    date.as_utc = lambda d: d.astimezone(timezone.utc)
    date.parse_date = lambda text, assume_utc=True, as_utc=True: datetime.fromisoformat(text).astimezone(timezone.utc)
    monkeypatch.delitem(sys.modules, "calibre_dedup.bridge_script", raising=False)
    return importlib.import_module("calibre_dedup.bridge_script")


class FakeDB:
    def __init__(self, tags=()):
        self.tags = {1: tuple(tags)}

    def field_for(self, name, book_id):
        return self.tags[book_id]

    def set_field(self, name, values, allow_case_change=True):
        self.tags.update(values)


def test_ai_updated_tag_only_when_a_field_changed(bridge):
    db = FakeDB(["Fantasy"])
    action = {"updated_tag": "AIUpdated"}
    assert bridge.tag_updated(db, 1, [], action) == "" and db.tags[1] == ("Fantasy",)
    assert bridge.tag_updated(db, 1, ["year"], {}) == "" and db.tags[1] == ("Fantasy",)
    assert bridge.tag_updated(db, 1, ["year"], action) == "; tagged AIUpdated"
    assert db.tags[1] == ("Fantasy", "AIUpdated")
    bridge.tag_updated(db, 1, ["title"], action)  # already tagged: not twice
    assert db.tags[1] == ("Fantasy", "AIUpdated")


class FieldsDB:
    def __init__(self, **fields):
        self.fields = {k: {1: v} for k, v in fields.items()}

    def field_for(self, name, book_id):
        return self.fields[name][book_id]

    def set_field(self, name, values, allow_case_change=True):
        self.fields.setdefault(name, {}).update(values)


def test_a_book_changed_since_the_analysis_is_told_by_its_last_modified(bridge):
    db = FieldsDB(last_modified=datetime(2026, 10, 3, 9, 30, 15, tzinfo=timezone.utc))  # Calibre's API: no fractions
    seen = "2026-10-03 09:30:15.123456+00:00"  # as metadata.db has it, read by the analysis
    assert not bridge.changed_since(db, 1, seen)
    assert not bridge.changed_since(db, 1, "")  # not known: not checked
    assert bridge.changed_since(db, 1, "2026-10-03 09:30:14.999999+00:00")
    assert bridge.stamp(db, 1) == "2026-10-03T09:30:15+00:00"
    assert not bridge.changed_since(db, 1, bridge.stamp(db, 1))  # what the bridge returns, for the next Execute


def test_one_library_is_one_folder_whatever_its_spelling(bridge, tmp_path):
    assert bridge.same_folder(str(tmp_path / "Books"), str(tmp_path / "x" / ".." / "Books"))
    assert not bridge.same_folder(str(tmp_path / "Books"), str(tmp_path / "Trash"))
    assert not bridge.same_folder(str(tmp_path / "Books"), None)


class FormatsDB:
    """add_format as Calibre's: the file is copied in, unless the import plugins replace it."""

    def __init__(self, folder, convert=False):
        self.folder, self.convert, self.files, self.calls = folder, convert, {}, []
        folder.mkdir(parents=True, exist_ok=True)

    def add_format(self, book_id, fmt, path, replace=True, run_hooks=True):
        self.calls.append((fmt, replace, run_hooks))
        if run_hooks and self.convert:
            fmt = "ZIP"  # e.g. Calibre's "HTML to ZIP" plugin
        dest = self.folder / f"book.{fmt.lower()}"
        dest.write_bytes(Path(path).read_bytes())
        self.files[fmt] = str(dest)

    def format_abspath(self, book_id, fmt):
        return self.files.get(fmt)

    def formats(self, book_id):
        return tuple(self.files)


def test_a_format_is_added_as_it_is_and_checked(bridge, tmp_path):
    page = tmp_path / "page.html"
    page.write_bytes(b"<html>hello</html>")
    db = FormatsDB(tmp_path / "lib", convert=True)
    bridge.add_format(db, 1, "HTML", str(page))
    assert db.calls == [("HTML", False, False)] and db.formats(1) == ("HTML",)  # no import plugin, no replacing

    class Lost(FormatsDB):
        def add_format(self, *a, **k):
            pass  # Calibre did nothing
    with pytest.raises(RuntimeError, match="HTML could not be added to book #1"):
        bridge.add_format(Lost(tmp_path / "lost"), 1, "HTML", str(page))


def test_merging_adds_only_the_formats_the_kept_copy_lacks(bridge, tmp_path):
    src = FormatsDB(tmp_path / "source")
    for fmt in ("EPUB", "MOBI"):
        f = tmp_path / f"in.{fmt.lower()}"
        f.write_bytes(fmt.encode())
        src.add_format(1, fmt, str(f))
    keep = FormatsDB(tmp_path / "kept")
    keep.add_format(2, "EPUB", str(tmp_path / "in.epub"))
    keep.calls.clear()
    assert bridge.merge_formats(src, 1, keep, 2, ["EPUB", "MOBI"]) == ["MOBI"]
    assert keep.calls == [("MOBI", False, False)]


class BookDB(FieldsDB):
    def formats(self, book_id):
        return ()

    def format_abspath(self, book_id, fmt):
        return None


def test_review_writes_an_isbn_only_where_there_is_none_and_the_language(bridge):
    db = BookDB(identifiers={"amazon": "B00X"}, languages=("eng",), path="a/b")
    changed, path, formats = bridge.set_metadata(db, 1, {"isbn": "9788845207266", "language": "ita"})
    assert changed == ["isbn", "language"] and path == "a/b" and formats == {}
    assert db.fields["identifiers"][1] == {"amazon": "B00X", "isbn": "9788845207266"}
    assert db.fields["languages"][1] == ["ita"]
    db = BookDB(identifiers={"isbn": "9780000000002"}, path="a/b")  # has one: kept
    assert bridge.set_metadata(db, 1, {"isbn": "9788845207266"})[0] == []
    assert db.fields["identifiers"][1] == {"isbn": "9780000000002"}


class NamesDB(BookDB):
    """Calibre's authors, publishers and series: one item per name whatever its case. Writing a
    name in another case renames the item for every book that has it, unless allow_case_change=False."""

    def __init__(self, **fields):
        super().__init__(path="a/b", **fields)
        self.renamed = []

    def set_field(self, name, values, allow_case_change=True):
        for book_id, value in values.items():
            if name in ("authors", "publisher", "series", "tags"):
                many = isinstance(value, (list, tuple))
                spelled = []
                for v in (value if many else [value]):
                    known = {x.casefold(): x for b in self.fields.get(name, {}).values()
                             for x in (b if isinstance(b, (list, tuple)) else [b]) if x}
                    old = known.get(v.casefold())
                    if old is not None and old != v and allow_case_change:
                        self.renamed.append((old, v))  # every book with `old` now has `v`
                        for b, have in self.fields[name].items():
                            self.fields[name][b] = (tuple(v if x == old else x for x in have) if many
                                                    else v if have == old else have)
                    spelled.append(old if old is not None and not allow_case_change else v)
                value = tuple(spelled) if many else spelled[0]
            self.fields.setdefault(name, {})[book_id] = value


def test_review_writes_a_name_as_the_library_spells_it(bridge):
    db = NamesDB(publisher="Feltrinelli Editore", authors=("Mario Rossi",), series="Classici Italiani")
    db.fields["publisher"][2], db.fields["path"][2] = "Adelphi", "c/d"
    changed, _, _ = bridge.set_metadata(db, 2, {"publisher": "FELTRINELLI EDITORE", "authors": ["MARIO ROSSI"],
                                                "series": "Oscar Gialli"})
    assert db.renamed == []  # book 1 keeps its names: no rename for every book
    assert db.fields["publisher"] == {1: "Feltrinelli Editore", 2: "Feltrinelli Editore"}
    assert db.fields["authors"][2] == ("Mario Rossi",) and db.fields["series"][2] == "Oscar Gialli"
    assert changed == ["authors (as the library's 'Mario Rossi')", "publisher (as the library's 'Feltrinelli Editore')",
                       "series"]


def test_the_reviewed_tag_keeps_the_library_spelling(bridge):
    db = NamesDB(tags=("Fantasy",))
    db.fields["tags"][2] = ("aireviewed",)
    bridge.add_tag(db, [1], "AIReviewed")
    assert db.renamed == [] and db.fields["tags"] == {1: ("Fantasy", "aireviewed"), 2: ("aireviewed",)}
