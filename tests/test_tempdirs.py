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


import os
import time

import pytest

from calibre_dedup import tempdirs
from calibre_dedup.tempdirs import LOCK_NAME, RunDir, sweep


@pytest.fixture(autouse=True)
def temp(tmp_path, monkeypatch):
    """%TEMP% is this test's folder: the real one is never touched."""
    monkeypatch.setattr(tempdirs.tempfile, "gettempdir", lambda: str(tmp_path))
    return tmp_path


def aged(path, seconds):
    t = time.time() - seconds
    os.utime(path, (t, t))
    return path


def test_a_run_folder_is_locked_while_used_and_deleted_after(temp):
    run = RunDir("read_")
    assert run.path.parent == temp / "CalibreDedup" and (run.path / LOCK_NAME).is_file()
    (run.path / "c0.txt").write_text("text")
    run.cleanup()
    assert not run.path.exists()


def test_sweep_deletes_only_folders_no_program_uses(temp):
    running = RunDir("read_")
    (running.path / "c0.txt").write_text("text")
    killed = RunDir("read_")
    killed._lock.close()  # the process died: its lock is free
    killed._lock = None
    just_made = temp / "CalibreDedup" / "read_new"  # created, not locked yet
    just_made.mkdir()
    abandoned = temp / "CalibreDedup" / "read_old"  # no lock at all, an hour old
    abandoned.mkdir()
    aged(abandoned, 3600)
    assert sweep() == 2
    assert running.path.exists() and (running.path / "c0.txt").exists() and just_made.exists()
    assert not killed.path.exists() and not abandoned.exists()
    running.cleanup()


def test_sweep_deletes_old_folders_of_earlier_versions_only_after_a_day(temp):
    old, recent, other = temp / "cdr_old", temp / "cdr_exec_recent", temp / "someone_else"
    for folder in (old, recent, other):
        folder.mkdir()
    aged(old, 2 * 24 * 3600)
    aged(other, 2 * 24 * 3600)  # not ours: never touched
    assert sweep() == 1
    assert not old.exists() and recent.exists() and other.exists()


def test_sweep_with_nothing_to_do(temp):
    assert sweep() == 0
