import pytest

pytest.importorskip("PySide6")
from calibre_dedup.gui.main_window import has_no_duplicate  # noqa: E402
from calibre_dedup.models import Action, Book, Identity, PlanItem  # noqa: E402

OTHER = Book(99, "Dune", ["Frank Herbert"], None, None, set(), {}, "u99", "p99", "L")


def item(action, title="Dune", authors=("Frank Herbert",), match=None):
    book = Book(1, title, list(authors), None, None, set(), {"EPUB": "x"}, "u1", "p1", "L")
    return PlanItem(book, action, "reason", Identity(title=title or None, authors=list(authors)), match=match)


def test_no_duplicate_means_left_with_nothing_to_compare_or_only_different_editions():
    assert has_no_duplicate(item(Action.LEAVE))


@pytest.mark.parametrize("it", [
    item(Action.LEAVE, match=OTHER),              # undecided pair: to review
    item(Action.LEAVE, title="", authors=()),     # title/authors unreadable: to review
    item(Action.MOVE),
    item(Action.TRASH, match=OTHER),
])
def test_everything_else_is_not_counted_as_no_duplicate(it):
    assert not has_no_duplicate(it)
