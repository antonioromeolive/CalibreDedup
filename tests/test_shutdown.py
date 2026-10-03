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

"""Shut down when done, aware of the other windows (shutdown.py, gui/shutdown_box.py)."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from calibre_dedup import shutdown  # noqa: E402
from calibre_dedup.shutdown import (  # noqa: E402
    NOTHING, OTHER_SHUTS_DOWN, SHUT_DOWN, WAIT, Instance, State, decide,
)


def test_the_last_window_to_finish_shuts_down_when_none_is_busy():
    me = State("dedup", shutdown=True, ended=100.0, id="a")
    review = State("review", busy=True, activity="analyzing", id="b")
    assert decide(me, [review]) == (WAIT, [review])  # busy: waited for, ticked or not
    review.busy = False
    assert decide(me, [review]) == (SHUT_DOWN, [])  # idle without a run: it just closes
    later = State("review", shutdown=True, ended=200.0, id="c")
    assert decide(me, [later])[0] == OTHER_SHUTS_DOWN and decide(later, [me])[0] == SHUT_DOWN
    assert decide(State("dedup", shutdown=True, id="d"), [])[0] == NOTHING  # no run ended since ticked
    assert decide(State("dedup", ended=100.0, id="e"), [])[0] == NOTHING  # not ticked
    assert decide(State("dedup", shutdown=True, busy=True, ended=100.0, id="f"), [])[0] == NOTHING


def test_windows_see_each_other_and_share_the_switch(tmp_path):
    a = Instance("dedup", tmp_path)
    b = Instance("review", tmp_path)
    try:
        b.update(busy=True, activity="executing")
        [seen] = a.others()
        assert (seen.program, seen.busy, seen.describe()) == ("review", True, "calibre-review (executing)")
        a.set_armed(True)
        assert b.armed()
        b.set_armed(False)
        assert not a.armed()
        a.request_shutdown()
        assert b.shutdown_requested() and not a.shutdown_requested()  # never its own
    finally:
        a.close()
        b.close()
    assert list(tmp_path.glob("*.lock")) == []


def test_a_crashed_window_is_forgotten(tmp_path):
    a = Instance("dedup", tmp_path)
    (tmp_path / "dead.lock").write_bytes(b"")  # nobody holds its lock
    (tmp_path / "dead.json").write_text('{"program": "review", "busy": true}', encoding="utf-8")
    try:
        assert a.others() == [] and not (tmp_path / "dead.json").exists()
    finally:
        a.close()


def test_the_first_window_forgets_what_an_earlier_session_left(tmp_path):
    (tmp_path / shutdown.ARMED).write_text("{}", encoding="utf-8")
    a = Instance("dedup", tmp_path)
    try:
        assert not a.armed()
        a.set_armed(True)
        b = Instance("review", tmp_path)  # joins a running window: keeps the switch
        assert b.armed()
        b.close()
    finally:
        a.close()


def test_the_box_asks_first_and_follows_the_other_windows(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox
    from calibre_dedup.gui import shutdown_box
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(shutdown, "_folder", lambda: tmp_path)
    answers = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: answers.pop(0))
    w1, w2 = QMainWindow(), QMainWindow()
    w1.setWindowTitle("Dedup")
    w2.setWindowTitle("Review")
    s1 = shutdown_box.ShutdownWhenDone(w1, "dedup", lambda t: None)
    s2 = shutdown_box.ShutdownWhenDone(w2, "review", lambda t: None)
    try:
        answers.append(QMessageBox.No)
        s1.box.setChecked(True)
        assert not s1.box.isChecked() and not s1.instance.armed()  # not confirmed
        answers.append(QMessageBox.Yes)
        s1.box.setChecked(True)
        assert s1.box.isChecked() and not s1.banner.isHidden() and "SHUT DOWN" in s1.banner.text()
        assert w1.windowTitle().startswith(shutdown_box.TITLE_MARK)
        s2._tick()  # the other window follows, without asking
        assert s2.box.isChecked() and w2.windowTitle().startswith(shutdown_box.TITLE_MARK) and answers == []
        s2.box.setChecked(False)  # off in one, off in all
        s1._tick()
        assert not s1.box.isChecked() and w1.windowTitle() == "Dedup" and s1.banner.isHidden()
    finally:
        s1.close()
        s2.close()
