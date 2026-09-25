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


def test_same_edition_and_year_read_by_ai_is_duplicate_without_publisher():
    def read(**kw):
        return ident(ai_fields={"edition"}, **kw)
    c = compare(read(edition=1, ai_year=2016), read(edition=1, ai_year=2016))
    assert c.verdict is Verdict.DUPLICATE and "read by AI" in c.reason
    # Not read by AI in both books, or the year is missing: still unknown.
    assert compare(ident(edition=1, ai_year=2016), read(edition=1, ai_year=2016)).verdict is Verdict.UNKNOWN
    assert compare(read(edition=1), read(edition=1)).verdict is Verdict.UNKNOWN
    # A different edition is distinct; different years read by AI stay unknown.
    assert compare(read(edition=1, ai_year=2016), read(edition=2, ai_year=2016)).verdict is Verdict.DISTINCT
    assert compare(read(edition=1, ai_year=2016), read(edition=1, ai_year=2018)).verdict is Verdict.UNKNOWN


def test_decide_prefers_duplicate_then_unknown():
    src = ident(edition=1, publisher="Ace")
    cands = [ident(edition=2, publisher="Ace"), ident(), ident(edition=1, publisher="Ace")]
    d = decide(src, cands)
    assert d.verdict is Verdict.DUPLICATE and d.match_index == 2
    assert decide(src, cands[:2]).verdict is Verdict.UNKNOWN
    assert decide(src, cands[:1]).verdict is Verdict.DISTINCT


def test_year_only_difference_is_flagged_as_weak():
    c = compare(ident(publisher="Ace", year=2003), ident(publisher="Ace Books", year=2017))
    assert c.verdict is Verdict.DISTINCT and c.year_only
    c = compare(ident(year=2003), ident(year=2017))  # publisher unknown: still only the years
    assert c.verdict is Verdict.DISTINCT and c.year_only
    c = compare(ident(publisher="Ace", year=2003), ident(publisher="Gollancz", year=2017))
    assert c.verdict is Verdict.DISTINCT and not c.year_only  # the publisher differs too
    c = compare(ident(edition=1, publisher="Ace"), ident(edition=2, publisher="Ace"))
    assert c.verdict is Verdict.DISTINCT and not c.year_only  # printed editions differ


def test_ai_readings_are_compared_like_with_like():
    # Metadata years differ, but the AI read the same year in both books.
    a = ident(publisher="Ace", year=1965, ai_year=2005)
    b = ident(publisher="Ace", year=2005, ai_year=2005)
    c = compare(a, b)
    assert c.verdict is Verdict.DUPLICATE and "read by AI" in c.reason
    # Read in one book only: metadata on both sides.
    assert compare(ident(publisher="Ace", year=1965, ai_year=2005), ident(publisher="Ace", year=2005)).year_only
    # Different years read by AI are a real difference, not a weak one.
    c = compare(ident(publisher="Ace", year=2005, ai_year=1990), ident(publisher="Ace", year=2005, ai_year=2005))
    assert c.verdict is Verdict.DISTINCT and not c.year_only
    # Publisher: the AI's readings of both books win over metadata.
    c = compare(ident(year=2005, publisher="Amazon", ai_publisher="Mondadori"),
                ident(year=2005, publisher="Mondadori", ai_publisher="Arnoldo Mondadori Editore"))
    assert c.verdict is Verdict.DUPLICATE
