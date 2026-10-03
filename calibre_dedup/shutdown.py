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

"""Shut the computer down when the work is done, aware of the other windows running
(Merge and Dedup, Metadata Review, any number of each).

"Shut down when done" is one switch for every window (shutdown.on): ticked in one, it is
ticked in all. Each window registers itself in ~/.CalibreDedup/instances/: a locked marker
(a window killed or crashed frees its lock, and is forgotten) and its state: busy or not
(and with what), whether it should shut down when done, when its last run ended. A window
whose run ended with the switch on waits while any other window is busy. When none is busy, the last one to finish
(the latest end) shuts down: it asks the other windows to close (shutdown.request; idle
windows close themselves), then shuts Windows down. Never while something runs."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import BinaryIO

from .calibre_env import CREATE_NO_WINDOW
from .config import config_dir
from .library_use import PROGRAM_NAMES
from .tempdirs import lock, unlock

log = logging.getLogger(__name__)
REQUEST = "shutdown.request"
ARMED = "shutdown.on"  # "shut down when done" is on: one switch for every window
REQUEST_MAX_AGE = 600  # seconds: an older request (a shutdown cancelled by Windows) is ignored


def _folder() -> Path:
    return config_dir() / "instances"


@dataclass
class State:
    program: str
    busy: bool = False
    activity: str = ""  # what it is doing, e.g. "analyzing"
    shutdown: bool = False  # shut down when done (ticked)
    ended: float = 0.0  # when its last run ended with the box ticked (time.time()); 0: not yet
    id: str = ""

    def describe(self) -> str:
        name = PROGRAM_NAMES.get(self.program, self.program)
        return f"{name} ({self.activity})" if self.activity else name


class Instance:
    """This window, as the other running windows see it."""

    def __init__(self, program: str, folder: Path | None = None):
        self.folder = folder or _folder()
        self.folder.mkdir(parents=True, exist_ok=True)
        self.state = State(program, id=uuid.uuid4().hex)
        self._marker = self.folder / f"{self.state.id}.lock"
        self._lock: BinaryIO | None = self._marker.open("a+b")
        lock(self._lock)
        self._write()
        if not self.others():  # the first window: what an earlier session left means nothing now
            self.set_armed(False)
            self.clear_request()

    @property
    def id(self) -> str:
        return self.state.id

    def _write(self) -> None:
        path = self.folder / f"{self.id}.json"
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps(asdict(self.state)), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as e:
            log.warning("Cannot write %s: %s", path, e)

    def update(self, **changes) -> None:
        new = {k: v for k, v in changes.items() if getattr(self.state, k) != v}
        if new:
            for k, v in new.items():
                setattr(self.state, k, v)
            self._write()

    def others(self) -> list[State]:
        """The other windows running now. Those killed or crashed are forgotten."""
        found = []
        for marker in self.folder.glob("*.lock"):
            other = marker.stem
            if other == self.id:
                continue
            state_file = self.folder / f"{other}.json"
            if not _held(marker):
                for f in (marker, state_file):
                    try:
                        f.unlink()
                    except OSError:
                        pass
                continue
            try:
                found.append(State(**json.loads(state_file.read_text(encoding="utf-8"))))
            except (OSError, ValueError, TypeError):
                pass  # just starting: its state comes next time
        return found

    def close(self) -> None:
        if self._lock is None:
            return
        try:
            unlock(self._lock)
        except OSError:
            pass
        self._lock.close()
        self._lock = None
        for f in (self._marker, self.folder / f"{self.id}.json"):
            try:
                f.unlink()
            except OSError:
                pass

    # --- "shut down when done": one switch for every window --------------------------------
    def armed(self) -> bool:
        """Whether "shut down when done" is on (in any window: it is one for all)."""
        return (self.folder / ARMED).is_file()

    def set_armed(self, on: bool) -> None:
        path = self.folder / ARMED
        try:
            if on:
                path.write_text(json.dumps({"by": self.id, "at": time.time()}), encoding="utf-8")
            else:
                path.unlink(missing_ok=True)
        except OSError as e:
            log.warning("Cannot change %s: %s", path, e)

    # --- the shutdown request ---------------------------------------------------------
    def request_shutdown(self) -> None:
        """Tell the other windows to close: the computer shuts down."""
        (self.folder / REQUEST).write_text(json.dumps({"by": self.id, "at": time.time()}), encoding="utf-8")

    def shutdown_requested(self) -> bool:
        """Another window asked every window to close (a recent request)."""
        try:
            data = json.loads((self.folder / REQUEST).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return data.get("by") != self.id and time.time() - float(data.get("at", 0)) < REQUEST_MAX_AGE

    def clear_request(self) -> None:
        try:
            (self.folder / REQUEST).unlink()
        except OSError:
            pass


def _held(marker: Path) -> bool:
    """True if a running window holds this marker."""
    try:
        with marker.open("a+b") as f:
            lock(f)
            unlock(f)
    except OSError:
        return True
    return False


WAIT, NOTHING, SHUT_DOWN, OTHER_SHUTS_DOWN = "wait", "nothing", "shut down", "other"


def decide(own: State, others: list[State]) -> tuple[str, list[State]]:
    """What a window does about shutting down: NOTHING (not asked, or still busy), WAIT for
    the busy other windows (returned), SHUT_DOWN (the last one to finish: none busy), or
    OTHER_SHUTS_DOWN (another window finished later and will do it)."""
    if not own.shutdown or own.busy or not own.ended:
        return NOTHING, []
    busy = [o for o in others if o.busy]
    if busy:
        return WAIT, busy
    later = [o for o in others if o.shutdown and o.ended and (o.ended, o.id) > (own.ended, own.id)]
    return (OTHER_SHUTS_DOWN, later) if later else (SHUT_DOWN, [])


def shut_down_computer() -> None:
    """Shut Windows down now (applications with unsaved work may still ask)."""
    if sys.platform != "win32":
        log.error("Shutting down is only supported on Windows")
        return
    log.info("Shutting the computer down: the work is done")
    subprocess.Popen(["shutdown", "/s", "/t", "0"], creationflags=CREATE_NO_WINDOW)
