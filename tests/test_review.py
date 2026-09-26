"""calibre-review: what the AI read vs the metadata, actions, and the reviewer with a fake AI."""

import json

import pytest

from calibre_dedup.ai import AICache, AIError, Provider, ReviewMetadata
from calibre_dedup.config import ProviderProfile
from calibre_dedup.extract import Excerpt
from calibre_dedup.models import Book
from calibre_dedup.review import (
    FIELDS,
    ReviewAction, ReviewItem, Reviewer, apply_to_book, find_changes, format_value, review_actions, scan_library,
    summary,
)
from tests.test_planner import make_library


def book(**kw) -> Book:
    base = dict(id=1, title="Il nome della rosa", authors=["Umberto Eco"], publisher="Bompiani", pub_year=1980,
                isbns=set(), formats={"EPUB": "book.epub"}, uuid="u1", path="Eco/Il nome (1)", library="lib")
    base.update(kw)
    return Book(**base)


def meta(**kw) -> ReviewMetadata:
    base = dict(title="Il nome della rosa", authors=["Umberto Eco"], publisher="Bompiani", year=1980)
    base.update(kw)
    return ReviewMetadata(**base)


# --- parsing ---------------------------------------------------------------------
def test_reply_is_parsed_with_series_and_number():
    m = ReviewMetadata.from_json(json.dumps({"title": "Dune", "authors": "Frank Herbert", "publisher": None,
                                             "year": "1965", "series": "Dune", "series_index": "1,5"}))
    assert (m.title, m.authors, m.publisher, m.year, m.series, m.series_index) == (
        "Dune", ["Frank Herbert"], None, 1965, "Dune", 1.5)


@pytest.mark.parametrize("series,index,expected", [
    (None, 3, (None, None)),  # a number without a series means nothing
    ("null", 3, (None, None)),
    ("Urania", "n. 12", ("Urania", None)),
    ("Urania", 1234, ("Urania", 1234.0)),
])
def test_series_number_needs_a_series_and_a_number(series, index, expected):
    m = ReviewMetadata.from_json(json.dumps({"title": "x", "series": series, "series_index": index}))
    assert (m.series, m.series_index) == expected


def test_empty_reply_is_nothing_found():
    assert ReviewMetadata.from_json("") == ReviewMetadata()


# --- comparing -------------------------------------------------------------------
def test_same_values_written_differently_are_not_changes():
    found = meta(title="IL NOME DELLA ROSA", authors=["Eco, Umberto"], publisher="Bompiani Editore")
    assert find_changes(book(), found) == {}


def test_what_the_ai_did_not_find_is_never_a_change():
    assert find_changes(book(), ReviewMetadata()) == {}


def test_real_differences_are_changes():
    found = meta(title="Il pendolo di Foucault", authors=["Umberto Eco", "Mario Rossi"], publisher="Mondadori",
                 year=1988, series="Oscar", series_index=12)
    assert find_changes(book(), found) == {
        "title": "Il pendolo di Foucault", "authors": ["Umberto Eco", "Mario Rossi"], "publisher": "Mondadori",
        "year": 1988, "series": ("Oscar", 12)}


def test_unknown_title_and_missing_fields_are_filled():
    changes = find_changes(book(title="Unknown", authors=["Sconosciuto"], publisher=None, pub_year=None), meta())
    assert set(changes) == {"title", "authors", "publisher", "year"}


def test_same_series_keeps_its_name_and_changes_only_the_number():
    b = book(series="Il Giallo Mondadori", series_index=1.0)
    assert find_changes(b, meta(series="IL GIALLO MONDADORI", series_index=2345)) == {
        "series": ("Il Giallo Mondadori", 2345)}
    assert find_changes(b, meta(series="Il giallo Mondadori", series_index=None)) == {}


def test_values_are_shown_as_in_calibre():
    assert format_value("series", ("Urania", 12.0)) == "Urania [12]"
    assert format_value("series", ("Urania", 1.5)) == "Urania [1.5]"
    assert format_value("authors", ["A", "B"]) == "A & B"
    assert format_value("year", None) == ""


# --- actions ---------------------------------------------------------------------------
def test_books_with_differences_are_proposed_for_update_the_others_kept():
    changed = ReviewItem(book(), meta(year=1981))
    same = ReviewItem(book(id=2), meta())
    unread = ReviewItem(book(id=3), None, "no readable file")
    assert (changed.action, changed.selected) == (ReviewAction.UPDATE, True)
    assert (same.action, same.selected) == (ReviewAction.KEEP, False)
    assert (unread.action, unread.selected) == (ReviewAction.KEEP, False)


def test_only_checked_fields_that_are_on_and_not_excluded_are_written():
    it = ReviewItem(book(), meta(title="Altro", year=1981, series="Oscar", series_index=3))
    it.excluded.add("title")
    trash = ReviewItem(book(id=2), None)
    trash.set_action(ReviewAction.TRASH)
    kept = ReviewItem(book(id=3), meta(year=1999))
    kept.set_action(ReviewAction.KEEP)
    nothing = ReviewItem(book(id=4), meta(year=1999))  # only a field that is off
    actions = review_actions([it, trash, kept, nothing], {"title", "series"})
    assert actions == [
        {"src_id": 1, "title": "Il nome della rosa", "op": "set", "set": {"series": "Oscar", "series_index": 3},
         "tag": "AIReviewed"},
        {"src_id": 2, "title": "Il nome della rosa", "op": "trash", "no_target": True},
        {"op": "tag", "src_ids": [3, 4], "tag": "AIReviewed"},
    ]


def test_after_an_update_the_row_compares_the_new_values():
    it = ReviewItem(book(), meta(title="Il nome della rosa (ed. 2012)", year=2012))
    written = {"year": 2012}  # the title was not written
    it.book_changed(apply_to_book(it.book, written, "Eco/Il nome (1)", {"epub": "x.epub"}))
    assert it.book.pub_year == 2012 and it.book.formats == {"EPUB": "x.epub"}
    assert set(it.changes) == {"title"}
    assert it.selected is False


# --- the reviewer, with a fake AI -------------------------------------------------------
class FakeProvider(Provider):
    def __init__(self, reply="", fail=False, name="m"):
        super().__init__(ProviderProfile(name=name, model=name))
        self.reply, self.fail, self.calls = reply, fail, []

    def chat(self, system, user, images=None):
        self.calls.append((user, len(images or [])))
        if self.fail:
            raise AIError("down")
        return self.reply


class FakeExtractor:
    pdf_pages, text_chars = 6, 12000

    def __init__(self, text="Frontespizio"):
        self.text = text

    @staticmethod
    def pick_format(formats):
        return next(iter(formats.items()), None)

    def excerpt(self, formats, part):
        return Excerpt(text=self.text, source="EPUB start")

    def embedded_cover(self, formats):
        return None


REPLY = json.dumps({"title": "Dune", "authors": ["Frank Herbert"], "year": 1965, "series": "Dune",
                    "series_index": 1})


def test_reviewer_reads_once_then_uses_the_cache(tmp_path):
    lib = make_library(tmp_path / "lib", [{"title": "dune", "text": "x"}])
    text = FakeProvider(REPLY)
    reviewer = Reviewer(text, FakeExtractor(), AICache(tmp_path / "cache.json"))
    first = scan_library(lib, "", reviewer)
    second = scan_library(lib, "", reviewer)
    assert len(text.calls) == 1
    assert first.items[0].changes == second.items[0].changes == {"year": 1965, "series": ("Dune", 1.0)}
    assert "cached" in second.items[0].note


def test_with_an_image_ai_the_cover_is_sent_first(tmp_path, monkeypatch):
    lib = make_library(tmp_path / "lib", [{"title": "Dune", "text": "x"}])
    (tmp_path / "lib" / "a/b (1)" / "cover.jpg").write_bytes(b"jpg")
    monkeypatch.setattr("calibre_dedup.review.cover_png", lambda data: "PNG")
    text, vision = FakeProvider(REPLY), FakeProvider(REPLY, name="v")
    reviewer = Reviewer(text, FakeExtractor(), AICache(tmp_path / "cache.json"), vision)
    item = scan_library(lib, "", reviewer).items[0]
    assert text.calls == [] and vision.calls[0][1] == 1
    assert vision.calls[0][0].startswith("The first attached image is the book's cover.")
    assert "cover" in item.note


def test_an_ai_that_keeps_failing_asks_and_can_be_skipped(tmp_path):
    lib = make_library(tmp_path / "lib", [{"title": f"Book {i}", "text": "x"} for i in range(5)])
    asked = []
    reviewer = Reviewer(FakeProvider(fail=True), FakeExtractor(), AICache(tmp_path / "cache.json"),
                        on_down=lambda msg, image: asked.append(image) or False)
    result = scan_library(lib, "", reviewer)
    assert asked == [False]
    assert all(i.found is None for i in result.items)
    assert result.ai_down and "disabled" in result.items[-1].note


class FilteringProvider(FakeProvider):
    """Refuses any request whose text holds the violent passage."""
    def chat(self, system, user, images=None):
        self.calls.append((user, len(images or [])))
        if "SANGUE" in user:
            raise AIError("Azure OpenAI content filter refused the request (violence)", status=400, filtered=True)
        return self.reply


def test_a_filtered_book_is_asked_again_with_less_and_nothing_is_disabled(tmp_path, monkeypatch):
    lib = make_library(tmp_path / "lib", [{"title": f"Book {i}", "text": "x"} for i in range(4)])
    monkeypatch.setattr(Reviewer, "_cover", lambda self, b: ("PNG", ""))
    vision = FilteringProvider(REPLY, name="v")
    title_page = "Frontespizio " * 10
    # the violence is after the title page: the shorter text is accepted
    reviewer = Reviewer(FakeProvider(), FakeExtractor(title_page + "x" * 5000 + " SANGUE"),
                        AICache(tmp_path / "c1.json"), vision, on_down=lambda m, i: pytest.fail("asked"))
    items = scan_library(lib, "", reviewer).items
    assert all(i.found is not None for i in items) and "first 3000 chars" in items[0].note
    assert not reviewer.image_disabled_reason
    # violence on the first page: only the cover is read
    vision.calls.clear()
    reviewer = Reviewer(FakeProvider(), FakeExtractor("SANGUE " + "x" * 5000), AICache(tmp_path / "c2.json"),
                        vision, on_down=lambda m, i: pytest.fail("asked"))
    items = scan_library(lib, "", reviewer).items
    assert all(i.found is not None for i in items) and "cover only" in items[0].note
    assert vision.calls[2] == ("The first attached image is the book's cover.\nNo text could be extracted.", 1)


def test_filtered_errors_never_turn_the_ai_off(tmp_path):
    lib = make_library(tmp_path / "lib", [{"title": f"Book {i}", "text": "x"} for i in range(5)])
    reviewer = Reviewer(FilteringProvider(REPLY), FakeExtractor("SANGUE"), AICache(tmp_path / "c.json"),
                        on_down=lambda m, i: pytest.fail("asked"))
    result = scan_library(lib, "", reviewer)
    assert all(i.found is None and "content filter" in i.note for i in result.items)
    assert not reviewer.disabled_reason and not result.ai_down


def test_trash_library_must_differ_from_the_reviewed_one(tmp_path):
    lib = make_library(tmp_path / "lib", [])
    with pytest.raises(Exception, match="different"):
        scan_library(lib, lib, Reviewer(FakeProvider(), FakeExtractor(), AICache(tmp_path / "c.json")))


# --- the AIReviewed tag ---------------------------------------------------------------------
def test_books_tagged_reviewed_are_skipped_unless_asked(tmp_path):
    lib = make_library(tmp_path / "lib", [{"title": "Dune", "text": "x", "tags": ["SF", "aireviewed"]},
                                          {"title": "Emma", "text": "x", "tags": ["Classic"]}])
    text = FakeProvider(REPLY)
    reviewer = Reviewer(text, FakeExtractor(), AICache(tmp_path / "cache.json"))
    result = scan_library(lib, "", reviewer)
    assert [i.book.title for i in result.items] == ["Emma"]
    assert (result.skipped, result.total_books) == (1, 1)
    assert "1 already AIReviewed" in summary(result)
    everything = scan_library(lib, "", reviewer, skip_reviewed=False)
    assert len(everything.items) == 2 and everything.skipped == 0


def test_every_book_read_gets_the_tag_updated_or_not():
    updated = ReviewItem(book(), meta(year=1981))
    same = ReviewItem(book(id=2), meta())  # nothing to change
    unchecked = ReviewItem(book(id=3), meta(year=1999))
    unchecked.selected = False
    unread = ReviewItem(book(id=4), None, "no readable file")  # retried by the next scan
    kept = ReviewItem(book(id=5), None, "AI error")
    kept.set_action(ReviewAction.KEEP)  # the user decided: reviewed
    trashed = ReviewItem(book(id=6), meta())
    trashed.set_action(ReviewAction.TRASH)
    already = ReviewItem(book(id=7, tags={"AIReviewed"}), meta())
    actions = review_actions([updated, same, unchecked, unread, kept, trashed, already], {"year"})
    assert [(a.get("src_id"), a["op"], a.get("tag"), a.get("src_ids")) for a in actions] == [
        (1, "set", "AIReviewed", None),
        (6, "trash", None, None),
        (None, "tag", "AIReviewed", [2, 3, 5]),
    ]


def test_a_record_with_no_files_is_proposed_for_the_trash():
    empty = ReviewItem(book(formats={}), None, "no files in Calibre")
    missing = ReviewItem(book(id=2, formats={"EPUB": "gone.epub"}), None, "no readable file")
    assert (empty.action, empty.selected) == (ReviewAction.TRASH, True)
    assert (missing.action, missing.selected) == (ReviewAction.KEEP, False)
    actions = review_actions([empty, missing], set(FIELDS))
    assert actions == [{"src_id": 1, "title": "Il nome della rosa", "op": "trash", "no_target": True}]
