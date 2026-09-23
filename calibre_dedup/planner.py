"""Build the plan: decide, for every source book, whether to move it to the
target library, send it to the trash library, or leave it where it is. When
source and target are the same library, duplicate records are merged into the
record with richer metadata and the weaker record is sent to trash.

AI is only consulted when metadata is not enough:
* the source book has no usable title/authors, or
* a same-title/same-author book exists in the target but edition or publisher
  can't be compared from metadata (both books are then enriched).
It reads the first pages, then the last pages if fields are still missing.
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .ai import AICache, AIError, AIMetadata, Provider, extract_metadata
from .extract import TextExtractor
from .library import read_books
from .matcher import Verdict, decide
from .models import Action, Book, Identity, Plan, PlanItem
from .normalize import (
    authors_key, is_unknown, normalize_isbn, parse_edition_number, title_key,
)

log = logging.getLogger(__name__)

ProgressFn = Callable[[int, int, str], None]  # (done, total, message)
MAX_CONSECUTIVE_AI_ERRORS = 3
MAX_LIBRARY_PATH = 89  # Calibre refuses longer library paths on Windows


class Cancelled(Exception):
    pass


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
    )


def merge_ai(ident: Identity, meta: AIMetadata) -> Identity:
    """Fill fields missing from `ident` with what the AI found."""
    out = ident.copy()
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


class AIResolver:
    """Reads book excerpts and asks the model for missing metadata."""

    def __init__(self, provider: Provider, extractor: TextExtractor, cache: AICache,
                 vision_provider: Provider | None = None):
        self.provider = provider
        self.vision = vision_provider
        self.extractor = extractor
        self.cache = cache
        self.consecutive_errors = 0
        self.disabled_reason = ""

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
                self.consecutive_errors += 1
                notes.append(f"AI error: {e}")
                log.warning("AI error on %s: %s", book.label(), e)
                if self.consecutive_errors >= MAX_CONSECUTIVE_AI_ERRORS:
                    self.disabled_reason = f"AI disabled after {self.consecutive_errors} consecutive errors"
                    log.error(self.disabled_reason)
                break
            self.consecutive_errors = 0
            notes.append(note)
            if meta:
                ident = merge_ai(ident, meta)
        return ident, notes

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
        if excerpt.images and self.vision:
            provider, images = self.vision, excerpt.images
        elif excerpt.text.strip():
            provider, images = self.provider, None
        else:
            return None, f"no text in {part} pages ({excerpt.source})"
        log.info("AI reading %s of %s (%s)", part, book.label(), excerpt.source)
        meta = extract_metadata(provider, excerpt.text, images)
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
    formats: set[str] = field(default_factory=set)  # formats the target copy will have

    def __post_init__(self):
        self.formats = self.formats or set(self.book.formats)


def _key(ident: Identity, ignore_subtitle: bool) -> tuple | None:
    if not ident.title or not ident.authors:
        return None
    t, a = title_key(ident.title, ignore_subtitle), authors_key(ident.authors)
    return (t, a) if t and a else None


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
) -> Plan:
    check_libraries(source, target, trash)
    progress = progress or (lambda *_: None)
    source_books = read_books(source)
    same_library = str(Path(source).resolve()).casefold() == str(Path(target).resolve()).casefold()
    target_books = read_books(target) if Path(target, "metadata.db").is_file() else []
    plan = Plan(source, target, trash)

    index: dict[tuple, list[_Candidate]] = defaultdict(list)
    main_index: dict[tuple, list[_Candidate]] = defaultdict(list)  # subtitle ignored

    def add_to_index(c: _Candidate) -> None:
        k = _key(c.identity, ignore_subtitle)
        if k:
            index[k].append(c)
            main_index[_key(c.identity, True)].append(c)

    if not same_library:
        for tb in target_books:
            add_to_index(_Candidate(tb, metadata_identity(tb), planned=False))

    total = len(source_books)
    analysis_books = source_books
    if same_library:
        analysis_books = sorted(source_books, key=_metadata_richness, reverse=True)
    for n, sb in enumerate(analysis_books, 1):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        progress(n - 1, total, f"Analyzing {sb.label()}")
        item = _plan_one(sb, index, main_index, resolver, ignore_subtitle, same_library)
        plan.items.append(item)
        if same_library:
            if item.action is not Action.TRASH:
                add_to_index(_Candidate(sb, item.identity, planned=False, ai_done=item.ai_used))
        elif item.action is Action.MOVE:
            add_to_index(_Candidate(sb, item.identity, planned=True, ai_done=item.ai_used))
        if resolver and n % 10 == 0:
            resolver.cache.save()
    if resolver:
        resolver.cache.save()
    progress(total, total, "Analysis complete")
    if same_library:
        plan.items.sort(key=lambda item: source_books.index(item.source))
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
              ignore_subtitle: bool, same_library: bool = False) -> PlanItem:
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

    key = _key(ident, ignore_subtitle)
    if key is None:
        return PlanItem(sb, Action.LEAVE, "title/authors unusable after normalization", ident, ai_used=ai_used)
    candidates = index.get(key, [])

    if not candidates:
        if ident.ai_fields & {"title", "authors"} and not ignore_subtitle:
            near = main_index.get(_key(ident, True), [])
            if near:
                return PlanItem(
                    sb, Action.LEAVE,
                    _join(f"AI-found title matches {near[0].book.label()!r} only when ignoring the subtitle; check manually", notes),
                    ident, match=near[0].book, match_planned=near[0].planned, ai_used=ai_used)
        action = Action.LEAVE if same_library else Action.MOVE
        reason = "no duplicate in this library" if same_library else "not in target"
        return PlanItem(sb, action, _join(reason, notes), ident, ai_used=ai_used)

    # 2. Same title/authors exist: compare edition and publisher
    decision = decide(ident, [c.identity for c in candidates])
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

    if decision.verdict is Verdict.DUPLICATE:
        cand = candidates[decision.match_index]
        missing = [f for f in sb.formats if f not in cand.formats and f != "PDF"]
        cand.formats.update(missing)  # later duplicates must not add the same format again
        reason = f"duplicate of {cand.book.label()!r} ({decision.reason})"
        if missing:
            reason += f"; adding {', '.join(missing)} to target copy"
        return PlanItem(sb, Action.TRASH, _join(reason, notes), ident, match=cand.book,
                        match_planned=cand.planned, add_formats=missing, ai_used=ai_used)
    if decision.verdict is Verdict.DISTINCT:
        action = Action.LEAVE if same_library else Action.MOVE
        reason = (f"different from existing books: {decision.reason}" if same_library
                  else f"different from target copies: {decision.reason}")
        return PlanItem(sb, action, _join(reason, notes),
                        ident, ai_used=ai_used)
    return PlanItem(sb, Action.LEAVE,
                    _join(f"same title/authors as {candidates[0].book.label()!r} but {decision.reason}", notes),
                    ident, match=candidates[0].book, match_planned=candidates[0].planned, ai_used=ai_used)


def _join(reason: str, notes: list[str]) -> str:
    notes = [n for n in dict.fromkeys(notes) if n]
    return reason + (f" [{'; '.join(notes)}]" if notes else "")
