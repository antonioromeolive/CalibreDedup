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

"""calibre-review: what the AI read vs the metadata, actions, and the reviewer with a fake AI."""

import json

import pytest

from calibre_dedup.ai import AICache, AIError, Provider, ReviewMetadata
from calibre_dedup.config import ProviderProfile
from calibre_dedup.extract import Excerpt
from calibre_dedup.models import Book
from calibre_dedup.review import (
    FIELDS,
    ReviewAction, ReviewItem, Reviewer, apply_to_book, ask_ai, find_changes, format_value, mark_series_publishers,
    read_evidence, review_actions, scan_library, summary,
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


def read(b: Book, found: ReviewMetadata, text: str) -> ReviewMetadata:
    """`found`, as read in a book whose first pages are `text` (its evidence)."""
    found.evidence = read_evidence(b, found, text)
    return found


# --- parsing ---------------------------------------------------------------------
def test_reply_is_parsed_with_series_and_number():
    m = ReviewMetadata.from_json(json.dumps({"title": "Dune", "authors": "Frank Herbert", "publisher": None,
                                             "year": "1965", "series": "Dune", "series_index": "1,5"}))
    assert (m.title, m.authors, m.publisher, m.year, m.series, m.series_index) == (
        "Dune", ["Frank Herbert"], None, 1965, "Dune", 1.5)


@pytest.mark.parametrize("series,index,expected", [
    (None, 3, (None, None)),  # a number without a series means nothing
    ("null", 3, (None, None)),
    ("Gutenberg", "n. 12", ("Gutenberg", None)),
    ("Gutenberg", 1234, ("Gutenberg", 1234.0)),
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


@pytest.mark.parametrize("current,read", [
    ("Fantozzi: la trilogia", "Fantozzi"),  # a subtitle
    ("Il nome della rosa - Postille", "Il nome della rosa"),
    ("Storia d'Italia (2)", "Storia d'Italia"),  # a volume
    ("Dune parte 2", "Dune"),
])
def test_a_title_that_only_drops_a_subtitle_or_volume_is_not_proposed(current, read):
    assert "title" not in find_changes(book(title=current), meta(title=read))


@pytest.mark.parametrize("current,read", [
    ("La forza della ragione (Italian Edition)", "La forza della ragione"),  # an edition note
    ("Il nome della rosa - Umberto Eco", "Il nome della rosa"),  # the author's name
    ("Il nome della rosa (Everyman)", "Il nome della rosa"),  # a collection
    ("Il nome della rosa (Itali", "Il nome della rosa"),  # cut short
    ("Il nome", "Il nome della rosa"),  # longer: says more
])
def test_a_title_that_cleans_up_noise_is_proposed(current, read):
    assert find_changes(book(title=current), meta(title=read))["title"] == read


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
    assert format_value("series", ("Gutenberg", 12.0)) == "Gutenberg [12]"
    assert format_value("series", ("Gutenberg", 1.5)) == "Gutenberg [1.5]"
    assert format_value("authors", ["A", "B"]) == "A & B"
    assert format_value("year", None) == ""


# --- actions ---------------------------------------------------------------------------
def test_books_with_differences_are_proposed_for_update_the_others_kept():
    changed = ReviewItem(book(), read(book(), meta(year=1981), "Umberto Eco IL NOME DELLA ROSA Bompiani 1981"))
    same = ReviewItem(book(id=2), meta())
    unread = ReviewItem(book(id=3), None, "no readable file")
    assert (changed.action, changed.selected) == (ReviewAction.UPDATE, True)
    assert (same.action, same.selected) == (ReviewAction.KEEP, False)
    assert (unread.action, unread.selected) == (ReviewAction.KEEP, False)


def test_only_checked_fields_that_are_on_and_not_excluded_are_written():
    it = ReviewItem(book(), read(book(), meta(title="Altro", year=1981, series="Oscar", series_index=3),
                                 PAGES + "\nOscar 3"))
    it.excluded.add("title")
    it.reviewed = True  # as the window marks it when the user turns a field off
    trash = ReviewItem(book(id=2), None)
    trash.set_action(ReviewAction.TRASH)
    kept = ReviewItem(book(id=3), meta(year=1999))
    kept.set_action(ReviewAction.KEEP)
    nothing = ReviewItem(book(id=4), meta(year=1999))  # only a field that is off
    actions = review_actions([it, trash, kept, nothing], {"title", "series"})
    assert actions == [
        {"src_id": 1, "title": "Il nome della rosa", "stamp": "", "op": "set",
         "set": {"series": "Oscar", "series_index": 3}, "updated_tag": "AIUpdated", "tag": "AIReviewed"},
        {"src_id": 2, "title": "Il nome della rosa", "stamp": "", "op": "trash", "no_target": True},
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
DUNE_PAGES = "FRANK HERBERT\nDUNE\nIl ciclo di Dune 1\n1965"  # what REPLY reads, printed


def test_reviewer_reads_once_then_uses_the_cache(tmp_path):
    lib = make_library(tmp_path / "lib", [{"title": "dune", "text": "x"}])
    text = FakeProvider(REPLY)
    reviewer = Reviewer(text, FakeExtractor(), AICache(tmp_path / "cache.json"))
    first = scan_library(lib, "", reviewer)
    second = scan_library(lib, "", reviewer)
    assert len(text.calls) == 1
    assert first.items[0].changes == second.items[0].changes == {"year": 1965, "series": ("Dune", 1.0)}
    assert "cached" in second.items[0].note


def test_asking_the_ai_again_skips_the_cache_and_rebuilds_the_row(tmp_path):
    lib = make_library(tmp_path / "lib", [{"title": "dune", "text": "x"}])
    cache = AICache(tmp_path / "cache.json")
    old = scan_library(lib, "", Reviewer(FakeProvider(""), FakeExtractor(), cache)).items[0]
    assert old.changes == {}
    old.set_action(ReviewAction.TRASH)  # what the user did to the old row is not kept
    text = FakeProvider(REPLY)
    reviewer = Reviewer(text, FakeExtractor(DUNE_PAGES), cache)
    seen = []
    [(was, new)] = ask_ai(reviewer, [old], on_item=lambda a, b: seen.append((a, b)))
    assert was is old and seen == [(old, new)] and len(text.calls) == 1
    assert new.changes == {"year": 1965, "series": ("Dune", 1.0)}
    assert (new.action, new.selected, new.manual) == (ReviewAction.UPDATE, True, False)
    assert not reviewer.fresh
    # the new answer is cached: a scan with the same model reuses it
    again = scan_library(lib, "", reviewer).items[0]
    assert len(text.calls) == 1 and "cached" in again.note and again.changes == new.changes


def test_asking_the_ai_with_another_model_keeps_the_first_models_answer(tmp_path):
    lib = make_library(tmp_path / "lib", [{"title": "dune", "text": "x"}])
    cache = AICache(tmp_path / "cache.json")
    first = FakeProvider(json.dumps({"year": 1970}), name="a")
    old = scan_library(lib, "", Reviewer(first, FakeExtractor(), cache)).items[0]
    other = FakeProvider(REPLY, name="b")
    [(_, new)] = ask_ai(Reviewer(other, FakeExtractor(), cache), [old])
    assert "year" in new.changes and new.changes["year"] == 1965
    back = scan_library(lib, "", Reviewer(first, FakeExtractor(), cache)).items[0]
    assert len(first.calls) == 1 and back.changes == {"year": 1970}


def test_asking_the_ai_stops_when_cancelled(tmp_path):
    import threading
    lib = make_library(tmp_path / "lib", [{"title": f"Book {i}", "text": "x"} for i in range(3)])
    cache = AICache(tmp_path / "cache.json")
    items = scan_library(lib, "", Reviewer(FakeProvider(REPLY), FakeExtractor(), cache)).items
    cancel = threading.Event()
    done = ask_ai(Reviewer(FakeProvider(REPLY), FakeExtractor(), cache), items, cancel=cancel,
                  on_item=lambda *_: cancel.set())
    assert len(done) == 1


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


def test_skip_this_book_keeps_the_ai_on_and_asks_again_after_as_many_errors(tmp_path):
    from calibre_dedup.planner import SKIP_BOOK
    lib = make_library(tmp_path / "lib", [{"title": f"Book {i}", "text": "x"} for i in range(7)])
    asked = []
    reviewer = Reviewer(FakeProvider(fail=True), FakeExtractor(), AICache(tmp_path / "cache.json"),
                        on_down=lambda msg, image: asked.append(msg) or SKIP_BOOK)
    result = scan_library(lib, "", reviewer)
    assert len(asked) == 2  # after books 3 and 6
    assert not reviewer.disabled_reason and not result.ai_down
    assert all(i.found is None and i.note == "AI error: down" for i in result.items)


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
    assert vision.calls[3] == ("The first attached image is the book's cover.\nNo text could be extracted.", 1)


class ImageFilteringProvider(FakeProvider):
    """Refuses any request with images, as Azure does for a cover it will not look at."""
    def chat(self, system, user, images=None):
        self.calls.append((user, len(images or [])))
        if images:
            raise AIError("Azure OpenAI content filter refused the image", status=400, filtered=True)
        return self.reply


def test_a_refused_cover_is_skipped_and_the_text_is_read_alone(tmp_path, monkeypatch):
    lib = make_library(tmp_path / "lib", [{"title": f"Book {i}", "text": "x"} for i in range(4)])
    monkeypatch.setattr(Reviewer, "_cover", lambda self, b: ("PNG", ""))
    vision = ImageFilteringProvider(REPLY, name="v")
    reviewer = Reviewer(FakeProvider(), FakeExtractor("Frontespizio"), AICache(tmp_path / "c.json"),
                        vision, on_down=lambda m, i: pytest.fail("asked"))
    items = scan_library(lib, "", reviewer).items
    assert all(i.found is not None and "text only" in i.note for i in items)
    assert not reviewer.image_disabled_reason


def test_filtered_errors_never_turn_the_ai_off(tmp_path):
    lib = make_library(tmp_path / "lib", [{"title": f"Book {i}", "text": "x"} for i in range(5)])
    reviewer = Reviewer(FilteringProvider(REPLY), FakeExtractor("SANGUE"), AICache(tmp_path / "c.json"),
                        on_down=lambda m, i: pytest.fail("asked"))
    result = scan_library(lib, "", reviewer)
    assert all(i.found is None and "content filter" in i.note for i in result.items)
    assert not reviewer.disabled_reason and not result.ai_down


class SmallContextProvider(FakeProvider):
    """A model with a 2000-token context (one character = one token)."""
    def chat(self, system, user, images=None):
        self.calls.append((user, len(images or [])))
        if len(user) > 2000:
            raise AIError(f'Ollama error 400: {{"message":"request ({len(user)} tokens) exceeds the available '
                          f'context size (2000 tokens)"}}', status=400)
        return self.reply


def test_a_book_longer_than_the_context_is_read_again_shorter_and_nothing_is_disabled(tmp_path):
    lib = make_library(tmp_path / "lib", [{"title": f"Book {i}", "text": "x"} for i in range(4)])
    vision = SmallContextProvider(REPLY, name="v")
    reviewer = Reviewer(FakeProvider(), FakeExtractor("Frontespizio " * 1000), AICache(tmp_path / "c.json"),
                        vision, on_down=lambda m, i: pytest.fail("asked"))
    items = scan_library(lib, "", reviewer).items
    assert all(i.found is not None and "context size" in i.note for i in items)
    assert not reviewer.image_disabled_reason


def test_a_book_whose_file_is_another_format_is_proposed_for_the_trash_unread(tmp_path):
    lib = make_library(tmp_path / "lib", [{"title": "Il giocatore", "text": "x"}, {"title": "Dune", "text": "x"}])
    (tmp_path / "lib" / "a/b (1)" / "book.epub").write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(600))
    text = FakeProvider(REPLY)
    items = scan_library(lib, "", Reviewer(text, FakeExtractor(), AICache(tmp_path / "c.json"))).items
    assert (items[0].action, items[0].selected, items[0].found) == (ReviewAction.TRASH, True, None)
    assert "EPUB file is really a Word 97-2003 document" in items[0].note
    assert len(text.calls) == 1 and items[1].action is ReviewAction.UPDATE


def test_file_problem_checks_the_first_bytes(tmp_path):
    from calibre_dedup.extract import file_problem
    def write(name, data):
        (tmp_path / name).write_bytes(data)
        return str(tmp_path / name)
    assert file_problem("PDF", write("ok.pdf", b"%PDF-1.4\n...")) == ""
    assert "a Microsoft Reader (LIT) book" in file_problem("PDF", write("lit.pdf", b"ITOLITLS" + bytes(100)))
    assert "a MOBI book" in file_problem("PDF", write("mobi.pdf", b"ernani" + bytes(54) + b"BOOKMOBI"))
    assert "a JPEG image" in file_problem("PDF", write("jpg.pdf", b"\xff\xd8\xff\xeb" + bytes(100)))
    assert "empty" in file_problem("EPUB", write("empty.epub", b""))
    assert file_problem("TXT", write("any.txt", b"\xff\xd8\xff")) == ""  # formats without a signature: not checked
    assert file_problem("PDF", str(tmp_path / "missing.pdf")) == ""


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


def test_the_books_done_with_get_the_tag():
    updated = ReviewItem(book(), read(book(), meta(year=1981), "Umberto Eco IL NOME DELLA ROSA Bompiani 1981"))
    same = ReviewItem(book(id=2), meta())  # nothing to change
    unchecked = ReviewItem(book(id=3), meta(year=1999))  # left for later: untouched, shown again next time
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
        (None, "tag", "AIReviewed", [2, 5]),
    ]


def test_a_book_to_review_is_written_but_not_tagged_until_decided():
    pages = "UMBERTO ECO\nRomanzo\nBompiani\n1981"  # the year read is printed, "Altro" is not
    it = ReviewItem(book(), read(book(), meta(title="Altro", year=1981), pages))
    assert it.doubts == {"title": "the new value is not in the book's text"} and it.selected
    assert it.review == "changes left out: title" and it.needs_review
    [action] = review_actions([it], {"title", "year"})
    assert action["set"] == {"year": 1981} and "tag" not in action  # the supported change only, no AIReviewed
    it.selected = False
    assert review_actions([it], {"title", "year"}) == []  # unticked: untouched, not tagged
    it.reviewed = True  # right-click: Mark reviewed
    assert not it.needs_review and review_actions([it], {"title", "year"}) == [
        {"op": "tag", "src_ids": [1], "tag": "AIReviewed"}]


def test_a_record_with_no_files_is_proposed_for_the_trash():
    empty = ReviewItem(book(formats={}), None, "no files in Calibre")
    missing = ReviewItem(book(id=2, formats={"EPUB": "gone.epub"}), None, "no readable file")
    assert (empty.action, empty.selected) == (ReviewAction.TRASH, True)
    assert (missing.action, missing.selected) == (ReviewAction.KEEP, False)
    actions = review_actions([empty, missing], set(FIELDS))
    assert actions == [{"src_id": 1, "title": "Il nome della rosa", "stamp": "", "op": "trash", "no_target": True}]


def test_a_run_without_cache_is_written_as_csv(tmp_path):
    import csv
    from calibre_dedup.review import ReviewResult, write_run_csv
    wrong = ReviewItem(book(id=7, title="Il nome della rosaa"), meta())
    unread = ReviewItem(book(id=8), None, "nothing to read")
    path = write_run_csv(ReviewResult("C:/libs/loc-test", "", items=[wrong, unread]), tmp_path)
    assert path.name.startswith("review_loc-test_")
    rows = list(csv.DictReader(path.open(encoding="utf-8-sig")))
    assert rows[0]["book_id"] == "7" and rows[0]["calibre_title"] == "Il nome della rosaa"
    assert rows[0]["read_title"] == rows[0]["new_title"] == "Il nome della rosa"
    assert rows[0]["calibre_authors"] == rows[0]["read_authors"] == "Umberto Eco"
    assert rows[0]["new_authors"] == "" and rows[0]["action"] == "update"
    assert rows[1]["read_title"] == "" and rows[1]["note"] == "nothing to read"


# --- what the book supports: in doubt, the metadata stays ---------------------------------
PAGES = "UMBERTO ECO\nIL NOME DELLA ROSA\nRomanzo\nBompiani\nPrima edizione 1980\nISBN 88-452-0726-9"


def test_the_ai_reads_isbn_language_and_cover():
    m = ReviewMetadata.from_json(json.dumps({"title": "x", "isbn": ["88-452-0726-9", "123"], "language": "it",
                                             "cover": "Text"}))
    assert (m.isbn, m.language, m.cover) == (["9788845207266"], "ita", "text")
    m = ReviewMetadata.from_json(json.dumps({"title": "x", "language": "Italian", "cover": "photo"}))
    assert (m.language, m.cover) == ("ita", None)
    assert ReviewMetadata.from_dict({"title": "x", "unknown_key": 1}).title == "x"


def test_a_change_the_book_supports_is_ticked():
    b = book(title="nome rosa scan", pub_year=2013)  # a file name, Calibre's file date
    it = ReviewItem(b, read(b, meta(isbn=["9788845207266"]), PAGES))
    assert set(it.changes) == {"title", "year", "isbn"} and it.doubts == {}
    assert it.action is ReviewAction.UPDATE and it.selected and it.changes["isbn"] == "9788845207266"


@pytest.mark.parametrize("calibre,found,why", [
    (dict(pub_year=1971), dict(year=1980), None),  # 1971 not printed, 1980 printed: supported
    (dict(pub_year=1975), dict(year=1971), "the new value is not in the book's text"),
    (dict(publisher="Bompiani"), dict(publisher="Mondadori"), "Calibre's value is printed in the book"),
    (dict(title="Il nome della rosa"), dict(title="La rosa"), "Calibre's value is printed in the book"),
])
def test_a_value_is_replaced_only_when_the_book_supports_it(calibre, found, why):
    b = book(**calibre)
    it = ReviewItem(b, read(b, meta(**found), PAGES))
    [name] = [n for n in it.changes if n not in ("isbn", "language")]
    assert it.doubts.get(name) == why and (name in it.excluded) == bool(why)


def test_without_text_to_check_nothing_is_written():
    b = book(pub_year=1971, publisher=None)
    it = ReviewItem(b, meta(year=1980))  # e.g. a scanned book: no evidence
    assert it.doubts == {"year": "not checked against the book", "publisher": "not checked against the book"}
    assert it.to_write(FIELDS) == {} and not it.selected and it.action is ReviewAction.UPDATE
    changed = book(pub_year=1972)  # Calibre's value changed since the AI read the book
    assert ReviewItem(changed, read(b, meta(year=1980), PAGES)).doubts == {"year": "not checked against the book"}


@pytest.mark.parametrize("found,why", [
    (dict(publisher="Bompiani"), None),  # printed in the pages the AI read
    (dict(publisher="Einaudi"), "the new value is not in the book's text"),
])
def test_an_empty_field_is_filled_only_with_a_value_printed_in_the_book(found, why):
    b = book(publisher=None)
    it = ReviewItem(b, read(b, meta(**found), PAGES))
    assert set(it.changes) == {"publisher"} and it.doubts.get("publisher") == why
    assert it.selected == (why is None)

@pytest.mark.parametrize("authors", [["Heinlein, Bradbury, Amis"], ["F. Brown e altri"], ["Eco", "Rossi"]])
def test_a_change_that_loses_an_author_is_left_out(authors):
    b = book(authors=authors)
    it = ReviewItem(b, read(b, meta(authors=["Robert A. Heinlein"]), PAGES + " Robert A. Heinlein"))
    assert it.doubts["authors"] == "an author would be lost"


def test_a_title_that_loses_its_issue_number_is_left_out_unless_it_is_the_series_number():
    b = book(title="Galaxy Mensile Di Fantascienza N 04")
    it = ReviewItem(b, read(b, meta(title="Galaxy"), "GALAXY mensile"))
    assert "title" not in it.changes  # it only drops a part of the title: not proposed (_drops_part)
    it = ReviewItem(b, read(b, meta(title="Galaxy Fantascienza"), "GALAXY Fantascienza"))
    assert it.doubts["title"] == "the issue or volume number would be lost"
    it = ReviewItem(b, read(b, meta(title="Galaxy", series="Galaxy", series_index=4), "GALAXY mensile"))
    assert "title" in it.changes and "title" not in it.doubts


@pytest.mark.parametrize("calibre,authors,found,pages,ticked", [
    # titles Calibre took from file names, put right: what Merge and Dedup waits for
    ("Classici del giallo 0024 - Per", ["Erle Stanley Gardner"], "Perry Mason e il siero della verità",
     "I CLASSICI DEL GIALLO MONDADORI Erle Stanley Gardner Perry Mason e il siero della verità", True),
    ("Galassia 038 - Budrys Algis - ", ["Algis Budrys"], "La torcia cadente", "GALASSIA Algis Budrys La torcia cadente",
     True),
    ("ITABOOK 0052 - Hemingway", ["Ernest Hemingway"], "Il vecchio e il mare", "Ernest Hemingway Il vecchio e il mare",
     True),
    ("Inediti d'autore 003 - Sandro Veronesi - Profezia", ["Sandro Veronesi"], "Profezia",
     "Sandro Veronesi Profezia Inediti d'autore", True),
    ("Segretissimo 0011 - OS 117 Ne", ["Jean Bruce"], "OS 117: New York-Odessa", "SEGRETISSIMO Jean Bruce OS 117: "
     "New York-Odessa", True),
    # fewer words than the file name: an extension, a collection and its number after the title
    ("La Dittatura Europea.htm", ["Ida Magli"], "La dittatura europea", "Ida Magli La dittatura europea", True),
    ("AAA ASSO DECONTAMINAZIONI INTERPLANETARIE Urania Millemondi s2 0065", ["Robert Sheckley"],
     "AAA Asso decontaminazioni interplanetarie", "URANIA MILLEMONDI Robert Sheckley AAA Asso decontaminazioni "
     "interplanetarie", True),
    # the same words: still a file name
    ("il_vecchio_e_il_mare", ["Ernest Hemingway"], "Il vecchio e il mare", "Ernest Hemingway Il vecchio e il mare",
     True),
    # Calibre's file name printed in the book (the collection's page) is no title
    ("Urania 0602", ["Jack Vance"], "Quando due mondi si incontrano", "URANIA 0602 Jack Vance Quando due mondi si "
     "incontrano", True),
    # a collection's number dropped: to check, unless the AI read it as the series number
    ("Capolavori Gialli Mondadori N 0180 Verso l'ora zero", ["Agatha Christie"], "Verso l'ora zero",
     "Agatha Christie Verso l'ora zero", False),
    # a volume dropped: to check
    ("Il Conte di Montecristo_2", ["Alexandre Dumas"], "Il conte di Montecristo",
     "Alexandre Dumas Il conte di Montecristo", False),
    ("Il Conte di Montecristo 2.epub", ["Alexandre Dumas"], "Il conte di Montecristo",
     "Alexandre Dumas Il conte di Montecristo", False),
])
def test_a_file_name_title_is_replaced_by_the_title_read(calibre, authors, found, pages, ticked):
    b = book(title=calibre, authors=authors, publisher=None, pub_year=None)
    it = ReviewItem(b, read(b, meta(title=found, authors=authors, publisher=None, year=None), pages))
    assert it.changes["title"] == found
    assert ("title" not in it.doubts) == ticked and it.selected == ticked, it.doubts
    if not ticked:
        assert it.doubts["title"] in ("the issue or volume number would be lost", "the volume number would change")


def test_a_title_that_is_not_a_file_name_keeps_its_volume():
    b = book(title="Il Conte di Montecristo - 2", authors=["Alexandre Dumas"])
    it = ReviewItem(b, read(b, meta(title="Il conte di Montecristo", authors=["Alexandre Dumas"]),
                            "Alexandre Dumas Il conte di Montecristo"))
    assert "title" not in it.changes  # only drops the volume: not proposed (_drops_part)
    b = book(title="Il trono di spade", authors=["George R. R. Martin"])
    it = ReviewItem(b, read(b, meta(title="Il trono di spade 2", authors=["George R. R. Martin"]),
                            "George R. R. Martin Il trono di spade 2"))
    assert it.doubts["title"] == "the volume number would change" and not it.selected


def test_a_series_name_is_not_a_publisher():
    b = book(publisher="La Tribuna")
    it = ReviewItem(b, read(b, meta(publisher="Galassia", series="Galassia", series_index=134), "GALASSIA"))
    assert it.doubts["publisher"] == "a series name, not a publisher"
    other = book(id=2, publisher="La Tribuna")
    it = ReviewItem(other, read(other, meta(publisher="Galassia"), "GALASSIA"))  # the series is another book's
    assert "publisher" not in it.doubts and it.selected
    mark_series_publishers([it], {"galassia"})
    assert it.doubts["publisher"] == "a series name, not a publisher" and "publisher" in it.excluded


def test_a_record_with_title_and_author_swapped_is_put_right():
    b = book(title="1969", authors=["La Contessa Di Ascot"])  # the title in the author field, a year as title
    it = ReviewItem(b, read(b, meta(title="La contessa di Ascot", authors=["Edgar Wallace"]),
                            "EDGAR WALLACE\nLA CONTESSA DI ASCOT\n1969"))
    assert "title" not in it.doubts and "authors" not in it.doubts
    b = book(title="Bernard Cornwell", authors=["L'Eroe Di Trafalgar"])
    it = ReviewItem(b, read(b, meta(title="L'eroe di Trafalgar", authors=["Bernard Cornwell"]),
                            "BERNARD CORNWELL\nL'EROE DI TRAFALGAR"))
    assert it.doubts == {}


def test_isbn_only_for_a_book_without_one_and_only_one_printed():
    b = book()
    assert "isbn" not in ReviewItem(book(isbns={"9780000000002"}), read(b, meta(isbn=["9788845207266"]), PAGES)).changes
    two = read(b, meta(isbn=["9788845207266", "9788804668237"]), PAGES + " ISBN 978-88-04-66823-7")
    assert "isbn" not in ReviewItem(b, two).changes  # two printed: the print and the e-book's, or another book's
    absent = read(b, meta(isbn=["9788804668237"]), PAGES)  # read, but not in the text
    assert "isbn" not in ReviewItem(b, absent).changes
    scanned = ReviewItem(b, meta(isbn=["9788845207266"]))  # no text to look in: as read, left out
    assert scanned.changes["isbn"] == "9788845207266" and scanned.doubts["isbn"] == "not checked against the book"


def test_the_language_is_the_texts_own():
    italian = "Il vecchio pescatore guardava il mare che non si calmava e pensava alla barca che aveva " * 12
    b = book(languages=["eng"])  # Calibre's default language, wrong
    it = ReviewItem(b, read(b, meta(language="ita"), italian))
    assert it.changes["language"] == "ita" and "language" not in it.doubts
    it = ReviewItem(b, meta(language="ita"))  # no text to tell it: the AI's word alone
    assert it.doubts["language"] == "not told by the book's text"
    assert ReviewItem(book(), read(book(), meta(language="ita"), italian)).to_write(FIELDS)["language"] == "ita"
    assert ReviewItem(book(), meta(language="ita")).to_write(FIELDS) == {}  # empty, but nothing tells it


def test_a_cover_the_ai_says_is_not_real_is_tagged_bad_cover():
    text_page = ReviewItem(book(), meta(cover="text"))
    real = ReviewItem(book(id=2), meta(cover="real"))
    tagged = ReviewItem(book(id=3, tags={"BadCover"}), meta(cover="placeholder"))
    assert text_page.bad_cover and not real.bad_cover and not tagged.bad_cover
    assert [a for a in review_actions([text_page, real, tagged], set()) if a.get("tag") == "BadCover"] == [
        {"op": "tag", "src_ids": [1], "tag": "BadCover"}]


def test_a_generic_cover_is_not_sent_to_the_ai(tmp_path):
    folder = tmp_path / "b"
    folder.mkdir()
    (folder / "cover.jpg").write_bytes(b"jpg")
    b = book(library=str(tmp_path), path="b")
    reviewer = Reviewer(FakeProvider(), FakeExtractor(), AICache(tmp_path / "c.json"))
    reviewer.generic = {str(folder / "cover.jpg"): 5}
    assert reviewer._cover(b) == (None, "generic cover not sent to the AI")


def test_the_evidence_is_kept_with_the_answer(tmp_path):
    lib = make_library(tmp_path / "lib", [{"title": "dune", "text": "x"}])
    text = FakeProvider(REPLY)
    reviewer = Reviewer(text, FakeExtractor("FRANK HERBERT\nDUNE\n1965"), AICache(tmp_path / "cache.json"))
    first = scan_library(lib, "", reviewer).items[0]
    again = scan_library(lib, "", reviewer).items[0]  # from the cache: no text read, the evidence is there
    assert len(text.calls) == 1 and again.found.evidence == first.found.evidence
    assert first.found.evidence["fields"]["year"] == {"current": "", "current_printed": False, "read_printed": True}


def test_an_authors_name_is_not_a_publisher():
    b = book(publisher=None, authors=["La Contessa Di Ascot"])
    it = ReviewItem(b, read(b, meta(publisher="Edgar Fallace", authors=["Edgar Wallace"]), "EDGAR FALLACE"))
    assert it.doubts["publisher"] == "an author's name, not a publisher"  # one letter from the author's name


def test_an_update_with_nothing_to_write_is_shown_as_keep():
    from calibre_dedup.review import ReviewAction
    b = book(publisher=None)
    it = ReviewItem(b, read(b, meta(title="Il nome della rosa", publisher="Einaudi"), PAGES))
    assert it.action is ReviewAction.UPDATE and "publisher" in it.excluded  # left out: not in the pages
    assert it.shown_action() is ReviewAction.KEEP and not it.checkable and not it.selected
    it.excluded.discard("publisher")  # right-click: Change publisher again
    assert it.shown_action() is ReviewAction.UPDATE and it.checkable
    assert it.shown_action({"title"}) is ReviewAction.KEEP  # the publisher field turned off
    it.set_action(ReviewAction.UPDATE)  # chosen by the user: shown as such
    assert it.shown_action({"title"}) is ReviewAction.UPDATE
