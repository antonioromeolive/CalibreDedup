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


"""What was found in the files of each book (its cover, its formats, their hashes, the
language and length of its text, its fingerprint), kept between runs: a library on a
network drive is not read again, file by file, at each analysis.
An entry holds while the book's last_modified is the same: Calibre changes it when
the book's cover or formats change (not when a file is replaced by hand)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections import defaultdict
from pathlib import Path
from typing import Any

from .extract import same_text_format, unreadable_formats, whole_text
from .library_use import library_hash
from .models import Book
from .same_text import fingerprint

log = logging.getLogger(__name__)


class LibraryCache:
    """One kind of fact ("covers", "files") about each book, one file per library in
    `folder`; None: nothing is kept."""

    VERSION = 1

    def __init__(self, folder: Path | None, kind: str):
        self.folder = folder
        self.kind = kind
        self._books: dict[str, dict[str, list]] = {}  # library -> book path -> [last_modified, value]
        self._seen: dict[str, set[str]] = defaultdict(set)
        self._changed: set[str] = set()

    def _file(self, library: str) -> Path:
        return self.folder / f"{library_hash(library)}.{self.kind}.json"

    def _entries(self, library: str) -> dict[str, list]:
        if library not in self._books:
            entries: dict[str, list] = {}
            try:
                data = json.loads(self._file(library).read_text(encoding="utf-8"))
                if data.get("version") == self.VERSION:
                    entries = data["books"]
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                pass
            self._books[library] = entries
        return self._books[library]

    def get(self, b: Book) -> Any:
        """The value kept for this version of the book, or None."""
        if self.folder is None or not b.last_modified:
            return None
        self._seen[b.library].add(b.path)
        entry = self._entries(b.library).get(b.path)
        return entry[1] if isinstance(entry, list) and len(entry) == 2 and entry[0] == b.last_modified else None

    def put(self, b: Book, value: Any) -> None:
        if self.folder is None or not b.last_modified:
            return
        self._seen[b.library].add(b.path)
        self._entries(b.library)[b.path] = [b.last_modified, value]
        self._changed.add(b.library)

    def save(self, whole: bool = False) -> None:
        """`whole`: every book of the libraries was looked at, so the others (deleted,
        or without what is kept now) are forgotten."""
        if self.folder is None:
            return
        for library, entries in self._books.items():
            if whole:
                gone = entries.keys() - self._seen[library]
                for path in gone:
                    del entries[path]
                if gone:
                    self._changed.add(library)
            if library not in self._changed:
                continue
            target = self._file(library)
            tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
            try:
                self.folder.mkdir(parents=True, exist_ok=True)
                tmp.write_text(json.dumps({"version": self.VERSION, "library": library, "books": entries}),
                               encoding="utf-8")
                os.replace(tmp, target)  # two programs saving at once: one whole file wins
            except OSError:
                log.warning("Could not save the %s cache of %s", self.kind, library, exc_info=True)
                tmp.unlink(missing_ok=True)
        self._changed.clear()


class FileChecks:
    """extract.unreadable_formats of each book, kept: every file is opened to be told
    apart, which takes long on a network drive. Checked again when the book's
    formats are not those checked."""

    def __init__(self, folder: Path | None):
        self.cache = LibraryCache(folder, "files")

    def bad(self, b: Book) -> dict[str, str]:
        names = {fmt: Path(path).name for fmt, path in b.formats.items()}
        known = self.cache.get(b)
        if isinstance(known, dict) and known.get("formats") == names:
            return dict(known["bad"])
        bad = unreadable_formats(b.formats)
        self.cache.put(b, {"formats": names, "bad": bad})
        return bad

    def save(self, whole: bool = False) -> None:
        self.cache.save(whole)


class FileHashes:
    """The SHA-1 of a book's files, kept: read only for books that have a file of the
    same format and size as another (the sizes come from metadata.db), to tell whether
    it is the same file. A file that can't be read has none."""

    def __init__(self, folder: Path | None):
        self.cache = LibraryCache(folder, "hashes")
        self._read: dict[str, str | None] = {}  # path -> digest, this run (also without last_modified)

    def digest(self, b: Book, fmt: str) -> str | None:
        path = b.formats.get(fmt)
        if not path:
            return None
        if path in self._read:
            return self._read[path]
        name, size = Path(path).name, b.sizes.get(fmt)
        known = self.cache.get(b)
        entries = dict(known) if isinstance(known, dict) else {}
        entry = entries.get(name)
        if isinstance(entry, list) and len(entry) == 2 and entry[0] == size:
            digest = entry[1]
        else:
            try:
                h = hashlib.sha1()
                with open(path, "rb") as f:
                    for block in iter(lambda: f.read(1 << 20), b""):
                        h.update(block)
                digest = h.hexdigest()
            except OSError as e:
                log.info("Cannot read %s: %s", path, e)
                digest = None
            if digest is not None:
                entries[name] = [size, digest]
                self.cache.put(b, entries)
        self._read[path] = digest
        return digest

    def save(self) -> None:
        self.cache.save()


class TextFacts:
    """The language and length of each book's text (extract.TextExtractor.text_profile),
    kept: read again when the book's formats change."""

    def __init__(self, folder: Path | None, extractor):
        self.cache = LibraryCache(folder, "text")
        self.extractor = extractor
        self._read: dict[tuple, tuple[str | None, int | None]] = {}  # this run

    def get(self, b: Book) -> tuple[str | None, int | None]:
        names = {fmt: Path(path).name for fmt, path in b.formats.items()}
        key = (b.library, b.path, tuple(sorted(b.formats.items())))
        if key not in self._read:
            known = self.cache.get(b)
            if isinstance(known, dict) and known.get("formats") == names:
                self._read[key] = (known.get("language"), known.get("chars"))
            else:
                language, chars = self.extractor.text_profile(b.formats)
                self.cache.put(b, {"formats": names, "language": language, "chars": chars})
                self._read[key] = (language, chars)
        return self._read[key]

    def save(self) -> None:
        self.cache.save()


class TextPrints:
    """The fingerprint of each book's text (same_text.fingerprint) from its one compared
    format (extract.same_text_format), kept: computed only for books still undecided, and
    a whole text takes seconds to read (a MOBI is converted). `extractor`: a
    TextExtractor, for PDF and the formats Calibre converts; None: EPUB and TXT only.
    A file that can't be read has none (only the AI's reading marks a file unreadable)."""

    def __init__(self, folder: Path | None, extractor=None):
        self.cache = LibraryCache(folder, "prints")
        self.extractor = extractor
        self._read: dict[str, str | None] = {}  # path -> fingerprint, this run

    def get(self, b: Book) -> str | None:
        picked = same_text_format(b.formats)
        if picked is None:
            return None
        fmt, path = picked
        if path in self._read:
            return self._read[path]
        name = Path(path).name
        known = self.cache.get(b)
        if isinstance(known, dict) and known.get("name") == name:
            fp = known.get("print")
        else:
            fp = self._fingerprint(fmt, path)
            self.cache.put(b, {"name": name, "print": fp})
        self._read[path] = fp
        return fp

    def _fingerprint(self, fmt: str, path: str) -> str | None:
        try:
            text = self.extractor.whole_text(fmt, path) if self.extractor is not None else whole_text(fmt, path)
        except Exception as e:  # corrupt, DRM, unsupported...
            log.info("Cannot read the whole text of %s: %s", path, e)
            return None
        return fingerprint(text) if text else None

    def save(self) -> None:
        self.cache.save()
