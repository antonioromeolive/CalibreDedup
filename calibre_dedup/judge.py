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

"""Judge AI: an on-demand second opinion on a pair the analysis left undecided, from a
stronger model (any AI profile, reasoning allowed), never used in the everyday analysis.
It gets everything known about both records: their Calibre metadata, what the everyday
AI read in them, their files, the first and last pages of each text, both covers, and the
facts the program found (the same text, identical or page covers). Its answer is a
suggestion: the row gets it unticked, to review (apply_judgement); nothing is applied by
itself. Tested on 2026-10-03 (TODO.md): right on the pair, wrong on details (it took an
original's year for the edition's), so nothing it reads is ever written to Calibre."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from .ai import AICache, Provider
from .covers import page_cover
from .extract import cover_png, same_text_format
from .models import Action, Book, Plan, PlanItem
from .same_text import percent, share
from .selection import mergeable_formats

log = logging.getLogger(__name__)

START_CHARS = 20000  # of each text, from the start
END_CHARS = 5000  # and from the end
COVER_SIDE = 768
VERDICTS = ("duplicate", "different", "unsure")

JUDGE_PROMPT = """You are an expert librarian judging two e-book records that an automatic deduplicator \
could not decide. BOOK SOURCE is in the library being cleaned; BOOK TARGET is the other copy (in the \
main library, or in the same library). For each you get its Calibre metadata, what a small AI read \
in it (often incomplete or wrong), its files, the first and last pages of its text, and, when \
attached, its Calibre cover. FACTS are checks the program made without AI: trust them.

Decide from evidence only. Calibre's dates are often the date the book was added, or the original \
publication; a "cover" may be only a page of the book.

Respond with a single JSON object with exactly these keys:
{"verdict": "duplicate"|"different"|"unsure", "same_work": "yes"|"no"|"unsure",
 "same_edition": "yes"|"no"|"unsure", "better": "SOURCE"|"TARGET"|"equal", "confidence": integer 0-100,
 "evidence": [string], "covers": string}

- "verdict": "duplicate" when both records are the same edition of the same work (the same text, \
possibly in another file format): SOURCE can go to the trash. "different" when they are different \
books or editions (another translation, an abridged or extended edition, another content). \
"unsure" when the evidence does not prove either. Be conservative: without proof, "unsure".
- "better": which record is the better copy to keep (more complete text, a real cover).
- "evidence": short concrete facts from the pages or the FACTS that support the verdict.
- "covers": what each cover image really is (a real cover, a page of text...), in a few words.
"""


@dataclass
class Judgement:
    verdict: str = "unsure"
    same_work: str = "unsure"
    same_edition: str = "unsure"
    better: str = ""
    confidence: int | None = None
    evidence: list[str] = field(default_factory=list)
    covers: str = ""
    model: str = ""  # the profile that judged

    @classmethod
    def from_json(cls, text: str, model: str = "") -> "Judgement":
        m = re.search(r"\{.*\}", text or "", re.S)
        try:
            data = json.loads(m.group()) if m else {}
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}

        def word(key: str, allowed: tuple[str, ...], default: str) -> str:
            v = str(data.get(key) or "").strip().casefold()
            return v if v in allowed else default

        try:
            confidence = max(0, min(100, int(data.get("confidence"))))
        except (TypeError, ValueError):
            confidence = None
        evidence = data.get("evidence") or []
        covers = data.get("covers") or ""
        if isinstance(covers, dict):
            covers = "; ".join(f"{k}: {v}" for k, v in covers.items())
        return cls(verdict=word("verdict", VERDICTS, "unsure"),
                   same_work=word("same_work", ("yes", "no", "unsure"), "unsure"),
                   same_edition=word("same_edition", ("yes", "no", "unsure"), "unsure"),
                   better={"source": "SOURCE", "target": "TARGET", "equal": "equal"}.get(
                       str(data.get("better") or "").strip().casefold(), ""),
                   confidence=confidence,
                   evidence=[str(e) for e in (evidence if isinstance(evidence, list) else [evidence]) if e][:8],
                   covers=str(covers)[:300], model=model)

    def summary(self) -> str:
        """One line for the row: "duplicate (95%): first fact; second fact"."""
        head = self.verdict + (f" ({self.confidence}%)" if self.confidence is not None else "")
        facts = [e.strip().rstrip(".").strip() for e in self.evidence[:3]]
        return f"{head}: {'; '.join(f for f in facts if f)}" if any(facts) else head


# --- what the judge is told -------------------------------------------------------------
def _metadata(book: Book) -> dict:
    return {"title": book.title, "authors": book.authors, "publisher": book.publisher, "year": book.pub_year,
            "isbn": sorted(book.isbns), "series": book.series,
            "series_index": book.series_index if book.series else None, "languages": book.languages,
            "tags": sorted(book.tags), "has_cover": book.has_cover,
            "files": [f"{Path(p).name} ({book.sizes.get(f) or '?'} bytes)" for f, p in book.formats.items()]}


def _readings(cache: AICache | None, book: Book) -> dict:
    """What the everyday AI read in the book (from the AI cache), by part."""
    if cache is None:
        return {}
    found = {}
    for part in ("start", "end"):
        for path in book.formats.values():
            answer = cache.get(AICache.key(path, part))
            if answer:
                found[part] = {k: v for k, v in answer.items() if v not in (None, [], "")}
                break
    return found


def book_text(extractor, book: Book) -> tuple[str, str, str]:
    """(format read, first pages, last pages) of the book's one compared format."""
    picked = same_text_format(book.formats)
    if picked is None:
        return "", "", ""
    fmt, path = picked
    try:
        text = extractor.whole_text(fmt, path)
    except Exception as e:  # corrupt, DRM, unsupported...
        log.info("Judge AI: cannot read %s: %s", path, e)
        return fmt, "", ""
    head = text[:START_CHARS]
    tail = text[-END_CHARS:] if len(text) > START_CHARS else ""
    return fmt, head, tail


def _cover_file(book: Book) -> Path | None:
    path = Path(book.library, book.path, "cover.jpg")
    return path if book.has_cover and path.is_file() else None


def pair_request(item: PlanItem, extractor, cache: AICache | None = None, prints=None,
                 images: bool = True) -> tuple[str, list[str]]:
    """The judge's question about a row and its match: (text, images)."""
    a, b = item.source, item.match
    parts: list[str] = []
    pictures: list[str] = []
    facts = [f"the analysis left it as: {item.reason}"]
    files = [_cover_file(a), _cover_file(b)]
    for label, book, cover in (("SOURCE", a, files[0]), ("TARGET", b, files[1])):
        fmt, head, tail = book_text(extractor, book)
        if cover is not None and page_cover(cover):
            facts.append(f"{label}'s cover is a page of the book (the exact shape of a page, white, no colour)")
        png = cover_png(cover, COVER_SIDE) if cover is not None and images else None
        if png:
            pictures.append(png)
        where = f"its cover is image {len(pictures)}" if png else "no cover attached"
        text = (f"=== BOOK {label} ({where})\n"
                f"Calibre metadata: {json.dumps(_metadata(book), ensure_ascii=False)}\n"
                f"What a small AI read in it: {json.dumps(_readings(cache, book), ensure_ascii=False)}\n")
        text += (f"--- first pages of its {fmt} ({len(head)} characters):\n{head}\n" if head
                 else f"--- no text could be read ({fmt or 'no file'})\n")
        if tail:
            text += f"--- last pages ({len(tail)} characters):\n{tail}\n"
        parts.append(text)
    if prints is not None:
        fa, fb = prints.get(a), prints.get(b)
        if fa and fb:
            facts.append(f"share of the same text, both ways: {percent(share(fa, fb))} (95% or more: the same text)")
        else:
            facts.append("the texts could not be compared")
    if all(files) and files[0].read_bytes() == files[1].read_bytes():
        facts.append("the two cover files are identical")
    return "FACTS:\n" + "\n".join(f"- {f}" for f in facts) + "\n\n" + "\n".join(parts), pictures


def judge_pair(provider: Provider, item: PlanItem, extractor, cache: AICache | None = None,
               prints=None) -> Judgement:
    """Ask the judge about a row with a match. Raises ai.AIError."""
    if item.match is None:
        raise ValueError("no other copy to compare with")
    text, images = pair_request(item, extractor, cache, prints, images=provider.profile.vision)
    log.info("Judge AI (%s) on %s and %s", provider.profile.name, item.source.label(), item.match.label())
    judgement = Judgement.from_json(provider.chat(JUDGE_PROMPT, text, images or None), provider.profile.name)
    log.info("Judge AI on %s: %s", item.source.label(), judgement.summary())
    return judgement


# --- what the row becomes -----------------------------------------------------------
JUDGE_REVIEW = "the Judge AI's suggestion: check it, then tick"


def apply_judgement(plan: Plan, item: PlanItem, judgement: Judgement) -> None:
    """The judge's answer becomes the row's suggestion, as if the analysis had decided it,
    unticked and to review (selection.needs_review): "duplicate" trashes the book into its
    match (adding the formats the match lacks: Merge & Trash); "different" moves it (two
    libraries) or keeps it (one library); "unsure" only adds the answer to the reason.
    Revert goes back to this suggestion; the analysis' own is in the log."""
    note = f"Judge AI ({judgement.model}): {judgement.summary()}"
    if judgement.verdict == "duplicate" and item.match is not None:
        item.action = Action.TRASH
        item.different = False
        item.add_formats = mergeable_formats(item)
        reason = f"duplicate of {item.match.label()!r} per the {note}"
        if item.add_formats:
            reason += f"; adding {', '.join(item.add_formats)} to the other copy"
    elif judgement.verdict == "different":
        # A record the analysis never moves (a file name for title, title and author swapped) stays.
        held = item.swapped or bool(item.file_name_title) or not item.identity.has_title_authors
        item.action = Action.LEAVE if plan.same_library or held else Action.MOVE
        item.different = True
        item.add_formats = []
        reason = f"different from {item.match.label()!r} per the {note}" if item.match is not None else note
    else:
        item.reason = item.planned_reason = f"{item.planned_reason} [{note}]"
        return
    log.info("%s: %s", item.source.label(), reason)
    item.reason = item.planned_reason = reason
    item.planned_action, item.planned_add_formats = item.action, list(item.add_formats)
    item.manual = item.reviewed = False
    item.selected = item.planned_selected = False
    item.review = JUDGE_REVIEW
