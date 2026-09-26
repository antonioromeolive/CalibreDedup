"""Pure duplicate-decision logic.

A source book is a duplicate of a target book when title and authors match AND
it is the same edition from the same publisher:

* a shared ISBN proves it is the same edition and publisher (different ISBNs
  prove nothing, as e-book and print ISBNs of one edition differ); so does a
  shared Amazon ASIN, which Amazon assigns to one edition; and, with the "same
  series" option, the same series and number (not Calibre's default 1);
* otherwise edition (edition number, falling back to publication year) and
  publisher are compared. If either differs, the books are distinct. If either
  is unknown, no decision is possible.

Year and publisher are compared like with like: when the AI read them in both
books, those readings are compared; otherwise the metadata of both. A
difference of metadata years alone is flagged `year_only`: Calibre's date is
often the original publication, so the planner may re-check it with the AI.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .models import Identity
from .normalize import same_publisher


class Verdict(str, Enum):
    DUPLICATE = "duplicate"
    DISTINCT = "distinct"
    UNKNOWN = "unknown"


@dataclass
class Comparison:
    verdict: Verdict
    reason: str
    year_only: bool = False  # distinct only because the metadata years differ
    # Duplicate by an identifier (ISBN, ASIN, series and number), whatever the
    # title: enough even for books whose titles only look alike.
    proof: bool = False


def _like_with_like(meta_a, ai_a, meta_b, ai_b) -> tuple:
    """(value a, value b, read by AI): the AI's readings if it read both books."""
    if ai_a and ai_b:
        return ai_a, ai_b, True
    return meta_a, meta_b, False


def compare_edition(a: Identity, b: Identity) -> Comparison:
    if a.edition is not None and b.edition is not None:
        if a.edition == b.edition:
            return Comparison(Verdict.DUPLICATE, f"same edition ({a.edition})")
        return Comparison(Verdict.DISTINCT, f"different edition ({a.edition} vs {b.edition})")
    ya, yb, by_ai = _like_with_like(a.year, a.ai_year, b.year, b.ai_year)
    if ya is not None and yb is not None:
        source = ", read by AI" if by_ai else ""
        if ya == yb:
            return Comparison(Verdict.DUPLICATE, f"same year ({ya}{source})")
        return Comparison(Verdict.DISTINCT, f"different year ({ya} vs {yb}{source})", year_only=not by_ai)
    return Comparison(Verdict.UNKNOWN, "edition unknown")


def compare_publisher(a: Identity, b: Identity) -> Comparison:
    pa, pb, by_ai = _like_with_like(a.publisher, a.ai_publisher, b.publisher, b.ai_publisher)
    if pa and pb:
        source = ", read by AI" if by_ai else ""
        if same_publisher(pa, pb):
            return Comparison(Verdict.DUPLICATE, f"same publisher{source}")
        return Comparison(Verdict.DISTINCT, f"different publisher ({pa!r} vs {pb!r}{source})")
    return Comparison(Verdict.UNKNOWN, "publisher unknown")


def compare(src: Identity, tgt: Identity) -> Comparison:
    """Compare two books already known to share title and authors."""
    if src.isbns and tgt.isbns and src.isbns & tgt.isbns:
        return Comparison(Verdict.DUPLICATE, f"same ISBN ({sorted(src.isbns & tgt.isbns)[0]})", proof=True)
    if src.asins & tgt.asins:
        return Comparison(Verdict.DUPLICATE, f"same ASIN ({sorted(src.asins & tgt.asins)[0]})", proof=True)
    if src.series is not None and src.series == tgt.series:  # only set with the "same series" option
        return Comparison(Verdict.DUPLICATE, f"same series and number ({src.series[0]} #{src.series[1]:g})",
                          proof=True)

    ed = compare_edition(src, tgt)
    pub = compare_publisher(src, tgt)
    if Verdict.DISTINCT in (ed.verdict, pub.verdict):
        why = ed if ed.verdict is Verdict.DISTINCT else pub
        # Only the years differ (the publisher matches or is unknown): weak evidence.
        return Comparison(Verdict.DISTINCT, why.reason, year_only=ed.year_only and pub.verdict is not Verdict.DISTINCT)
    if ed.verdict is Verdict.DUPLICATE and pub.verdict is Verdict.DUPLICATE:
        return Comparison(Verdict.DUPLICATE, f"{ed.reason}, {pub.reason}")
    # Publisher unknown, but both books print the same edition and year (e.g.
    # "I Edizione novembre 2016"), as read by the AI: enough to call it the same book.
    if ("edition" in src.ai_fields and "edition" in tgt.ai_fields and src.edition == tgt.edition
            and src.ai_year is not None and src.ai_year == tgt.ai_year):
        return Comparison(Verdict.DUPLICATE,
                          f"same edition ({src.edition}) and year ({src.ai_year}) read by AI, publisher unknown")

    # Different ISBNs alone prove nothing: e-book and print ISBNs of the same
    # edition differ, so fall through to "unknown".
    missing = [c.reason for c in (ed, pub) if c.verdict is Verdict.UNKNOWN]
    return Comparison(Verdict.UNKNOWN, ", ".join(missing))


@dataclass
class Decision:
    verdict: Verdict
    reason: str
    match_index: int | None = None  # index into the candidate list


def decide(src: Identity, candidates: list[Identity]) -> Decision:
    """Decide against every target book with the same title and authors.

    Any duplicate wins. Otherwise, if any comparison is undecidable, the book
    stays where it is. Only if it is distinct from all candidates is it moved.
    """
    unknown: list[str] = []
    distinct: list[str] = []
    for i, cand in enumerate(candidates):
        c = compare(src, cand)
        if c.verdict is Verdict.DUPLICATE:
            return Decision(Verdict.DUPLICATE, c.reason, i)
        (unknown if c.verdict is Verdict.UNKNOWN else distinct).append(c.reason)
    if unknown:
        return Decision(Verdict.UNKNOWN, "; ".join(dict.fromkeys(unknown)))
    return Decision(Verdict.DISTINCT, "; ".join(dict.fromkeys(distinct)))
