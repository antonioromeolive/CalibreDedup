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

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


@dataclass
class Book:
    """A book as read from a Calibre library's metadata.db."""

    id: int
    title: str
    authors: list[str]
    publisher: str | None
    pub_year: int | None
    isbns: set[str]
    formats: dict[str, str]  # format (upper case) -> absolute file path
    uuid: str
    path: str  # relative folder inside the library
    library: str
    has_cover: bool = False
    comments: str | None = None
    asins: set[str] = field(default_factory=set)  # Amazon ids (mobi-asin, amazon*)
    series: str | None = None
    series_index: float | None = None  # only meaningful with a series
    tags: set[str] = field(default_factory=set)
    last_modified: str = ""  # Calibre's, changed with the metadata or the cover
    sizes: dict[str, int] = field(default_factory=dict)  # format -> file size, as metadata.db has it
    languages: list[str] = field(default_factory=list)  # Calibre's codes ("ita"), in its order

    def label(self) -> str:
        return f"{self.title} — {' & '.join(self.authors)}"


@dataclass
class Identity:
    """What we know about a book's bibliographic identity."""

    title: str | None = None
    authors: list[str] = field(default_factory=list)
    publisher: str | None = None
    edition: int | None = None  # edition number (1 = first edition)
    year: int | None = None  # publication year of this edition
    isbns: set[str] = field(default_factory=set)
    ai_fields: set[str] = field(default_factory=set)  # fields filled by AI
    # What the AI read in the book, kept even when metadata already has a value,
    # so that two books read by the AI can be compared on what is printed in them.
    ai_year: int | None = None
    ai_publisher: str | None = None
    asins: set[str] = field(default_factory=set)  # from metadata only
    # (normalized series name, number): set only with the "same series" option and
    # a real number (not Calibre's default 1). Same series and number = same book.
    series: tuple[str, float] | None = None

    def copy(self) -> "Identity":
        return Identity(
            self.title, list(self.authors), self.publisher, self.edition,
            self.year, set(self.isbns), set(self.ai_fields), self.ai_year, self.ai_publisher,
            set(self.asins), self.series,
        )

    @property
    def has_title_authors(self) -> bool:
        return bool(self.title) and bool(self.authors)

    @property
    def has_edition_info(self) -> bool:
        return self.edition is not None or self.year is not None


class Action(str, Enum):
    MOVE = "move"  # not in target: move source -> target
    TRASH = "trash"  # already in target: move source -> trash
    LEAVE = "leave"  # undecidable: leave in source


@dataclass
class PlanItem:
    source: Book
    action: Action
    reason: str
    identity: Identity
    match: Book | None = None  # the target copy (or a source book planned to move)
    match_planned: bool = False  # True if `match` is a source book being moved
    match_in_source: bool = False  # True if `match` is a source book that stays there (cleanup only)
    # `match` is a different edition, not a duplicate: kept so the user can still
    # force Trash into it (and open it to compare).
    different: bool = False
    add_formats: list[str] = field(default_factory=list)  # formats to add to the target copy
    ai_used: bool = False
    # Checks that were wanted for this book but could not run (see planner.SKIP_*),
    # e.g. the cover check with no Image AI, or the AI turned off after errors.
    skipped: list[str] = field(default_factory=list)
    by_cover: bool = False  # a duplicate because the covers are the same
    # A duplicate only because nothing tells the copies apart: no edition data to compare,
    # and the covers don't differ (one missing, not a real cover, or the AI unsure). Not proof.
    no_edition: bool = False
    # The record's title looked like a file name ("ITABOOK 0052 - Hemingway"): the book is
    # matched with the title the AI read in it (identity.title), if it could.
    file_name_title: str = ""
    # Title and author were swapped in the record: `identity` has them put right, and
    # Execute writes them (with "Write found metadata" on) to a moved or ticked book.
    swapped: bool = False
    # Formats Calibre can't open (format -> why). All of them: the item trashes the book.
    # Some: the book was decided on the others, and with `trash_bad` its whole record is
    # copied to the trash library as it is, then those formats are removed from the source.
    bad_formats: dict[str, str] = field(default_factory=dict)
    trash_bad: bool = False
    planned_trash_bad: bool = False  # the analysis' own choice (the setting)
    # The book's archives (RAR, ZIP, 7Z) as archives.Unpack: unpacked on Execute when
    # their `unpack` is on, ticked or not (like the unreadable formats).
    archives: list = field(default_factory=list)
    status: str = ""  # filled during execution
    selected: bool = True  # user wants this item executed (meaningless for LEAVE)
    manual: bool = False  # action overridden by the user
    # The analysis' own decision, kept so a manual override can be reverted.
    planned_action: Action | None = None
    planned_reason: str = ""
    planned_add_formats: list[str] = field(default_factory=list)
    planned_selected: bool = True

    def __post_init__(self):
        self.planned_action = self.action
        self.planned_reason = self.reason
        self.planned_add_formats = list(self.add_formats)
        self.selected = self.planned_selected = self.action is not Action.LEAVE

    @property
    def unreadable(self) -> bool:
        """Every format of the book is one Calibre can't open."""
        return bool(self.bad_formats) and set(self.bad_formats) >= set(self.source.formats)

    @property
    def bad_formats_to_trash(self) -> list[str]:
        """The formats to take out of the source on Execute (some formats bad, box ticked)."""
        return sorted(self.bad_formats) if self.trash_bad and not self.unreadable else []

    @property
    def archives_to_unpack(self) -> list:
        return [u for u in self.archives if u.unpack and not u.problem]


@dataclass
class Plan:
    source_library: str
    target_library: str
    trash_library: str
    items: list[PlanItem] = field(default_factory=list)
    total_books: int = 0  # source books to analyze (only those with `tag`, if given)
    tag: str = ""  # only the source books with this tag were analyzed; "" = all
    tag_exclude: bool = False  # ... without this tag, instead
    stopped: bool = False  # analysis was stopped: only some source books have an item
    stop_reason: str = ""  # why it stopped by itself (e.g. a disk error); empty when the user stopped it
    same_library: bool = False  # duplicates within one library (source == target)
    # What the AI did (planner.AIResolver.stats) plus "year_rechecks", for the summary.
    stats: dict[str, int] = field(default_factory=dict)
    # An AI that stopped responding during the run, e.g. "text AI stopped at book 812 of 1500".
    ai_down: list[str] = field(default_factory=list)

    def count(self, action: Action) -> int:
        return sum(1 for i in self.items if i.action is action)
