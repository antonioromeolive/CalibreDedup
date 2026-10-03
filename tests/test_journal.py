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

"""The way back: a copy of metadata.db before each Execute, and the journal of what it did."""

import csv
import sqlite3
from datetime import datetime

import pytest

from calibre_dedup import journal, library_use


def library(path, book_id=7) -> str:
    path.mkdir(parents=True)
    con = sqlite3.connect(path / "metadata.db")
    con.execute("CREATE TABLE books (id INTEGER)")
    con.execute("INSERT INTO books VALUES (?)", (book_id,))
    con.commit()
    con.close()
    return str(path)


def test_a_copy_of_metadata_db_keeps_the_last_ones(tmp_path, monkeypatch):
    lib = library(tmp_path / "Books")
    stamps = iter(datetime(2026, 10, 3, 9, 0, s) for s in range(10))
    monkeypatch.setattr(journal, "datetime", type("Clock", (), {"now": staticmethod(lambda: next(stamps))}))
    copies = [journal.snapshot(lib, tmp_path / "snapshots", keep=2) for _ in range(3)]
    folder = copies[0].parent
    assert folder.name.startswith("Books_") and sorted(folder.glob("metadata_*.db")) == copies[1:]
    assert sqlite3.connect(copies[-1]).execute("SELECT id FROM books").fetchall() == [(7,)]
    assert (folder / "library.txt").read_text(encoding="utf-8").strip() == str((tmp_path / "Books").resolve())
    other = journal.snapshot(library(tmp_path / "x" / "Books"), tmp_path / "snapshots")  # same name, own folder
    assert other.parent != folder


def test_the_journal_has_a_row_per_book_and_an_empty_one_is_removed(tmp_path):
    with journal.Journal("dedup", tmp_path) as log:
        log.write(library="L", book_id=3, title="Dune", action="Trash only", ok="yes", result="moved to trash (id 9)",
                  why="duplicate of 'Dune'", match="#1 Dune — Frank Herbert")
    [row] = csv.DictReader(open(log.path, encoding="utf-8-sig"))
    assert row["title"] == "Dune" and row["why"] == "duplicate of 'Dune'" and row["match"].startswith("#1")
    assert row["time"] and row["before"] == ""
    with journal.Journal("review", tmp_path) as empty:  # only tags were written: nothing to keep
        pass
    assert not empty.path.exists() and log.path.exists()


def test_execute_does_nothing_when_metadata_db_cannot_be_copied(tmp_path, monkeypatch):
    from calibre_dedup.executor import ExecutionError, run_bridge
    monkeypatch.setattr(library_use, "_folder", lambda: tmp_path / "in_use")
    source = tmp_path / "Books"
    source.mkdir()
    (source / "metadata.db").write_bytes(b"not a database")
    with pytest.raises(ExecutionError, match="Nothing was done: the copy of .*metadata.db"):
        run_bridge(tmp_path / "no-calibre-here", {"source": str(source), "actions": []}, lambda msg: None,
                   snapshots=tmp_path / "snapshots")
