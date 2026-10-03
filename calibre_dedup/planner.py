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

"""Build the plan: decide, for every source book, whether to move it to the
target library, send it to the trash library, or leave it where it is. When
source and target are the same library, duplicate records are merged into the
record with richer metadata and the weaker record is sent to trash.

A book is identified by its own record: one without title or authors, with a title
made from a file name, or with title and author swapped, is never moved (the Metadata
Review fixes the record first); it is still trashed when proven a duplicate. AI is only
consulted to tell two copies apart, when a same-title/same-author book exists but
edition or publisher can't be compared from metadata (both books are then read): the
first pages, then the last pages if fields are still missing.
If that still can't decide, the covers do (see _by_covers): with no edition data
to compare, two books are the same unless their covers differ; the same cover is
proof, unless it is a generic cover (the same image on books of different titles,
see covers.py). Without the AI, identical files prove the same book whatever the
metadata, and identical EPUB text proves it before any AI call (copies that differ
only in metadata or cover). A copy whose text is in another language is another book.
A duplicate that isn't proven (no edition data, decided by the covers, authors matched
only by the AI or the surname) starts unticked, for the user to check (PlanItem.review).
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Callable

from . import perf
from .archives import ask_once, prepare as unpack_archives
from .ai import (
    AICache, AIError, AIMetadata, Provider, ask_fitting, compare_authors, compare_covers, extract_metadata,
)
from .covers import GENERIC_COVER_BOOKS, generic_covers, generic_note
from .extract import (CALIBRE_INPUT_FORMATS, TextExtractor, cover_png, epub_text_digest, openable_elsewhere,
                      unreadable_formats)
from .language import language_name
from .library_cache import FileChecks, FileHashes, TextFacts
from .library import read_books, tag_filter_text, tag_selects
from .matcher import Decision, Verdict, compare, decide
from .models import Action, Book, Identity, Plan, PlanItem
from .normalize import (
    MIN_SURNAME_LENGTH, VARIOUS_AUTHORS_KEY, author_key, authors_key, contains_title, initials_match, is_unknown,
    looks_like_file_name, looks_like_name, names_nearly_equal, noise_words, normalize_isbn, parse_edition_number,
    related_titles, series_key, similar_authors_keys, surname_match, title_key, title_variants,
)
from .selection import action_label, has_cleanup

log = logging.getLogger(__name__)

ProgressFn = Callable[[int, int, str], None]  # (done, total, message)
MAX_CONSECUTIVE_AI_ERRORS = 3
# Answers to "AI not responding" (AIResolver.on_down). True/False mean RETRY/AI_OFF.
RETRY = "retry"  # send the same request again
SKIP_BOOK = "skip"  # leave this book without the AI, keep the AI on (asked again after as many errors)
AI_OFF = "off"  # go on without that AI for the rest of the run
MAX_LIBRARY_PATH = 89  # Calibre refuses longer library paths on Windows
# Checks wanted for a book that could not run (PlanItem.skipped).
SKIP_AI = "text AI"  # turned off after repeated errors
SKIP_IMAGE_AI = "image AI"  # turned off after repeated errors
SKIP_COVER = "cover check"  # no Image AI (or no AI at all)
SKIP_YEARS = "year re-check"  # AI off
SKIP_SCANNED = "scanned PDF"  # a PDF with no text, and no Image AI to read its pages
SCANNED_PDF_SKIPPED = "scanned PDF skipped: no Image AI"
# What two covers say about two books (see _covers).
SAME = "same"  # identical files, or the same cover per the Image AI
DIFFERENT = "different"
UNCLEAR = "unclear"  # the books' own doing: a cover missing, not a real one, or the AI unsure
SKIPPED = "skipped"  # the check can't run here: cover check off, no Image AI, the AI down
_COVER_VERDICTS = {"same": SAME, "different": DIFFERENT, "unsure": UNCLEAR}
LENGTH_RATIO = 3  # a copy this many times longer than the other is another content


def metadata_identity(book: Book, same_series: bool = False) -> Identity:
    """`same_series`: the book's series and number identify it (the option), unless
    the number is Calibre's default 1 (or 0): then it means nothing."""
    title = None if is_unknown(book.title) else book.title
    authors = [a for a in book.authors if not is_unknown(a)]
    series = None
    if same_series and book.series and book.series_index not in (None, 0, 1) and series_key(book.series):
        series = (series_key(book.series), float(book.series_index))
    return Identity(
        title=title,
        authors=authors,
        publisher=book.publisher,
        edition=parse_edition_number(title) if title else None,
        year=book.pub_year,
        isbns=set(book.isbns),
        asins=set(book.asins),
        series=series,
    )


def merge_ai(ident: Identity, meta: AIMetadata) -> Identity:
    """Fill fields missing from `ident` with what the AI found, and keep what it
    read for year and publisher (see matcher: compared like with like)."""
    out = ident.copy()
    if out.ai_year is None and meta.year:
        out.ai_year = meta.year
    if not out.ai_publisher and meta.publisher:
        out.ai_publisher = meta.publisher
    if not out.title and meta.title:
        out.title = meta.title
        out.ai_fields.add("title")
    if not out.authors and meta.authors:
        out.authors = list(meta.authors)
        out.ai_fields.add("authors")
    if not out.publisher and meta.publisher:
        out.publisher = meta.publisher
        out.ai_fields.add("publisher")
    if out.edition is None:
        edition = meta.edition_number or parse_edition_number(meta.edition)
        if edition is not None:
            out.edition = edition
            out.ai_fields.add("edition")
    if out.year is None and meta.year:
        out.year = meta.year
        out.ai_fields.add("year")
    isbns = {i for i in (normalize_isbn(x) for x in meta.isbn) if i}
    if isbns - out.isbns:
        out.isbns |= isbns
        out.ai_fields.add("isbn")
    return out


def _needs_edition_publisher(i: Identity) -> bool:
    return not i.has_edition_info or not i.publisher


def _needs_ai_year_publisher(i: Identity) -> bool:
    return i.ai_year is None or not i.ai_publisher


class ImageAIError(AIError):
    """An error from a request with images, sent to the image AI."""


class AIResolver:
    """Reads book excerpts and asks the model for missing metadata.

    `provider` is the text AI. `vision` (optional) is the image AI, which reads
    text and images: it compares covers and reads scanned PDFs. Each counts its
    own errors, so a failing image AI never switches off the text AI.

    After MAX_CONSECUTIVE_AI_ERRORS errors in a row, `on_down(message, image)` is
    asked (the GUI shows a dialog): RETRY (or True) sends the same request again,
    SKIP_BOOK leaves this book without the AI and goes on with it, AI_OFF (or False)
    turns that AI off for the rest of this run. Without it, the AI is turned off. It
    is never turned off in the settings: the next run tries it again.
    Errors about one book (content filter, too long for the context) are not counted.
    """

    def __init__(self, provider: Provider, extractor: TextExtractor, cache: AICache,
                 vision_provider: Provider | None = None,
                 on_down: Callable[[str, bool], str | bool] | None = None):
        self.provider = provider
        self.vision = vision_provider
        self.extractor = extractor
        self.cache = cache
        self.on_down = on_down
        self.consecutive_errors = 0
        self.disabled_reason = ""
        self.image_errors = 0
        self.image_disabled_reason = ""
        self.generic: dict[str, int] = {}  # generic covers (covers.generic_covers): never proof
        # What the AI did during the run, for the summary: "read", "read_cached",
        # "cover", "cover_cached", "cover_identical".
        self.stats: Counter[str] = Counter()

    @staticmethod
    def _model_id(p: Provider) -> str:
        return f"{p.profile.kind}:{p.profile.base_url}:{p.profile.model}"

    def enrich(self, book: Book, ident: Identity, need: Callable[[Identity], bool]) -> tuple[Identity, list[str]]:
        notes: list[str] = []
        for part in ("start", "end"):
            if not need(ident):
                break
            if self.disabled_reason:
                notes.append(self.disabled_reason)
                break
            try:
                meta, note = self._query(book, part)
            except AIError as e:
                note = self._error(book, e, image=isinstance(e, ImageAIError))
                if note is None:  # the user chose Retry
                    return self.enrich(book, ident, need)
                notes.append(note)
                break
            self.consecutive_errors = 0
            notes.append(note)
            if meta:
                ident = merge_ai(ident, meta)
        return ident, notes

    def _error(self, book: Book, e: AIError, image: bool = False) -> str | None:
        """Count an error; returns the note for the book, or None to retry the request."""
        what = "image AI" if image else "AI"
        log.warning("%s error on %s: %s", what, book.label(), e)
        if e.filtered or e.too_long:  # refused for this book (its content, its length): the AI works
            return f"{what}: {e}"
        if image:
            self.image_errors += 1
            count, disabled = self.image_errors, self.image_disabled_reason
        else:
            self.consecutive_errors += 1
            count, disabled = self.consecutive_errors, self.disabled_reason
        if count >= MAX_CONSECUTIVE_AI_ERRORS and not disabled:
            choice = AI_OFF if self.on_down is None else self.on_down(
                f"The {'image' if image else 'text'} AI failed {count} times in a row.\n\n"
                f"Last error (on {book.label()}):\n{e}", image)
            choice = {True: RETRY, False: AI_OFF}.get(choice, choice)
            if choice in (RETRY, SKIP_BOOK):
                if image:
                    self.image_errors = 0
                else:
                    self.consecutive_errors = 0
            if choice == RETRY:
                log.info("%s: retrying after %d consecutive errors", what, count)
                return None
            if choice == SKIP_BOOK:
                log.info("%s: %s skipped after %d consecutive errors, %s kept on", what, book.label(), count, what)
                return f"{what} error: {e}"
            reason = f"{what} disabled after {count} consecutive errors"
            if image:
                self.image_disabled_reason = reason
            else:
                self.disabled_reason = reason
            log.error(reason)
        return f"{what} error: {e}"

    def same_cover(self, a: Book, b: Book) -> tuple[bool, str, bool]:
        """Whether both books show the same cover: identical files, or the same per the
        vision model; a generic cover is never proof. Returns (same, note, model_called)."""
        pa, pb = _cover_path(a), _cover_path(b)
        if self.vision is None or not (pa.is_file() and pb.is_file()):
            return False, "", False
        for book, path in ((a, pa), (b, pb)):
            if str(path) in self.generic:
                return False, f"{generic_note(self.generic[str(path)])} on {book.label()}: not proof", False
        if _identical(pa, pb):
            self.stats["cover_identical"] += 1
            return True, "identical cover files", False
        verdict, note, called = self.ai_cover_verdict(a, b)
        return verdict == SAME, note, called

    def ai_cover_verdict(self, a: Book, b: Book) -> tuple[str, str, bool]:
        """What the vision model says of two books' covers (both there, neither generic nor
        identical: see _covers): SAME, DIFFERENT, UNCLEAR (it is unsure, or an image can't
        be read) or SKIPPED (the image AI is down, or failed). Returns (verdict, note,
        model_called)."""
        pa, pb = _cover_path(a), _cover_path(b)
        key = AICache.pair_key(str(pa), str(pb), f"cover|{self._model_id(self.vision)}")
        cached = self.cache.get(key)
        if cached is not None:
            self.stats["cover_cached"] += 1
            return (_COVER_VERDICTS.get(cached["verdict"], UNCLEAR), f"AI cover check: {cached['verdict']} (cached)",
                    False)
        if self.image_disabled_reason:
            return SKIPPED, self.image_disabled_reason, False
        covers = cover_png(pa), cover_png(pb)
        if None in covers:
            return UNCLEAR, "cover image unreadable", False
        log.info("AI comparing covers of %s and %s", a.label(), b.label())
        try:
            verdict, reason = compare_covers(self.vision, *covers)
        except AIError as e:
            note = self._error(a, e, image=True)
            if note is None:  # the user chose Retry
                return self.ai_cover_verdict(a, b)
            return SKIPPED, note, True
        self.image_errors = 0
        self.stats["cover"] += 1
        self.cache.put(key, {"verdict": verdict, "reason": reason})
        return _COVER_VERDICTS.get(verdict, UNCLEAR), f"AI cover check: {verdict}", True

    def same_person(self, book: Book, names_a: str, names_b: str,
                    title: str | None = None) -> tuple[bool, str, bool]:
        """Whether two author names are the same person, per the text AI (asked in
        English, answer cached); `title`: the title both books have, given as context.
        Returns (same, note, model_called)."""
        if self.disabled_reason:
            return False, self.disabled_reason, False
        pair = "|".join(sorted((names_a.casefold(), names_b.casefold())))
        # "author2": asked with the title; answers to the older question (names only) are not reused
        context = title_key(title) if title else ""
        key = hashlib.sha1(f"author2|{pair}|{context}|{self._model_id(self.provider)}".encode()).hexdigest()
        cached = self.cache.get(key)
        if cached is not None:
            self.stats["author_cached"] += 1
            return cached["verdict"] == "same", f"AI: {cached['verdict']} person (cached)", False
        log.info("AI comparing authors %r and %r", names_a, names_b)
        try:
            verdict, reason = compare_authors(self.provider, names_a, names_b, title)
        except AIError as e:
            note = self._error(book, e)
            if note is None:  # the user chose Retry
                return self.same_person(book, names_a, names_b, title)
            return False, note, True
        self.consecutive_errors = 0
        self.stats["author"] += 1
        self.cache.put(key, {"verdict": verdict, "reason": reason})
        return verdict == "same", f"AI: {verdict} person", True

    def same_embedded_cover(self, a: Book, b: Book) -> tuple[bool, str, bool]:
        """Whether the covers stored inside both books' files are the same: the
        check that Calibre's cover.jpg (which may be a downloaded picture) really
        is each book's own. Returns (same, note, model_called)."""
        if self.vision is None or self.extractor is None:
            return False, "", False
        found = self.extractor.embedded_cover(a.formats), self.extractor.embedded_cover(b.formats)
        missing = [book.label() for book, f in zip((a, b), found) if f is None]
        if missing:
            return False, f"no cover inside the file of {missing[0]!r} to confirm", False
        (pa, ca), (pb, cb) = found
        if ca == cb:
            self.stats["embedded_identical"] += 1
            return True, "identical covers inside the files", False
        key = AICache.pair_key(pa, pb, f"embedded-cover|{self._model_id(self.vision)}")
        cached = self.cache.get(key)
        if cached is not None:
            self.stats["embedded_cached"] += 1
            return cached["verdict"] == "same", f"covers inside the files: {cached['verdict']} (cached)", False
        if self.image_disabled_reason:
            return False, self.image_disabled_reason, False
        covers = cover_png(ca), cover_png(cb)
        if None in covers:
            return False, "cover inside the file unreadable", False
        log.info("AI comparing the covers inside the files of %s and %s", a.label(), b.label())
        try:
            verdict, reason = compare_covers(self.vision, *covers)
        except AIError as e:
            note = self._error(a, e, image=True)
            if note is None:  # the user chose Retry
                return self.same_embedded_cover(a, b)
            return False, note, True
        self.image_errors = 0
        self.stats["embedded"] += 1
        self.cache.put(key, {"verdict": verdict, "reason": reason})
        return verdict == "same", f"covers inside the files: {verdict}", True

    def _query(self, book: Book, part: str) -> tuple[AIMetadata | None, str]:
        picked = self.extractor.pick_format(book.formats)
        if not picked:
            return None, "no readable file"
        path = picked[1]
        cached = self.cache.get(AICache.key(path, part))
        if cached is not None:
            self.stats["read_cached"] += 1
            return AIMetadata(**cached), f"AI {part} pages (cached)"

        providers = [self.provider] + ([self.vision] if self.vision else [])
        for p in providers:
            key = AICache.key(path, part, self._model_id(p))
            cached = self.cache.get(key)
            if cached is not None:
                self.cache.put(AICache.key(path, part), cached)
                self.stats["read_cached"] += 1
                return AIMetadata(**cached), f"AI {part} pages (cached)"

        excerpt = self.extractor.excerpt(book.formats, part)
        if excerpt.images and self.vision and not self.image_disabled_reason:
            provider, images = self.vision, excerpt.images
        elif excerpt.text.strip():
            provider, images = self.provider, None
        else:
            if self.image_disabled_reason:
                return None, self.image_disabled_reason
            note = f"no text in {part} pages ({excerpt.source})"
            if self.vision is None and picked[0] == "PDF":
                note += f"; {SCANNED_PDF_SKIPPED}"
            return None, note
        log.info("AI reading %s of %s (%s)", part, book.label(), excerpt.source)
        try:
            meta, _ = ask_fitting(lambda text: extract_metadata(provider, text, images), excerpt.text,
                                  book.label(), keep_end=part == "end")
        except AIError as e:
            raise (ImageAIError(str(e), e.status, e.filtered) if images else e) from e
        self.stats["read"] += 1
        if images:
            self.image_errors = 0
        self.cache.put(AICache.key(path, part), meta.to_dict())
        for p in providers:
            self.cache.put(AICache.key(path, part, self._model_id(p)), meta.to_dict())
        return meta, f"AI read {excerpt.source}"


@dataclass
class _Candidate:
    book: Book
    identity: Identity
    planned: bool  # a source book that the plan moves into the target
    ai_done: bool = False
    year_rechecked: bool = False  # the AI read it to re-check a year difference
    formats: set[str] = field(default_factory=set)  # formats the target copy will have
    _variants: list[str] | None = None

    def __post_init__(self):
        self.formats = self.formats or set(self.book.formats)

    @property
    def variants(self) -> list[str]:
        """Title keys for similar-title matching (see normalize.title_variants)."""
        if self._variants is None:
            self._variants = title_variants(self.identity.title or "", self.identity.authors)
        return self._variants


def _keys(ident: Identity, ignore_subtitle: bool, similar: bool) -> list[tuple]:
    """Index keys of a book. Strict: title + all authors. Similar: title + each
    author, loosely normalized, so books sharing any author are compared."""
    if not ident.authors:
        return []
    if similar:
        authors = similar_authors_keys(ident.authors)
    else:
        a = authors_key(ident.authors)
        authors = [a] if a else []
    t = title_key(ident.title, ignore_subtitle) if ident.title else ""
    return [(t, a) for a in authors] if t else []


def _series_key(ident: Identity) -> tuple:
    """Index key of a book's series and number (the "same series" option), whatever
    its title and authors."""
    return ("series", *ident.series)


def _series_agrees(a: Identity, b: Identity) -> bool:
    """Same series and number is proof only when the titles or the authors agree too:
    libraries file a publisher's sub-series (Millemondi, Classici) and wrong numbers
    under one series name, so the number alone pairs unrelated books."""
    if a.title and b.title and related_titles(a.title, b.title, noise_words(a.series[0])):
        return True
    people = [(x, y) for x in a.authors for y in b.authors
              if not is_unknown(x) and not is_unknown(y) and author_key(x) != VARIOUS_AUTHORS_KEY]
    return any(author_key(x) == author_key(y) or names_nearly_equal(x, y) or initials_match(x, y)
               for x, y in people)


def _author_keys(ident: Identity, similar: bool) -> list:
    """Index keys of a book's authors, as _keys matches them."""
    if similar:
        return similar_authors_keys(ident.authors)
    a = authors_key(ident.authors)
    return [a] if a else []


SWAPPED_NOTE = "title and author were swapped"
PERSON_TITLE_NOTE = "authors not compared by AI: the title is a person's name (title and author swapped?)"


class SwapDetector:
    """Books whose title and author are swapped ("Kingston — The Log House by the Lake"), usually
    from file names ("Kingston - The Log House by the Lake.epub") that Calibre read as "Title - Author".
    From metadata only, over both libraries: the title is the name of a person who is the
    author of other books, written as a name; the author field is not an author of any other
    book, and reads as a title (a single capitalized word needs a person with at least
    two other books). Books named after a person ("Ruskin — Walter Thornbury", a
    biography "Rousseau — John Morley") keep their metadata: their author field is a name."""

    MIN_SUPPORT_SINGLE_WORD = 2

    def __init__(self, books: list[Book]):
        self.by_author: dict[tuple, set[int]] = defaultdict(set)  # author key -> id(book)
        self.by_part: dict[str, set[tuple]] = defaultdict(set)  # name part -> author keys
        self.by_title: dict[str, set[int]] = defaultdict(set)  # title key -> id(book)
        for b in books:
            self.by_title[title_key(b.title)].add(id(b))
            for a in b.authors:
                k = author_key(a)
                if is_unknown(a) or not looks_like_name(a) or not k or k == VARIOUS_AUTHORS_KEY:
                    continue  # a swapped book's "author" is a title: not a person
                self.by_author[k].add(id(b))
                for part in k:
                    if len(part) >= MIN_SURNAME_LENGTH:
                        self.by_part[part].add(k)

    def _others(self, key: tuple, title: str) -> set[int]:
        """Books by `key`, other than those with this title (the swapped copies themselves)."""
        return self.by_author.get(key, set()) - self.by_title.get(title_key(title), set())

    def person(self, title: str) -> tuple | None:
        """The author key of the person `title` names, if a known author of other books:
        the whole name, or a surname alone that only one author has."""
        if is_unknown(title) or not looks_like_name(title):
            return None
        k = author_key(title)
        if not k or k == VARIOUS_AUTHORS_KEY:
            return None
        if self._others(k, title):
            return k
        if len(k) == 1:
            found = [x for x in self.by_part.get(k[0], ()) if self._others(x, title)]
            if len(found) == 1:
                return found[0]
        return None

    def swap(self, book: Book) -> Book | None:
        """The book with title and authors put right, or None if they look right."""
        authors = [a for a in book.authors if not is_unknown(a)]
        person = self.person(book.title) if authors else None
        if person is None:
            return None
        if any(self._others(author_key(a), book.title) for a in authors):
            return None  # its author has other books: a book named after a person
        text = " & ".join(authors)
        tokens = set(author_key(text))
        if any(len(k) > 1 and set(k) <= tokens and self._others(k, book.title)
               for k in self.by_author if k[0] in tokens):
            return None  # names a known person ("Jean-Jacques Rousseau (ed.)")
        words = re.findall(r"[^\W\d_]+|\d+", text)
        if (not words or not (words[0][0].isupper() or words[0].isdigit())
                or re.search(r"[^\W\d_]\d|\d[^\W\d_]", text)):
            return None  # junk ("* * *", "ab01234", "a cura di ..."): the title is no better
        if all(looks_like_name(a) for a in authors):
            if any(len(re.findall(r"[^\W\d_]+", a)) > 1 for a in authors):
                return None  # a person's name: a book about someone
            if len(self._others(person, book.title)) < self.MIN_SUPPORT_SINGLE_WORD:
                return None
        return replace(book, title=text, authors=[book.title])


@dataclass
class _SimilarIndex:
    by_author: dict  # author key -> candidates
    collections: set[str]  # words of the libraries' collections (see _collection_words)


COLLECTION_MIN_BOOKS = 20  # a series this large is a collection (Gutenberg), not a story's series


def _collection_words(books: list[Book]) -> set[str]:
    """Words of the collections in the libraries: series with many books, like
    "Gutenberg" or "Gutenberg Classics". Found around titles, they don't make another book."""
    counts = Counter(series_key(b.series) for b in books if b.series)
    return {w for name, n in counts.items() if n >= COLLECTION_MIN_BOOKS for w in name.split()}


def _similar_title(sb: Book, ident: Identity, c: _Candidate, collections: set[str]) -> str:
    """The title both books' titles are made of, with only noise around it (see
    normalize.contains_title), or '' when there's none. Looked for among both
    books' title variants: "brother jacob" is in "(Gutenberg - 0411- Brother Jacob -
    George Eliot)" and is "Brother Jacob"."""
    noise = collections | noise_words(*sb.authors, *c.book.authors, sb.series, c.book.series,
                                      sb.publisher, c.book.publisher)
    mine = title_variants(ident.title or "", ident.authors)
    if not mine or not c.variants:
        return ""
    core_mine, core_theirs = mine[0], c.variants[0]
    for title in dict.fromkeys(mine + c.variants):
        if contains_title(core_mine, title, noise) and contains_title(core_theirs, title, noise):
            return repr(title)
    return ""


def _lookup(index: dict[tuple, list[_Candidate]], keys: list[tuple]) -> list[_Candidate]:
    found: dict[int, _Candidate] = {}
    for k in keys:
        for c in index.get(k, []):
            found.setdefault(id(c), c)
    return list(found.values())


def _placeholder(books: list[Book]) -> bool:
    """The same file on books of GENERIC_COVER_BOOKS or more titles and authors is a
    placeholder (a "file not found" page), as a generic cover is."""
    return (len({title_key(b.title or "") for b in books}) >= GENERIC_COVER_BOOKS
            and len({authors_key(b.authors) for b in books}) >= GENERIC_COVER_BOOKS)


class _FileIndex:
    """The books' files by format and size (from metadata.db: nothing is opened), to find
    the same file in two books whatever their titles and authors: the same book. Only
    files of the same size are read (FileHashes), to tell."""

    def __init__(self, hashes: FileHashes):
        self.hashes = hashes
        self.by_size: dict[tuple[str, int], list[_Candidate]] = defaultdict(list)

    def add(self, c: _Candidate) -> None:
        for fmt in c.book.formats:
            if c.book.sizes.get(fmt):
                self.by_size[(fmt, c.book.sizes[fmt])].append(c)

    def match(self, book: Book) -> tuple[_Candidate, str] | None:
        """A book of the index with one of `book`'s files, and that file's format."""
        for fmt in book.formats:
            size = book.sizes.get(fmt)
            others = [c for c in self.by_size.get((fmt, size), []) if c.book is not book] if size else []
            mine = self.hashes.digest(book, fmt) if others else None
            same = [c for c in others if mine is not None and self.hashes.digest(c.book, fmt) == mine]
            if same and not _placeholder([book] + [c.book for c in same]):
                return same[0], fmt
        return None


@dataclass
class _Context:
    """What deciding a book needs besides the book: the indexes of the books it may be
    a copy of, the AI, the options, and what was found in the books' files."""
    index: dict[tuple, list[_Candidate]]
    resolver: AIResolver | None
    stats: Counter[str]
    ignore_subtitle: bool = False
    same_library: bool = False
    similar_matching: bool = False
    cover_check: bool = False
    recheck_years: bool = False
    same_series: bool = False
    always_cover: bool = False
    similar: _SimilarIndex | None = None  # similar titles
    by_title: dict | None = None  # authors written differently
    persons: SwapDetector | None = None
    generic: dict[str, int] = field(default_factory=dict)  # generic covers (covers.py): never proof
    files: _FileIndex | None = None
    texts: TextFacts | None = None  # language and length of the books' text; None: not read
    # Books that stay as they are (the target's) whose title is a file name, by author key:
    # one may be any book by that author, which is then not moved (see _plan_one).
    named: dict[tuple, list[_Candidate]] = field(default_factory=dict)


@dataclass
class _Outcome:
    """What comparing a book with its candidates found (see _decide_one)."""
    decision: Decision
    content: bool = False  # the same text: in the same language, whatever it is
    by_cover: bool = False
    override: bool = False  # the same cover overruled the metadata
    no_edition: bool = False  # PlanItem.no_edition
    called: bool = False  # the AI was asked


def _unreachable(paths: list[Path]) -> str:
    """The first library database that can't be reached any more, or ''."""
    for p in paths:
        try:
            if p.is_file():
                continue
        except OSError:
            pass
        return str(p.parent)
    return ""


def _same_text(sb: Book, ident: Identity, candidates: list[_Candidate]) -> Decision | None:
    """A candidate that metadata can't tell apart and whose EPUB has the same text."""
    mine = _epub_digest(sb)
    if not mine:
        return None
    for i, c in enumerate(candidates):
        if compare(ident, c.identity).verdict is Verdict.UNKNOWN and _epub_digest(c.book) == mine:
            return Decision(Verdict.DUPLICATE, "identical EPUB text", i)
    return None


def _epub_digest(book: Book) -> str | None:
    path = book.formats.get("EPUB")
    try:
        st = os.stat(path) if path else None
    except OSError:
        return None
    return _digest(path, st.st_mtime_ns, st.st_size) if st else None


@lru_cache(maxsize=4096)
def _digest(path: str, mtime_ns: int, size: int) -> str | None:  # keyed on mtime/size: files change
    return epub_text_digest(path)


def _cover_path(book: Book) -> Path:
    return Path(book.library, book.path, "cover.jpg")


def _identical(a: Path, b: Path) -> bool:
    try:
        return a.stat().st_size == b.stat().st_size and a.read_bytes() == b.read_bytes()
    except OSError:  # a cover that went away is no proof, and not this book's error
        return False


def _covers(ctx: _Context, a: Book, b: Book) -> tuple[str, str, bool, str]:
    """What the two books' covers say: SAME (identical files, which needs no AI, or the
    same per the Image AI), DIFFERENT, UNCLEAR (a cover missing, a generic one, an image
    that can't be read, or the AI unsure: the books' own doing) or SKIPPED (the check
    can't run here: cover check off, no Image AI, the AI down). Returns (verdict, note,
    model called, the check to report as skipped (PlanItem.skipped) or "")."""
    if not (ctx.cover_check or ctx.always_cover):
        return SKIPPED, "", False, ""
    pa, pb = _cover_path(a), _cover_path(b)
    present = [(book, path) for book, path in ((a, pa), (b, pb)) if book.has_cover and path.is_file()]
    generic = [(book, path) for book, path in present if str(path) in ctx.generic]
    if len(present) == 2 and not generic and _identical(pa, pb):
        ctx.stats["cover_identical"] += 1
        return SAME, "identical cover files", False, ""
    resolver = ctx.resolver
    if resolver is None or resolver.vision is None:
        return SKIPPED, f"cover check skipped: {'AI is off' if resolver is None else 'no Image AI'}", False, SKIP_COVER
    if resolver.image_disabled_reason:
        return SKIPPED, resolver.image_disabled_reason, False, SKIP_IMAGE_AI
    if len(present) < 2:
        missing = a if not present or present[0][0] is not a else b
        return UNCLEAR, f"no cover on {missing.label()!r}", False, ""
    if generic:
        book, path = generic[0]
        return UNCLEAR, f"{generic_note(ctx.generic[str(path)])} on {book.label()!r}: not a real cover", False, ""
    verdict, note, called = resolver.ai_cover_verdict(a, b)
    return verdict, note, called, SKIP_IMAGE_AI if verdict == SKIPPED and resolver.image_disabled_reason else ""


def _other_language(ctx: _Context, a: Book, b: Book) -> str:
    """How `b`'s text is in another language than `a`'s, or "" (the same, or unknown)."""
    if ctx.texts is None:
        return ""
    mine, theirs = ctx.texts.get(a)[0], ctx.texts.get(b)[0]
    if mine and theirs and mine != theirs:
        return f"in another language ({language_name(theirs)} text, this one {language_name(mine)})"
    return ""


def _longer(ctx: _Context, a: Book, b: Book) -> str:
    """How one of the books is LENGTH_RATIO times longer than the other: another content
    (a collection and one of its stories, a complete and an abridged edition). "" if not,
    or unknown."""
    if ctx.texts is None:
        return ""
    la, lb = ctx.texts.get(a)[1], ctx.texts.get(b)[1]
    if not la or not lb or max(la, lb) < LENGTH_RATIO * min(la, lb):
        return ""
    return f"{(a if la > lb else b).label()!r} is {max(la, lb) / min(la, lb):.1f} times longer: another content"


def check_libraries(source: str, target: str, trash: str) -> None:
    paths = [source, target, trash]
    if any(not p for p in paths):
        raise ValueError("Select a source, a target and a trash library.")
    resolved = [str(Path(p).resolve()).casefold() for p in paths]
    if resolved[0] == resolved[2] or resolved[1] == resolved[2]:
        raise ValueError("The trash library must be different from the source and target libraries.")
    if not Path(source, "metadata.db").is_file():
        raise ValueError(f"The source is not a Calibre library: {source}")
    for p in (target, trash):
        folder = Path(p)
        if folder.is_dir() and not (folder / "metadata.db").is_file() and any(folder.iterdir()):
            raise ValueError(f"{p} is neither a Calibre library nor an empty folder.")
        if len(str(folder.resolve())) >= MAX_LIBRARY_PATH:
            raise ValueError(f"Calibre requires library paths shorter than {MAX_LIBRARY_PATH} characters: {p}")


def build_plan(
    source: str, target: str, trash: str,
    resolver: AIResolver | None = None,
    ignore_subtitle: bool = False,
    progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
    *,
    similar_matching: bool = False,
    cover_check: bool = False,
    recheck_years: bool = False,
    same_series: bool = False,
    similar_titles: bool = False,
    always_cover: bool = False,
    author_variants: bool = False,
    fix_swapped: bool = False,
    trash_unreadable: bool = False,
    cleanup_only: bool = False,
    tag: str = "",
    tag_exclude: bool = False,
    on_item: Callable[[PlanItem], None] | None = None,
    unpack: Callable[[int], bool] | None = None,
    generic_check: bool = True,
    library_cache: Path | None = None,
) -> Plan:
    """`on_item` is called with each book as soon as it is decided.
    Books with formats Calibre can't open (PlanItem.bad_formats) are always flagged;
    `trash_unreadable` also ticks them: all formats bad, the book goes to the trash
    library; some, its record is copied there and those formats leave the source.
    `cleanup_only`: nothing is copied to the target; only the source books already in
    it go to the trash library (see _cleanup_only). Ignored when source is target.
    `similar_titles`: with no book of the same title, also look at books by the
    same author whose title contains this one's, or the other way round (see
    _decide_similar). `always_cover`: compare covers also when the metadata says
    the books differ; the same cover (inside the files too) makes a duplicate.
    `author_variants`: with no candidate, books of the same title whose authors
    are the same person written differently (see _author_variant_candidates).
    `fix_swapped`: books whose title and author are swapped (see SwapDetector) are
    analyzed, and matched, with them put right (PlanItem.swapped).
    `unpack(n)`: asked once, before the first book, whether to unpack the clear archives
    (RAR, ZIP, 7Z) of the n books that have one (see archives.ask_once); None: archives
    are left as they are. Needs the AI's resolver (its extractor).
    `generic_check`: look for generic covers (see covers.generic_covers), which are then
    never proof; off (for tests on a big library), any cover can be. `library_cache`:
    where what is found in the books' files (covers, unreadable formats, hashes, the
    language and length of the text) is kept for the next time (see library_cache);
    None: not kept.
    `tag`: only the source books with this tag (with `tag_exclude`, without it) get an
    item; the others are still matched against (in one library, as copies that stay:
    see _keep_chosen). The target is always read whole.
    In every mode the source books are analyzed best copy first (keep_rank): of two
    copies, the first decided is the one moved or kept, the other is trashed into it."""
    check_libraries(source, target, trash)
    progress = progress or (lambda *_: None)
    tag = tag.strip()
    tag_exclude = tag_exclude and bool(tag)
    source_books = read_books(source)
    chosen = [b for b in source_books if tag_selects(b, tag, tag_exclude)]
    unpack = ask_once(chosen, unpack) if resolver is not None else None
    same_library = str(Path(source).resolve()).casefold() == str(Path(target).resolve()).casefold()
    target_books = read_books(target) if Path(target, "metadata.db").is_file() else []
    plan = Plan(source, target, trash, total_books=len(chosen), same_library=same_library,
                tag=tag, tag_exclude=tag_exclude)
    if tag:
        log.info("Only the %d of %d source books %s are analyzed", len(chosen), len(source_books),
                 tag_filter_text(tag, tag_exclude))
    file_checks = FileChecks(library_cache)
    hashes = FileHashes(library_cache)
    extractor = getattr(resolver, "extractor", None)
    # The books' text is read (for its language and length) only by the AI's extractor:
    # with the AI off, the analysis reads the metadata only.
    texts = TextFacts(library_cache, extractor) if hasattr(extractor, "text_profile") else None
    generic: dict[str, int] = {}
    if cover_check or always_cover:
        if generic_check:
            progress(0, len(chosen), "Looking for generic covers…")
            generic = generic_covers(source_books + target_books, cancel, library_cache)
        else:
            log.warning("Generic covers not looked for (setting): a placeholder cover may count as proof")
    if resolver is not None:
        resolver.generic = generic

    index: dict[tuple, list[_Candidate]] = defaultdict(list)
    files = _FileIndex(hashes)
    # Books that stay as they are, with a file name for title (see _Context.named).
    named: dict[tuple, list[_Candidate]] = defaultdict(list)
    # For similar titles: books by author key, and the collections' words.
    similar = _SimilarIndex(defaultdict(list), _collection_words(source_books + target_books)) \
        if similar_titles else None
    by_title: dict[str, list[_Candidate]] | None = defaultdict(list) if author_variants else None
    swaps = SwapDetector(source_books + target_books) if fix_swapped or author_variants else None

    def put_right(b: Book) -> Book | None:
        fixed = swaps.swap(b) if fix_swapped and swaps is not None else None
        if fixed is not None:
            log.info("%s: %s, analyzed as %s", b.label(), SWAPPED_NOTE, fixed.label())
        return fixed

    def add_titles(c: _Candidate) -> None:
        for k in _keys(c.identity, ignore_subtitle, similar_matching):
            index[k].append(c)

    def add_to_index(c: _Candidate) -> None:
        add_titles(c)
        if c.identity.series is not None:
            index[_series_key(c.identity)].append(c)
        if similar is not None and c.identity.title:
            for k in _author_keys(c.identity, similar_matching):
                similar.by_author[k].append(c)
        if by_title is not None and c.identity.title and c.identity.authors:
            by_title[title_key(c.identity.title, ignore_subtitle)].append(c)
        files.add(c)

    def add_staying(c: _Candidate) -> None:
        """A book that is never analyzed itself (the target's, or left out by the tag filter)."""
        add_to_index(c)
        if c.identity.title and looks_like_file_name(c.identity.title):
            for k in _author_keys(c.identity, similar_matching):
                named[k].append(c)

    if not same_library:
        for tb in target_books:
            if not tb.formats:
                continue  # an empty record (no file) is no copy of a book
            tb = put_right(tb) or tb
            add_staying(_Candidate(tb, metadata_identity(tb, same_series), planned=False))

    total = len(chosen)
    cleanup = cleanup_only and not same_library
    unreadable: dict[int, dict[str, str]] = {}  # id(book) -> its formats Calibre can't open, once checked

    def keep_rank(b: Book) -> tuple:
        """Of two copies, the one to keep: the best format (EPUB, MOBI, AZW), then the richer metadata.
        Before its turn, a book's files are not opened: only the formats Calibre can't read count."""
        bad = unreadable[id(b)] if id(b) in unreadable else {
            f: "" for f in b.formats if f not in CALIBRE_INPUT_FORMATS}
        return -_format_rank(b, bad), _metadata_richness(b)

    # Of two copies, the one decided first is moved or kept. A fake file found only in its
    # turn can make a copy come first that shouldn't: see the check after _plan_one.
    analysis_books = sorted(chosen, key=keep_rank, reverse=True)
    # The source copy each book indexed in one library stands for: id(indexed book) -> record.
    records: dict[int, tuple[_Candidate, Book]] = {}
    # One library, with a tag filter: the books it leaves out are copies that stay, matched
    # as they are (no AI reads them unless a chosen book needs it). candidate book -> record
    others: dict[int, tuple[_Candidate, Book]] = {}
    if same_library and tag:
        for ob in source_books:
            bad = {f: "" for f in ob.formats if f not in CALIBRE_INPUT_FORMATS}  # not opened: never analyzed
            if tag_selects(ob, tag, tag_exclude) or not ob.formats or (bad and set(bad) >= set(ob.formats)):
                continue  # an unreadable or empty book is no copy to keep
            book = replace(ob, formats={f: p for f, p in ob.formats.items() if f not in bad}) if bad else ob
            book = put_right(book) or book
            c = _Candidate(book, metadata_identity(book, same_series), planned=False)
            add_staying(c)
            others[id(book)] = (c, ob)
    libraries = [Path(source, "metadata.db")] + ([Path(target, "metadata.db")] if target_books else [])
    checked = time.monotonic()

    def stop(reason: str) -> None:
        plan.stopped, plan.stop_reason = True, reason
        log.error("Analysis stopped: %s", reason)

    stats: Counter[str] = Counter()
    down = {"text AI": 0, "image AI": 0}  # the book at which that AI was turned off
    ctx = _Context(index, resolver, stats, ignore_subtitle=ignore_subtitle, same_library=same_library,
                   similar_matching=similar_matching, cover_check=cover_check, recheck_years=recheck_years,
                   same_series=same_series, always_cover=always_cover, similar=similar, by_title=by_title,
                   persons=swaps, generic=generic, files=files, texts=texts, named=named)

    perf.run_start("dedup", total, getattr(resolver, "provider", None), getattr(resolver, "vision", None))
    for n, sb in enumerate(analysis_books, 1):
        if cancel is not None and cancel.is_set():
            plan.stopped = True  # keep what was analyzed so far
            break
        perf.book(n)
        progress(n - 1, total, f"Analyzing {sb.label()}")
        book = sb  # as the analysis sees it: without the formats Calibre can't open
        seen = sb  # with the files of an archive the user unpacks, instead of the archive
        archives: list = []
        kept = False  # cleanup: a book not in the target, kept in the source (its copies are trashed)
        no_file = False  # no file of the book Calibre can open
        try:
            if unpack is not None:
                seen, archives = unpack_archives(sb, resolver.extractor, unpack)
            bad = file_checks.bad(sb) if seen is sb else unreadable_formats(seen.formats)
            unreadable[id(sb)] = bad
            if not sb.formats:  # an empty record: nothing to read, nothing to keep
                no_file = True
                item = _empty_item(sb, same_series)
                stats["empty"] += 1
            elif bad and set(bad) >= set(seen.formats):
                no_file = True
                item = _unreadable_item(sb, bad, trash_unreadable, same_series)
            else:
                # Decided on the formats Calibre can open: a fake PDF is never merged into a match.
                book = replace(seen, formats={f: p for f, p in seen.formats.items() if f not in bad}) if bad else seen
                fixed = put_right(book)
                book = fixed or book
                item = _plan_one(book, ctx)
                item.source = sb  # the record itself (the executor acts on it)
                if fixed is not None:
                    stats["swapped"] += 1
                    swap_note = f"{SWAPPED_NOTE} (was {sb.title!r} by {' & '.join(sb.authors)!r})"
                    if item.action is Action.MOVE:  # trashed if proven a copy, never moved with a wrong record
                        item = _hold(item, swap_note, stats)
                    else:
                        item.reason = item.planned_reason = _join(item.reason, [swap_note])
                    item.swapped = True
                if hasattr(extractor, "failed_formats"):  # files that failed to open while deciding
                    bad.update(extractor.failed_formats(book.formats))
                if bad and set(bad) >= set(seen.formats):
                    no_file = True
                    item = _unreadable_item(sb, bad, trash_unreadable, same_series)
                elif bad:
                    item.bad_formats = bad
                    item.reason = item.planned_reason = _join(item.reason, [_bad_note(bad)])
            if archives:
                item.archives = archives
                item.reason = item.planned_reason = _join(item.reason, [u.note for u in archives])
            if cleanup:
                kept = _cleanup_only(item)
            _review_files(item, trash_unreadable)
        except OSError as e:
            # A missing or locked file is this book's problem; anything else (the
            # drive went away, I/O errors) would fail every book: stop, keep the rest.
            if not isinstance(e, (FileNotFoundError, NotADirectoryError, PermissionError)):
                stop(f"disk error on {sb.label()}: {e}")
                break
            if _unreachable(libraries):
                stop(f"library not reachable: {_unreachable(libraries)}")
                break
            log.warning("File error on %s: %s", sb.label(), e)
            item = PlanItem(sb, Action.LEAVE, f"file error: {e}", metadata_identity(sb))
        if item.action is Action.TRASH and item.match is not None and id(item.match) in others:
            c, record = others[id(item.match)]
            if keep_rank(sb) > keep_rank(record):
                item = _keep_chosen(item, c, f"the other is {tag_filter_text(tag, not tag_exclude)}")
        elif item.action is Action.TRASH and item.match is not None and id(item.match) in records:
            c, record = records[id(item.match)]
            if keep_rank(sb) > keep_rank(record):  # its files, opened in its turn, made it worse
                item = _keep_chosen(item, c, f"but the other's {', '.join(unreadable[id(record)])} "
                                                "can't be opened (found when it was analyzed)")
        # File errors are also caught (as "unreadable") deeper down: a vanished
        # drive would quietly leave books undecided. Check, at most once a second.
        if time.monotonic() - checked >= 1:
            gone = _unreachable(libraries)
            if gone:
                stop(f"library not reachable: {gone}")
                break
            checked = time.monotonic()
        plan.items.append(item)
        if resolver:
            for what, reason in (("text AI", resolver.disabled_reason), ("image AI", resolver.image_disabled_reason)):
                if reason and not down[what]:
                    down[what] = n
        if same_library:
            if item.action is not Action.TRASH and not no_file:  # a book left with no file to open is no copy to keep
                c = _Candidate(book, item.identity, planned=False, ai_done=item.ai_used)
                add_to_index(c)
                records[id(book)] = (c, sb)
        elif item.action is Action.MOVE or kept:
            add_to_index(_Candidate(book, item.identity, planned=True, ai_done=item.ai_used))
        # Only now: the user may change the item from here on (planner decisions
        # above use the analysis' own verdict, never the user's override).
        if on_item is not None:
            on_item(item)
        if resolver and n % 10 == 0:
            resolver.cache.save()
    perf.run_end(len(plan.items), plan.stopped)
    file_checks.save()  # those of the books analyzed one by one
    hashes.save()
    if texts is not None:
        texts.save()
    if resolver:
        resolver.cache.save()
        stats.update(resolver.stats)
    plan.stats = dict(stats)
    for what, n in down.items():
        if n:
            rest = (": the books after it were decided without it" if n < len(plan.items)
                    else "")
            plan.ai_down.append(f"The {what} stopped responding at book {n} of {total}{rest}.")
    progress(len(plan.items), total, "Analysis stopped" if plan.stopped else "Analysis complete")
    order = {id(b): i for i, b in enumerate(source_books)}
    plan.items.sort(key=lambda item: order[id(item.source)])
    return plan


SKIP_EXPLAINED = {
    SKIP_AI: "decided without the text AI (it stopped responding)",
    SKIP_IMAGE_AI: "decided without the image AI (it stopped responding)",
    SKIP_COVER: "cover check skipped (no Image AI, or AI off)",
    SKIP_YEARS: "year re-check skipped (AI off)",
    SKIP_SCANNED: "scanned PDF not read (no Image AI)",
}


def run_summary(plan: Plan) -> tuple[str, list[str]]:
    """What the analysis did, and what it could not do: (one line, warnings)."""
    s = plan.stats
    parts = []
    if s.get("read") or s.get("read_cached"):
        parts.append(f"{s.get('read', 0)} AI page reads ({s.get('read_cached', 0)} cached)")
    covers = s.get("cover", 0) + s.get("cover_cached", 0) + s.get("cover_identical", 0)
    if covers:
        parts.append(f"{covers} cover comparisons ({s.get('cover', 0)} by AI, {s.get('cover_cached', 0)} cached, "
                     f"{s.get('cover_identical', 0)} identical files)")
    inside = s.get("embedded", 0) + s.get("embedded_cached", 0) + s.get("embedded_identical", 0)
    if inside:
        parts.append(f"{inside} comparisons of covers inside the files ({s.get('embedded', 0)} by AI)")
    if s.get("author") or s.get("author_cached"):
        parts.append(f"{s.get('author', 0)} author names checked by AI ({s.get('author_cached', 0)} cached)")
    if s.get("year_rechecks"):
        parts.append(f"{s['year_rechecks']} year re-checks")
    if s.get("swapped"):
        parts.append(f"{s['swapped']} books with title and author swapped")
    if s.get("identical_files"):
        parts.append(f"{s['identical_files']} duplicates by identical files")
    if s.get("no_edition"):
        parts.append(f"{s['no_edition']} duplicates without edition data (covers not different)")
    if s.get("held"):
        parts.append(f"{s['held']} not moved: record to fix with the Metadata Review first")
    if s.get("empty"):
        parts.append(f"{s['empty']} empty records (no file)")
    reviews = sum(1 for it in plan.items if it.review)
    if reviews:
        parts.append(f"{reviews} to review before ticking")
    if s.get("other_language"):
        parts.append(f"{s['other_language']} copies in another language")
    counts = Counter(k for it in plan.items for k in it.skipped)
    warnings = list(plan.ai_down)
    warnings += [f"{n} book(s): {SKIP_EXPLAINED[k]}" for k, n in counts.items()]
    return " · ".join(parts) or "AI not used", warnings


UNREADABLE_REASON = "no file Calibre can open"
EMPTY_REASON = "no file: an empty record, nothing to keep"
CLEANUP = "cleanup only"
FIX_FIRST = "fix it with the Metadata Review first"
# What to check before ticking (PlanItem.review)
REVIEW_NO_EDITION = "no edition data: nothing tells the copies apart, compare them"
REVIEW_COVER = "a duplicate because of the covers: compare them"
REVIEW_PERSON = "authors matched {how}: check they are the same person"
REVIEW_UNREADABLE = "no file Calibre can open: check before it goes to the trash library"
REVIEW_UNOPENABLE = "{formats} open nowhere: taken out of the book on Execute"


def _to_review(item: PlanItem, why: str, untick: bool = True) -> None:
    """The analysis isn't sure: the user decides (see selection.needs_review). Unticked,
    nothing happens to the book."""
    item.review = f"{item.review}; {why}" if item.review else why
    if untick:
        item.selected = item.planned_selected = False


def _hold(item: PlanItem, why: str, stats: Counter) -> PlanItem:
    """A book the analysis would move, whose record isn't good enough for the target: it
    stays in the source until the Metadata Review fixes it."""
    stats["held"] += 1
    return _logged(replace(item, action=Action.LEAVE, review="", reason=f"not moved: {why}; {FIX_FIRST} "
                                                                          f"({item.reason})"))


def _review_files(item: PlanItem, trash_unreadable: bool) -> None:
    """Ticks for what Execute does to the book's files. Left in place, a book is ticked for
    its cleanup (an archive the user said to unpack, files that open nowhere). Files that
    open nowhere are taken out (never moved to the target) when the book is ticked; with
    "move files Calibre can't open without asking" off, it starts unticked, to review. A
    doubtful archive (archives.Unpack.doubt) is left packed, the book to review."""
    if item.unreadable:
        return
    if item.action is Action.LEAVE and has_cleanup(item):
        item.selected = item.planned_selected = True
    if item.unopenable and item.action is not Action.TRASH and not trash_unreadable:
        _to_review(item, REVIEW_UNOPENABLE.format(formats=", ".join(item.unopenable)))
    for u in item.archives:
        if u.doubt:
            _to_review(item, u.note, untick=False)


def _empty_item(sb: Book, same_series: bool) -> PlanItem:
    """A record with no file at all: nothing to keep, to the trash library (ticked)."""
    item = PlanItem(sb, Action.TRASH, EMPTY_REASON, metadata_identity(sb, same_series))
    log.info("%s: %s", sb.label(), item.reason)
    return item


def _keep_chosen(item: PlanItem, cand: _Candidate, why: str) -> PlanItem:
    """In one library, a duplicate that is the copy to keep (better format or metadata)
    after all, `why` the other came first: the tag filter left it out ("the other is not
    tagged 'New'"), or its best format turned out to be unreadable. Neither is trashed:
    left, with the match, so the user can still Trash one into the other."""
    cand.formats.difference_update(item.add_formats)  # nothing is added to it after all
    base, bracket, notes = item.reason.partition(" [")
    reason = (f"{base.split('; adding ')[0]}; this copy is the one to keep (better format or "
              f"metadata), {why}: neither is handled{bracket}{notes}")
    return _logged(replace(item, action=Action.LEAVE, add_formats=[], reason=reason, review=""))


def _cleanup_only(item: PlanItem) -> bool:
    """Cleanup of the source: the target is never written. A book not in the target
    stays in the source (returns True: it is the copy kept); a copy of such a book
    goes to the trash library, the kept one stays. A duplicate whose kept copy lacks
    some of its formats (trashing it would drop them from both libraries) stays too:
    the user decides, e.g. right-click Merge & Trash. The other duplicates go to the
    trash library as usual."""
    kept = False
    if item.action is Action.TRASH and item.match_planned and item.match is not None:
        item.match_planned, item.match_in_source = False, True  # nothing is moved: it stays
        if not item.add_formats:
            item.reason = item.planned_reason = (f"{CLEANUP}: another copy (#{item.match.id}) stays "
                                                 f"in the source ({item.reason})")
            return False
        why = f"another copy (#{item.match.id}) stays in the source, but it lacks {', '.join(item.add_formats)}"
    elif item.action is Action.MOVE:
        why, kept = "not in the target", True
    elif item.action is Action.TRASH and item.add_formats:
        why = f"in the target, but its copy lacks {', '.join(item.add_formats)}"
    else:
        return False
    item.action = item.planned_action = Action.LEAVE
    item.add_formats, item.planned_add_formats = [], []
    item.reason = item.planned_reason = f"{CLEANUP}: {why}, left in source ({item.reason})"
    item.selected = item.planned_selected = False
    item.review = ""
    return kept


def _bad_note(bad: dict[str, str]) -> str:
    return "unreadable: " + "; ".join(bad.values())


def _unreadable_item(sb: Book, bad: dict[str, str], selected: bool, same_series: bool) -> PlanItem:
    """Every format of the book is one Calibre can't open. None opens anywhere (empty, or
    not what its format says): nothing to keep, to the trash library (ticked only with the
    setting on; the user decides otherwise). One another program may open (a DOC, a file
    only Calibre's converter fails on): it stays in the source."""
    reason = f"{UNREADABLE_REASON} ({'; '.join(bad.values())})"
    if any(openable_elsewhere(why) for why in bad.values()):
        item = PlanItem(sb, Action.LEAVE, f"{reason}: kept, another program may open it",
                        metadata_identity(sb, same_series), bad_formats=bad)
    else:
        item = PlanItem(sb, Action.TRASH, reason, metadata_identity(sb, same_series), bad_formats=bad)
        if not selected:
            _to_review(item, REVIEW_UNREADABLE)
    log.info("%s: %s", sb.label(), item.reason)
    return item


# Which copy of a book is kept when there are several: the one with the best of these formats.
KEEP_FORMAT_ORDER = [("EPUB",), ("MOBI",), ("AZW", "AZW3")]


def _format_rank(book: Book, bad: dict[str, str]) -> int:
    """0 for a book with an EPUB Calibre can open, 1 with a MOBI, 2 with an AZW, 3 otherwise."""
    good = set(book.formats) - set(bad)
    return next((rank for rank, fmts in enumerate(KEEP_FORMAT_ORDER) if good & set(fmts)), len(KEEP_FORMAT_ORDER))


def _metadata_richness(book: Book) -> int:
    return (
        len(book.formats) +
        int(book.has_cover) +
        int(bool(book.comments and book.comments.strip())) +
        int(bool(book.publisher)) +
        int(book.pub_year is not None) +
        len(book.isbns)
    )


def _plan_one(sb: Book, ctx: _Context) -> PlanItem:
    """A book whose title is a file name is matched as it is (an identical file, the same
    file name), but never moved. Nor is one that may be a target book by the same author
    whose title is a file name: the Metadata Review fixes such titles first."""
    skipped: list[str] = []
    item = _decide_one(sb, ctx, skipped)
    file_name = not is_unknown(sb.title) and looks_like_file_name(sb.title)
    if item.action is Action.MOVE and file_name:
        item = _hold(item, "the title looks like a file name", ctx.stats)
    elif item.action is Action.MOVE:
        named = next((c for k in _author_keys(item.identity, ctx.similar_matching) for c in ctx.named.get(k, [])
                      if c.book is not sb), None)
        if named is not None:
            item = _hold(item, f"the target's {named.book.label()!r}, by the same author, has a file name for "
                               "title: it may be this book", ctx.stats)
            item.match, item.different = named.book, True
            item.reason = item.planned_reason = item.reason.replace(FIX_FIRST, "fix that title with the Metadata "
                                                                              "Review first")
    if file_name:
        item.file_name_title = sb.title
    item.skipped = list(dict.fromkeys(skipped))
    return item


def _decide_one(sb: Book, ctx: _Context, skipped: list[str]) -> PlanItem:
    """`skipped` collects the checks wanted for this book that could not run."""
    resolver = ctx.resolver
    ident = metadata_identity(sb, ctx.same_series)
    notes: list[str] = []
    ai_used = False

    def enrich(book: Book, i: Identity, need: Callable[[Identity], bool]) -> tuple[Identity, list[str]]:
        i, n = resolver.enrich(book, i, need)
        if resolver.disabled_reason and resolver.disabled_reason in n:
            skipped.append(SKIP_AI)
        if resolver.image_disabled_reason and resolver.image_disabled_reason in n:
            skipped.append(SKIP_IMAGE_AI)
        if any(x.endswith(SCANNED_PDF_SKIPPED) for x in n):
            skipped.append(SKIP_SCANNED)
        return i, n

    # 0. One of its files in another book: the same book, whatever their metadata say.
    found = ctx.files.match(sb) if ctx.files is not None else None
    if found is not None:
        c, fmt = found
        ctx.stats["identical_files"] += 1
        return _duplicate_item(sb, ident, c, f"identical {fmt} file", notes, ai_used)

    # 1. Same series and number, with the title or an author in common: proof, so
    # looked up first; a match decides the book and nothing else is checked (no AI
    # either). Unrelated books at the same number go through the other checks, and so
    # do copies in another language.
    if ident.series is not None:
        candidates = _lookup(ctx.index, [_series_key(ident)])
        clash = next((c for c in candidates if not _series_agrees(ident, c.identity)), None)
        candidates = [c for c in candidates if _series_agrees(ident, c.identity)]
        if clash is not None and not candidates:
            notes.append(f"same series and number as {clash.book.label()!r}, but another title "
                         "and authors: not taken as a duplicate")
        for c in list(candidates):
            other = _other_language(ctx, sb, c.book)
            if other:
                notes.append(f"same series and number as {c.book.label()!r}, but {other}")
                candidates.remove(c)
        if candidates:
            decision = decide(ident, [c.identity for c in candidates])
            return _duplicate_item(sb, ident, candidates[decision.match_index], decision.reason, notes, ai_used)

    # 2. Title and authors: the record's own (a title made from a file name is matched as
    # it is: see _plan_one). Without them the book can't be identified: it stays.
    if not ident.has_title_authors:
        ctx.stats["held"] += 1
        return PlanItem(sb, Action.LEAVE, f"not moved: title or authors missing; {FIX_FIRST}", ident)

    keys = _keys(ident, ctx.ignore_subtitle, ctx.similar_matching)
    if not keys:
        return PlanItem(sb, Action.LEAVE, "title/authors unusable after normalization", ident, ai_used=ai_used)
    candidates = _lookup(ctx.index, keys)
    weak: dict[int, str] = {}  # id(candidate) -> how its authors matched, when that's no proof
    if not candidates and ctx.by_title is not None:
        candidates, n, called, weak = _author_variant_candidates(sb, ident, ctx.by_title, resolver,
                                                                 ctx.ignore_subtitle, ctx.persons)
        notes += n
        ai_used = ai_used or called

    if not candidates:
        if ctx.similar is not None:
            alike = [(c, how) for c in _lookup(ctx.similar.by_author, _author_keys(ident, ctx.similar_matching))
                     if c.book is not sb and (how := _similar_title(sb, ident, c, ctx.similar.collections))]
            if alike:
                return _decide_similar(sb, ident, alike, ctx, notes, skipped, ai_used)
        action = Action.LEAVE if ctx.same_library else Action.MOVE
        reason = "no duplicate in this library" if ctx.same_library else "not in target"
        return PlanItem(sb, action, _join(reason, notes), ident, ai_used=ai_used)

    # 4. Same title/authors exist: compare them (evaluate). A copy whose text is in
    # another language is another book: the others are compared again without it.
    rechecked = False

    def evaluate(cands: list[_Candidate]) -> _Outcome:
        """Edition and publisher, by the metadata, else by what the AI reads in both
        books; the years printed in them; then the covers (see _by_covers)."""
        nonlocal ident, ai_used, rechecked
        decision = decide(ident, [c.identity for c in cands])
        if decision.verdict is Verdict.UNKNOWN:
            same = _same_text(sb, ident, cands)
            if same is not None:
                return _Outcome(same, content=True)
        if decision.verdict is Verdict.UNKNOWN and resolver:
            if _needs_edition_publisher(ident):
                ident, n = enrich(sb, ident, _needs_edition_publisher)
                notes.extend(n)
                ai_used = True
            for c in cands:
                if not c.ai_done and _needs_edition_publisher(c.identity):
                    c.identity, _ = enrich(c.book, c.identity, _needs_edition_publisher)
                    c.ai_done = True
                    ai_used = True
            decision = decide(ident, [c.identity for c in cands])

        # Different only by metadata year: Calibre's date is often the original
        # publication, so read both books and compare the years printed in them.
        unconfirmed: set[int] = set()
        if ctx.recheck_years and decision.verdict is not Verdict.DUPLICATE:
            weak = [c for c in cands if compare(ident, c.identity).year_only]
            if weak and not resolver:
                notes.append("year re-check skipped: AI is off")
                skipped.append(SKIP_YEARS)
            elif weak:
                if not rechecked:
                    ctx.stats["year_rechecks"] += 1
                    rechecked = True
                if _needs_ai_year_publisher(ident):
                    ident, n = enrich(sb, ident, _needs_ai_year_publisher)
                    notes.extend(n)
                    ai_used = True
                for c in weak:
                    if not c.year_rechecked and _needs_ai_year_publisher(c.identity):
                        c.identity, _ = enrich(c.book, c.identity, _needs_ai_year_publisher)
                        ai_used = True
                    c.year_rechecked = True
                decision = decide(ident, [c.identity for c in cands])
            unconfirmed = {id(c) for c in weak if compare(ident, c.identity).year_only}
        if decision.verdict is Verdict.DUPLICATE:
            return _Outcome(decision)
        outcome = _by_covers(ctx, sb, ident, cands, unconfirmed, decision, notes, skipped)
        ai_used = ai_used or outcome.called
        return outcome

    other_language: list[_Candidate] = []
    while True:
        outcome = evaluate(candidates)
        if outcome.decision.verdict is not Verdict.DUPLICATE:
            break
        c = candidates[outcome.decision.match_index]
        other = "" if outcome.content else _other_language(ctx, sb, c.book)
        if not other:
            return _duplicate_item(sb, ident, c, outcome.decision.reason, notes, ai_used, outcome.by_cover,
                                   outcome.override, outcome.no_edition, weak.get(id(c), ""))
        notes.append(f"{c.book.label()!r} is {other}: another book")
        ctx.stats["other_language"] += 1
        other_language.append(c)
        candidates = [x for x in candidates if x is not c]
        if not candidates:  # every copy is in another language
            reason = "no duplicate in this library" if ctx.same_library else "not in target"
            return _logged(PlanItem(sb, Action.LEAVE if ctx.same_library else Action.MOVE, _join(reason, notes),
                                    ident, match=other_language[0].book, match_planned=other_language[0].planned,
                                    different=True, ai_used=ai_used))

    decision = outcome.decision
    if decision.verdict is Verdict.DISTINCT:
        action = Action.LEAVE if ctx.same_library else Action.MOVE
        reason = (f"different from existing books: {decision.reason}" if ctx.same_library
                  else f"different from target copies: {decision.reason}")
        return _logged(PlanItem(sb, action, _join(reason, notes), ident, match=candidates[0].book,
                                match_planned=candidates[0].planned, different=True, ai_used=ai_used))
    return _logged(PlanItem(
        sb, Action.LEAVE,
        _join(f"same title/authors as {candidates[0].book.label()!r} but {decision.reason}", notes),
        ident, match=candidates[0].book, match_planned=candidates[0].planned, ai_used=ai_used))


# How the metadata left a pair open, for the covers to settle (see _by_covers).
_OPEN = "open"  # no edition data to compare
_INCOMPARABLE = "incomparable"  # edition data on both, that can't be compared
_WEAK = "weak"  # only Calibre's years differ, not confirmed by reading the books
_DIFFERENT = "different"  # the metadata differs ("always compare covers")


def _by_covers(ctx: _Context, sb: Book, ident: Identity, cands: list[_Candidate], unconfirmed: set[int],
               decision: Decision, notes: list[str], skipped: list[str]) -> _Outcome:
    """What the covers say about the books the metadata left open (README: How two books
    are compared). With no edition data to compare (on one copy or both), two books are
    the same unless their covers differ: a cover missing, not a real one, or the AI unsure
    changes nothing, but a copy LENGTH_RATIO times longer is another content. With
    edition data on both that can't be compared, only the same cover makes a duplicate.
    When only Calibre's years differ and reading the books didn't confirm it, the same
    cover makes a duplicate, another cover another edition; unclear, the book stays. With
    "always compare covers", the same cover also overrules a difference in the metadata.
    Covers that can't be checked here (cover check off, no Image AI, the AI down) say
    nothing: the book stays."""
    called = False
    open_pairs: list[tuple[int, _Candidate, str]] = []  # no edition data, covers not different
    stays: list[str] = []  # why the book can't be decided
    for i, c in enumerate(cands):
        comp = compare(ident, c.identity)
        if comp.verdict is Verdict.UNKNOWN:
            kind = _INCOMPARABLE if comp.incomparable else _OPEN
        elif comp.verdict is Verdict.DISTINCT and id(c) in unconfirmed:
            kind = _WEAK
        elif comp.verdict is Verdict.DISTINCT and ctx.always_cover:
            kind = _DIFFERENT
        else:
            continue
        verdict, note, asked, skip = _covers(ctx, sb, c.book)
        called = called or asked
        notes.append(note)
        if skip:
            skipped.append(skip)
        if verdict == SAME:
            override = comp.verdict is Verdict.DISTINCT
            why = f"same cover; metadata differs: {comp.reason}" if override else "same cover"
            return _Outcome(Decision(Verdict.DUPLICATE, why, i), by_cover=True, override=override, called=called)
        if kind == _OPEN and verdict == UNCLEAR:
            open_pairs.append((i, c, comp.reason))
        elif kind == _OPEN and verdict == DIFFERENT:
            stays.append(f"{comp.reason}, and another cover: another edition?")
        elif kind in (_OPEN, _INCOMPARABLE):
            stays.append(comp.reason)
        elif kind == _WEAK and verdict != DIFFERENT:
            stays.append(f"{comp.reason} in Calibre only, not confirmed by reading the books")
    for i, c, reason in open_pairs:
        longer = _longer(ctx, sb, c.book)
        if longer:
            stays.append(f"{reason}, but {longer}")
            continue
        ctx.stats["no_edition"] += 1
        return _Outcome(Decision(Verdict.DUPLICATE, f"{reason}: nothing tells them apart, and the covers "
                                                    "don't differ", i), no_edition=True, called=called)
    if stays:
        return _Outcome(Decision(Verdict.UNKNOWN, "; ".join(dict.fromkeys(stays))), called=called)
    return _Outcome(decision, called=called)


def _duplicate_item(sb: Book, ident: Identity, cand: _Candidate, why: str, notes: list[str], ai_used: bool,
                    by_cover: bool = False, override: bool = False, no_edition: bool = False,
                    person: str = "") -> PlanItem:
    """`sb` is a duplicate of `cand`: to trash, adding the formats the kept copy lacks.
    `override`: a cover overruling the metadata; the same book, but its files may be
    another edition's, so nothing is added to the other copy (Trash only).
    `no_edition`: see PlanItem.no_edition. `person`: how the authors matched, when that is
    no proof (the surname only, the AI). An unproven duplicate (no edition data, the
    covers, such authors) starts unticked: nothing happens to it, nor is anything added to
    the target copy, until the user checks and ticks it."""
    missing = [] if override else [f for f in sb.formats if f not in cand.formats and f != "PDF"]
    cand.formats.update(missing)  # later duplicates must not add the same format again
    reason = f"duplicate of {cand.book.label()!r} ({why})"
    if missing:
        reason += f"; adding {', '.join(missing)} to target copy"
    item = _logged(PlanItem(sb, Action.TRASH, _join(reason, notes), ident, match=cand.book,
                            match_planned=cand.planned, add_formats=missing, ai_used=ai_used,
                            by_cover=by_cover, no_edition=no_edition))
    for flag, why_review in ((no_edition, REVIEW_NO_EDITION), (by_cover, REVIEW_COVER),
                             (person, REVIEW_PERSON.format(how=person))):
        if flag:
            _to_review(item, why_review)
    return item


def _author_variant_candidates(sb: Book, ident: Identity, by_title: dict, resolver: AIResolver | None,
                               ignore_subtitle: bool, persons: SwapDetector | None = None,
                               ) -> tuple[list[_Candidate], list[str], bool, dict[int, str]]:
    """Books with the same title whose authors are the same person written
    differently: one letter apart in a name ("Frederickk Marryat" / "Frederick Marryat"),
    first names as initials ("E. Marshall" / "Marshall, Emma"), the surname alone
    ("Kingston" / "William Henry Giles Kingston"), or else the same person according to the text AI,
    told the shared title (typos, transliterations, pen names). They are then compared
    like any book with the same title and authors. The AI is not asked when the title is
    itself a person's name (`persons`): title and author are then swapped, and the
    "authors" it would compare are book titles. Returns (candidates, notes, model called,
    {id(candidate): how} for those matched only by the surname or the AI: no proof)."""
    found, notes, called, weak = [], [], False, {}
    mine = " & ".join(ident.authors)
    ask_ai = resolver is not None and (persons is None or persons.person(ident.title) is None)
    for c in by_title.get(title_key(ident.title, ignore_subtitle), []):
        if c.book is sb:
            continue
        theirs = " & ".join(c.identity.authors)
        if any(names_nearly_equal(a, b) for a in ident.authors for b in c.identity.authors):
            same, how = True, "one letter apart"
        elif any(initials_match(a, b) for a in ident.authors for b in c.identity.authors):
            same, how = True, "initials"
        elif any(surname_match(a, b) for a in ident.authors for b in c.identity.authors):
            same, how = True, "surname only"
            weak[id(c)] = "by the surname only"
        elif ask_ai:
            same, how, asked = resolver.same_person(sb, mine, theirs, ident.title)
            called = called or asked
            weak[id(c)] = "by the AI"
            if not same and how == resolver.disabled_reason:
                notes.append(how)
        else:
            if resolver is not None:
                notes.append(PERSON_TITLE_NOTE)
            continue
        if same:
            found.append(c)
            notes.append(f"same person: {mine!r} / {theirs!r} ({how})")
    return found, notes, called, weak


def _inside(ctx: _Context, a: Book, b: Book, notes: list[str], skipped: list[str]) -> tuple[str, bool]:
    """The covers stored inside both books' files (SAME, DIFFERENT or UNCLEAR): for similar
    titles whose metadata differ, Calibre's cover.jpg alone isn't trusted (it may be a
    downloaded picture). Returns (verdict, model called)."""
    resolver = ctx.resolver
    if resolver is None:
        notes.append("covers inside the files not compared: AI is off")
        return UNCLEAR, False
    same, note, called = resolver.same_embedded_cover(a, b)
    notes.append(note)
    if note and note == resolver.image_disabled_reason:
        skipped.append(SKIP_IMAGE_AI)
    if same:
        return SAME, called
    return (DIFFERENT if note.startswith("covers inside the files: different") else UNCLEAR), called


def _decide_similar(sb: Book, ident: Identity, similar: list[tuple[_Candidate, str]], ctx: _Context,
                    notes: list[str], skipped: list[str], ai_used: bool) -> PlanItem:
    """Books by the same author whose title contains this one's (or the other way
    round): "(Gutenberg - 0411- Brother Jacob - George Eliot)" and "Brother Jacob".
    Titles alike are weaker than the same title, so only proof makes a duplicate:
    the same ISBN, ASIN or series number, identical EPUB text, or the same cover.
    A real difference in metadata (not only the year), a different cover or a text in
    another language rules a book out, unless "always compare covers" finds the same
    cover (inside the files too). Anything else is left for the user to check."""
    unproven: list[tuple[_Candidate, str]] = []
    mine = _epub_digest(sb)
    for c, how in similar:
        comp = compare(ident, c.identity)
        proof = comp.reason if comp.proof else ""
        same_text = not proof and bool(mine) and _epub_digest(c.book) == mine
        if same_text:
            proof = "identical EPUB text"
        if not proof and comp.verdict is Verdict.DISTINCT and not comp.year_only and not ctx.always_cover:
            notes.append(f"similar title {c.book.label()!r} is another book: {comp.reason}")
            continue
        by_cover = False
        if not proof and (ctx.cover_check or ctx.always_cover) and sb.has_cover and c.book.has_cover:
            verdict, note, called, skip = _covers(ctx, sb, c.book)
            ai_used = ai_used or called
            notes.append(note)
            if skip:
                skipped.append(skip)
            if verdict == SAME and comp.verdict is Verdict.DISTINCT:
                verdict, called = _inside(ctx, sb, c.book, notes, skipped)
                ai_used = ai_used or called
            if verdict == SAME:
                proof, by_cover = "same cover", True
                if comp.verdict is Verdict.DISTINCT:
                    proof += f", also inside the files; metadata differs: {comp.reason}"
            elif verdict == DIFFERENT:
                notes.append(f"similar title {c.book.label()!r} has another cover")
                continue
        if not proof and comp.verdict is Verdict.DISTINCT and not comp.year_only:
            notes.append(f"similar title {c.book.label()!r} is another book: {comp.reason}")
            continue
        other = _other_language(ctx, sb, c.book) if proof and not same_text else ""
        if other:
            notes.append(f"similar title {c.book.label()!r} is {other}: another book")
            continue
        if proof:
            # Books whose metadata differ: nothing is added to the other copy (Trash only).
            missing = [] if comp.verdict is Verdict.DISTINCT else [
                f for f in sb.formats if f not in c.formats and f != "PDF"]
            c.formats.update(missing)
            reason = f"duplicate of {c.book.label()!r} (similar title: {how}; {proof})"
            if missing:
                reason += f"; adding {', '.join(missing)} to target copy"
            item = _logged(PlanItem(sb, Action.TRASH, _join(reason, notes), ident, match=c.book,
                                    match_planned=c.planned, add_formats=missing, ai_used=ai_used,
                                    by_cover=by_cover))
            if by_cover:
                _to_review(item, REVIEW_COVER)
            return item
        unproven.append((c, how))
    if unproven:
        c, how = unproven[0]
        return _logged(PlanItem(
            sb, Action.LEAVE,
            _join(f"similar title to {c.book.label()!r} ({how}), not proven the same book: check manually", notes),
            ident, match=c.book, match_planned=c.planned, ai_used=ai_used))
    action = Action.LEAVE if ctx.same_library else Action.MOVE
    reason = "no duplicate in this library" if ctx.same_library else "not in target"
    c = similar[0][0]
    return _logged(PlanItem(sb, action, _join(reason, notes), ident, match=c.book, match_planned=c.planned,
                            different=True, ai_used=ai_used))


def _logged(item: PlanItem) -> PlanItem:
    """Log the decision for a book that had candidates (books with none are the
    vast majority and would flood the log)."""
    log.info("Decision: %s -> %s: %s", item.source.label(), action_label(item), item.reason)
    return item


def _join(reason: str, notes: list[str]) -> str:
    notes = [n for n in dict.fromkeys(notes) if n]
    return reason + (f" [{'; '.join(notes)}]" if notes else "")
