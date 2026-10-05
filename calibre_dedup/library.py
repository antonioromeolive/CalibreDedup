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

"""Read-only access to a Calibre library's metadata.db.

Reading directly with SQLite (read-only) means the analysis can run even while
Calibre is open. All writes go through Calibre itself, see bridge.py.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from .models import Book
from .normalize import is_unknown, normalize_asin, normalize_isbn


class LibraryError(Exception):
    pass


def is_library(path: str | Path) -> bool:
    return Path(path, "metadata.db").is_file()


def read_books(library: str | Path) -> list[Book]:
    library = Path(library)
    db_path = library / "metadata.db"
    if not db_path.is_file():
        if library.is_dir() and not any(library.iterdir()):
            return []  # an empty folder is a new, empty library
        raise LibraryError(f"{library} is not a Calibre library (no metadata.db)")

    uri = db_path.resolve().as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as e:
        raise LibraryError(f"Cannot open {db_path}: {e}") from e
    try:
        authors: dict[int, list[str]] = defaultdict(list)
        for book, name in conn.execute(
            "SELECT l.book, a.name FROM books_authors_link l JOIN authors a ON a.id = l.author ORDER BY l.id"
        ):
            authors[book].append(name.replace("|", ","))  # Calibre stores ',' in names as '|'

        publishers = dict(conn.execute(
            "SELECT l.book, p.name FROM books_publishers_link l JOIN publishers p ON p.id = l.publisher"
        ))
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        series = dict(conn.execute(
            "SELECT l.book, s.name FROM books_series_link l JOIN series s ON s.id = l.series"
        )) if {"books_series_link", "series"} <= tables else {}
        languages: dict[int, list[str]] = defaultdict(list)
        if {"books_languages_link", "languages"} <= tables:
            for book, code in conn.execute(
                "SELECT l.book, g.lang_code FROM books_languages_link l JOIN languages g ON g.id = l.lang_code "
                "ORDER BY l.book, l.item_order"
            ):
                languages[book].append(code)
        tags: dict[int, set[str]] = defaultdict(set)
        if {"books_tags_link", "tags"} <= tables:
            for book, name in conn.execute(
                "SELECT l.book, t.name FROM books_tags_link l JOIN tags t ON t.id = l.tag"
            ):
                tags[book].add(name)

        isbns: dict[int, set[str]] = defaultdict(set)
        asins: dict[int, set[str]] = defaultdict(set)
        for book, kind, val in conn.execute("SELECT book, type, val FROM identifiers"):
            kind = (kind or "").lower()
            if kind == "isbn":
                isbn = normalize_isbn(val)
                if isbn:
                    isbns[book].add(isbn)
            elif kind == "mobi-asin" or kind.startswith("amazon"):
                asin = normalize_asin(val)
                if asin:
                    asins[book].add(asin)

        columns = {row[1] for row in conn.execute("PRAGMA table_info(books)")}
        optional = []
        if "has_cover" in columns:
            optional.append("has_cover")
        if "comments" in columns:
            optional.append("comments")
        if "series_index" in columns:
            optional.append("series_index")
        if "last_modified" in columns:
            optional.append("last_modified")
        rows = conn.execute(
            "SELECT id, title, pubdate, path, uuid" +
            (", " + ", ".join(optional) if optional else "") +
            " FROM books ORDER BY id"
        ).fetchall()
        paths = {row[0]: row[3] for row in rows}

        formats: dict[int, dict[str, str]] = defaultdict(dict)
        sizes: dict[int, dict[str, int]] = defaultdict(dict)
        size = "uncompressed_size" if "uncompressed_size" in {
            row[1] for row in conn.execute("PRAGMA table_info(data)")} else "NULL"
        for book, fmt, name, length in conn.execute(f"SELECT book, format, name, {size} FROM data"):
            if book in paths:
                formats[book][fmt.upper()] = str(library / paths[book] / f"{name}.{fmt.lower()}")
                if length:
                    sizes[book][fmt.upper()] = int(length)
    except sqlite3.Error as e:
        raise LibraryError(f"Cannot read {db_path}: {e}") from e
    finally:
        conn.close()

    books = []
    for row in rows:
        bid, title, pubdate, path, uuid = row[:5]
        metadata = dict(zip(optional, row[5:]))
        publisher = publishers.get(bid)
        books.append(Book(
            id=bid,
            title=title or "",
            authors=authors.get(bid, []),
            publisher=None if is_unknown(publisher) else publisher,
            pub_year=_year(pubdate),
            isbns=isbns.get(bid, set()),
            asins=asins.get(bid, set()),
            series=series.get(bid) or None,
            series_index=metadata.get("series_index") if series.get(bid) else None,
            tags=tags.get(bid, set()),
            languages=languages.get(bid, []),
            formats=formats.get(bid, {}),
            sizes=sizes.get(bid, {}),
            has_cover=bool(metadata.get("has_cover", 0)),
            comments=metadata.get("comments") or None,
            uuid=uuid or "",
            last_modified=str(metadata.get("last_modified") or ""),
            path=path,
            library=str(library),
        ))
    return books


def library_tags(library: str | Path) -> list[str]:
    """The tags used by the library's books, sorted; [] if it can't be read."""
    db_path = Path(library, "metadata.db")
    if not db_path.is_file():
        return []
    try:
        conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            names = [row[0] for row in conn.execute(
                "SELECT DISTINCT t.name FROM tags t JOIN books_tags_link l ON l.tag = t.id")]
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return []
    return sorted((n for n in names if n), key=str.casefold)


def has_tag(book: Book, tag: str) -> bool:
    """`tag` (any case) is one of the book's tags; an empty tag matches every book."""
    tag = tag.strip().casefold()
    return not tag or tag in {t.casefold() for t in book.tags}


def tag_selects(book: Book, tag: str, exclude: bool = False) -> bool:
    """The tag filter keeps the book: it has `tag` (with `exclude`, it hasn't); no tag keeps all."""
    return not tag.strip() or has_tag(book, tag) != exclude


def tag_filter_text(tag: str, exclude: bool = False) -> str:
    """The filter as shown to the user: "tagged 'New'", "not tagged 'New'"; "" for none."""
    tag = tag.strip()
    return f"{'not ' if exclude else ''}tagged {tag!r}" if tag else ""


def _year(pubdate: str | None) -> int | None:
    # Calibre stores an undefined date as year 101.
    try:
        year = int((pubdate or "")[:4])
    except ValueError:
        return None
    if year < 1400:
        return None
    try:
        # Stored in UTC; Calibre shows local time, so '1964-12-31 23:00Z' is 1965 in Europe.
        return datetime.fromisoformat(pubdate).astimezone().year
    except (ValueError, OSError, OverflowError):
        return year
