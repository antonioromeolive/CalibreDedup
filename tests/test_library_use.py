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
from pathlib import Path

import pytest

from calibre_dedup import library_use
from calibre_dedup.library_use import Conflict, ExecutionLock, LibraryInUse, LibraryUse, execution_conflicts


@pytest.fixture(autouse=True)
def folder(tmp_path, monkeypatch):
    """The markers go to this test's folder: the real config folder is never touched."""
    d = tmp_path / "in_use"
    monkeypatch.setattr(library_use, "_folder", lambda: d)
    return d


def test_a_library_being_analyzed_cannot_be_another_programs_trash(tmp_path):
    dedup = LibraryUse("dedup")
    dedup.claim([str(tmp_path / "Source"), str(tmp_path / "Target")], str(tmp_path / "Trash"))
    with pytest.raises(LibraryInUse, match="being analyzed by Calibre Duplicate Remover"):
        LibraryUse("review").claim([str(tmp_path / "Other")], str(tmp_path / "Target"))


def test_a_trash_library_cannot_be_analyzed_by_another_program(tmp_path):
    review = LibraryUse("review")  # held: a dropped LibraryUse closes its files, freeing the locks
    review.claim([str(tmp_path / "Books")], str(tmp_path / "Trash"))
    with pytest.raises(LibraryInUse, match="trash library of calibre-review"):
        LibraryUse("dedup").claim([str(tmp_path / "Trash"), str(tmp_path / "Target")], str(tmp_path / "Trash2"))


def test_programs_may_share_the_trash_and_the_analyzed_libraries(tmp_path):
    trash = str(tmp_path / "Trash")
    held = [LibraryUse("dedup"), LibraryUse("review"), LibraryUse("dedup")]
    held[0].claim([str(tmp_path / "A"), str(tmp_path / "T")], trash)
    held[1].claim([str(tmp_path / "A")], trash)  # same library analyzed: not checked
    held[2].claim([str(tmp_path / "B"), str(tmp_path / "T")], trash)


def test_the_same_library_written_differently_is_the_same(tmp_path):
    dedup = LibraryUse("dedup")
    dedup.claim([str(tmp_path / "Books")], "")
    with pytest.raises(LibraryInUse):
        LibraryUse("review").claim([str(tmp_path / "X")], str(tmp_path / "sub" / ".." / "BOOKS"))


def test_released_or_refused_leaves_nothing_behind(tmp_path, folder):
    first = LibraryUse("dedup")
    first.claim([str(tmp_path / "A")], str(tmp_path / "Trash"))
    second = LibraryUse("review")
    with pytest.raises(LibraryInUse):
        second.claim([str(tmp_path / "B")], str(tmp_path / "A"))
    assert len(list(folder.glob("*.lock"))) == 2  # only the first's
    first.release()
    assert not list(folder.glob("*.lock"))
    second.claim([str(tmp_path / "B")], str(tmp_path / "A"))


def test_a_marker_left_by_a_crashed_program_is_ignored_and_deleted(tmp_path, folder):
    crashed = LibraryUse("dedup")
    crashed.claim([str(tmp_path / "A")], "")
    marker, f = crashed._held[0]
    f.close()  # the system frees a killed program's locks; its file stays
    LibraryUse("review").claim([str(tmp_path / "B")], str(tmp_path / "A"))
    assert not marker.exists()


def _libs(tmp_path, source, target, trash):
    return {"source": str(tmp_path / source), "target": str(tmp_path / target), "trash": str(tmp_path / trash)}


def test_executions_on_different_libraries_run_side_by_side(tmp_path):
    first, second = ExecutionLock("dedup"), ExecutionLock("dedup")
    assert first.acquire(_libs(tmp_path, "S1", "T1", "Trash1")) == []
    assert second.acquire(_libs(tmp_path, "S2", "T2", "Trash2")) == []


def test_a_shared_trash_names_the_library_its_role_and_the_other_program(tmp_path):
    review = ExecutionLock("review")
    assert review.acquire({"source": str(tmp_path / "Books"), "trash": str(tmp_path / "Trash")}) == []
    conflicts = ExecutionLock("dedup").acquire(_libs(tmp_path, "S", "T", "Trash"))
    assert conflicts == [Conflict("trash", str(tmp_path / "Trash"), "review", "trash")]
    assert conflicts[0].describe("dedup") == (f"The trash library {tmp_path / 'Trash'} is being written by "
                                              "calibre-review (its trash library).")


def test_a_library_written_in_another_role_is_named_by_both_roles(tmp_path):
    review = ExecutionLock("review")
    review.acquire({"source": str(tmp_path / "Books"), "trash": ""})
    [conflict] = ExecutionLock("dedup").acquire(_libs(tmp_path, "S", "Books", "Trash"))
    assert conflict.describe("dedup") == (f"The target library {tmp_path / 'Books'} is being written by "
                                          "calibre-review (its library to review).")


def test_a_refused_execution_keeps_nothing_and_a_check_keeps_nothing(tmp_path, folder):
    first = ExecutionLock("dedup")
    first.acquire(_libs(tmp_path, "S", "T", "Trash"))
    assert len(execution_conflicts("dedup", _libs(tmp_path, "S2", "T2", "Trash"))) == 1
    assert ExecutionLock("dedup").acquire(_libs(tmp_path, "S2", "T2", "Trash"))
    assert len(list(folder.glob("*.lock"))) == 3  # only the first's
    first.release()
    second = ExecutionLock("dedup")
    assert second.acquire(_libs(tmp_path, "S2", "T2", "Trash")) == []
    assert execution_conflicts("dedup", _libs(tmp_path, "S3", "T3", "Other")) == []


def test_one_library_in_two_roles_is_locked_once(tmp_path, folder):
    held = ExecutionLock("dedup")
    assert held.acquire(_libs(tmp_path, "Books", "Books", "Trash")) == []
    assert len(list(folder.glob("*.lock"))) == 2


def test_run_bridge_refuses_before_starting_calibre(tmp_path):
    from calibre_dedup.executor import ExecutionError, run_bridge
    held = ExecutionLock("review")
    held.acquire({"source": str(tmp_path / "Books"), "trash": str(tmp_path / "Trash")})
    payload = {**_libs(tmp_path, "S", "T", "Trash"), "actions": []}
    with pytest.raises(ExecutionError, match="trash library .* is being written by calibre-review"):
        run_bridge(tmp_path / "no-calibre-here", payload, lambda msg: None)
