"""Planner tests on real (tiny) metadata.db files built with the Calibre schema subset we read."""

import itertools
import sqlite3
import zipfile
from pathlib import Path

import pytest

from calibre_dedup.ai import AIMetadata
from calibre_dedup.models import Action
from calibre_dedup.planner import build_plan

SCHEMA = """
CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, pubdate TEXT, path TEXT, uuid TEXT,
                    has_cover INTEGER DEFAULT 0, comments TEXT, series_index REAL DEFAULT 1.0);
CREATE TABLE series (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE books_series_link (id INTEGER PRIMARY KEY, book INTEGER, series INTEGER);
CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE books_authors_link (id INTEGER PRIMARY KEY, book INTEGER, author INTEGER);
CREATE TABLE publishers (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE books_publishers_link (id INTEGER PRIMARY KEY, book INTEGER, publisher INTEGER);
CREATE TABLE identifiers (id INTEGER PRIMARY KEY, book INTEGER, type TEXT, val TEXT);
CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE books_tags_link (id INTEGER PRIMARY KEY, book INTEGER, tag INTEGER);
CREATE TABLE data (id INTEGER PRIMARY KEY, book INTEGER, format TEXT, name TEXT, uncompressed_size INTEGER);
"""


def make_library(path: Path, books: list[dict]) -> str:
    path.mkdir()
    conn = sqlite3.connect(path / "metadata.db")
    conn.executescript(SCHEMA)
    for i, b in enumerate(books, 1):
        year = b.get("year")
        pubdate = f"{year}-06-15 12:00:00+00:00" if year else "0101-01-01 00:00:00+00:00"
        conn.execute("INSERT INTO books VALUES (?,?,?,?,?,?,?,?)", (
            i, b["title"], pubdate, f"a/b ({i})", f"u{i}",
            int(b.get("cover", False)), b.get("comments"), b.get("series_index", 1.0),
        ))
        if b.get("series"):
            sid = conn.execute("INSERT INTO series (name) VALUES (?)", (b["series"],)).lastrowid
            conn.execute("INSERT INTO books_series_link (book, series) VALUES (?,?)", (i, sid))
        for a in b.get("authors", ["Frank Herbert"]):
            aid = conn.execute("INSERT INTO authors (name) VALUES (?)", (a,)).lastrowid
            conn.execute("INSERT INTO books_authors_link (book, author) VALUES (?,?)", (i, aid))
        if b.get("publisher"):
            pid = conn.execute("INSERT INTO publishers (name) VALUES (?)", (b["publisher"],)).lastrowid
            conn.execute("INSERT INTO books_publishers_link (book, publisher) VALUES (?,?)", (i, pid))
        for tag in b.get("tags", []):
            tid = conn.execute("INSERT INTO tags (name) VALUES (?)", (tag,)).lastrowid
            conn.execute("INSERT INTO books_tags_link (book, tag) VALUES (?,?)", (i, tid))
        if b.get("isbn"):
            conn.execute("INSERT INTO identifiers (book, type, val) VALUES (?,?,?)", (i, "isbn", b["isbn"]))
        for kind, val in b.get("ids", {}).items():
            conn.execute("INSERT INTO identifiers (book, type, val) VALUES (?,?,?)", (i, kind, val))
        if "text" in b:  # a real EPUB with this text and a per-book OPF and cover
            folder = path / f"a/b ({i})"
            folder.mkdir(parents=True)
            with zipfile.ZipFile(folder / "book.epub", "w") as z:
                z.writestr("content.opf", f"<package>{b['title']} {i}</package>")
                body = b["text"] if b["text"].startswith("<") else f"<p>{b['text'] * 200}</p>"
                z.writestr("text/ch1.xhtml", f"<html><head><style>p {{ x: y }}</style></head><body>{body}</body></html>")
                z.writestr("cover.jpeg", bytes([i]) * 50)
        for fmt in b.get("formats", ["EPUB"]):
            conn.execute("INSERT INTO data (book, format, name) VALUES (?,?,?)", (i, fmt, "book"))
    conn.commit()
    conn.close()
    return str(path)


class FakeResolver:
    """Stands in for AIResolver: returns canned metadata per book title."""

    vision = object()  # an Image AI is configured
    disabled_reason = image_disabled_reason = ""

    def __init__(self, answers: dict[str, AIMetadata]):
        self.answers = answers
        self.calls: list[str] = []
        self.cache = type("C", (), {"save": lambda self: None})()
        self.stats = {}
        self.person_calls: list[tuple[str, str]] = []
        self.same_people: set[frozenset[str]] = set()  # author pairs the fake AI calls the same person

    def same_person(self, book, a, b, title=None):
        self.person_calls.append((a, b))
        same = frozenset((a, b)) in self.same_people
        return same, f"AI: {'same' if same else 'different'} person", True

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


def test_similar_matching_needs_only_one_shared_author(libs):
    src, tgt, trash = libs(
        source=[{"title": "Dune", "authors": ["Frank Herbert", "Brian Herbert"], "isbn": "9780441013593"}],
        target=[{"title": "Dune", "authors": ["Herbert, Frank"], "isbn": "9780441013593"}],
    )
    assert build_plan(src, tgt, trash).items[0].action is Action.MOVE
    item = build_plan(src, tgt, trash, similar_matching=True).items[0]
    assert item.action is Action.TRASH and "same ISBN" in item.reason


class CoverResolver(FakeResolver):
    def __init__(self, same: bool):
        super().__init__({})
        self.same = same
        self.cover_calls: list[tuple[str, str]] = []

    def same_cover(self, a, b):
        self.cover_calls.append((a.title, b.title))
        return self.same, f"AI cover check: {'same' if self.same else 'different'}", True


def test_same_cover_proves_duplicate_when_metadata_cannot_decide(libs):
    src, tgt, trash = libs(
        source=[{"title": "Children of Dune", "cover": True}],
        target=[{"title": "Children of Dune", "publisher": "Ace", "year": 1991, "cover": True}],
    )
    resolver = CoverResolver(same=True)
    assert build_plan(src, tgt, trash, resolver).items[0].action is Action.LEAVE  # cover check off
    item = build_plan(src, tgt, trash, resolver, cover_check=True).items[0]
    assert item.action is Action.TRASH and "same cover" in item.reason and item.ai_used

    item = build_plan(src, tgt, trash, CoverResolver(same=False), cover_check=True).items[0]
    assert item.action is Action.LEAVE and "AI cover check: different" in item.reason


def test_cover_check_skips_decided_pairs_and_missing_covers(libs):
    src, tgt, trash = libs(
        source=[{"title": "Dune", "publisher": "Ace", "year": 1990, "cover": True},
                {"title": "Children of Dune", "cover": True}],
        target=[{"title": "Dune", "publisher": "Gollancz", "year": 2003, "cover": True},
                {"title": "Children of Dune", "publisher": "Ace", "year": 1991}],
    )
    resolver = CoverResolver(same=True)
    plan = build_plan(src, tgt, trash, resolver, cover_check=True)
    assert actions(plan) == [("Dune", Action.MOVE), ("Children of Dune", Action.LEAVE)]
    assert resolver.cover_calls == []


def test_resolver_same_cover(tmp_path):
    from PySide6.QtGui import QColor, QImage

    from calibre_dedup.ai import AICache
    from calibre_dedup.models import Book
    from calibre_dedup.planner import AIResolver

    class Vision:
        profile = type("P", (), {"kind": "fake", "base_url": "", "model": "v"})()
        calls = 0

        def chat(self, system, user, images=None):
            Vision.calls += 1
            assert len(images) == 2
            return '{"verdict": "same", "reason": "same artwork"}'

    def book(i, color):
        folder = tmp_path / f"b{i}"
        folder.mkdir()
        img = QImage(600, 900, QImage.Format_RGB32)
        img.fill(QColor(color))
        img.save(str(folder / "cover.jpg"), "JPEG")
        return Book(i, "T", ["A"], None, None, set(), {}, "", f"b{i}", str(tmp_path), has_cover=True)

    a, b, c = book(1, "red"), book(2, "red"), book(3, "blue")
    resolver = AIResolver(None, None, AICache(tmp_path / "cache.json"), Vision())
    assert resolver.same_cover(a, b) == (True, "identical cover files", False)
    assert resolver.same_cover(a, c) == (True, "AI cover check: same", True)
    assert resolver.same_cover(c, a) == (True, "AI cover check: same (cached)", False)
    assert Vision.calls == 1
    assert AIResolver(None, None, AICache(tmp_path / "x.json")).same_cover(a, c) == (False, "", False)


def test_stopped_analysis_returns_books_analyzed_so_far(libs):
    import threading

    src, tgt, trash = libs(
        source=[{"title": "Dune"}, {"title": "Dune Messiah"}, {"title": "Children of Dune"}],
        target=[],
    )
    cancel = threading.Event()

    def progress(done, total, msg):
        if done == 1:
            cancel.set()  # the user presses Stop while the second book is analyzed

    plan = build_plan(src, tgt, trash, progress=progress, cancel=cancel)
    assert plan.stopped and plan.total_books == 3
    assert actions(plan) == [("Dune", Action.MOVE), ("Dune Messiah", Action.MOVE)]
    assert not build_plan(src, tgt, trash).stopped


def test_plan_knows_same_library(tmp_path, monkeypatch):
    monkeypatch.setattr("calibre_dedup.planner.MAX_LIBRARY_PATH", 10_000)
    library = make_library(tmp_path / "lib", [{"title": "Dune"}])
    assert build_plan(library, library, str(tmp_path / "trash")).same_library


def test_image_ai_errors_do_not_switch_off_the_text_ai(tmp_path):
    from calibre_dedup.ai import AICache, AIError
    from calibre_dedup.models import Book
    from calibre_dedup.planner import MAX_CONSECUTIVE_AI_ERRORS, AIResolver

    resolver = AIResolver(None, None, AICache(tmp_path / "cache.json"))
    book = Book(1, "T", ["A"], None, None, set(), {}, "", "b", str(tmp_path))
    for _ in range(MAX_CONSECUTIVE_AI_ERRORS):
        assert resolver._error(book, AIError("no images"), image=True).startswith("image AI error")
    assert resolver.image_disabled_reason and not resolver.disabled_reason
    for _ in range(MAX_CONSECUTIVE_AI_ERRORS):
        resolver._error(book, AIError("down"))
    assert resolver.disabled_reason.startswith("AI disabled")


def test_image_probe():
    from calibre_dedup.ai import Provider

    class Fake(Provider):
        def __init__(self, reply):
            self.reply, self.images = reply, None

        def chat(self, system, user, images=None):
            self.images = images
            return self.reply

    seeing = Fake('{"colour": "Red"}')
    assert seeing.test_images() == (True, '{"colour": "Red"}') and len(seeing.images) == 1
    assert Fake('{"colour": "unknown"}').test_images() == (False, '{"colour": "unknown"}')


class LibraryResolver(FakeResolver):
    """Canned answers per library folder name ('src' or 'tgt'), for same-title books."""

    def enrich(self, book, ident, need):
        from calibre_dedup.planner import merge_ai
        self.calls.append(Path(book.library).name)
        meta = self.answers.get(Path(book.library).name)
        return (merge_ai(ident, meta) if meta else ident), ["fake AI"]


def test_year_only_difference_is_rechecked_by_reading_both_books(libs):
    src, tgt, trash = libs(
        source=[{"title": "Dune", "publisher": "Ace", "year": 1965}],   # the original publication date
        target=[{"title": "Dune", "publisher": "Ace", "year": 2005}],
    )
    same = {"src": AIMetadata(year=2005, publisher="Ace"), "tgt": AIMetadata(year=2005, publisher="Ace Books")}
    assert build_plan(src, tgt, trash, LibraryResolver(same)).items[0].action is Action.MOVE  # setting off

    resolver = LibraryResolver(same)
    item = build_plan(src, tgt, trash, resolver, recheck_years=True).items[0]
    assert item.action is Action.TRASH and "read by AI" in item.reason
    assert sorted(resolver.calls) == ["src", "tgt"]

    different = {"src": AIMetadata(year=1965), "tgt": AIMetadata(year=2005)}
    item = build_plan(src, tgt, trash, LibraryResolver(different), recheck_years=True).items[0]
    assert item.action is Action.MOVE and "1965 vs 2005, read by AI" in item.reason

    nothing = LibraryResolver({})  # the AI finds no year: the metadata verdict stands
    assert build_plan(src, tgt, trash, nothing, recheck_years=True).items[0].action is Action.MOVE


def test_publisher_difference_is_not_rechecked(libs):
    src, tgt, trash = libs(
        source=[{"title": "Dune", "publisher": "Ace", "year": 1965}],
        target=[{"title": "Dune", "publisher": "Gollancz", "year": 2005}],
    )
    resolver = LibraryResolver({"src": AIMetadata(year=2005), "tgt": AIMetadata(year=2005)})
    assert build_plan(src, tgt, trash, resolver, recheck_years=True).items[0].action is Action.MOVE
    assert resolver.calls == []


def test_decisions_are_logged_only_for_books_with_candidates(libs, caplog):
    import logging

    src, tgt, trash = libs(
        source=[{"title": "Dune", "isbn": "9780441013593"}, {"title": "Dune Messiah"}],
        target=[{"title": "Dune", "isbn": "9780441013593"}],
    )
    with caplog.at_level(logging.INFO, logger="calibre_dedup.planner"):
        build_plan(src, tgt, trash)
    decisions = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Decision:")]
    assert len(decisions) == 1  # "Dune Messiah" has nothing to compare with: not logged
    assert decisions[0].startswith("Decision: Dune — Frank Herbert -> Trash only: duplicate of")


def test_each_book_is_reported_as_soon_as_it_is_decided(libs):
    src, tgt, trash = libs(source=[{"title": "Dune"}, {"title": "Dune Messiah"}], target=[])
    seen = []
    plan = build_plan(src, tgt, trash, on_item=seen.append)
    assert seen == plan.items and [i.source.title for i in seen] == ["Dune", "Dune Messiah"]


def test_shared_asin_proves_duplicate(libs):
    src, tgt, trash = libs(
        source=[{"title": "Children of Dune", "ids": {"mobi-asin": "b01blyjwma"}}],
        target=[{"title": "Children of Dune", "ids": {"amazon_it": "B01BLYJWMA"}}],
    )
    item = build_plan(src, tgt, trash).items[0]
    assert item.action is Action.TRASH and "same ASIN (B01BLYJWMA)" in item.reason


def test_identical_epub_text_proves_duplicate_before_ai(libs):
    src, tgt, trash = libs(
        source=[{"title": "Children of Dune", "text": "Chapter one."},
                {"title": "Dune Messiah", "text": "Other words."}],
        target=[{"title": "Children of Dune", "publisher": "Ace", "text": "Chapter one."},
                {"title": "Dune Messiah", "publisher": "Ace", "text": "Different words."}],
    )
    resolver = FakeResolver({})
    plan = build_plan(src, tgt, trash, resolver)
    assert plan.items[0].action is Action.TRASH and "identical EPUB text" in plan.items[0].reason
    assert plan.items[1].action is Action.LEAVE
    assert resolver.calls == ["Dune Messiah", "Dune Messiah"]  # AI only where the text differs


def test_image_only_epubs_are_not_matched_by_text(libs):
    page = '<div><img src="../images/page001.jpg" alt=""/></div>'
    src, tgt, trash = libs(
        source=[{"title": "Children of Dune", "text": page}],
        target=[{"title": "Children of Dune", "publisher": "Ace", "text": page}],
    )
    item = build_plan(src, tgt, trash).items[0]
    assert item.action is Action.LEAVE and "identical EPUB text" not in item.reason


def test_empty_ai_reply_means_nothing_found():
    assert AIMetadata.from_json("  \n") == AIMetadata()


def _failing_plan_one(error, on_title):
    from calibre_dedup import planner
    real = planner._plan_one

    def plan_one(sb, *args, **kw):
        if sb.title == on_title:
            raise error
        return real(sb, *args, **kw)
    return plan_one


def test_missing_file_leaves_that_book_and_goes_on(libs, monkeypatch):
    src, tgt, trash = libs(source=[{"title": "Dune"}, {"title": "Dune Messiah"}], target=[])
    monkeypatch.setattr("calibre_dedup.planner._plan_one",
                        _failing_plan_one(FileNotFoundError(2, "No such file", "cover.jpg"), "Dune"))
    plan = build_plan(src, tgt, trash)
    assert actions(plan) == [("Dune", Action.LEAVE), ("Dune Messiah", Action.MOVE)]
    assert "file error" in plan.items[0].reason and not plan.stopped


def test_disk_error_stops_the_analysis_and_keeps_the_rest(libs, monkeypatch):
    src, tgt, trash = libs(source=[{"title": "Dune"}, {"title": "Dune Messiah"}, {"title": "Children of Dune"}],
                           target=[])
    monkeypatch.setattr("calibre_dedup.planner._plan_one",
                        _failing_plan_one(OSError(22, "A device which does not exist was specified"), "Dune Messiah"))
    plan = build_plan(src, tgt, trash)
    assert actions(plan) == [("Dune", Action.MOVE)]
    assert plan.stopped and "disk error on Dune Messiah" in plan.stop_reason


def test_missing_file_on_a_vanished_library_stops_the_analysis(libs, monkeypatch):
    src, tgt, trash = libs(source=[{"title": "Dune"}, {"title": "Dune Messiah"}], target=[])
    monkeypatch.setattr("calibre_dedup.planner._plan_one",
                        _failing_plan_one(FileNotFoundError(3, "Path not found", "x"), "Dune"))
    monkeypatch.setattr("calibre_dedup.planner._unreachable", lambda paths: str(paths[0].parent))
    plan = build_plan(src, tgt, trash)
    assert plan.items == [] and plan.stopped and "library not reachable" in plan.stop_reason


def test_unreachable(tmp_path):
    from calibre_dedup.planner import _unreachable
    db = tmp_path / "metadata.db"
    db.write_bytes(b"")
    assert _unreachable([db]) == ""
    db.unlink()
    assert _unreachable([db]) == str(tmp_path)


def test_library_vanishing_between_books_stops_the_analysis(libs, monkeypatch):
    src, tgt, trash = libs(source=[{"title": "Dune"}, {"title": "Dune Messiah"}, {"title": "Children of Dune"}],
                           target=[])
    clock = itertools.count(0, 2)  # every check is "a second later"
    monkeypatch.setattr("calibre_dedup.planner.time.monotonic", lambda: next(clock))
    answers = iter(["", r"F:\lib"])  # reachable after the first book, gone after the second
    monkeypatch.setattr("calibre_dedup.planner._unreachable", lambda paths: next(answers))
    plan = build_plan(src, tgt, trash)
    assert actions(plan) == [("Dune", Action.MOVE)]  # the second book's decision is dropped
    assert plan.stopped and plan.stop_reason == r"library not reachable: F:\lib"


def test_same_series_and_number_proves_duplicate_whatever_the_title(libs):
    src, tgt, trash = libs(
        source=[{"title": "An 2391", "authors": ["B. R. Bruss"], "series": "Urania", "series_index": 243}],
        target=[{"title": "Anno 2391", "authors": ["Bruss, B. R."], "series": "urania", "series_index": 243,
                 "publisher": "Mondadori", "year": 1960}],
    )
    assert build_plan(src, tgt, trash).items[0].action is Action.MOVE  # option off: titles differ
    item = build_plan(src, tgt, trash, same_series=True).items[0]
    assert item.action is Action.TRASH and "same series and number (urania #243)" in item.reason


@pytest.mark.parametrize("target", [
    {"series": "Urania", "series_index": 244},                  # other number
    {"series": "Galassia", "series_index": 243},                # other series
    {"series": "Urania", "series_index": 1},                    # target at Calibre's default number
    {},                                                         # target without a series
])
def test_series_decides_nothing_when_series_or_number_differ(libs, target):
    src, tgt, trash = libs(source=[{"title": "An 2391", "series": "Urania", "series_index": 243}],
                           target=[{"title": "Anno 2391", **target}])
    assert build_plan(src, tgt, trash, same_series=True).items[0].action is Action.MOVE


@pytest.mark.parametrize("source", [
    {},                                                         # no series
    {"series": "Urania", "series_index": 1},                    # Calibre's default number
    {"series": "Urania", "series_index": 0},
    {"series_index": 7},                                        # a number without a series
])
def test_with_the_series_option_books_without_series_number_are_left_untouched(libs, source):
    from calibre_dedup.planner import NO_SERIES_REASON
    # Even an obvious duplicate (same ISBN) is left alone: nothing is decided or executed for it.
    src, tgt, trash = libs(source=[{"title": "Dune", "isbn": "9780441013593", **source}],
                           target=[{"title": "Dune", "isbn": "9780441013593"}])
    item = build_plan(src, tgt, trash, same_series=True).items[0]
    assert item.action is Action.LEAVE and item.reason == NO_SERIES_REASON and not item.ai_used
    assert build_plan(src, tgt, trash).items[0].action is Action.TRASH  # option off: as before


def test_the_series_option_changes_nothing_when_off(libs):
    books = [
        {"title": "Dune", "publisher": "Ace", "year": 1990, "series": "Dune", "series_index": 1},
        {"title": "Dune Messiah", "isbn": "9780441013593", "series": "Dune", "series_index": 2},
        {"title": "Children of Dune", "series_index": 7},
        {"title": "Unknown", "authors": ["Unknown"]},
    ]
    src, tgt, trash = libs(source=books, target=[{"title": "Dune", "publisher": "Ace", "year": 1990}])
    default, off = build_plan(src, tgt, trash), build_plan(src, tgt, trash, same_series=False)
    assert [(i.action, i.reason) for i in off.items] == [(i.action, i.reason) for i in default.items]
    assert not any("series" in i.reason for i in off.items)


def test_a_move_judged_a_different_edition_can_be_forced_to_trash(libs):
    from calibre_dedup.executor import plan_actions
    from calibre_dedup.selection import can_override, override
    src, tgt, trash = libs(
        source=[{"title": "Il nemico di nebbia", "publisher": "Mondadori", "year": 2014},
                {"title": "Dune Messiah", "publisher": "Ace", "year": 1990}],
        target=[{"title": "Il Nemico Di Nebbia", "publisher": "Mondadori", "year": 1964}],
    )
    plan = build_plan(src, tgt, trash)
    different, new = plan.items
    assert different.action is Action.MOVE and "different year" in different.reason
    assert different.match is not None and different.match.title == "Il Nemico Di Nebbia" and different.different
    assert new.action is Action.MOVE and new.match is None  # nothing in the target: nothing to trash into
    assert can_override(different, Action.TRASH) and can_override(new, Action.TRASH)  # Trash: always possible
    override(different, Action.TRASH)
    action = next(a for a in plan_actions(plan, update_metadata=False) if a["src_id"] == different.source.id)
    assert action["op"] == "trash" and action["target_id"] == different.match.id


# --- checks that could not run -------------------------------------------------------
def test_ai_down_asks_and_retry_resets_the_counter(tmp_path):
    from calibre_dedup.ai import AICache, AIError
    from calibre_dedup.models import Book
    from calibre_dedup.planner import MAX_CONSECUTIVE_AI_ERRORS, AIResolver

    asked = []
    answers = iter([True, False])  # Retry once, then Continue without AI
    resolver = AIResolver(None, None, AICache(tmp_path / "cache.json"),
                          on_down=lambda msg, image: asked.append((msg, image)) or next(answers))
    book = Book(1, "T", ["A"], None, None, set(), {}, "", "b", str(tmp_path))
    for _ in range(MAX_CONSECUTIVE_AI_ERRORS - 1):
        assert resolver._error(book, AIError("down")) == "AI error: down"
    assert resolver._error(book, AIError("down")) is None  # retry: nothing turned off
    assert resolver.consecutive_errors == 0 and not resolver.disabled_reason
    for _ in range(MAX_CONSECUTIVE_AI_ERRORS):
        resolver._error(book, AIError("down"))
    assert resolver.disabled_reason.startswith("AI disabled")
    assert len(asked) == 2 and asked[0][1] is False and "failed 3 times" in asked[0][0]


def test_cover_check_without_image_ai_is_reported(libs):
    from calibre_dedup.planner import SKIP_COVER, run_summary

    src, tgt, trash = libs(
        source=[{"title": "Children of Dune", "cover": True}],
        target=[{"title": "Children of Dune", "publisher": "Ace", "year": 1991, "cover": True}],
    )
    blind = CoverResolver(same=True)
    blind.vision = None
    plan = build_plan(src, tgt, trash, blind, cover_check=True)
    item = plan.items[0]
    assert item.action is Action.LEAVE and item.skipped == [SKIP_COVER]
    assert "cover check skipped: no Image AI" in item.reason and blind.cover_calls == []
    assert run_summary(plan)[1] == ["1 book(s): cover check skipped (no Image AI, or AI off)"]

    item = build_plan(src, tgt, trash, None, cover_check=True).items[0]
    assert item.skipped == [SKIP_COVER] and "cover check skipped: AI is off" in item.reason
    assert build_plan(src, tgt, trash, None).items[0].skipped == []  # cover check off: nothing to report


def test_year_recheck_without_ai_is_reported(libs):
    from calibre_dedup.planner import SKIP_YEARS

    src, tgt, trash = libs(
        source=[{"title": "Dune", "publisher": "Ace", "year": 1965}],
        target=[{"title": "Dune", "publisher": "Ace", "year": 2005}],
    )
    item = build_plan(src, tgt, trash, None, recheck_years=True).items[0]
    assert item.action is Action.MOVE and item.skipped == [SKIP_YEARS]
    assert "year re-check skipped: AI is off" in item.reason


class DyingResolver(FakeResolver):
    """The text AI is turned off while reading the first book."""

    def enrich(self, book, ident, need):
        self.calls.append(book.title)
        self.disabled_reason = "AI disabled after 3 consecutive errors"
        return ident, [self.disabled_reason]


def test_books_decided_after_the_ai_went_down_are_marked(libs):
    from calibre_dedup.planner import SKIP_AI, run_summary

    src, tgt, trash = libs(
        source=[{"title": "Dune"}, {"title": "Dune Messiah"}, {"title": "Other"}],
        target=[{"title": "Dune"}, {"title": "Dune Messiah"}],
    )
    plan = build_plan(src, tgt, trash, DyingResolver({}))
    assert [it.skipped for it in plan.items] == [[SKIP_AI], [SKIP_AI], []]
    assert plan.ai_down == ["The text AI stopped responding at book 1 of 3: "
                            "the books after it were decided without it."]
    assert len(run_summary(plan)[1]) == 2


def test_run_summary_counts_what_the_ai_did():
    from calibre_dedup.models import Plan
    from calibre_dedup.planner import run_summary

    plan = Plan("s", "t", "x", stats={"read": 4, "read_cached": 10, "cover": 2, "cover_identical": 1,
                                      "year_rechecks": 3})
    assert run_summary(plan) == ("4 AI page reads (10 cached) · 3 cover comparisons (2 by AI, 0 cached, "
                                 "1 identical files) · 3 year re-checks", [])
    assert run_summary(Plan("s", "t", "x"))[0] == "AI not used"


# --- similar titles -------------------------------------------------------------------
def _similar(libs, source, target, **kw):
    src, tgt, trash = libs(source=source, target=target)
    return build_plan(src, tgt, trash, similar_titles=True, **kw).items[0]


def test_similar_title_without_proof_is_left_to_check(libs):
    item = _similar(libs, [{"title": "1 Abissi D'acciaio", "authors": ["Isaac Asimov"]}],
                    [{"title": "Abissi D'Acciaio", "authors": ["Asimov, Isaac"]}])
    assert item.action is Action.LEAVE and "check manually" in item.reason and item.match.title == "Abissi D'Acciaio"


def test_similar_title_is_off_by_default_and_unrelated_titles_are_not_similar(libs):
    src, tgt, trash = libs(source=[{"title": "1 Dune"}, {"title": "Dune Messiah"}], target=[{"title": "Dune"}])
    assert actions(build_plan(src, tgt, trash)) == [("1 Dune", Action.MOVE), ("Dune Messiah", Action.MOVE)]
    plan = build_plan(src, tgt, trash, similar_titles=True)
    assert actions(plan) == [("1 Dune", Action.LEAVE), ("Dune Messiah", Action.MOVE)]
    assert plan.items[1].match is None


def test_similar_file_name_title_is_a_duplicate_with_proof(libs):
    item = _similar(libs, [{"title": "(Urania - 0411- Supernormale - J. Hunter Holly)", "authors": ["J. Hunter Holly"],
                            "isbn": "0-306-40615-2"}],
                    [{"title": "Supernormale", "authors": ["J. Hunter Holly"], "isbn": "9780306406157"}])
    assert item.action is Action.TRASH and "'supernormale'" in item.reason and "same ISBN" in item.reason


def test_similar_title_with_identical_epub_text(libs):
    item = _similar(libs, [{"title": "ASTRONAVI MALEDETTE", "authors": ["AA.VV."], "text": "Una storia. "}],
                    [{"title": "ASTRONAVI MALEDETTE Inverno 2001", "authors": ["Autori Vari"], "text": "Una storia. "}])
    assert item.action is Action.TRASH and "identical EPUB text" in item.reason


def test_similar_title_cover_decides(libs):
    src = [{"title": "1 Abissi D'acciaio", "authors": ["Isaac Asimov"], "cover": True}]
    tgt = [{"title": "Abissi D'Acciaio", "authors": ["Isaac Asimov"], "cover": True}]
    item = _similar(libs, src, tgt, resolver=CoverResolver(same=True), cover_check=True)
    assert item.action is Action.TRASH and "same cover" in item.reason


def test_similar_title_with_another_cover_is_moved(libs):
    item = _similar(libs, [{"title": "1 Abissi D'acciaio", "authors": ["Isaac Asimov"], "cover": True}],
                    [{"title": "Abissi D'Acciaio", "authors": ["Isaac Asimov"], "cover": True}],
                    resolver=CoverResolver(same=False), cover_check=True)
    assert item.action is Action.MOVE and "has another cover" in item.reason and item.different


def test_similar_title_of_another_edition_is_moved(libs):
    item = _similar(libs, [{"title": "1 Abissi D'acciaio", "authors": ["Isaac Asimov"], "publisher": "Urania"}],
                    [{"title": "Abissi D'Acciaio", "authors": ["Isaac Asimov"], "publisher": "Oscar"}])
    assert item.action is Action.MOVE and "is another book: different publisher" in item.reason


def test_similar_title_cover_check_without_image_ai_is_reported(libs):
    from calibre_dedup.planner import SKIP_COVER

    blind = CoverResolver(same=True)
    blind.vision = None
    item = _similar(libs, [{"title": "1 Abissi D'acciaio", "authors": ["Isaac Asimov"], "cover": True}],
                    [{"title": "Abissi D'Acciaio", "authors": ["Isaac Asimov"], "cover": True}],
                    resolver=blind, cover_check=True)
    assert item.action is Action.LEAVE and item.skipped == [SKIP_COVER]


# --- always compare covers ------------------------------------------------------------
class InsideCoverResolver(CoverResolver):
    def __init__(self, same: bool, inside: bool | None):
        super().__init__(same)
        self.inside = inside  # None: no cover inside a file

    def same_embedded_cover(self, a, b):
        if self.inside is None:
            return False, f"no cover inside the file of {a.label()!r} to confirm", False
        return self.inside, f"covers inside the files: {'same' if self.inside else 'different'}", True


YEARS_DIFFER = dict(
    source=[{"title": "L'inferno a rovescio", "year": 2011, "publisher": "Mondadori", "cover": True,
             "formats": ["EPUB", "MOBI"]}],
    target=[{"title": "L'Inferno A Rovescio", "year": 1986, "publisher": "Mondadori", "cover": True}],
)


def test_always_cover_same_cover_overrides_the_metadata_as_trash_only(libs):
    src, tgt, trash = libs(**YEARS_DIFFER)
    item = build_plan(src, tgt, trash, InsideCoverResolver(True, True), always_cover=True).items[0]
    assert item.action is Action.TRASH and item.by_cover and item.add_formats == []
    assert "metadata differs: different year (2011 vs 1986)" in item.reason


def test_always_cover_needs_the_covers_inside_the_files_too(libs):
    src, tgt, trash = libs(**YEARS_DIFFER)
    for inside in (False, None):
        item = build_plan(src, tgt, trash, InsideCoverResolver(True, inside), always_cover=True).items[0]
        assert item.action is Action.MOVE and not item.by_cover
    item = build_plan(src, tgt, trash, InsideCoverResolver(False, True), always_cover=True).items[0]
    assert item.action is Action.MOVE


def test_without_always_cover_different_metadata_is_not_compared(libs):
    src, tgt, trash = libs(**YEARS_DIFFER)
    resolver = InsideCoverResolver(True, True)
    assert build_plan(src, tgt, trash, resolver, cover_check=True).items[0].action is Action.MOVE
    assert resolver.cover_calls == []


def test_same_cover_when_metadata_cannot_decide_still_merges_formats(libs):
    src, tgt, trash = libs(
        source=[{"title": "Children of Dune", "cover": True, "formats": ["EPUB", "MOBI"]}],
        target=[{"title": "Children of Dune", "publisher": "Ace", "year": 1991, "cover": True}],
    )
    item = build_plan(src, tgt, trash, InsideCoverResolver(True, None), always_cover=True).items[0]
    assert item.action is Action.TRASH and item.by_cover and item.add_formats == ["MOBI"]


def test_epub_cover_is_read_from_the_package(tmp_path):
    import zipfile
    from calibre_dedup.extract import _epub_cover

    def epub(name, opf_meta, items):
        path = tmp_path / name
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("META-INF/container.xml", '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                       '<rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>')
            z.writestr("OEBPS/content.opf", '<package xmlns="http://www.idpf.org/2007/opf"><metadata>'
                       f'{opf_meta}</metadata><manifest>{items}</manifest></package>')
            z.writestr("OEBPS/img/front.jpg", b"FRONT")
            z.writestr("OEBPS/img/other.jpg", b"OTHER")
        return str(path)

    other = '<item id="o" href="img/other.jpg" media-type="image/jpeg"/>'
    assert _epub_cover(epub("a.epub", "", other + '<item id="f" href="img/front.jpg" media-type="image/jpeg" '
                                         'properties="cover-image"/>')) == b"FRONT"
    assert _epub_cover(epub("b.epub", '<meta name="cover" content="f"/>',
                            other + '<item id="f" href="img/front.jpg" media-type="image/jpeg"/>')) == b"FRONT"
    assert _epub_cover(epub("c.epub", "", other)) is None


# --- authors written differently ------------------------------------------------------
TUCKER = dict(
    source=[{"title": "Signori Del Tempo", "authors": ["Wilson Tucke"], "text": "Il tempo dei signori. "}],
    target=[{"title": "Signori Del Tempo", "authors": ["Wilson Tucker"], "text": "Il tempo dei signori. "}],
)


def test_author_one_letter_apart_is_the_same_person(libs):
    src, tgt, trash = libs(**TUCKER)
    assert build_plan(src, tgt, trash).items[0].action is Action.MOVE  # option off
    resolver = FakeResolver({})
    item = build_plan(src, tgt, trash, resolver, author_variants=True).items[0]
    assert item.action is Action.TRASH and "identical EPUB text" in item.reason
    assert "same person: 'Wilson Tucke' / 'Wilson Tucker' (one letter apart)" in item.reason
    assert resolver.person_calls == []  # no AI needed
    assert build_plan(src, tgt, trash, None, author_variants=True).items[0].action is Action.TRASH  # nor any AI


def test_other_author_spellings_are_asked_to_the_ai(libs):
    src, tgt, trash = libs(
        source=[{"title": "Delitto e castigo", "authors": ["Dostoevskij"], "text": "Pietroburgo. "}],
        target=[{"title": "Delitto e castigo", "authors": ["Fyodor Dostoyevsky"], "text": "Pietroburgo. "},
                {"title": "Delitto e castigo", "authors": ["Mario Rossi"]}],
    )
    resolver = FakeResolver({})
    resolver.same_people = {frozenset(("Dostoevskij", "Fyodor Dostoyevsky"))}
    item = build_plan(src, tgt, trash, resolver, author_variants=True).items[0]
    assert item.action is Action.TRASH and item.match.authors == ["Fyodor Dostoyevsky"] and item.ai_used
    assert sorted(resolver.person_calls) == [("Dostoevskij", "Fyodor Dostoyevsky"), ("Dostoevskij", "Mario Rossi")]


def test_different_people_with_the_same_title_are_not_compared(libs):
    src, tgt, trash = libs(source=[{"title": "Nebbia", "authors": ["James Herbert"]}],
                           target=[{"title": "Nebbia", "authors": ["Frank Herbert"]}])
    item = build_plan(src, tgt, trash, FakeResolver({}), author_variants=True).items[0]
    assert item.action is Action.MOVE and item.reason == "not in target"


def test_same_person_asks_in_english_and_caches(tmp_path):
    from calibre_dedup.ai import AICache
    from calibre_dedup.models import Book
    from calibre_dedup.planner import AIResolver

    class Text:
        profile = type("P", (), {"kind": "fake", "base_url": "", "model": "t"})()
        prompts = []

        def chat(self, system, user, images=None):
            Text.prompts.append((system, user))
            return '{"verdict": "same", "reason": "typo"}'

    resolver = AIResolver(Text(), None, AICache(tmp_path / "cache.json"))
    book = Book(1, "T", ["A"], None, None, set(), {}, "", "b", str(tmp_path))
    assert resolver.same_person(book, "Wilson Tucke", "Wilson Tucker") == (True, "AI: same person", True)
    assert resolver.same_person(book, "Wilson Tucker", "Wilson Tucke") == (True, "AI: same person (cached)", False)
    system, user = Text.prompts[0]
    assert len(Text.prompts) == 1 and "same person" in system and 'Name 1: "Wilson Tucke"' in user
    assert "titled" not in user  # no title given: names only
    assert resolver.same_person(book, "Wilson Tucke", "Wilson Tucker", "Il mondo di Tucker")[2] is True  # own key
    assert 'Both books are titled: "Il mondo di Tucker"' in Text.prompts[1][1]


def test_first_names_as_initials_are_the_same_person_without_ai(libs):
    src, tgt, trash = libs(
        source=[{"title": "L'astronave che sapeva", "authors": ["Scott, Melissa"], "text": "Silenzio nello spazio profondo. "}],
        target=[{"title": "L'Astronave Che Sapeva", "authors": ["M.Scott"], "text": "Silenzio nello spazio profondo. "}],
    )
    resolver = FakeResolver({})
    item = build_plan(src, tgt, trash, resolver, author_variants=True).items[0]
    assert item.action is Action.TRASH and "(initials)" in item.reason
    assert resolver.person_calls == []
