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


import pytest

from calibre_dedup import awake
from calibre_dedup.awake import ES_CONTINUOUS, ES_SYSTEM_REQUIRED, keep_awake


@pytest.fixture
def calls(monkeypatch):
    """The states asked of Windows: the real one is never touched."""
    seen = []
    monkeypatch.setattr(awake, "_set_state", lambda flags: seen.append(flags) or True)
    return seen


def test_sleep_is_held_off_during_the_block_and_allowed_after(calls):
    with keep_awake():
        assert calls == [ES_CONTINUOUS | ES_SYSTEM_REQUIRED]
    assert calls == [ES_CONTINUOUS | ES_SYSTEM_REQUIRED, ES_CONTINUOUS]


def test_sleep_is_allowed_again_when_the_run_fails(calls):
    with pytest.raises(RuntimeError):
        with keep_awake():
            raise RuntimeError("run failed")
    assert calls[-1] == ES_CONTINUOUS


def test_a_failing_power_call_never_stops_the_run(monkeypatch):
    def broken(flags):
        raise OSError("no power management")
    monkeypatch.setattr(awake, "_set_state", broken)
    ran = False
    with keep_awake():
        ran = True
    assert ran


def test_nothing_to_release_when_windows_refused(monkeypatch):
    seen = []
    monkeypatch.setattr(awake, "_set_state", lambda flags: seen.append(flags) and False)
    with keep_awake():
        pass
    assert seen == [ES_CONTINUOUS | ES_SYSTEM_REQUIRED]
