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

"""The way back after an Execute, in both programs: before it, a copy of the metadata.db
of each library it writes (the last KEEP_SNAPSHOTS per library, in the data folder);
while it runs, a journal of what was done to each book and why (one CSV per Execute):
why a book is in the trash library, which book it duplicated, a value before and after."""

from __future__ import annotations

import csv
import hashlib
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from .config import config_dir

log = logging.getLogger(__name__)

SNAPSHOTS_FOLDER = "snapshots"
JOURNAL_FOLDER = "journal"
KEEP_SNAPSHOTS = 3  # per library


def snapshot(library: str, folder: Path | None = None, keep: int = KEEP_SNAPSHOTS) -> Path:
    """Copy the library's metadata.db (SQLite's own backup: a consistent copy) to
    snapshots/<library>_<id>/metadata_<date>.db in the data folder, and delete the copies
    older than the last `keep`. Returns the copy. Copying it back over metadata.db, with
    Calibre closed, puts every record as it was before that Execute (the files moved or
    trashed since are in the target, the trash library or Calibre's recycle bin)."""
    lib = Path(library).resolve()
    tag = hashlib.sha1(str(lib).casefold().encode()).hexdigest()[:8]
    where = (folder or config_dir() / SNAPSHOTS_FOLDER) / f"{lib.name or 'library'}_{tag}"
    where.mkdir(parents=True, exist_ok=True)
    (where / "library.txt").write_text(f"{lib}\n", encoding="utf-8")
    dest = where / f"metadata_{datetime.now():%Y%m%d_%H%M%S}.db"
    src = sqlite3.connect(f"{(lib / 'metadata.db').as_uri()}?mode=ro", uri=True)
    try:
        out = sqlite3.connect(dest)
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()
    for old in sorted(where.glob("metadata_*.db"))[:-keep]:
        old.unlink(missing_ok=True)
    log.info("Copy of %s saved as %s", lib / "metadata.db", dest)
    return dest


JOURNAL_FIELDS = ["time", "library", "book_id", "title", "authors", "action", "ok", "result", "why", "match",
                  "before", "after"]


class Journal:
    """journal/<program>_<date>.csv in the data folder: a row for each book an Execute
    acted on (not the books only tagged). Created before the Execute starts, so a folder
    that can't be written stops it; a row that can't be written later is only logged.
    Deleted at the end when no book was acted on."""

    def __init__(self, program: str, folder: Path | None = None):
        where = folder or config_dir() / JOURNAL_FOLDER
        where.mkdir(parents=True, exist_ok=True)
        self.path = where / f"{program}_{datetime.now():%Y%m%d_%H%M%S}.csv"
        self._file = open(self.path, "a", newline="", encoding="utf-8-sig")
        self._writer = csv.writer(self._file)
        self._writer.writerow(JOURNAL_FIELDS)
        self._file.flush()
        self.rows = 0

    def write(self, **row) -> None:
        try:
            self._writer.writerow([datetime.now().isoformat(timespec="seconds") if f == "time" else row.get(f, "")
                                   for f in JOURNAL_FIELDS])
            self._file.flush()
            self.rows += 1
        except (OSError, ValueError) as e:
            log.warning("Journal %s: %s", self.path, e)

    def close(self) -> None:
        self._file.close()
        if not self.rows:
            self.path.unlink(missing_ok=True)

    def __enter__(self) -> "Journal":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
