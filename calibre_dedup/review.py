"""calibre-review: read every book of a library with the AI (its first pages and
its cover) and propose corrections to title, authors, publisher, year and series.

Nothing is written during the scan. The user then chooses, per book, to update
its metadata, keep it as it is, or move it to a trash library; the bridge
(bridge_script.py, op "set" and "trash") does the writing inside Calibre.
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
import threading
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Callable

from .ai import AICache, AIError, ReviewMetadata, read_book_metadata
from .executor import ExecutionError, run_bridge
from .calibre_env import calibre_is_running
from .config import config_dir
from .extract import cover_png
from .library import LibraryError, read_books
from .models import Book
from .normalize import authors_key, is_unknown, same_publisher, series_key, strip_accents
from .planner import MAX_LIBRARY_PATH, AIResolver

log = logging.getLogger(__name__)

FIELDS = ["title", "authors", "publisher", "year", "series"]  # "series" includes its number
FIELD_LABELS = {"title": "Title", "authors": "Authors", "publisher": "Publisher", "year": "Year",
                "series": "Series"}
CACHE_VERSION = "review-v1"  # change when the prompt changes, so old answers are not reused
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
    excluded: set[str] = field(default_factory=set)  # fields the user doesn't want changed
    action: ReviewAction = ReviewAction.KEEP
    selected: bool = False
    manual: bool = False  # action chosen by the user
    status: str = ""  # filled during execution

    def __post_init__(self):
        if self.found is not None and not self.changes:
            self.changes = find_changes(self.book, self.found)
        if self.changes and self.action is ReviewAction.KEEP and not self.manual:
            self.action, self.selected = ReviewAction.UPDATE, True
        elif not self.book.formats and self.action is ReviewAction.KEEP and not self.manual:
            self.action, self.selected = ReviewAction.TRASH, True  # a record with no file: nothing to keep

    def to_write(self, fields_on: set[str] | list[str]) -> dict:
        """The changes that will be written: not excluded, and the field is on."""
        return {k: v for k, v in self.changes.items() if k in fields_on and k not in self.excluded}

    def set_action(self, action: ReviewAction) -> None:
        self.action, self.manual = action, True
        self.selected = action is not ReviewAction.KEEP

    def book_changed(self, book: Book) -> None:
        """The book was updated in the library: compare again with what the AI read."""
        self.book = book
        self.changes = find_changes(book, self.found) if self.found is not None else {}
        self.excluded &= set(self.changes)
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
    raise KeyError(name)


def find_changes(book: Book, found: ReviewMetadata) -> dict:
    """The fields where the AI read something different from the metadata. A field
    the AI did not find is never a change: nothing is ever erased."""
    changes: dict = {}
    if found.title and _loose(found.title) != _loose(current_value(book, "title")):
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
    return changes


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


def review_cache() -> AICache:
    """calibre-review's own AI cache, so that it can run with the duplicate remover
    (each writes its whole cache back). The first time it starts as a copy of the
    shared ai_cache.json, which holds the answers of reviews made before the split."""
    path = config_dir() / REVIEW_CACHE_FILE
    shared = config_dir() / "ai_cache.json"
    if not path.exists() and shared.is_file():
        try:
            shutil.copyfile(shared, path)
            log.info("Review AI cache created from %s", shared)
        except OSError as e:
            log.warning("Could not copy %s to %s: %s", shared, path, e)
    return AICache(path)


def is_reviewed(book: Book) -> bool:
    return REVIEWED_TAG.casefold() in {t.casefold() for t in book.tags}


def book_cover_file(book: Book) -> Path | None:
    path = Path(book.library, book.path, "cover.jpg")
    return path if path.is_file() else None


class Reviewer(AIResolver):
    """Reads a book's first pages, and its cover when there is an Image AI. With an
    Image AI, every book is sent to it (it reads text too), else to the text AI.
    Errors and "AI not responding" are handled as in the analysis (AIResolver)."""

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
        cached = self.cache.get(key)
        if cached is not None:
            self.stats["read_cached"] += 1
            return ReviewMetadata(**cached), _join(f"{fmt} first pages" + (" + cover" if cover else "")
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
            meta, read = self._read(book, provider, excerpt.text, images, bool(cover), read)
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
        self.cache.put(key, meta.to_dict())
        if self.stats["read"] % SAVE_CACHE_EVERY == 0:
            self.cache.save()
        return meta, _join(f"AI read {read}", cover_note)

    def _read(self, book: Book, provider, text: str, images: list[str], has_cover: bool,
              read: str) -> tuple[ReviewMetadata, str]:
        """Ask the AI; when its content filter refuses the pages (a violent novel),
        ask again with less: the first FILTERED_RETRY_CHARS characters (the title
        page is at the start), then the cover alone. Returns (metadata, what was read)."""
        attempts = [(text, images, read)]
        if len(text) > FILTERED_RETRY_CHARS:
            attempts.append((text[:FILTERED_RETRY_CHARS], images,
                             f"{read} (first {FILTERED_RETRY_CHARS} chars: content filter)"))
        if has_cover and (text.strip() or len(images) > 1):
            attempts.append(("", images[:1], "cover only (content filter)"))
        for i, (t, imgs, what) in enumerate(attempts):
            try:
                return read_book_metadata(provider, t, imgs, has_cover=has_cover), what
            except AIError as e:
                if not e.filtered or i == len(attempts) - 1:
                    raise
                log.info("AI content filter refused %s: asking again with %s", book.label(), attempts[i + 1][2])
        raise AssertionError("unreachable")

    def _cover(self, book: Book) -> tuple[str | None, str]:
        """The cover as a PNG for the AI: Calibre's cover.jpg, else the one inside the file."""
        data: bytes | None = None
        cover_file = book_cover_file(book)
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
    skipped: int = 0  # books already tagged REVIEWED_TAG, not read again
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
                 skip_reviewed: bool = True) -> ReviewResult:
    """`skip_reviewed`: leave out the books tagged REVIEWED_TAG (reviewed on an earlier day)."""
    check_libraries(library, trash)
    books = read_books(library)
    skipped = sum(1 for b in books if is_reviewed(b)) if skip_reviewed else 0
    if skipped:
        books = [b for b in books if not is_reviewed(b)]
    result = ReviewResult(library, trash, total_books=len(books), skipped=skipped)
    log.info("Reviewing %d books of %s%s", len(books), library,
             f" ({skipped} tagged {REVIEWED_TAG} skipped)" if skipped else "")
    for n, book in enumerate(books):
        if cancel is not None and cancel.is_set():
            result.stopped = True
            log.info("Review stopped after %d of %d books", n, len(books))
            break
        if progress:
            progress(n, len(books), f"Reading {book.label()}")
        meta, note = reviewer.review(book)
        item = ReviewItem(book, meta, note)
        if item.changes:
            log.info("%s: %s", book.label(), ", ".join(
                f"{k} {format_value(k, current_value(book, k))!r} -> {format_value(k, v)!r}"
                for k, v in item.changes.items()))
        result.items.append(item)
        if on_item:
            on_item(item)
    if progress:
        progress(len(result.items), len(books), "Done")
    result.stats = dict(reviewer.stats)
    result.ai_down = [r for r in (reviewer.disabled_reason, reviewer.image_disabled_reason) if r]
    return result


def summary(result: ReviewResult) -> str:
    read, cached = result.stats.get("read", 0), result.stats.get("read_cached", 0)
    failed = sum(1 for i in result.items if i.found is None)
    changed = sum(1 for i in result.items if i.changes)
    head = (f"Stopped after {len(result.items)} of {result.total_books} books" if result.stopped
            else f"{len(result.items)} books reviewed")
    skipped = f" · {result.skipped} already {REVIEWED_TAG}, skipped" if result.skipped else ""
    return (f"{head}: {changed} with differences, {failed} not read{skipped} · AI: {read} read, "
            f"{cached} from cache" + "".join(f" · {r}" for r in result.ai_down))


# --- executing ---------------------------------------------------------------------------
def review_actions(items: list[ReviewItem], fields_on: set[str] | list[str]) -> list[dict]:
    """Bridge actions: "set" for the checked updates, "trash" (no target) for the
    checked trash, and one "tag" with every other reviewed book: updated or not,
    each book of the scan is tagged REVIEWED_TAG ("set" tags its book too). Not the
    books the AI couldn't read (unless the user chose Keep): the next scan tries
    them again."""
    actions = []
    tag_ids = []
    for it in items:
        base = {"src_id": it.book.id, "title": it.book.title}
        if it.selected and it.action is ReviewAction.TRASH:
            actions.append({**base, "op": "trash", "no_target": True})
            continue
        changes = it.to_write(fields_on) if it.selected and it.action is ReviewAction.UPDATE else {}
        if not changes:
            if (it.found is not None or it.manual) and not is_reviewed(it.book):
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
        actions.append({**base, "op": "set", "set": values, "tag": REVIEWED_TAG})
    if tag_ids:
        actions.append({"op": "tag", "src_ids": tag_ids, "tag": REVIEWED_TAG})
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
    if path:
        out.path = path
    if formats:
        out.formats = {fmt.upper(): p for fmt, p in formats.items() if p}
    return out


def execute_review(result: ReviewResult, fields_on: set[str] | list[str], calibre_dir: Path,
                   permanent: bool = False,
                   on_result: Callable[[ReviewItem, bool, str], None] | None = None,
                   cancel: threading.Event | None = None) -> tuple[int, int, int]:
    """Write the checked updates, move the checked books to the trash library, and
    tag the reviewed books REVIEWED_TAG (see review_actions). Returns (succeeded,
    failed, tagged with nothing written)."""
    if calibre_is_running():
        raise ExecutionError("Calibre is running. Close Calibre (and calibre-server) before executing.")
    actions = review_actions(result.items, fields_on)
    if not actions:
        return 0, 0, 0
    if any(a["op"] == "trash" for a in actions) and not result.trash_library:
        raise ExecutionError("Choose a trash library to move books to it.")
    items = {i.book.id: i for i in result.items}
    sent = {a["src_id"]: a for a in actions if "src_id" in a}
    ok = failed = tagged = 0

    def on_message(msg: dict) -> None:
        nonlocal ok, failed, tagged
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
                if on_result:
                    on_result(item, msg["ok"], msg["msg"])
            return
        if msg["event"] != "result":
            return
        item = items[msg["src_id"]]
        item.status = ("OK: " if msg["ok"] else "FAILED: ") + msg["msg"]
        if msg["ok"]:
            ok += 1
            action = sent[item.book.id]
            if action["op"] == "set":
                book = apply_to_book(item.book, action["set"], msg.get("path"), msg.get("formats"))
                book.tags = set(book.tags) | ({action["tag"]} if action.get("tag") else set())
                item.book_changed(book)
            log.info("Book %s (%s): %s", item.book.id, item.book.title, msg["msg"])
        else:
            failed += 1
            log.error("Book %s (%s): %s", item.book.id, item.book.title, msg.get("trace") or msg["msg"])
        if on_result:
            on_result(item, msg["ok"], msg["msg"])

    run_bridge(calibre_dir, {"source": result.library, "trash": result.trash_library,
                             "permanent": permanent, "actions": actions}, on_message, cancel)
    return ok, failed, tagged
