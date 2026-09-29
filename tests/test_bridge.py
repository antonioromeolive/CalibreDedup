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

import pytest


@pytest.fixture
def bridge(monkeypatch):
    for name in ("calibre", "calibre.db", "calibre.db.copy_to_library", "calibre.library", "calibre.utils",
                 "calibre.utils.date"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    sys.modules["calibre.db.copy_to_library"].copy_one_book = None
    sys.modules["calibre.library"].db = None
    sys.modules["calibre.utils.date"].as_local_time = sys.modules["calibre.utils.date"].local_tz = None
    monkeypatch.delitem(sys.modules, "calibre_dedup.bridge_script", raising=False)
    return importlib.import_module("calibre_dedup.bridge_script")


class FakeDB:
    def __init__(self, tags=()):
        self.tags = {1: tuple(tags)}

    def field_for(self, name, book_id):
        return self.tags[book_id]

    def set_field(self, name, values):
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

    def set_field(self, name, values):
        self.fields.setdefault(name, {}).update(values)


def test_swapped_title_and_author_are_written_only_if_unchanged(bridge):
    swap = {"title": "Underwoods", "authors": ["Kingston"], "tag": "TitleAuthorSwapped",
            "was_title": "Kingston", "was_authors": ["Underwoods"]}
    db = FieldsDB(title="Kingston", authors=("Underwoods",), tags=("Novels",))
    assert bridge.swap_title_author(db, 1, swap) == "; title and author swapped back, tagged TitleAuthorSwapped"
    assert db.fields["title"][1] == "Underwoods" and db.fields["authors"][1] == ["Kingston"]
    assert db.fields["tags"][1] == ("Novels", "TitleAuthorSwapped")
    db = FieldsDB(title="Kingston", authors=("William Henry Giles Kingston",), tags=())  # fixed by hand meanwhile
    assert "changed since the analysis" in bridge.swap_title_author(db, 1, swap)
    assert db.fields["title"][1] == "Kingston" and db.fields["tags"][1] == ()
    assert bridge.swap_title_author(db, 1, None) == ""
