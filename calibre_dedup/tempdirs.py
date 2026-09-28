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


"""Temporary folders of a run (converted texts, covers, rendered pages, the plan
sent to Calibre), all in %TEMP%/CalibreDedup. Each holds a lock file while its run
lives; a folder whose lock is free was left by a run that was killed or crashed,
and `sweep` deletes it at the next start of either program."""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import BinaryIO

log = logging.getLogger(__name__)

LOCK_NAME = "in_use.lock"
# Before 2026-09-28 the folders were cdr_* straight in %TEMP%, with no lock: deleted
# only when a day old, so a copy of the older version still running keeps its own.
LEGACY_PATTERN = "cdr_*"
LEGACY_MIN_AGE = 24 * 3600
# A folder just created has no lock yet (made, then locked): left alone for a minute.
NEW_FOLDER_GRACE = 60


def base_dir() -> Path:
    return Path(tempfile.gettempdir()) / "CalibreDedup"


def lock(f: BinaryIO) -> None:
    """Lock an open file's first byte, or raise OSError if another process holds it."""
    f.seek(0)
    if f.read(1) == b"":  # a byte to lock
        f.seek(0)
        f.write(b"0")
        f.flush()
    f.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def unlock(f: BinaryIO) -> None:
    f.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


class RunDir:
    """A temporary folder for one run, marked in use until `cleanup` deletes it."""

    def __init__(self, prefix: str):
        base = base_dir()
        base.mkdir(parents=True, exist_ok=True)
        self.path = Path(tempfile.mkdtemp(prefix=prefix, dir=base))
        self.name = str(self.path)
        self._lock: BinaryIO | None = (self.path / LOCK_NAME).open("a+b")
        lock(self._lock)

    def cleanup(self) -> None:
        if self._lock is not None:
            try:
                unlock(self._lock)
            except OSError:
                pass
            self._lock.close()
            self._lock = None
        shutil.rmtree(self.path, ignore_errors=True)

    def __enter__(self) -> Path:
        return self.path

    def __exit__(self, *exc) -> None:
        self.cleanup()


def _in_use(folder: Path) -> bool:
    path = folder / LOCK_NAME
    try:
        if not path.exists():
            return time.time() - folder.stat().st_mtime < NEW_FOLDER_GRACE
        with path.open("a+b") as f:
            lock(f)
            unlock(f)
        return False
    except OSError:  # locked by a running program (or not readable: leave it)
        return True


def sweep() -> int:
    """Delete the temporary folders no running program uses. Returns how many.
    Never fails: a folder that can't be looked at or deleted is left for next time."""
    try:
        return _sweep()
    except OSError as e:
        log.warning("Could not clean up temporary folders: %s", e)
        return 0


def _sweep() -> int:
    removed = 0
    base = base_dir()
    folders = [d for d in base.iterdir() if d.is_dir()] if base.is_dir() else []
    for folder in folders:
        if not _in_use(folder):
            shutil.rmtree(folder, ignore_errors=True)
            removed += not folder.exists()
    now = time.time()
    for folder in Path(tempfile.gettempdir()).glob(LEGACY_PATTERN):
        try:
            old = folder.is_dir() and now - folder.stat().st_mtime > LEGACY_MIN_AGE
        except OSError:
            continue
        if old:
            shutil.rmtree(folder, ignore_errors=True)
            removed += not folder.exists()
    if removed:
        log.info("Removed %d temporary folders left by earlier runs", removed)
    return removed
