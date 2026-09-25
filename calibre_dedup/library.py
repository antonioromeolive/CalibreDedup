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
from .normalize import is_unknown, normalize_isbn


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

        isbns: dict[int, set[str]] = defaultdict(set)
        asins: dict[int, set[str]] = defaultdict(set)
        for book, kind, val in conn.execute("SELECT book, type, val FROM identifiers"):
            kind = (kind or "").lower()
            if kind == "isbn":
                isbn = normalize_isbn(val)
                if isbn:
                    isbns[book].add(isbn)
            elif (kind == "mobi-asin" or kind.startswith("amazon")) and val and val.strip():
                asins[book].add(val.strip().upper())

        columns = {row[1] for row in conn.execute("PRAGMA table_info(books)")}
        optional = []
        if "has_cover" in columns:
            optional.append("has_cover")
        if "comments" in columns:
            optional.append("comments")
        rows = conn.execute(
            "SELECT id, title, pubdate, path, uuid" +
            (", " + ", ".join(optional) if optional else "") +
            " FROM books ORDER BY id"
        ).fetchall()
        paths = {row[0]: row[3] for row in rows}

        formats: dict[int, dict[str, str]] = defaultdict(dict)
        for book, fmt, name in conn.execute("SELECT book, format, name FROM data"):
            if book in paths:
                formats[book][fmt.upper()] = str(library / paths[book] / f"{name}.{fmt.lower()}")
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
            formats=formats.get(bid, {}),
            has_cover=bool(metadata.get("has_cover", 0)),
            comments=metadata.get("comments") or None,
            uuid=uuid or "",
            path=path,
            library=str(library),
        ))
    return books


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
