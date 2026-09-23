from calibre_dedup.matcher import Verdict, compare, decide
from calibre_dedup.models import Identity


def ident(**kw):
    base = dict(title="Dune", authors=["Frank Herbert"])
    base.update(kw)
    return Identity(**base)


def test_same_isbn_is_duplicate():
    a = ident(isbns={"9780441013593"})
    b = ident(isbns={"9780441013593", "9780000000002"})
    assert compare(a, b).verdict is Verdict.DUPLICATE


def test_different_isbn_alone_is_unknown():
    a = ident(isbns={"9780441013593"})
    b = ident(isbns={"9780306406157"})
    assert compare(a, b).verdict is Verdict.UNKNOWN


def test_same_edition_and_publisher_is_duplicate():
    a = ident(edition=2, publisher="Ace Books")
    b = ident(edition=2, publisher="Ace")
    assert compare(a, b).verdict is Verdict.DUPLICATE


def test_year_used_when_edition_missing():
    assert compare(ident(year=1990, publisher="Ace"), ident(year=1990, publisher="Ace")).verdict is Verdict.DUPLICATE
    assert compare(ident(year=1990, publisher="Ace"), ident(year=2005, publisher="Ace")).verdict is Verdict.DISTINCT


def test_different_edition_or_publisher_is_distinct():
    assert compare(ident(edition=1, publisher="Ace"), ident(edition=2, publisher="Ace")).verdict is Verdict.DISTINCT
    assert compare(ident(edition=1, publisher="Ace"), ident(edition=1, publisher="Chilton")).verdict is Verdict.DISTINCT
    # distinct even if the other attribute is unknown
    assert compare(ident(publisher="Ace"), ident(publisher="Chilton")).verdict is Verdict.DISTINCT


def test_missing_attributes_are_unknown():
    assert compare(ident(publisher="Ace"), ident(publisher="Ace")).verdict is Verdict.UNKNOWN
    assert compare(ident(edition=1), ident(edition=1)).verdict is Verdict.UNKNOWN


def test_decide_prefers_duplicate_then_unknown():
    src = ident(edition=1, publisher="Ace")
    cands = [ident(edition=2, publisher="Ace"), ident(), ident(edition=1, publisher="Ace")]
    d = decide(src, cands)
    assert d.verdict is Verdict.DUPLICATE and d.match_index == 2
    assert decide(src, cands[:2]).verdict is Verdict.UNKNOWN
    assert decide(src, cands[:1]).verdict is Verdict.DISTINCT
