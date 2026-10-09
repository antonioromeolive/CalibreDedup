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
import gzip
import sqlite3
from dataclasses import asdict

import pytest

from calibre_dedup import plan_store
from calibre_dedup.archives import Unpack
from calibre_dedup.config import Settings
from calibre_dedup.executor import apply_result
from calibre_dedup.models import Action
from calibre_dedup.planner import build_plan
from calibre_dedup.selection import override
from calibre_dedup.session import analysis_signature, changed_settings
from tests.test_planner import libs, make_library  # noqa: F401  (a fixture)


@pytest.fixture
def folder(tmp_path):
    return tmp_path / "plans"


@pytest.fixture
def plan(libs):
    src, tgt, trash = libs(
        source=[
            {"title": "Dune", "publisher": "Ace", "year": 1990, "isbn": "9780441172719", "tags": ["sf"]},
            {"title": "Dune Messiah", "publisher": "Ace", "year": 1990},
            {"title": "Children of Dune"},
        ],
        target=[
            {"title": "Dune", "publisher": "Ace Books", "year": 1990, "formats": ["PDF"]},
            {"title": "Children of Dune", "publisher": "Ace", "year": 1991},
        ],
    )
    p = build_plan(src, tgt, trash)
    p.state = plan_store.libraries_state(src, tgt)
    return p


def test_a_saved_plan_comes_back_as_it_was(plan, folder):
    """Every field: the books' sets, the identity's series, the archives, the moves, the
    user's changes and ticks, and the analysis' own decision behind an override."""
    plan.items[0].identity.series = ("dune", 1.0)
    plan.items[0].archives = [Unpack("RAR", "x.rar", 10, 20, add={"EPUB": "a.epub"}, unpack=True, planned=True)]
    plan.items[0].bad_formats = {"DOC": "Calibre can't open it"}
    plan.moved = {7: 70}
    override(plan.items[1], Action.LEAVE)
    plan.items[2].selected = not plan.items[2].selected
    plan.items[2].reviewed = True
    sig = analysis_signature(Settings())
    plan_store.save(plan, sig, executed=4, version="0.1.24", folder=folder, exec_stopped=True)

    saved = plan_store.load(plan.source_library, plan.target_library, folder=folder)

    assert asdict(saved.plan) == asdict(plan)
    assert saved.plan.items[1].manual and saved.plan.items[1].planned_action is Action.MOVE
    assert isinstance(saved.plan.items[0].source.isbns, set) and saved.plan.moved == {7: 70}
    assert (saved.executed, saved.version, saved.exec_stopped) == (4, "0.1.24", True)
    assert changed_settings(saved.signature, sig) == []  # read back from JSON: no false "settings changed"


def test_a_plan_fits_its_libraries_until_something_else_changes_them(plan, folder):
    assert plan_store.changes(plan) == []
    conn = sqlite3.connect(f"{plan.target_library}/metadata.db")
    conn.execute("UPDATE books SET last_modified = '2026-10-09 10:00:00+00:00' WHERE id = 1")
    conn.commit()
    conn.close()
    assert plan_store.changes(plan) == ["books of the target library were changed"]
    make_library_book(plan.source_library)
    assert plan_store.changes(plan)[0] == "the source library has 4 books, 3 then"


def make_library_book(library: str) -> None:
    conn = sqlite3.connect(f"{library}/metadata.db")
    conn.execute("INSERT INTO books (title, path, uuid) VALUES ('New', 'n', 'u-new')")
    conn.commit()
    conn.close()


def test_what_an_execute_writes_keeps_the_plan_fitting(plan):
    """The Execute's own writes are taken into the plan's state; a change by something else
    before it makes the plan stale for good."""
    plan_store.refresh_state(plan, before=True)
    make_library_book(plan.target_library)  # stands in for the Execute's move
    plan_store.refresh_state(plan, before=False)
    assert plan.state is not None and plan_store.changes(plan) == []

    make_library_book(plan.target_library)  # someone else, before the next Execute
    plan_store.refresh_state(plan, before=True)
    plan_store.refresh_state(plan, before=False)
    assert plan.state is None and plan_store.changes(plan) == ["the libraries were changed while the plan was open"]


def test_executed_books_stay_executed_in_the_saved_plan(plan, folder):
    item = plan.items[1]  # Dune Messiah: moved
    apply_result(plan, item, {"op": "move", "kept": {"library": "target", "id": 9, "path": "p",
                                                     "formats": {}, "stamp": "s"}})
    plan_store.save(plan, {}, folder=folder)
    saved = plan_store.load(plan.source_library, plan.target_library, folder=folder)
    assert saved.plan.items[1].done and saved.plan.moved == {item.source.id: 9}


def test_each_tag_filter_has_its_own_plan(plan, folder):
    plan_store.save(plan, {}, folder=folder)
    assert plan_store.load(plan.source_library, plan.target_library, "sf", folder=folder) is None
    assert plan_store.load(plan.source_library, plan.target_library, folder=folder) is not None


def test_a_plan_with_nothing_left_is_deleted(plan, folder):
    plan_store.save(plan, {}, folder=folder)
    plan.items = []
    plan_store.save(plan, {}, folder=folder)
    assert not list(folder.iterdir())


@pytest.mark.parametrize("content", [b"not gzip", gzip.compress(b'{"format": 0}')])
def test_a_plan_that_cant_be_read_is_deleted(plan, folder, content):
    path = plan_store.save(plan, {}, folder=folder)
    path.write_bytes(content)
    assert plan_store.load(plan.source_library, plan.target_library, folder=folder) is None
    assert not path.exists()


def test_only_the_plans_of_the_last_libraries_are_kept(tmp_path, folder, monkeypatch):
    monkeypatch.setattr(plan_store, "KEEP_PLANS", 2)
    plans = []
    for n in range(3):
        lib = make_library(tmp_path / f"lib{n}", [{"title": "Dune"}])
        p = build_plan(lib, lib, str(tmp_path / "trash"))
        plan_store.save(p, {}, folder=folder)
        plans.append(p)
    assert len(list(folder.iterdir())) == 2
    assert plan_store.load(plans[2].source_library, plans[2].target_library, folder=folder) is not None


def test_one_library_is_described_once(tmp_path):
    lib = make_library(tmp_path / "lib", [{"title": "Dune"}])
    p = build_plan(lib, lib, str(tmp_path / "trash"))
    p.state = plan_store.libraries_state(lib, lib)
    make_library_book(lib)
    assert plan_store.changes(p) == ["the library has 2 books, 1 then"]
