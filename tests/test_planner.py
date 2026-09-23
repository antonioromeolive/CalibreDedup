"""Planner tests on real (tiny) metadata.db files built with the Calibre schema subset we read."""

import sqlite3
from pathlib import Path

import pytest

from calibre_dedup.ai import AIMetadata
from calibre_dedup.models import Action
from calibre_dedup.planner import build_plan

SCHEMA = """
CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, pubdate TEXT, path TEXT, uuid TEXT,
                    has_cover INTEGER DEFAULT 0, comments TEXT);
CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE books_authors_link (id INTEGER PRIMARY KEY, book INTEGER, author INTEGER);
CREATE TABLE publishers (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE books_publishers_link (id INTEGER PRIMARY KEY, book INTEGER, publisher INTEGER);
CREATE TABLE identifiers (id INTEGER PRIMARY KEY, book INTEGER, type TEXT, val TEXT);
CREATE TABLE data (id INTEGER PRIMARY KEY, book INTEGER, format TEXT, name TEXT, uncompressed_size INTEGER);
"""


def make_library(path: Path, books: list[dict]) -> str:
    path.mkdir()
    conn = sqlite3.connect(path / "metadata.db")
    conn.executescript(SCHEMA)
    for i, b in enumerate(books, 1):
        year = b.get("year")
        pubdate = f"{year}-06-15 12:00:00+00:00" if year else "0101-01-01 00:00:00+00:00"
        conn.execute("INSERT INTO books VALUES (?,?,?,?,?,?,?)", (
            i, b["title"], pubdate, f"a/b ({i})", f"u{i}",
            int(b.get("cover", False)), b.get("comments"),
        ))
        for a in b.get("authors", ["Frank Herbert"]):
            aid = conn.execute("INSERT INTO authors (name) VALUES (?)", (a,)).lastrowid
            conn.execute("INSERT INTO books_authors_link (book, author) VALUES (?,?)", (i, aid))
        if b.get("publisher"):
            pid = conn.execute("INSERT INTO publishers (name) VALUES (?)", (b["publisher"],)).lastrowid
            conn.execute("INSERT INTO books_publishers_link (book, publisher) VALUES (?,?)", (i, pid))
        if b.get("isbn"):
            conn.execute("INSERT INTO identifiers (book, type, val) VALUES (?,?,?)", (i, "isbn", b["isbn"]))
        for fmt in b.get("formats", ["EPUB"]):
            conn.execute("INSERT INTO data (book, format, name) VALUES (?,?,?)", (i, fmt, "book"))
    conn.commit()
    conn.close()
    return str(path)


class FakeResolver:
    """Stands in for AIResolver: returns canned metadata per book title."""

    def __init__(self, answers: dict[str, AIMetadata]):
        self.answers = answers
        self.calls: list[str] = []
        self.cache = type("C", (), {"save": lambda self: None})()

    def enrich(self, book, ident, need):
        from calibre_dedup.planner import merge_ai
        self.calls.append(book.title)
        meta = self.answers.get(book.title)
        return (merge_ai(ident, meta) if meta else ident), ["fake AI"]


@pytest.fixture
def libs(tmp_path, monkeypatch):
    monkeypatch.setattr("calibre_dedup.planner.MAX_LIBRARY_PATH", 10_000)  # pytest temp paths are long
    def _make(source, target):
        return (make_library(tmp_path / "src", source), make_library(tmp_path / "tgt", target),
                str(tmp_path / "trash"))
    return _make


def actions(plan):
    return [(i.source.title, i.action) for i in plan.items]


def test_basic_move_trash_leave(libs):
    src, tgt, trash = libs(
        source=[
            {"title": "Dune", "publisher": "Ace", "year": 1990},        # dup of target
            {"title": "Dune Messiah", "publisher": "Ace", "year": 1990}, # not in target
            {"title": "Children of Dune"},                               # target has it, no ed/pub info
            {"title": "Unknown", "authors": ["Unknown"]},                 # no title
        ],
        target=[
            {"title": "Dune", "publisher": "Ace Books", "year": 1990, "formats": ["PDF"]},
            {"title": "Children of Dune", "publisher": "Ace", "year": 1991},
        ],
    )
    plan = build_plan(src, tgt, trash)
    assert actions(plan) == [
        ("Dune", Action.TRASH), ("Dune Messiah", Action.MOVE),
        ("Children of Dune", Action.LEAVE), ("Unknown", Action.LEAVE),
    ]
    assert plan.items[0].add_formats == ["EPUB"]


def test_pdf_is_not_added_to_target(libs):
    src, tgt, trash = libs(
        source=[{"title": "Dune", "isbn": "9780441013593", "formats": ["PDF", "MOBI"]}],
        target=[{"title": "Dune", "isbn": "978-0-441-01359-3", "formats": ["EPUB"]}],
    )
    item = build_plan(src, tgt, trash).items[0]
    assert item.action is Action.TRASH and item.add_formats == ["MOBI"]


def test_duplicates_within_source_go_to_trash(libs):
    src, tgt, trash = libs(
        source=[{"title": "Dune", "isbn": "9780441013593"}, {"title": "Dune", "isbn": "9780441013593"}],
        target=[],
    )
    plan = build_plan(src, tgt, trash)
    assert actions(plan) == [("Dune", Action.MOVE), ("Dune", Action.TRASH)]
    assert plan.items[1].match_planned


def test_edition_on_one_side_and_year_on_other_is_unknown(libs):
    src, tgt, trash = libs(
        source=[{"title": "Dune (2nd Edition)", "publisher": "Ace"}],
        target=[{"title": "Dune", "publisher": "Ace", "year": 1965}],
    )
    assert build_plan(src, tgt, trash).items[0].action is Action.LEAVE


def test_ai_fills_title_and_edition(libs):
    src, tgt, trash = libs(
        source=[{"title": "Unknown", "authors": ["Unknown"]}, {"title": "Children of Dune"}],
        target=[{"title": "Dune", "publisher": "Ace", "year": 1990},
                {"title": "Children of Dune", "publisher": "Ace", "year": 1991}],
    )
    resolver = FakeResolver({
        "Unknown": AIMetadata(title="Dune", authors=["Frank Herbert"], publisher="Ace Books", year=1990),
        "Children of Dune": AIMetadata(publisher="Gollancz", year=2003),
    })
    plan = build_plan(src, tgt, trash, resolver)
    assert actions(plan) == [("Unknown", Action.TRASH), ("Children of Dune", Action.MOVE)]
    assert plan.items[0].ai_used and "title" in plan.items[0].identity.ai_fields


def test_non_library_folder_rejected(libs, tmp_path):
    src, tgt, _ = libs(source=[], target=[])
    junk = tmp_path / "junk"
    junk.mkdir()
    (junk / "file.txt").write_text("x")
    with pytest.raises(ValueError):
        build_plan(src, tgt, str(junk))


def test_same_library_merges_formats_and_keeps_richer_record(tmp_path, monkeypatch):
    monkeypatch.setattr("calibre_dedup.planner.MAX_LIBRARY_PATH", 10_000)
    library = make_library(tmp_path / "library", [
        {"title": "Dune", "publisher": "Ace", "year": 1965, "formats": ["EPUB"]},
        {"title": "Dune", "publisher": "Ace", "year": 1965, "formats": ["MOBI"],
         "cover": True, "comments": "A novel"},
    ])
    plan = build_plan(library, library, str(tmp_path / "trash"))

    assert actions(plan) == [("Dune", Action.TRASH), ("Dune", Action.LEAVE)]
    duplicate = plan.items[0]
    assert duplicate.add_formats == ["EPUB"]
    assert duplicate.match is not None


def test_same_library_leaves_distinct_books(tmp_path, monkeypatch):
    monkeypatch.setattr("calibre_dedup.planner.MAX_LIBRARY_PATH", 10_000)
    library = make_library(tmp_path / "library", [
        {"title": "Dune", "publisher": "Ace", "year": 1965},
        {"title": "Dune", "publisher": "Ace", "year": 1990},
    ])
    plan = build_plan(library, library, str(tmp_path / "trash"))

    assert actions(plan) == [("Dune", Action.LEAVE), ("Dune", Action.LEAVE)]
