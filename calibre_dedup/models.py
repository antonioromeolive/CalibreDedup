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
    # `match` is a different edition, not a duplicate: kept so the user can still
    # force Trash into it (and open it to compare).
    different: bool = False
    add_formats: list[str] = field(default_factory=list)  # formats to add to the target copy
    ai_used: bool = False
    # Checks that were wanted for this book but could not run (see planner.SKIP_*),
    # e.g. the cover check with no Image AI, or the AI turned off after errors.
    skipped: list[str] = field(default_factory=list)
    by_cover: bool = False  # a duplicate because the covers are the same
    status: str = ""  # filled during execution
    selected: bool = True  # user wants this item executed (meaningless for LEAVE)
    manual: bool = False  # action overridden by the user
    # The analysis' own decision, kept so a manual override can be reverted.
    planned_action: Action | None = None
    planned_reason: str = ""
    planned_add_formats: list[str] = field(default_factory=list)

    def __post_init__(self):
        self.planned_action = self.action
        self.planned_reason = self.reason
        self.planned_add_formats = list(self.add_formats)
        self.selected = self.action is not Action.LEAVE


@dataclass
class Plan:
    source_library: str
    target_library: str
    trash_library: str
    items: list[PlanItem] = field(default_factory=list)
    total_books: int = 0  # books in the source library
    stopped: bool = False  # analysis was stopped: only some source books have an item
    stop_reason: str = ""  # why it stopped by itself (e.g. a disk error); empty when the user stopped it
    same_library: bool = False  # duplicates within one library (source == target)
    # What the AI did (planner.AIResolver.stats) plus "year_rechecks", for the summary.
    stats: dict[str, int] = field(default_factory=dict)
    # An AI that stopped responding during the run, e.g. "text AI stopped at book 812 of 2066".
    ai_down: list[str] = field(default_factory=list)

    def count(self, action: Action) -> int:
        return sum(1 for i in self.items if i.action is action)
