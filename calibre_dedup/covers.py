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

"""Generic covers: the same image on books of different titles and authors, e.g. the "Microsoft
Word 2000" logo a converter took from a document, or a publisher's stock picture.
Such a cover says nothing about the book: it is never proof of a duplicate, and
calibre-review tags its books BAD_COVER_TAG so that a real cover can be found later."""

from __future__ import annotations

import hashlib
import logging
from collections import defaultdict
from pathlib import Path

from .models import Book
from .normalize import authors_key, title_key

log = logging.getLogger(__name__)

# An image on books of this many different titles AND authors is a placeholder. Authors
# too: copies of one book with messy titles ("Il vecchio e il mare", "ITABOOK 0052 -
# Hemingway") share their real cover, and so do the volumes of a series.
GENERIC_COVER_BOOKS = 3
BAD_COVER_TAG = "BadCover"


def cover_file(book: Book) -> Path:
    return Path(book.library, book.path, "cover.jpg")


def generic_covers(books: list[Book]) -> dict[str, int]:
    """The generic covers among these books' cover.jpg: path -> how many books show
    that image. Only files whose size another cover shares are read. A library read
    twice (source and target the same) counts each book once."""
    keys: dict[str, tuple] = {}  # cover path -> (title key, authors key)
    for b in books:
        if b.has_cover:
            keys[str(cover_file(b))] = (title_key(b.title or ""), authors_key(b.authors))

    def placeholder(paths: list[str]) -> bool:
        return all(len({keys[p][k] for p in paths}) >= GENERIC_COVER_BOOKS for k in (0, 1))

    by_size: dict[int, list[str]] = defaultdict(list)
    for path in keys:
        try:
            by_size[Path(path).stat().st_size].append(path)
        except OSError:
            pass
    by_hash: dict[str, list[str]] = defaultdict(list)
    for paths in by_size.values():
        if not placeholder(paths):
            continue
        for path in paths:
            try:
                by_hash[hashlib.sha1(Path(path).read_bytes()).hexdigest()].append(path)
            except OSError:
                pass
    generic = {p: len(paths) for paths in by_hash.values() if placeholder(paths) for p in paths}
    if generic:
        log.info("%d books have a generic cover (the same image on books of %d or more titles and authors)",
                 len(generic), GENERIC_COVER_BOOKS)
    return generic


def generic_note(count: int) -> str:
    return f"generic cover (the same image on {count} books)"
