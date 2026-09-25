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
