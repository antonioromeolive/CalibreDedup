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


"""Books stored as an archive (RAR, ZIP, 7Z) holding the real files.

During the analysis (read only) the archive's contents are listed and, if they are
clear, unpacked if the user said so: asked once, before the first book, for all the
books (see ask_once). Yes: the files are extracted into the run's temporary folder and the book is analyzed with them instead of the archive.
Nothing is written then: on Execute the archive is extracted again, the formats the
book doesn't have are added (exactly as they are), the whole record is copied to the
trash library as it is, and the archive is removed from the book.

Unclear archives are left untouched and flagged: two files of the same format (maybe
different books), nothing Calibre can read, an archive inside, password, damage."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Callable

from .ai import AICache
from .extract import CALIBRE_INPUT_FORMATS
from .models import Book

log = logging.getLogger(__name__)

ARCHIVE_FORMATS = ("RAR", "ZIP", "7Z")
# The e-book formats added from an archive: the ones Calibre reads, not loose web pages.
BOOK_FORMATS = CALIBRE_INPUT_FORMATS - set(ARCHIVE_FORMATS) - {
    "HTM", "HTML", "XHTM", "XHTML", "SHTM", "SHTML", "OPF", "RECIPE"}
# A TXT file this small is a note (a plot summary, "trama.txt"), not the book.
NOTE_TXT_MAX = 20_000


@dataclass
class Unpack:
    """One archive of a book: what it holds and what Execute would do with it."""
    format: str  # RAR, ZIP, 7Z
    path: str
    size: int  # the archive's size and date: Execute refuses an archive changed since
    mtime: int
    add: dict[str, str] = field(default_factory=dict)  # format -> file inside, added on Execute
    sizes: dict[str, int] = field(default_factory=dict)  # format -> its size, checked on Execute
    present: list[str] = field(default_factory=list)  # formats inside that the book already has
    ignored: list[str] = field(default_factory=list)  # files not added (DOC, pictures, notes…)
    problem: str = ""  # why it is left untouched ("" when clear)
    unpack: bool = False  # Execute unpacks it (the answer to the question; right-click changes it)
    planned: bool = False  # the answer given during the analysis
    asked: bool = False

    @property
    def note(self) -> str:
        """For the reason column."""
        if self.problem:
            return f"{self.format} not unpacked: {self.problem}"
        holds = ", ".join(sorted(self.add)) or "nothing new"
        if not self.asked:
            return f"{self.format} holds {holds}"
        if not self.planned:
            return f"{self.format} not unpacked (your choice): holds {holds}"
        text = f"{self.format} unpacked for the analysis: adds {holds}"
        if self.present:
            text += f" ({', '.join(sorted(self.present))} already present, skipped)"
        return text

    @property
    def summary(self) -> str:
        """For the question: what is inside and what would happen."""
        lines = [f"Adds: {', '.join(sorted(self.add)) or 'nothing new'}"]
        if self.present:
            lines.append(f"Already in the book (skipped): {', '.join(sorted(self.present))}")
        if self.ignored:
            lines.append(f"Not added: {', '.join(self.ignored)}")
        lines.append(f"The {self.format} file then goes to the trash library, inside a copy of the whole record.")
        return "\n".join(lines)

    def spec(self, remove: bool) -> dict:
        """What the bridge needs on Execute. `remove`: take the archive out of the book
        (not when the whole book goes to the trash anyway)."""
        return {"format": self.format, "size": self.size, "mtime": self.mtime, "add": dict(self.add),
                "sizes": dict(self.sizes), "remove": remove}


def plan_unpack(fmt: str, path: str, members: list[dict], book_formats) -> Unpack:
    """What unpacking this archive would do, from its files (see archive_tool.members)."""
    st = os.stat(path)
    u = Unpack(fmt, path, st.st_size, int(st.st_mtime))
    found: dict[str, list[dict]] = {}
    for m in members:
        name = PurePosixPath(m["name"].replace("\\", "/")).name
        ext = os.path.splitext(name)[1].lstrip(".").upper()
        if ext in ARCHIVE_FORMATS:
            u.problem = f"an archive inside ({name})"
            return u
        if ext == "TXT" and m["size"] < NOTE_TXT_MAX:
            u.ignored.append(name)  # a note
        elif ext in BOOK_FORMATS:
            found.setdefault(ext, []).append(m)
        else:
            u.ignored.append(name)
    twice = sorted(f for f, ms in found.items() if len(ms) > 1)
    if twice:
        u.problem = f"{len(found[twice[0]])} {twice[0]} files (different books?)"
    elif not found:
        u.problem = "nothing Calibre can read inside"
    for f, (m, *_) in sorted(found.items()):
        if f in book_formats:
            u.present.append(f)
        else:
            u.add[f], u.sizes[f] = m["name"], m["size"]
    return u


def ask_once(books: list[Book], ask: Callable[[int], bool] | None) -> Callable[[Book, Unpack], bool] | None:
    """The answer for every book's clear archive, from one question asked before the
    analysis: `ask(n)`, n = the books listing an archive format (read from the library,
    no file opened). None: no question (no `ask`, or no such book)."""
    n = sum(1 for b in books if any(f in ARCHIVE_FORMATS for f in b.formats))
    if ask is None or not n:
        return None
    yes = bool(ask(n))
    log.info("%d books stored as an archive: %s", n, "unpack" if yes else "keep the archives")
    return lambda book, archive: yes


def prepare(book: Book, extractor, ask: Callable[[Book, Unpack], bool]) -> tuple[Book, list[Unpack]]:
    """The book as the analysis sees it, and its archives. For each clear archive
    `ask` decides; yes: its files are extracted and used instead of the archive."""
    archives: list[Unpack] = []
    formats = dict(book.formats)
    for fmt, path in book.formats.items():
        if fmt not in ARCHIVE_FORMATS or not Path(path).is_file():
            continue
        try:
            u = plan_unpack(fmt, path, extractor.archive_members(fmt, path), book.formats)
        except Exception as e:  # damaged, password, not really an archive
            st = Path(path).stat()
            u = Unpack(fmt, path, st.st_size, int(st.st_mtime), problem=f"can't be opened ({_short(e)})")
        archives.append(u)
        if u.problem:
            log.info("%s: %s", book.label(), u.note)
            continue
        u.asked = True
        u.unpack = u.planned = bool(ask(book, u))
        if not u.unpack:
            continue
        try:
            extracted = extractor.extract_archive(fmt, path, u)
        except Exception as e:
            u.unpack = u.planned = False
            u.problem = f"extraction failed ({_short(e)})"
            log.warning("%s: %s", book.label(), u.note)
            continue
        formats.pop(fmt, None)
        formats.update({f: p for f, p in extracted.items() if f not in formats})
        for f, p in extracted.items():
            AICache.alias(p, f"{path}::{u.add[f]}")
        log.info("%s: %s", book.label(), u.note)
    return (replace(book, formats=formats) if formats != book.formats else book), archives


def _short(e: Exception) -> str:
    lines = [line.strip() for line in str(e).splitlines() if line.strip()]
    return (lines[-1] if lines else type(e).__name__)[:150]
