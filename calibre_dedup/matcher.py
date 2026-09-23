"""Pure duplicate-decision logic.

A source book is a duplicate of a target book when title and authors match AND
it is the same edition from the same publisher:

* a shared ISBN proves it is the same edition and publisher (different ISBNs
  prove nothing, as e-book and print ISBNs of one edition differ);
* otherwise edition (edition number, falling back to publication year) and
  publisher are compared. If either differs, the books are distinct. If either
  is unknown, no decision is possible.
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


def compare_edition(a: Identity, b: Identity) -> Comparison:
    if a.edition is not None and b.edition is not None:
        if a.edition == b.edition:
            return Comparison(Verdict.DUPLICATE, f"same edition ({a.edition})")
        return Comparison(Verdict.DISTINCT, f"different edition ({a.edition} vs {b.edition})")
    if a.year is not None and b.year is not None:
        if a.year == b.year:
            return Comparison(Verdict.DUPLICATE, f"same year ({a.year})")
        return Comparison(Verdict.DISTINCT, f"different year ({a.year} vs {b.year})")
    return Comparison(Verdict.UNKNOWN, "edition unknown")


def compare_publisher(a: Identity, b: Identity) -> Comparison:
    if a.publisher and b.publisher:
        if same_publisher(a.publisher, b.publisher):
            return Comparison(Verdict.DUPLICATE, "same publisher")
        return Comparison(Verdict.DISTINCT, f"different publisher ({a.publisher!r} vs {b.publisher!r})")
    return Comparison(Verdict.UNKNOWN, "publisher unknown")


def compare(src: Identity, tgt: Identity) -> Comparison:
    """Compare two books already known to share title and authors."""
    if src.isbns and tgt.isbns and src.isbns & tgt.isbns:
        return Comparison(Verdict.DUPLICATE, f"same ISBN ({sorted(src.isbns & tgt.isbns)[0]})")

    ed = compare_edition(src, tgt)
    pub = compare_publisher(src, tgt)
    if Verdict.DISTINCT in (ed.verdict, pub.verdict):
        why = ed if ed.verdict is Verdict.DISTINCT else pub
        return Comparison(Verdict.DISTINCT, why.reason)
    if ed.verdict is Verdict.DUPLICATE and pub.verdict is Verdict.DUPLICATE:
        return Comparison(Verdict.DUPLICATE, f"{ed.reason}, {pub.reason}")

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
