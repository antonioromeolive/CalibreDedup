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

"""calibre-review: read every book of a library with the AI (its first pages and
its cover) and propose corrections to title, authors, publisher, year and series.

Nothing is written during the scan. The user then chooses, per book, to update
its metadata, keep it as it is, or move it to a trash library; the bridge
(bridge_script.py, op "set" and "trash") does the writing inside Calibre.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import re
import shutil
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Callable

from . import perf
from .archives import ask_once, prepare as unpack_archives
from .ai import AICache, AIError, ReviewMetadata, ask_fitting, read_book_metadata
from .executor import AI_UPDATED_TAG, ExecutionError, run_bridge
from .journal import Journal
from .calibre_env import calibre_is_running
from .config import config_dir
from .covers import BAD_COVER_TAG, PAGE_NOTE, cover_file, generic_covers, generic_note, page_cover
from .extract import cover_png, openable_elsewhere, unreadable_formats
from .language import detect_language
from .library import LibraryError, read_books, tag_filter_text, tag_selects
from .models import Book
from .pause import Pause, wait_if_paused
from .normalize import (
    author_key, authors_key, is_unknown, looks_like_file_name, looks_like_name, names_nearly_equal, noise_words,
    normalize_isbn, publisher_tokens, same_publisher, series_key, strip_accents, title_key, volumes_differ,
    word_tokens,
)
from .planner import MAX_LIBRARY_PATH, AIResolver

log = logging.getLogger(__name__)

FIELDS = ["title", "authors", "publisher", "year", "series", "isbn", "language"]  # "series" includes its number
FIELD_LABELS = {"title": "Title", "authors": "Authors", "publisher": "Publisher", "year": "Year",
                "series": "Series", "isbn": "ISBN", "language": "Language"}
CACHE_VERSION = "review-v2"  # change when the prompt changes, so old answers are not reused
SAVE_CACHE_EVERY = 20  # books read by the AI between cache saves (a scan can take hours)
FILTERED_RETRY_CHARS = 3000  # text sent again when the content filter refuses the full excerpt
# Written on Execute to every book of the scan the AI read, updated or not: the next scan
# skips them, on any computer. Remove the tag in Calibre to review a book again.
REVIEWED_TAG = "AIReviewed"


class ReviewAction(str, Enum):
    UPDATE = "update"  # write the proposed values
    KEEP = "keep"  # leave the book as it is
    TRASH = "trash"  # move the book to the trash library


ACTION_LABELS = {ReviewAction.UPDATE: "Update", ReviewAction.KEEP: "Keep", ReviewAction.TRASH: "Trash"}


@dataclass
class ReviewItem:
    book: Book
    found: ReviewMetadata | None  # None: the AI could not read the book
    note: str = ""  # what was read, or why nothing was
    changes: dict = field(default_factory=dict)  # field -> proposed value, only where it differs
    # Fields not written: the user's choice, and the doubts below unless the user turns them on.
    excluded: set[str] = field(default_factory=set)
    # Changes left out because the book doesn't support them (field -> why, see doubtful_changes):
    # in doubt, the metadata stays as it is.
    doubts: dict[str, str] = field(default_factory=dict)
    action: ReviewAction = ReviewAction.KEEP
    selected: bool = False
    manual: bool = False  # action chosen by the user
    status: str = ""  # filled during execution
    # Formats Calibre can't open (format -> why): all of them, the book is proposed for
    # the trash; some, those taken out (`trash_bad`) have the whole record copied to the
    # trash library as it is first, then they are removed from the book (whatever its action).
    bad_formats: dict[str, str] = field(default_factory=dict)
    # Which bad formats are taken out: None (the default) only those that can't be opened at all
    # (empty, or not what their format says), not those Windows may open (a DOC, a file only
    # Calibre's converter fails on); True every one; False none.
    trash_bad: bool | None = None
    # The book's archives (archives.Unpack): unpacked on Execute when their `unpack` is on,
    # whatever the book's action (unless the whole book goes to the trash).
    archives: list = field(default_factory=list)
    # Its cover.jpg is a generic cover shown by this many books (covers.py), else 0.
    generic_cover: int = 0
    # Its cover.jpg is a page of the book (covers.page_cover), not a real cover.
    page_cover: bool = False
    # Read by the Judge AI (judge_ai): its profile. A suggestion: unticked, to review.
    judged: str = ""
    # The user looked at the book (a tick, a field turned on or off, Mark reviewed): it no
    # longer needs review (see needs_review).
    reviewed: bool = False

    @property
    def archives_to_unpack(self) -> list:
        return [u for u in self.archives if u.unpack and not u.problem]

    @property
    def cleanup(self) -> bool:
        """Formats Calibre can't open to take out, or an archive to unpack (when ticked)."""
        return bool(self.bad_formats_to_trash or self.archives_to_unpack)

    @property
    def checkable(self) -> bool:
        """Whether the row can be ticked: something to write or trash, or a cleanup."""
        return self.checkable_for(FIELDS)

    def shown_action(self, fields_on: set[str] | list[str] = FIELDS) -> ReviewAction:
        """The action as the list shows it: an Update with nothing to write (every change
        left out, or in a field turned off) is a Keep, unless the user chose Update."""
        if self.action is ReviewAction.UPDATE and not self.manual and not self.to_write(fields_on):
            return ReviewAction.KEEP
        return self.action

    def checkable_for(self, fields_on: set[str] | list[str] = FIELDS) -> bool:
        """checkable, with these fields on."""
        return self.shown_action(fields_on) is not ReviewAction.KEEP or self.cleanup

    def review_for(self, fields_on: set[str] | list[str] = FIELDS) -> str:
        """What the user should check: changes left out (not supported by the book) of the
        fields on, an archive left packed in doubt. "" when nothing."""
        left = [FIELD_LABELS[n].lower() for n in self.doubts
                if n in self.excluded and n in self.changes and n in fields_on]
        parts = [f"changes left out: {', '.join(left)}"] if left else []
        if self.judged:
            parts.append(f"read by the Judge AI ({self.judged}): check, then tick")
        return "; ".join(parts + [u.note for u in self.archives if u.doubt and not u.unpack])

    def needs_review_for(self, fields_on: set[str] | list[str] = FIELDS) -> bool:
        """Something to check (review_for) that the user hasn't looked at yet. Its supported
        changes are written when ticked, but it is not tagged REVIEWED_TAG: the next
        analysis shows it again, until the user decides (or marks it reviewed)."""
        return bool(self.review_for(fields_on)) and not self.manual and not self.reviewed

    @property
    def review(self) -> str:
        return self.review_for()

    @property
    def needs_review(self) -> bool:
        return self.needs_review_for()

    @property
    def broken(self) -> bool:
        """No file of the book can be opened by Calibre."""
        return bool(self.bad_formats) and set(self.bad_formats) >= set(self.book.formats)

    @property
    def unopenable(self) -> list[str]:
        """The bad formats that can't be opened at all: empty, or not what their format says."""
        return sorted(f for f, why in self.bad_formats.items() if not openable_elsewhere(why))

    @property
    def bad_formats_to_trash(self) -> list[str]:
        """Formats taken out of the book on Execute (not when the whole book goes to the trash)."""
        if self.trash_bad is False or self.broken or (self.selected and self.action is ReviewAction.TRASH):
            return []
        return sorted(self.bad_formats) if self.trash_bad else self.unopenable

    @property
    def bad_cover(self) -> bool:
        """Its cover is generic or a page of the book, or the AI says it is not a real cover (only
        a page of text, a placeholder): tagged BAD_COVER_TAG on Execute, unless the book goes to
        the trash."""
        return bool(self.generic_cover) or self.page_cover or (self.found is not None and self.found.cover in ("text", "placeholder")
                                            and BAD_COVER_TAG.casefold() not in {t.casefold() for t in self.book.tags})

    def __post_init__(self):
        if self.found is not None and not self.changes:
            self.changes = find_changes(self.book, self.found)
        if self.found is not None and self.changes and not self.doubts:
            self.doubts = doubtful_changes(self.book, self.found, self.changes)
        self.excluded |= set(self.doubts)
        if self.changes and self.action is ReviewAction.KEEP and not self.manual:
            self.action, self.selected = ReviewAction.UPDATE, bool(self.to_write(FIELDS))
        elif ((not self.book.formats or (self.broken and set(self.unopenable) >= set(self.bad_formats)))
              and self.action is ReviewAction.KEEP and not self.manual):
            self.action, self.selected = ReviewAction.TRASH, True  # no file that opens at all: nothing to keep
        self.tick_cleanup()

    def tick_cleanup(self) -> None:
        """A book with a cleanup to do (files that open nowhere, an archive to unpack) is
        ticked for it, unless the user decided otherwise."""
        if not self.manual and self.cleanup and self.action is not ReviewAction.TRASH:
            self.selected = True

    def to_write(self, fields_on: set[str] | list[str]) -> dict:
        """The changes that will be written: not excluded, and the field is on."""
        return {k: v for k, v in self.changes.items() if k in fields_on and k not in self.excluded}

    def set_action(self, action: ReviewAction) -> None:
        """Keep: the book is left as it is (tick it for its cleanup)."""
        self.action, self.manual = action, True
        self.selected = action is not ReviewAction.KEEP

    def book_changed(self, book: Book) -> None:
        """The book was updated in the library: compare again with what the AI read."""
        self.book = book
        self.changes = find_changes(book, self.found) if self.found is not None else {}
        self.doubts = doubtful_changes(book, self.found, self.changes) if self.changes else {}
        self.excluded = (self.excluded & set(self.changes)) | set(self.doubts)
        self.manual = False
        self.action = ReviewAction.UPDATE if self.changes else ReviewAction.KEEP
        self.selected = False  # done: nothing to execute again until the user says so


# --- comparing ---------------------------------------------------------------------
def _loose(text: str | None) -> str:
    """Case, accents and punctuation don't count: "IL NOME DELLA ROSA" is not a
    reason to change "Il nome della rosa"."""
    return re.sub(r"[\W_]+", " ", strip_accents(text or "").casefold()).strip()


def current_value(book: Book, name: str):
    if name == "title":
        return None if is_unknown(book.title) else book.title
    if name == "authors":
        return [a for a in book.authors if not is_unknown(a)]
    if name == "publisher":
        return book.publisher
    if name == "year":
        return book.pub_year
    if name == "series":
        return (book.series, book.series_index) if book.series else None
    if name == "isbn":
        return sorted(book.isbns)[0] if book.isbns else None
    if name == "language":
        return book.languages[0] if book.languages else None
    raise KeyError(name)


def read_value(found: ReviewMetadata, name: str):
    """What the AI read for a field, in the form of current_value."""
    if name == "series":
        return (found.series, found.series_index) if found.series else None
    if name == "isbn":
        return found.isbn[0] if len(found.isbn) == 1 else None
    return getattr(found, name)


def _file_name_title(book: Book, found: ReviewMetadata) -> bool:
    """Whether the book's title is a file name (normalize.looks_like_file_name) and the
    title the AI read is not: "il_vecchio_e_il_mare", "La Dittatura Europea.htm", "AAA
    ASSO DECONTAMINAZIONI INTERPLANETARIE Urania Millemondi s2 0065" -> a real title."""
    current = current_value(book, "title")
    return bool(current and found.title and looks_like_file_name(current) and not looks_like_file_name(found.title))


def find_changes(book: Book, found: ReviewMetadata) -> dict:
    """The fields where the AI read something different from the metadata. A field
    the AI did not find is never a change: nothing is ever erased. Nor is a title that
    only drops the current one's subtitle or volume part (_drops_part), but for a file
    name, which any real title replaces, even with the same words ("il_vecchio_e_il_mare")
    or fewer (an extension, a collection and its number). An ISBN only for a book without
    one, and only one printed in its pages; the language is the text's own when it can be
    told (language.detect_language), else the AI's."""
    changes: dict = {}
    if _file_name_title(book, found):
        if found.title.strip() != current_value(book, "title").strip():
            changes["title"] = found.title
    elif (found.title and _loose(found.title) != _loose(current_value(book, "title"))
            and not _drops_part(book, found)):
        changes["title"] = found.title
    if found.authors and authors_key(found.authors) != authors_key(book.authors):
        changes["authors"] = list(found.authors)
    if found.publisher and not (book.publisher and same_publisher(book.publisher, found.publisher)):
        changes["publisher"] = found.publisher
    if found.year and found.year != book.pub_year:
        changes["year"] = found.year
    if found.series:
        same = bool(book.series) and series_key(book.series) == series_key(found.series)
        index = found.series_index
        if not same:
            changes["series"] = (found.series, index)
        elif index is not None and index != book.series_index:
            changes["series"] = (book.series, index)  # same series: keep the name as it is written
    if found.isbn and not book.isbns:
        printed = found.evidence.get("isbns")  # None: no text to look in (a scanned book)
        isbns = [i for i in found.isbn if printed is None or i in printed]
        if len(isbns) == 1:  # several: the print and the e-book's, or other books listed in it
            changes["isbn"] = isbns[0]
    language = found.evidence.get("language") or found.language
    if language and language not in book.languages:
        changes["language"] = language
    return changes


def _drops_part(book: Book, found: ReviewMetadata) -> bool:
    """Whether the title read is the book's title without its last words, and those words
    are a subtitle or a volume part ("Fantozzi: la trilogia", "Storia d'Italia (2)"): the
    shorter title says less. Not when they are noise around the title, which a shorter
    title cleans up: an edition note or a collection ("(Italian Edition)", "(Everyman)"),
    a bracket cut short, or the authors', series' or publisher's names; nor when the
    number dropped is the series number read ("Galaxy N 04", series Galaxy 4)."""
    current = current_value(book, "title")
    mine, new = _loose(current), _loose(found.title)
    if not current or not new or not mine.startswith(new + " "):
        return False
    if title_key(current) == title_key(found.title):
        return False  # only noise dropped (see normalize.title_key)
    dropped = mine[len(new):].split()
    if found.series_index is not None and found.series_index in {float(w) for w in dropped if w.isdigit()}:
        return False  # the number goes to the series
    noise = noise_words(*book.authors, book.series, book.publisher)
    return any(w not in noise for w in dropped)


# --- what the book itself shows (no AI) -------------------------------------------------
_ISBN_RE = re.compile(r"(?:97[89][-\s]?)?(?:\d[-\s]?){9}[\dXx]")


def _printed(name: str, value, joined: str, vocabulary: set[str]) -> bool:
    """Whether a field's value is printed in a text (its words as `joined` and a set). A
    name or title of fewer than 3 letters ("PC", "1937") would be found anywhere: it is not."""
    text = str(value or "")
    if name == "series":
        text = text.split(" [")[0]  # the name: its number is written in many ways
    words = word_tokens(text)
    if name == "year":
        return text in vocabulary
    if sum(c.isalpha() for c in text) < 3:
        return False
    if name in ("title", "series"):
        return f" {' '.join(words)} " in joined
    if name == "authors":
        return all(all(t in vocabulary for t in author_key(a) if len(t) > 2) for a in text.split(" & ") if a.strip())
    if name == "publisher":
        tokens = publisher_tokens(text)
        return bool(tokens) and tokens <= vocabulary
    return False


def read_evidence(book: Book, found: ReviewMetadata, text: str) -> dict:
    """What the first pages the AI read show, checked without AI and kept with its answer:
    for each field, whether Calibre's value (as it was) and the AI's are printed in them;
    the language of the text; the AI's ISBNs printed in it (None: no text)."""
    words = word_tokens(text)
    joined, vocabulary = f" {' '.join(words)} ", set(words)
    fields = {}
    for name in ("title", "authors", "publisher", "year", "series"):
        current = format_value(name, current_value(book, name))
        fields[name] = {"current": current, "current_printed": _printed(name, current, joined, vocabulary),
                        "read_printed": _printed(name, format_value(name, read_value(found, name)), joined, vocabulary)}
    isbns = None
    if text.strip():
        in_text = {i for i in (normalize_isbn(m) for m in _ISBN_RE.findall(text)) if i}
        isbns = [i for i in found.isbn if i in in_text]
    return {"fields": fields, "language": detect_language(text) if text.strip() else None, "isbns": isbns}


# An issue or volume number in a title: "Galaxy N 04", "vol. 1", "#3", "parte 2".
_NUMBER_RE = re.compile(r"\b(?:n\.?|nr\.?|no\.|vol\.?|volume|#|parte|part|libro|tomo)\s*0*(\d+)", re.I)
# An author field naming several people without listing them all.
_SEVERAL_RE = re.compile(r"\b(?:e altri|et al\.?|and others|aa\.? ?vv\.?)", re.I)


def _people(authors: list[str]) -> int:
    """How many people an author list names: an entry with two commas or more is a list
    ("Heinlein, Bradbury, Amis"); "Heinlein, Robert A." is one person."""
    return sum(a.count(",") + 1 if a.count(",") >= 2 else 1 for a in authors)


def _swapped(name: str, book: Book, found: ReviewMetadata) -> bool:
    """Calibre had title and author the other way round: its author field holds the title
    the AI read ("La Contessa Di Ascot"), or its title is the author's name ("Bernard Cornwell")."""
    if name == "authors":
        mine = set(word_tokens(" ".join(current_value(book, "authors"))))
        return (bool(mine) and mine <= set(word_tokens(found.title or ""))
                and not any(looks_like_name(a) for a in current_value(book, "authors")))
    if name == "title":
        mine = set(word_tokens(book.title))
        return bool(mine) and looks_like_name(book.title) and mine <= set(word_tokens(" ".join(found.authors)))
    return False


def doubtful_changes(book: Book, found: ReviewMetadata, changes: dict) -> dict[str, str]:
    """The changes left out, with why: in doubt, the metadata stays as it is. A value
    written, in an empty field or over Calibre's, must be supported by the book itself:
    printed in the pages the AI read (the ISBN too; the language told by the text), and,
    replacing a value, Calibre's is not. Never by default: a change that drops an author,
    or an issue or volume number, or a series or an author's name as the publisher. A
    record with title and author swapped is put right."""
    evidence = found.evidence.get("fields", {})
    doubts: dict[str, str] = {}
    for name, new in changes.items():
        current = current_value(book, name)
        if name == "publisher" and series_key(new) in {series_key(s) for s in (book.series, found.series) if s}:
            doubts[name] = "a series name, not a publisher"
            continue
        if name == "publisher" and any(author_key(new) == author_key(a) or names_nearly_equal(new, a)
                                       for a in [*book.authors, *found.authors]):
            doubts[name] = "an author's name, not a publisher"
            continue
        if name == "isbn":  # find_changes keeps only one printed in the text, when there is one
            if found.evidence.get("isbns") is None:
                doubts[name] = "not checked against the book"
            continue
        if name == "language":
            if found.evidence.get("language") != new:
                doubts[name] = "not told by the book's text"
            continue
        if current:
            if name == "authors" and (len(new) < _people(current) or any(_SEVERAL_RE.search(a) for a in current)):
                doubts[name] = "an author would be lost"
                continue
            if name == "title" and (m := _NUMBER_RE.search(current)):
                number = int(m.group(1))
                if number not in {int(n) for n in re.findall(r"\d+", new)} and found.series_index != number:
                    doubts[name] = "the issue or volume number would be lost"
                    continue
            # "Il Conte di Montecristo_2" -> "Il conte di Montecristo": another volume (normalize.volumes_differ)
            if name == "title" and volumes_differ(current.replace("_", " "), new):
                doubts[name] = "the volume number would change"
                continue
            if _swapped(name, book, found):
                continue
        e = evidence.get(name)
        if not e or e.get("current") != format_value(name, current):
            doubts[name] = "not checked against the book"  # no text, or Calibre's value changed since
        elif current and e.get("current_printed") and not (name == "title" and _file_name_title(book, found)):
            doubts[name] = "Calibre's value is printed in the book"  # a file name ("URANIA 0602") is no title
        elif not e.get("read_printed"):
            doubts[name] = "the new value is not in the book's text"
    return doubts


def series_names(books: list[Book], items: list[ReviewItem]) -> set[str]:
    """The series of the library and those the AI read in its books (series_key)."""
    names = {series_key(b.series) for b in books if b.series}
    names |= {series_key(i.found.series) for i in items if i.found is not None and i.found.series}
    return {n for n in names if n}


def mark_series_publishers(items: list[ReviewItem], names: set[str]) -> None:
    """A publisher that is one of the library's series ("Galassia", "Urania") is no
    publisher: the AI read the series on the cover. Left out, like the other doubts."""
    for it in items:
        new = it.changes.get("publisher")
        if new and series_key(new) in names and "publisher" not in it.doubts:
            it.doubts["publisher"] = "a series name, not a publisher"
            it.excluded.add("publisher")
            if it.action is ReviewAction.UPDATE and not it.manual and not it.to_write(FIELDS):
                it.selected = it.cleanup


def format_value(name: str, value) -> str:
    if value is None or value == [] or value == "":
        return ""
    if name == "authors":
        return " & ".join(value)
    if name == "series":
        series, index = value
        if index is None:
            return series
        return f"{series} [{int(index) if float(index).is_integer() else index}]"  # as in Calibre
    return str(value)


# --- reading -------------------------------------------------------------------------
REVIEW_CACHE_FILE = "review_cache.json"


def review_cache(off: bool = False) -> AICache:
    """calibre-review's own AI cache, so that it can run with Merge and Dedup
    (each writes its whole cache back). The first time it starts as a copy of the
    shared ai_cache.json, which holds the answers of reviews made before the split.
    `off`: no cache (see AICache)."""
    path = config_dir() / REVIEW_CACHE_FILE
    shared = config_dir() / "ai_cache.json"
    if not path.exists() and shared.is_file():
        try:
            shutil.copyfile(shared, path)
            log.info("Review AI cache created from %s", shared)
        except OSError as e:
            log.warning("Could not copy %s to %s: %s", shared, path, e)
    return AICache(path, off=off)


def is_reviewed(book: Book) -> bool:
    return REVIEWED_TAG.casefold() in {t.casefold() for t in book.tags}


def book_cover_file(book: Book) -> Path | None:
    path = Path(book.library, book.path, "cover.jpg")
    return path if path.is_file() else None


class Reviewer(AIResolver):
    """Reads a book's first pages, and its cover when there is an Image AI. With an
    Image AI, every book is sent to it (it reads text too), else to the text AI.
    Errors and "AI not responding" are handled as in the analysis (AIResolver)."""

    fresh = False  # always ask the AI, never answer from the cache (the answer is still cached)
    # The Judge AI (judge_ai): more to go on for a book, given with its pages; None: nothing.
    hints: Callable[[Book], str] | None = None

    def review(self, book: Book) -> tuple[ReviewMetadata | None, str]:
        picked = self.extractor.pick_format(book.formats)
        if not picked:
            return None, ("no files in Calibre: proposed for the trash" if not book.formats
                          else "no readable file (files missing on disk?)")
        fmt, path = picked
        vision = self.vision if self.vision is not None and not self.image_disabled_reason else None
        provider = vision or self.provider
        if vision is None and self.disabled_reason:
            return None, self.disabled_reason

        cover, cover_note = (self._cover(book) if vision else (None, ""))
        if not vision and book_cover_file(book) is not None:
            cover_note = "cover not read: no Image AI"
        key = self._key(book, fmt, path, cover)
        cached = None if self.fresh else self.cache.get(key)
        if cached is not None:
            self.stats["read_cached"] += 1
            return ReviewMetadata.from_dict(cached), _join(f"{fmt} first pages" + (" + cover" if cover else "")
                                                           + " (cached)", cover_note)

        excerpt = self.extractor.excerpt(book.formats, "start")
        images = ([cover] if cover else []) + (excerpt.images if vision else [])
        if not excerpt.text.strip() and not images:
            note = f"nothing to read ({excerpt.source})"
            if fmt == "PDF" and vision is None:
                note += "; scanned PDF skipped: no Image AI"
            return None, _join(note, cover_note)
        read = excerpt.source + (" + cover" if cover else "")
        log.info("AI reading %s (%s)", book.label(), read)
        try:
            hints = self.hints(book) if self.hints is not None else ""
            meta, read = self._read(book, provider, excerpt.text, images, bool(cover), read, hints)
        except AIError as e:
            note = self._error(book, e, image=vision is not None)
            if note is None:  # the user chose Retry
                return self.review(book)
            return None, note
        if vision:
            self.image_errors = 0
        else:
            self.consecutive_errors = 0
        self.stats["read"] += 1
        meta.evidence = read_evidence(book, meta, excerpt.text)
        self.cache.put(key, meta.to_dict())
        if self.stats["read"] % SAVE_CACHE_EVERY == 0:
            self.cache.save()
        return meta, _join(f"AI read {read}", cover_note)

    def _read(self, book: Book, provider, text: str, images: list[str], has_cover: bool,
              read: str, hints: str = "") -> tuple[ReviewMetadata, str]:
        """Ask the AI; when its content filter refuses the pages (a violent novel),
        ask again with less: the first FILTERED_RETRY_CHARS characters (the title
        page is at the start), then the text without images (a refused cover), then
        the cover alone. Returns (metadata, what was read)."""
        attempts = [(text, images, read)]
        if len(text) > FILTERED_RETRY_CHARS:
            attempts.append((text[:FILTERED_RETRY_CHARS], images,
                             f"{read} (first {FILTERED_RETRY_CHARS} chars: content filter)"))
        if images and text.strip():
            attempts.append((text[:FILTERED_RETRY_CHARS], [], "text only, no images (content filter)"))
        if has_cover and (text.strip() or len(images) > 1):
            attempts.append(("", images[:1], "cover only (content filter)"))
        for i, (t, imgs, what) in enumerate(attempts):
            try:
                meta, cut = ask_fitting(lambda text: read_book_metadata(provider, text, imgs, has_cover=has_cover,
                                                                        hints=hints),
                                        t, book.label())
                return meta, (f"{what} (first {cut} chars: context size)" if cut else what)
            except AIError as e:
                if not e.filtered or i == len(attempts) - 1:
                    raise
                log.info("%s on %s: asking again with %s", e, book.label(), attempts[i + 1][2])
        raise AssertionError("unreachable")

    def _cover(self, book: Book) -> tuple[str | None, str]:
        """The cover as a PNG for the AI: Calibre's cover.jpg, else the one inside the file.
        A generic cover (the "Microsoft Word 2000" logo) is not sent: it would mislead it."""
        data: bytes | None = None
        cover_file = book_cover_file(book)
        if cover_file is not None and str(cover_file) in self.generic:
            return None, "generic cover not sent to the AI"
        if cover_file is not None:
            data = cover_file.read_bytes()
        else:
            found = self.extractor.embedded_cover(book.formats)
            data = found[1] if found else None
        if not data:
            return None, "no cover"
        png = cover_png(data)
        return (png, "") if png else (None, "cover unreadable")

    def _key(self, book: Book, fmt: str, path: str, cover: str | None) -> str:
        """By book (uuid), not by path: updating title or authors renames the files,
        and the next scan must still find the answer."""
        try:
            st = Path(path).stat()
            stamp = f"{st.st_size}:{int(st.st_mtime)}"
        except OSError:
            stamp = "?"
        provider = self.vision if self.vision is not None and not self.image_disabled_reason else self.provider
        cover_id = hashlib.sha1(cover.encode()).hexdigest()[:16] if cover else "none"
        extractor = f"{self.extractor.pdf_pages}p{self.extractor.text_chars}c"
        token = f"{CACHE_VERSION}|{book.uuid or book.id}|{fmt}|{stamp}|{cover_id}|{extractor}|{self._model_id(provider)}"
        return hashlib.sha1(token.encode()).hexdigest()


def _join(note: str, extra: str) -> str:
    return f"{note}; {extra}" if extra else note


# --- scanning ----------------------------------------------------------------------------
@dataclass
class ReviewResult:
    library: str
    trash_library: str
    items: list[ReviewItem] = field(default_factory=list)
    total_books: int = 0  # books to review (not counting the skipped ones)
    skipped: int = 0  # books left out by the tag filter
    tag: str = ""  # only the books with one of these tags (comma-separated) were reviewed; "" = all
    tag_exclude: bool = False  # ... without any of them, instead
    stopped: bool = False
    stats: dict[str, int] = field(default_factory=dict)
    ai_down: list[str] = field(default_factory=list)

    def count(self, action: ReviewAction) -> int:
        return sum(1 for i in self.items if i.action is action)


def check_libraries(library: str, trash: str) -> None:
    if not library:
        raise LibraryError("Choose the library to review.")
    if not Path(library, "metadata.db").is_file():
        raise LibraryError(f"{library} is not a Calibre library (no metadata.db)")
    if trash:
        if _same_path(library, trash):
            raise LibraryError("The trash library must be different from the library to review.")
        if Path(trash).exists() and not Path(trash, "metadata.db").is_file() and any(Path(trash).iterdir()):
            raise LibraryError(f"{trash} is neither a Calibre library nor an empty folder")
        if len(str(Path(trash).resolve())) > MAX_LIBRARY_PATH:
            raise LibraryError(f"The trash library path is too long for Calibre (max {MAX_LIBRARY_PATH} characters)")


def _same_path(a: str, b: str) -> bool:
    return str(Path(a).resolve()).casefold() == str(Path(b).resolve()).casefold()


def scan_library(library: str, trash: str, reviewer: Reviewer,
                 progress: Callable[[int, int, str], None] | None = None,
                 cancel: threading.Event | None = None,
                 on_item: Callable[[ReviewItem], None] | None = None,
                 unpack: Callable[[int], bool] | None = None,
                 tag: str = "", tag_exclude: bool = False,
                 library_cache: Path | None = None, pause: Pause | None = None) -> ReviewResult:
    """`unpack(n)`: asked once, before the first book, whether to unpack the clear archives
    of the n books that have one (see archives.ask_once); None: archives are read as they are.
    `tag`: review only the books with one of these tags, comma-separated (with `tag_exclude`,
    without any of them, e.g. REVIEWED_TAG: reviewed on an earlier day); "" = all.
    `library_cache`: where generic_covers keeps what it found (see library_cache).
    `pause`: the user may pause the run between two books (see pause.py)."""
    check_libraries(library, trash)
    books = read_books(library)
    library_books = books
    if progress is not None:
        progress(0, len(books), "Looking for generic covers…")
    generic = generic_covers(books, cancel, library_cache)  # over the whole library: reviewed books show the image too
    reviewer.generic = generic
    tag = tag.strip()
    tag_exclude = tag_exclude and bool(tag)
    kept = [b for b in books if tag_selects(b, tag, tag_exclude)]
    skipped, books = len(books) - len(kept), kept
    result = ReviewResult(library, trash, total_books=len(books), skipped=skipped, tag=tag,
                          tag_exclude=tag_exclude)
    filtered = tag_filter_text(tag, tag_exclude)
    log.info("Reviewing %d books of %s%s%s", len(books), library, f" {filtered}" if filtered else "",
             f" ({skipped} {tag_filter_text(tag, not tag_exclude)} left out)" if skipped else "")
    unpack = ask_once(books, unpack)
    perf.run_start("review", len(books), reviewer.provider, reviewer.vision)
    for n, book in enumerate(books):
        wait_if_paused(pause, cancel)
        if cancel is not None and cancel.is_set():
            result.stopped = True
            log.info("Review stopped after %d of %d books", n, len(books))
            break
        perf.book(n + 1)
        if progress:
            progress(n, len(books), f"Reading {book.label()}")
        item = _scan_book(reviewer, book, unpack)
        _mark_cover(item, generic.get(str(cover_file(book)), 0))
        result.items.append(item)
        if on_item:
            on_item(item)
    perf.run_end(len(result.items), result.stopped)
    # Known only now: every series name the AI read in the library's books
    mark_series_publishers(result.items, series_names(library_books, result.items))
    if progress:
        progress(len(result.items), len(books), "Done")
    result.stats = dict(reviewer.stats)
    result.ai_down = [r for r in (reviewer.disabled_reason, reviewer.image_disabled_reason) if r]
    return result


def ask_ai(reviewer: Reviewer, items: list[ReviewItem],
           progress: Callable[[int, int, str], None] | None = None,
           cancel: threading.Event | None = None,
           on_item: Callable[[ReviewItem, ReviewItem], None] | None = None,
           series: set[str] | None = None) -> list[tuple[ReviewItem, ReviewItem]]:
    """Ask the AI about these books again, never from the cache: each new row is built
    as if this were the first answer (what the user did to the old row is not kept).
    Archives are unpacked or not as the old row says. `on_item(old, new)` for each book.
    `series`: the series names of the review (series_names), never taken as a publisher.
    Returns the (old, new) pairs done."""
    done: list[tuple[ReviewItem, ReviewItem]] = []
    reviewer.fresh = True
    reviewer.generic = {str(cover_file(it.book)): it.generic_cover for it in items if it.generic_cover}
    perf.run_start("review-ask", len(items), reviewer.provider, reviewer.vision)
    try:
        for n, old in enumerate(items):
            if cancel is not None and cancel.is_set():
                log.info("Asking the AI stopped after %d of %d books", n, len(items))
                break
            perf.book(n + 1)
            if progress:
                progress(n, len(items), f"Reading {old.book.label()}")
            decided = {u.path: u.unpack for u in old.archives}
            unpack = (lambda book, u, d=decided: d.get(u.path, False)) if old.archives else None
            new = _scan_book(reviewer, old.book, unpack)
            _mark_cover(new, old.generic_cover)
            mark_series_publishers([new], (series or set()) | series_names([], [new]))
            done.append((old, new))
            if on_item:
                on_item(old, new)
    finally:
        reviewer.fresh = False
        perf.run_end(len(done), cancel is not None and cancel.is_set())
    if progress:
        progress(len(done), len(items), "Done")
    return done


JUDGE_END_CHARS = 5000  # of the last pages, given to the Judge AI


def judge_hints(reviewer: Reviewer, old: ReviewItem) -> str:
    """What the Judge AI is told besides the first pages and the cover: Calibre's metadata,
    the earlier reading, the last pages."""
    current = {name: format_value(name, current_value(old.book, name)) for name in FIELDS}
    lines = [f"Calibre's metadata now: {json.dumps(current, ensure_ascii=False)}"]
    if old.found is not None:
        read = {name: format_value(name, read_value(old.found, name)) for name in FIELDS}
        lines.append(f"A first reading by a smaller model: {json.dumps(read, ensure_ascii=False)}")
    end = reviewer.extractor.excerpt(old.book.formats, "end").text[-JUDGE_END_CHARS:]
    if end.strip():
        lines.append(f"Text of the last pages:\n{end}")
    return "\n".join(lines)


def judge_ai(reviewer: Reviewer, items: list[ReviewItem],
             progress: Callable[[int, int, str], None] | None = None,
             cancel: threading.Event | None = None,
             on_item: Callable[[ReviewItem, ReviewItem], None] | None = None,
             series: set[str] | None = None) -> list[tuple[ReviewItem, ReviewItem]]:
    """Ask the Judge AI (the reviewer's provider, a stronger model) about these books: as
    ask_ai, with more to go on (judge_hints). Each new row is a suggestion: unticked, to
    review (ReviewItem.judged), whatever it proposes."""
    olds = {it.book.id: it for it in items}
    model = (reviewer.vision or reviewer.provider).profile.name
    reviewer.hints = lambda book: judge_hints(reviewer, olds[book.id])

    def judged(old: ReviewItem, new: ReviewItem) -> None:
        new.judged = model
        new.selected = False
        new.note = _join(new.note, f"read by the Judge AI ({model})")
        if on_item:
            on_item(old, new)
    try:
        return ask_ai(reviewer, items, progress, cancel, judged, series)
    finally:
        reviewer.hints = None


def _mark_cover(item: ReviewItem, count: int) -> None:
    """A cover that is not a real one: generic (shown by `count` books), or a page of the book."""
    if BAD_COVER_TAG.casefold() in {t.casefold() for t in item.book.tags}:
        return
    if count:
        item.generic_cover = count
        item.note = _join(item.note, f"{generic_note(count)}: tagged {BAD_COVER_TAG} on Execute")
    elif item.book.has_cover and page_cover(cover_file(item.book)):
        item.page_cover = True
        item.note = _join(item.note, f"{PAGE_NOTE}: tagged {BAD_COVER_TAG} on Execute")


def _scan_book(reviewer: Reviewer, book: Book,
               unpack: Callable[[Book, object], bool] | None) -> ReviewItem:
    """One book of a scan: its archives unpacked as `unpack` says, then read."""
    seen, archives = unpack_archives(book, reviewer.extractor, unpack) if unpack is not None else (book, [])
    item = _review_book(reviewer, book, seen)
    if archives:
        item.archives = archives
        item.note = _join(item.note, "; ".join(u.note for u in archives))
        item.tick_cleanup()
    if item.changes:
        log.info("%s: %s", book.label(), ", ".join(
            f"{k} {format_value(k, current_value(book, k))!r} -> {format_value(k, v)!r}"
            for k, v in item.changes.items()))
    return item


def _review_book(reviewer: Reviewer, book: Book, seen: Book | None = None) -> ReviewItem:
    """Files Calibre can't open are not read: when no file can be, the AI isn't asked, and
    the book is proposed for the trash if none opens at all (empty, or not what its format
    says), else kept (a DOC, a file only Calibre's converter fails on: another program may
    open it). Else the AI reads the others (the next one when a file fails to open), and
    those that don't open at all are proposed for the trash library.
    `seen`: the book as read, with an unpacked archive's files instead of the archive."""
    seen = seen or book
    bad = unreadable_formats(seen.formats)
    while True:
        if bad and set(bad) >= set(seen.formats):
            log.warning("%s: %s", book.label(), "; ".join(bad.values()))
            fate = ("kept: another program may open it" if any(openable_elsewhere(w) for w in bad.values())
                    else "proposed for the trash")
            return ReviewItem(book, None, f"no file Calibre can open ({'; '.join(bad.values())}): {fate}",
                              bad_formats=bad)
        good = replace(seen, formats={f: p for f, p in seen.formats.items() if f not in bad}) if bad else seen
        meta, note = reviewer.review(good)
        extractor = reviewer.extractor
        failed = extractor.failed_formats(good.formats) if hasattr(extractor, "failed_formats") else {}
        if not failed:
            break
        bad.update(failed)  # read the book's next format
    if bad:
        log.warning("%s: %s", book.label(), "; ".join(bad.values()))
    return ReviewItem(book, meta, _join(note, f"unreadable: {'; '.join(bad.values())}" if bad else ""),
                      bad_formats=bad)


def summary(result: ReviewResult) -> str:
    read, cached = result.stats.get("read", 0), result.stats.get("read_cached", 0)
    failed = sum(1 for i in result.items if i.found is None)
    changed = sum(1 for i in result.items if i.changes)
    left_out = sum(1 for i in result.items for name in i.doubts if name in i.excluded)
    head = (f"Stopped after {len(result.items)} of {result.total_books} books" if result.stopped
            else f"{len(result.items)} books reviewed") + (
        f" ({tag_filter_text(result.tag, result.tag_exclude)})" if result.tag else "")
    skipped = (f" · {result.skipped} {tag_filter_text(result.tag, not result.tag_exclude)}, left out"
               if result.skipped else "")
    doubts = f" ({left_out} changes left out: not supported by the book)" if left_out else ""
    return (f"{head}: {changed} with differences{doubts}, {failed} not read{skipped} · AI: {read} read, "
            f"{cached} from cache" + "".join(f" · {r}" for r in result.ai_down))


RUNS_FOLDER = "review_runs"
RUN_FIELDS = ("title", "authors", "publisher", "year", "series", "isbn", "language")


def write_run_csv(result: ReviewResult, folder: Path | None = None) -> Path:
    """The analysis as a CSV, one row per book read: Calibre's values, what the AI read, and
    the proposed changes. Written after each analysis run without the AI cache (tests), whose
    answers would otherwise be lost: review_runs/review_<library>_<date>.csv in the data folder."""
    folder = folder or config_dir() / RUNS_FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"review_{Path(result.library).name}_{datetime.now():%Y%m%d_%H%M%S}.csv"

    def cell(name: str, value) -> str:
        return format_value(name, value)

    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["book_id"] + [f"calibre_{n}" for n in RUN_FIELDS] + [f"read_{n}" for n in RUN_FIELDS]
                   + [f"new_{n}" for n in RUN_FIELDS] + ["left_out", "cover", "action", "note"])
        for it in result.items:
            b, m = it.book, it.found
            calibre = [current_value(b, n) for n in RUN_FIELDS]
            read = ([m.title, m.authors, m.publisher, m.year, (m.series, m.series_index) if m.series else None,
                     " ".join(m.isbn), m.language] if m else [None] * len(RUN_FIELDS))
            w.writerow([b.id] + [cell(n, v) for n, v in zip(RUN_FIELDS, calibre)]
                       + [cell(n, v) for n, v in zip(RUN_FIELDS, read)]
                       + [cell(n, it.changes.get(n)) for n in RUN_FIELDS]
                       + ["; ".join(f"{n}: {why}" for n, why in it.doubts.items()), m.cover if m else "",
                          it.action.value, it.note])
    log.info("Analysis written to %s", path)
    return path


# --- executing ---------------------------------------------------------------------------
def decided(it: ReviewItem, fields_on: set[str] | list[str]) -> bool:
    """Whether the book is done with, to be tagged REVIEWED_TAG on Execute (the next
    analysis skips it): the user chose its action or marked it reviewed, or the AI read
    it and it is ticked, or proposes nothing. Not: a book that needs review, an unticked
    book that proposes something (left for later), a book the AI couldn't read."""
    if it.needs_review_for(fields_on):
        return False
    if it.manual or it.reviewed:
        return True
    if it.found is None:
        return False
    proposes = ((it.action is ReviewAction.UPDATE and bool(it.to_write(fields_on)))
                or it.action is ReviewAction.TRASH or it.cleanup)
    return it.selected or not proposes


def review_actions(items: list[ReviewItem], fields_on: set[str] | list[str]) -> list[dict]:
    """Bridge actions, for the ticked books only (an unticked book is left as it is):
    "set" for the updates, "trash" (no target) for the trash; "trash_formats": the formats
    Calibre can't open go first (the whole record is copied to the trash library as it
    is), on their own for a book with nothing to write; likewise "unpack". Then one "tag"
    REVIEWED_TAG with the other books done with (see decided; "set" tags its book itself,
    and AI_UPDATED_TAG when a field is written); last, one "tag" BAD_COVER_TAG with the
    books whose cover is not a real one (ReviewItem.bad_cover), unless they go to the
    trash. Each action carries the book's last_modified as the analysis read it ("stamp"):
    a book changed since in Calibre is not touched."""
    actions = []
    tag_ids = []
    for it in items:
        base = {"src_id": it.book.id, "title": it.book.title, "stamp": it.book.last_modified}
        done = decided(it, fields_on)
        if it.selected and it.action is ReviewAction.TRASH:
            actions.append({**base, "op": "trash", "no_target": True})
            continue
        if not it.selected:
            if done and not is_reviewed(it.book):
                tag_ids.append(it.book.id)
            continue
        bad = it.bad_formats_to_trash
        if bad:
            base["trash_formats"] = bad
        unpack = [u.spec(remove=True) for u in it.archives_to_unpack]
        if unpack:
            base["unpack"] = unpack
        changes = it.to_write(fields_on) if it.action is ReviewAction.UPDATE else {}
        if not changes:
            if bad or unpack:
                actions.append({**base, "op": "trash_formats" if bad else "unpack"})
            if done and not is_reviewed(it.book):
                tag_ids.append(it.book.id)
            continue
        values: dict = {}
        for name, value in changes.items():
            if name == "series":
                values["series"] = value[0]
                if value[1] is not None:
                    values["series_index"] = value[1]
            else:
                values[name] = value
        actions.append({**base, "op": "set", "set": values, "updated_tag": AI_UPDATED_TAG,
                        **({"tag": REVIEWED_TAG} if done else {})})
    if tag_ids:
        actions.append({"op": "tag", "src_ids": tag_ids, "tag": REVIEWED_TAG})
    bad_covers = [it.book.id for it in items
                  if it.bad_cover and not (it.selected and it.action is ReviewAction.TRASH)]
    if bad_covers:
        actions.append({"op": "tag", "src_ids": bad_covers, "tag": BAD_COVER_TAG})
    return actions


def apply_to_book(book: Book, values: dict, path: str | None, formats: dict | None) -> Book:
    """The book as it is in the library after a "set" action."""
    out = replace(book)
    if "title" in values:
        out.title = values["title"]
    if "authors" in values:
        out.authors = list(values["authors"])
    if "publisher" in values:
        out.publisher = values["publisher"]
    if "year" in values:
        out.pub_year = int(values["year"])
    if "series" in values:
        out.series = values["series"]
        if "series_index" in values:
            out.series_index = values["series_index"]
    if "isbn" in values:
        out.isbns = set(out.isbns) | {values["isbn"]}
    if "language" in values:
        out.languages = [values["language"]]
    if path:
        out.path = path
    if formats:
        out.formats = {fmt.upper(): p for fmt, p in formats.items() if p}
    return out


def _journal_values(book: Book, values: dict) -> tuple[str, str]:
    """(before, after) of the fields a "set" action writes, for the journal."""
    names = [n for n in FIELDS if n in values or (n == "series" and "series_index" in values)]
    after = {n: (values.get("series", book.series), values.get("series_index", book.series_index))
             if n == "series" else values[n] for n in names}
    return ("; ".join(f"{FIELD_LABELS[n]}: {format_value(n, current_value(book, n))}" for n in names),
            "; ".join(f"{FIELD_LABELS[n]}: {format_value(n, v)}" for n, v in after.items()))


def execute_review(result: ReviewResult, fields_on: set[str] | list[str], calibre_dir: Path,
                   permanent: bool = False,
                   on_result: Callable[[ReviewItem, bool, str], None] | None = None,
                   cancel: threading.Event | None = None, journal: Path | None = None) -> tuple[int, int, int]:
    """Write the checked updates, move the checked books to the trash library, and
    tag the reviewed books REVIEWED_TAG (see review_actions). Each book acted on (not
    only tagged) gets a row in the journal (journal.Journal, in `journal`, default the
    data folder). Returns (succeeded, failed, tagged with nothing written)."""
    if calibre_is_running():
        raise ExecutionError("Calibre is running. Close Calibre (and calibre-server) before executing.")
    actions = review_actions(result.items, fields_on)
    if not actions:
        return 0, 0, 0
    if any(a["op"] == "trash" or a.get("trash_formats") or a.get("unpack") for a in actions) \
            and not result.trash_library:
        raise ExecutionError("Choose a trash library to move books to it.")
    items = {i.book.id: i for i in result.items}
    sent = {a["src_id"]: a for a in actions if "src_id" in a}
    ok = failed = tagged = 0
    book_log = Journal("review", journal)

    def restamp(item: ReviewItem, value: str | None) -> None:
        """The book's last_modified after this run changed it: a next Execute of the same list checks against it."""
        if value:
            item.book.last_modified = value

    def on_message(msg: dict) -> None:
        nonlocal ok, failed, tagged
        stamps = msg.get("stamps") or {}
        if msg["event"] == "tagged" and msg.get("tag") == BAD_COVER_TAG:  # a mark only: the rows keep their status
            if msg["ok"]:
                for sid in msg["src_ids"]:
                    items[sid].book.tags = set(items[sid].book.tags) | {BAD_COVER_TAG}
                    items[sid].generic_cover, items[sid].page_cover = 0, False
                    restamp(items[sid], stamps.get(str(sid)))
                log.info("%d book(s) with a cover that is not a real one %s", len(msg["src_ids"]), msg["msg"])
            else:
                failed += len(msg["src_ids"])
                log.error("Tagging %s failed: %s", BAD_COVER_TAG, msg.get("trace") or msg["msg"])
            return
        if msg["event"] == "tagged":
            if msg["ok"]:
                tagged += len(msg["src_ids"])
                log.info("%d book(s) %s", len(msg["src_ids"]), msg["msg"])
            else:
                failed += len(msg["src_ids"])
                log.error("Tagging %s failed: %s", REVIEWED_TAG, msg.get("trace") or msg["msg"])
            for sid in msg["src_ids"]:
                item = items[sid]
                item.status = ("OK: " if msg["ok"] else "FAILED: ") + msg["msg"]
                if msg["ok"]:
                    item.book.tags = set(item.book.tags) | {REVIEWED_TAG}
                    restamp(item, stamps.get(str(sid)))
                if on_result:
                    on_result(item, msg["ok"], msg["msg"])
            return
        if msg["event"] != "result":
            return
        item = items[msg["src_id"]]
        item.status = ("OK: " if msg["ok"] else "FAILED: ") + msg["msg"]
        action = sent[item.book.id]
        before, after = _journal_values(item.book, action["set"]) if action["op"] == "set" else ("", "")
        book_log.write(library=result.library, book_id=item.book.id, title=item.book.title,
                       authors=" & ".join(item.book.authors),
                       action={"set": "Update metadata", "trash": "Move to the trash library",
                               "trash_formats": "Unreadable formats to the trash library",
                               "unpack": "Archive unpacked"}.get(action["op"], action["op"]),
                       ok="yes" if msg["ok"] else "no", result=msg["msg"],
                       why="; ".join([item.note] + [f"{f}: {w}" for f, w in item.bad_formats.items()
                                                    if f in (action.get("trash_formats") or [])]),
                       before=before, after=after)
        if msg["ok"]:
            ok += 1
            if action.get("trash_formats"):  # the book no longer has them
                gone = set(action["trash_formats"])
                item.book = replace(item.book, formats={f: p for f, p in item.book.formats.items() if f not in gone})
                item.bad_formats = {f: why for f, why in item.bad_formats.items() if f not in gone}
            if action.get("unpack"):  # done: its files are now the book's formats
                item.archives = [u for u in item.archives if not u.unpack]
                if msg.get("formats") and action["op"] != "set":
                    item.book = replace(item.book, formats={f.upper(): p for f, p in msg["formats"].items() if p})
            if action["op"] == "set":
                book = apply_to_book(item.book, action["set"], msg.get("path"), msg.get("formats"))
                book.tags = set(book.tags) | ({action["tag"]} if action.get("tag") else set())
                item.book_changed(book)
            restamp(item, msg.get("stamp"))
            log.info("Book %s (%s): %s", item.book.id, item.book.title, msg["msg"])
        else:
            failed += 1
            log.error("Book %s (%s): %s", item.book.id, item.book.title, msg.get("trace") or msg["msg"])
        if on_result:
            on_result(item, msg["ok"], msg["msg"])

    with book_log:
        log.info("Journal of this execution: %s", book_log.path)
        run_bridge(calibre_dir, {"source": result.library, "trash": result.trash_library,
                                 "permanent": permanent, "actions": actions}, on_message, cancel, program="review")
    return ok, failed, tagged
