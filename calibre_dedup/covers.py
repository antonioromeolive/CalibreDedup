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
calibre-review tags its books BAD_COVER_TAG so that a real cover can be found later.
So is a page used as cover (page_cover): a page of the book that Calibre took for its cover."""

from __future__ import annotations

import hashlib
import logging
import threading
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

from .library_cache import LibraryCache
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


def generic_covers(books: list[Book], cancel: threading.Event | None = None,
                   cache_dir: Path | None = None) -> dict[str, int]:
    """The generic covers among these books' cover.jpg: path -> how many books show
    that image. Only files whose size another cover shares are read. A library read
    twice (source and target the same) counts each book once. Every cover is looked at,
    which takes a while on a big library: once `cancel` is set, {} is returned.
    `cache_dir`: where the sizes and hashes found are kept for the next time (see
    library_cache); None: not kept."""
    def cancelled() -> bool:
        return cancel is not None and cancel.is_set()

    keys: dict[str, tuple] = {}  # cover path -> (title key, authors key)
    owner: dict[str, Book] = {}
    for b in books:
        if b.has_cover:
            path = str(cover_file(b))
            keys[path] = (title_key(b.title or ""), authors_key(b.authors))
            owner[path] = b

    def placeholder(paths: list[str]) -> bool:
        return all(len({keys[p][k] for p in paths}) >= GENERIC_COVER_BOOKS for k in (0, 1))

    cache = LibraryCache(cache_dir, "covers")
    whole = False
    try:
        by_size: dict[int, list[str]] = defaultdict(list)
        sizes: dict[str, int] = {}
        digests: dict[str, str] = {}
        for path, b in owner.items():
            if cancelled():
                return {}
            known = cache.get(b)  # [size, hash or ""]
            if known is not None:
                size, digests[path] = known
            else:
                try:
                    size = Path(path).stat().st_size
                except OSError:
                    continue
                cache.put(b, [size, ""])
            sizes[path] = size
            by_size[size].append(path)
        by_hash: dict[str, list[str]] = defaultdict(list)
        for paths in by_size.values():
            if not placeholder(paths):
                continue
            for path in paths:
                if cancelled():
                    return {}
                digest = digests.get(path)
                if not digest:
                    try:
                        digest = hashlib.sha1(Path(path).read_bytes()).hexdigest()
                    except OSError:
                        continue
                    cache.put(owner[path], [sizes[path], digest])
                by_hash[digest].append(path)
        whole = True
    finally:
        cache.save(whole)  # stopped halfway: what was found is kept too
    generic = {p: len(paths) for paths in by_hash.values() if placeholder(paths) for p in paths}
    if generic:
        log.info("%d books have a generic cover (the same image on books of %d or more titles and authors)",
                 len(generic), GENERIC_COVER_BOOKS)
    return generic


def generic_note(count: int) -> str:
    return f"generic cover (the same image on {count} books)"


# A page used as cover: Calibre's first page of a PDF or a document, rendered as the cover.
# Measured on 400 covers of a real library (TODO.md): pages have the exact shape of a sheet
# of paper and are white with no colour; real covers, even plain ones, have other shapes.
PAGE_SHAPES = (1.414, 1.294, 0.707, 0.773)  # height/width: A4, US Letter, upright and as a spread
PAGE_SHAPE_TOLERANCE = 0.02
PAGE_WHITE = 0.75  # at least this share of near-white pixels
PAGE_COLOUR = 0.03  # at most this share of coloured pixels
PAGE_NOTE = "a page used as cover"


def page_cover(path: str | Path) -> bool:
    """Whether the cover image is a page of the book (text, a title page, a blank page)
    rather than a real cover. False when it can't be read."""
    try:
        st = Path(path).stat()
    except OSError:
        return False
    return _page_cover(str(path), st.st_mtime_ns, st.st_size)


@lru_cache(maxsize=4096)
def _page_cover(path: str, mtime_ns: int, size: int) -> bool:  # keyed on mtime/size: covers change
    # Imported here so the module stays usable without Qt.
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage

    image = QImage(path)
    if image.isNull() or not image.width():
        return False
    shape = image.height() / image.width()
    if not any(abs(shape - s) <= PAGE_SHAPE_TOLERANCE * s for s in PAGE_SHAPES):
        return False
    small = image.scaled(96, max(1, round(96 * shape)), Qt.IgnoreAspectRatio,
                         Qt.SmoothTransformation).convertToFormat(QImage.Format_RGB32)
    white = colour = 0
    for y in range(small.height()):
        for x in range(small.width()):
            c = small.pixel(x, y)
            r, g, b = (c >> 16) & 255, (c >> 8) & 255, c & 255
            if max(r, g, b) - min(r, g, b) > 40:
                colour += 1
            if (r * 299 + g * 587 + b * 114) // 1000 > 200:
                white += 1
    n = small.width() * small.height()
    return white >= PAGE_WHITE * n and colour <= PAGE_COLOUR * n
