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


"""Which libraries the running copies of the programs use:
- LibraryUse: no library is analyzed by one while another uses it as its trash
  library. Several may share a trash library: it is written only when executing.
- ExecutionLock: no library is written by two executions at once (Calibre expects
  one writer per library). Executions on different libraries run side by side.

Each program holds a locked marker file per library in ~/.CalibreDedup/libraries_in_use/:
<library hash>.<role>.<program>.<id>[.<library role>].lock. A marker whose lock is free
was left by a program that was killed or crashed (the system frees its locks) and is
deleted."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .config import config_dir
from .tempdirs import lock, unlock

ANALYZED, TRASH, EXECUTING = "analyzed", "trash", "exec"
_CONFLICTS = {ANALYZED: TRASH, TRASH: ANALYZED}
PROGRAM_NAMES = {"dedup": "Calibre Merge and Dedup", "review": "calibre-review"}


class LibraryInUse(Exception):
    pass


def _folder() -> Path:
    return config_dir() / "libraries_in_use"


def same_library(a: str, b: str) -> bool:
    return a == b or (bool(a) and bool(b) and _library_hash(a) == _library_hash(b))


def _library_hash(library: str) -> str:
    return hashlib.sha1(str(Path(library).resolve()).casefold().encode()).hexdigest()[:16]


def _held_by_other(marker: Path) -> bool:
    """True if a running program holds this marker. A free one is deleted."""
    try:
        with marker.open("a+b") as f:
            lock(f)
            unlock(f)
    except OSError:
        return True
    try:
        marker.unlink()
    except OSError:
        pass
    return False


def _release(held: list[tuple[Path, BinaryIO]]) -> None:
    """Unlock and delete the markers, emptying the list."""
    while held:
        marker, f = held.pop()
        try:
            unlock(f)
        except OSError:
            pass
        f.close()
        try:
            marker.unlink()
        except OSError:
            pass


class LibraryUse:
    """The markers of one analysis. `claim` takes them or raises LibraryInUse."""

    def __init__(self, program: str):
        self.program = program
        self._id = uuid.uuid4().hex
        self._held: list[tuple[Path, BinaryIO]] = []

    def claim(self, analyzed: list[str], trash: str = "") -> None:
        """Mark `analyzed` and `trash` (may be empty) as used by this program, then check
        that no other program uses one of them in the conflicting role. Marking first:
        of two programs starting together, at least one sees the other."""
        roles = [(lib, ANALYZED) for lib in dict.fromkeys(analyzed) if lib] + ([(trash, TRASH)] if trash else [])
        folder = _folder()
        folder.mkdir(parents=True, exist_ok=True)
        for marker in folder.glob("*.lock"):  # those left by crashed programs go
            if self._id not in marker.name:
                _held_by_other(marker)
        try:
            for library, role in roles:
                marker = folder / f"{_library_hash(library)}.{role}.{self.program}.{self._id}.lock"
                f = marker.open("a+b")
                lock(f)
                self._held.append((marker, f))
            for library, role in roles:
                other = _CONFLICTS[role]
                for marker in folder.glob(f"{_library_hash(library)}.{other}.*.lock"):
                    if self._id in marker.name or not _held_by_other(marker):
                        continue
                    program = PROGRAM_NAMES.get(marker.name.split(".")[2], "another program")
                    if role == TRASH:
                        raise LibraryInUse(f"The trash library {library} is being analyzed by {program}: "
                                           "choose another trash library, or wait until that program has "
                                           "finished with it (or is closed).")
                    raise LibraryInUse(f"{library} is the trash library of {program}: it can't be analyzed "
                                       "until that program has finished with it (or is closed).")
        except BaseException:
            self.release()
            raise

    def release(self) -> None:
        _release(self._held)


def role_name(program: str, role: str) -> str:
    """How a program calls a library by its role ("source", "target", "trash")."""
    return "library to review" if program == "review" and role == "source" else f"{role} library"


@dataclass
class Conflict:
    role: str  # this program's role for the library
    library: str
    program: str  # the program writing it
    other_role: str  # and its role there

    def describe(self, program: str) -> str:
        other = PROGRAM_NAMES.get(self.program, "another program")
        return (f"The {role_name(program, self.role)} {self.library} is being written by {other} "
                f"(its {role_name(self.program, self.other_role)}).")


class ExecutionLock:
    """The libraries one execution writes, each locked while it runs."""

    def __init__(self, program: str):
        self.program = program
        self._id = uuid.uuid4().hex
        self._held: list[tuple[Path, BinaryIO]] = []

    def acquire(self, libraries: dict[str, str]) -> list[Conflict]:
        """Lock each library (role -> path; empty ones are skipped). If another
        execution writes one, nothing is kept and the conflicts are returned. Locking
        first, then looking: of two starting together, at least one sees the other."""
        seen: set[str] = set()
        roles: list[tuple[str, str, str]] = []  # (role, library, hash): a library once
        for role, library in libraries.items():
            h = _library_hash(library) if library else ""
            if h and h not in seen:
                seen.add(h)
                roles.append((role, library, h))
        folder = _folder()
        folder.mkdir(parents=True, exist_ok=True)
        conflicts: list[Conflict] = []
        try:
            for role, _, h in roles:
                marker = folder / f"{h}.{EXECUTING}.{self.program}.{self._id}.{role}.lock"
                f = marker.open("a+b")
                lock(f)
                self._held.append((marker, f))
            for role, library, h in roles:
                for marker in folder.glob(f"{h}.{EXECUTING}.*.lock"):
                    if self._id in marker.name or not _held_by_other(marker):
                        continue
                    parts = marker.name.split(".")
                    conflicts.append(Conflict(role, library, parts[2], parts[4] if len(parts) > 5 else "?"))
        except BaseException:
            self.release()
            raise
        if conflicts:
            self.release()
        return conflicts

    def release(self) -> None:
        _release(self._held)


def execution_conflicts(program: str, libraries: dict[str, str]) -> list[Conflict]:
    """What would stop an execution now (see ExecutionLock), without keeping the locks."""
    check = ExecutionLock(program)
    conflicts = check.acquire(libraries)
    check.release()
    return conflicts
