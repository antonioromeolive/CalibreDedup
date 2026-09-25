"""Build the plan: decide, for every source book, whether to move it to the
target library, send it to the trash library, or leave it where it is. When
source and target are the same library, duplicate records are merged into the
record with richer metadata and the weaker record is sent to trash.

AI is only consulted when metadata is not enough:
* the source book has no usable title/authors, or
* a same-title/same-author book exists in the target but edition or publisher
  can't be compared from metadata (both books are then enriched).
It reads the first pages, then the last pages if fields are still missing.
If that still can't decide, a vision model may compare the two covers: the
same cover is taken as proof of the same book.
Before any AI call, identical EPUB text proves the same book (copies that
differ only in metadata or cover).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Callable

from .ai import AICache, AIError, AIMetadata, Provider, compare_covers, extract_metadata
from .extract import TextExtractor, cover_png, epub_text_digest
from .library import read_books
from .matcher import Decision, Verdict, compare, decide
from .models import Action, Book, Identity, Plan, PlanItem
from .normalize import (
    authors_key, is_unknown, normalize_isbn, parse_edition_number, similar_authors_keys, title_key,
)
from .selection import action_label

log = logging.getLogger(__name__)

ProgressFn = Callable[[int, int, str], None]  # (done, total, message)
MAX_CONSECUTIVE_AI_ERRORS = 3
MAX_LIBRARY_PATH = 89  # Calibre refuses longer library paths on Windows


def metadata_identity(book: Book) -> Identity:
    title = None if is_unknown(book.title) else book.title
    authors = [a for a in book.authors if not is_unknown(a)]
    return Identity(
        title=title,
        authors=authors,
        publisher=book.publisher,
        edition=parse_edition_number(title) if title else None,
        year=book.pub_year,
        isbns=set(book.isbns),
        asins=set(book.asins),
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


def _needs_title_authors(i: Identity) -> bool:
    return not i.has_title_authors


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
    """

    def __init__(self, provider: Provider, extractor: TextExtractor, cache: AICache,
                 vision_provider: Provider | None = None):
        self.provider = provider
        self.vision = vision_provider
        self.extractor = extractor
        self.cache = cache
        self.consecutive_errors = 0
        self.disabled_reason = ""
        self.image_errors = 0
        self.image_disabled_reason = ""

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
                notes.append(self._error(book, e, image=isinstance(e, ImageAIError)))
                break
            self.consecutive_errors = 0
            notes.append(note)
            if meta:
                ident = merge_ai(ident, meta)
        return ident, notes

    def _error(self, book: Book, e: AIError, image: bool = False) -> str:
        what = "image AI" if image else "AI"
        log.warning("%s error on %s: %s", what, book.label(), e)
        if image:
            self.image_errors += 1
            if self.image_errors >= MAX_CONSECUTIVE_AI_ERRORS and not self.image_disabled_reason:
                self.image_disabled_reason = f"image AI disabled after {self.image_errors} consecutive errors"
                log.error(self.image_disabled_reason)
        else:
            self.consecutive_errors += 1
            if self.consecutive_errors >= MAX_CONSECUTIVE_AI_ERRORS and not self.disabled_reason:
                self.disabled_reason = f"AI disabled after {self.consecutive_errors} consecutive errors"
                log.error(self.disabled_reason)
        return f"{what} error: {e}"

    def same_cover(self, a: Book, b: Book) -> tuple[bool, str, bool]:
        """Whether both books show the same cover, per the vision model.
        Returns (same, note, model_called)."""
        pa, pb = _cover_path(a), _cover_path(b)
        if self.vision is None or not (pa.is_file() and pb.is_file()):
            return False, "", False
        if pa.stat().st_size == pb.stat().st_size and pa.read_bytes() == pb.read_bytes():
            return True, "identical cover files", False
        key = AICache.pair_key(str(pa), str(pb), f"cover|{self._model_id(self.vision)}")
        cached = self.cache.get(key)
        if cached is not None:
            return cached["verdict"] == "same", f"AI cover check: {cached['verdict']} (cached)", False
        if self.image_disabled_reason:
            return False, self.image_disabled_reason, False
        covers = cover_png(pa), cover_png(pb)
        if None in covers:
            return False, "cover image unreadable", False
        log.info("AI comparing covers of %s and %s", a.label(), b.label())
        try:
            verdict, reason = compare_covers(self.vision, *covers)
        except AIError as e:
            return False, self._error(a, e, image=True), True
        self.image_errors = 0
        self.cache.put(key, {"verdict": verdict, "reason": reason})
        return verdict == "same", f"AI cover check: {verdict}", True

    def _query(self, book: Book, part: str) -> tuple[AIMetadata | None, str]:
        picked = self.extractor.pick_format(book.formats)
        if not picked:
            return None, "no readable file"
        path = picked[1]
        cached = self.cache.get(AICache.key(path, part))
        if cached is not None:
            return AIMetadata(**cached), f"AI {part} pages (cached)"

        providers = [self.provider] + ([self.vision] if self.vision else [])
        for p in providers:
            key = AICache.key(path, part, self._model_id(p))
            cached = self.cache.get(key)
            if cached is not None:
                self.cache.put(AICache.key(path, part), cached)
                return AIMetadata(**cached), f"AI {part} pages (cached)"

        excerpt = self.extractor.excerpt(book.formats, part)
        if excerpt.images and self.vision and not self.image_disabled_reason:
            provider, images = self.vision, excerpt.images
        elif excerpt.text.strip():
            provider, images = self.provider, None
        else:
            return None, self.image_disabled_reason or f"no text in {part} pages ({excerpt.source})"
        log.info("AI reading %s of %s (%s)", part, book.label(), excerpt.source)
        try:
            meta = extract_metadata(provider, excerpt.text, images)
        except AIError as e:
            raise (ImageAIError(str(e)) if images else e) from e
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

    def __post_init__(self):
        self.formats = self.formats or set(self.book.formats)


def _keys(ident: Identity, ignore_subtitle: bool, similar: bool) -> list[tuple]:
    """Index keys of a book. Strict: title + all authors. Similar: title + each
    author, loosely normalized, so books sharing any author are compared."""
    if not ident.title or not ident.authors:
        return []
    t = title_key(ident.title, ignore_subtitle)
    if not t:
        return []
    if similar:
        return [(t, a) for a in similar_authors_keys(ident.authors)]
    a = authors_key(ident.authors)
    return [(t, a)] if a else []


def _lookup(index: dict[tuple, list[_Candidate]], keys: list[tuple]) -> list[_Candidate]:
    found: dict[int, _Candidate] = {}
    for k in keys:
        for c in index.get(k, []):
            found.setdefault(id(c), c)
    return list(found.values())


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
    on_item: Callable[[PlanItem], None] | None = None,
) -> Plan:
    """`on_item` is called with each book as soon as it is decided."""
    check_libraries(source, target, trash)
    progress = progress or (lambda *_: None)
    source_books = read_books(source)
    same_library = str(Path(source).resolve()).casefold() == str(Path(target).resolve()).casefold()
    target_books = read_books(target) if Path(target, "metadata.db").is_file() else []
    plan = Plan(source, target, trash, total_books=len(source_books), same_library=same_library)

    index: dict[tuple, list[_Candidate]] = defaultdict(list)
    main_index: dict[tuple, list[_Candidate]] = defaultdict(list)  # subtitle ignored

    def add_to_index(c: _Candidate) -> None:
        for k in _keys(c.identity, ignore_subtitle, similar_matching):
            index[k].append(c)
        for k in _keys(c.identity, True, similar_matching):
            main_index[k].append(c)

    if not same_library:
        for tb in target_books:
            add_to_index(_Candidate(tb, metadata_identity(tb), planned=False))

    total = len(source_books)
    analysis_books = source_books
    if same_library:
        analysis_books = sorted(source_books, key=_metadata_richness, reverse=True)
    libraries = [Path(source, "metadata.db")] + ([Path(target, "metadata.db")] if target_books else [])
    checked = time.monotonic()

    def stop(reason: str) -> None:
        plan.stopped, plan.stop_reason = True, reason
        log.error("Analysis stopped: %s", reason)

    for n, sb in enumerate(analysis_books, 1):
        if cancel is not None and cancel.is_set():
            plan.stopped = True  # keep what was analyzed so far
            break
        progress(n - 1, total, f"Analyzing {sb.label()}")
        try:
            item = _plan_one(sb, index, main_index, resolver, ignore_subtitle, same_library,
                             similar_matching, cover_check, recheck_years)
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
        # File errors are also caught (as "unreadable") deeper down: a vanished
        # drive would quietly leave books undecided. Check, at most once a second.
        if time.monotonic() - checked >= 1:
            gone = _unreachable(libraries)
            if gone:
                stop(f"library not reachable: {gone}")
                break
            checked = time.monotonic()
        plan.items.append(item)
        if same_library:
            if item.action is not Action.TRASH:
                add_to_index(_Candidate(sb, item.identity, planned=False, ai_done=item.ai_used))
        elif item.action is Action.MOVE:
            add_to_index(_Candidate(sb, item.identity, planned=True, ai_done=item.ai_used))
        # Only now: the user may change the item from here on (planner decisions
        # above use the analysis' own verdict, never the user's override).
        if on_item is not None:
            on_item(item)
        if resolver and n % 10 == 0:
            resolver.cache.save()
    if resolver:
        resolver.cache.save()
    progress(len(plan.items), total, "Analysis stopped" if plan.stopped else "Analysis complete")
    if same_library:
        order = {id(b): i for i, b in enumerate(source_books)}
        plan.items.sort(key=lambda item: order[id(item.source)])
    return plan


def _metadata_richness(book: Book) -> int:
    return (
        len(book.formats) +
        int(book.has_cover) +
        int(bool(book.comments and book.comments.strip())) +
        int(bool(book.publisher)) +
        int(book.pub_year is not None) +
        len(book.isbns)
    )


def _plan_one(sb: Book, index, main_index, resolver: AIResolver | None,
              ignore_subtitle: bool, same_library: bool = False,
              similar_matching: bool = False, cover_check: bool = False,
              recheck_years: bool = False) -> PlanItem:
    ident = metadata_identity(sb)
    notes: list[str] = []
    ai_used = False

    # 1. Title and authors
    if not ident.has_title_authors:
        if not resolver:
            return PlanItem(sb, Action.LEAVE, "title/authors missing and AI is off", ident)
        ident, n = resolver.enrich(sb, ident, _needs_title_authors)
        notes += n
        ai_used = True
        if not ident.has_title_authors:
            return PlanItem(sb, Action.LEAVE, _join("title/authors could not be determined", notes), ident, ai_used=True)

    keys = _keys(ident, ignore_subtitle, similar_matching)
    if not keys:
        return PlanItem(sb, Action.LEAVE, "title/authors unusable after normalization", ident, ai_used=ai_used)
    candidates = _lookup(index, keys)

    if not candidates:
        if ident.ai_fields & {"title", "authors"} and not ignore_subtitle:
            near = _lookup(main_index, _keys(ident, True, similar_matching))
            if near:
                return _logged(PlanItem(
                    sb, Action.LEAVE,
                    _join(f"AI-found title matches {near[0].book.label()!r} only when ignoring the subtitle; check manually", notes),
                    ident, match=near[0].book, match_planned=near[0].planned, ai_used=ai_used))
        action = Action.LEAVE if same_library else Action.MOVE
        reason = "no duplicate in this library" if same_library else "not in target"
        return PlanItem(sb, action, _join(reason, notes), ident, ai_used=ai_used)

    # 2. Same title/authors exist: compare edition and publisher
    decision = decide(ident, [c.identity for c in candidates])
    if decision.verdict is Verdict.UNKNOWN:
        decision = _same_text(sb, ident, candidates) or decision
    if decision.verdict is Verdict.UNKNOWN and resolver:
        if _needs_edition_publisher(ident):
            ident, n = resolver.enrich(sb, ident, _needs_edition_publisher)
            notes += n
            ai_used = True
        for c in candidates:
            if not c.ai_done and _needs_edition_publisher(c.identity):
                c.identity, _ = resolver.enrich(c.book, c.identity, _needs_edition_publisher)
                c.ai_done = True
                ai_used = True
        decision = decide(ident, [c.identity for c in candidates])

    # 3. Different only by metadata year: Calibre's date is often the original
    # publication, so read both books and compare the years printed in them.
    if recheck_years and resolver and decision.verdict is not Verdict.DUPLICATE:
        weak = [c for c in candidates if compare(ident, c.identity).year_only]
        if weak:
            if _needs_ai_year_publisher(ident):
                ident, n = resolver.enrich(sb, ident, _needs_ai_year_publisher)
                notes += n
                ai_used = True
            for c in weak:
                if not c.year_rechecked and _needs_ai_year_publisher(c.identity):
                    c.identity, _ = resolver.enrich(c.book, c.identity, _needs_ai_year_publisher)
                    ai_used = True
                c.year_rechecked = True
            decision = decide(ident, [c.identity for c in candidates])

    # 4. Still undecided: the same cover proves the same book
    if decision.verdict is Verdict.UNKNOWN and resolver and cover_check and sb.has_cover:
        for i, c in enumerate(candidates):
            if not c.book.has_cover or compare(ident, c.identity).verdict is not Verdict.UNKNOWN:
                continue
            same, note, called = resolver.same_cover(sb, c.book)
            notes.append(note)
            ai_used = ai_used or called
            if same:
                decision = Decision(Verdict.DUPLICATE, "same cover", i)
                break

    if decision.verdict is Verdict.DUPLICATE:
        cand = candidates[decision.match_index]
        missing = [f for f in sb.formats if f not in cand.formats and f != "PDF"]
        cand.formats.update(missing)  # later duplicates must not add the same format again
        reason = f"duplicate of {cand.book.label()!r} ({decision.reason})"
        if missing:
            reason += f"; adding {', '.join(missing)} to target copy"
        return _logged(PlanItem(sb, Action.TRASH, _join(reason, notes), ident, match=cand.book,
                                match_planned=cand.planned, add_formats=missing, ai_used=ai_used))
    if decision.verdict is Verdict.DISTINCT:
        action = Action.LEAVE if same_library else Action.MOVE
        reason = (f"different from existing books: {decision.reason}" if same_library
                  else f"different from target copies: {decision.reason}")
        return _logged(PlanItem(sb, action, _join(reason, notes), ident, ai_used=ai_used))
    return _logged(PlanItem(
        sb, Action.LEAVE,
        _join(f"same title/authors as {candidates[0].book.label()!r} but {decision.reason}", notes),
        ident, match=candidates[0].book, match_planned=candidates[0].planned, ai_used=ai_used))


def _logged(item: PlanItem) -> PlanItem:
    """Log the decision for a book that had candidates (books with none are the
    vast majority and would flood the log)."""
    log.info("Decision: %s -> %s: %s", item.source.label(), action_label(item), item.reason)
    return item


def _join(reason: str, notes: list[str]) -> str:
    notes = [n for n in dict.fromkeys(notes) if n]
    return reason + (f" [{'; '.join(notes)}]" if notes else "")
