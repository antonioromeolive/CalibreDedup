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

import logging

import pytest

pytest.importorskip("PySide6")
from calibre_dedup.gui.main_window import is_ai_record  # noqa: E402


def record(name, msg):
    return logging.LogRecord(name, logging.INFO, __file__, 1, msg, None, None)


@pytest.mark.parametrize("name,msg,ai", [
    ("calibre_dedup.ai", "AI call started: provider=ollama", True),
    ("calibre_dedup.ai", "AI reply is empty: taken as no metadata found", True),
    ("calibre_dedup.planner", "AI reading start of Dune — Frank Herbert", True),
    ("calibre_dedup.planner", "AI error on Dune: timed out", True),
    ("calibre_dedup.planner", "image AI disabled after 3 consecutive errors", True),
    ("calibre_dedup.session", "AI: text = Ollama · images = none", True),
    ("calibre_dedup.planner", "Decision: Dune -> Leave in source [AI read EPUB start]", False),
    ("calibre_dedup", "Analysis started", False),
    ("calibre_dedup.executor", "Book 12 (AIDA): moved to trash", False),
])
def test_ai_lines_are_recognised(name, msg, ai):
    assert is_ai_record(record(name, msg)) is ai
