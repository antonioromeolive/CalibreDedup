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

"""Keep Windows awake while a run works: an unattended analysis or execution must
not be paused halfway by the idle sleep timer. Only the idle timer is held off:
the screen may still turn off, and sleeping by hand (lid, power button, Start menu)
still works. When the run ends, the idle timer starts again from zero."""

from __future__ import annotations

import contextlib
import logging
import os
from typing import Iterator

log = logging.getLogger(__name__)

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def _set_state(flags: int) -> bool:
    if os.name != "nt":
        return False
    import ctypes
    return bool(ctypes.windll.kernel32.SetThreadExecutionState(flags))


@contextlib.contextmanager
def keep_awake() -> Iterator[None]:
    """No idle sleep while the block runs. The request belongs to the calling thread:
    Windows drops it too if the thread ends without leaving the block."""
    held = False
    try:
        held = _set_state(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        if held:
            log.debug("Idle sleep held off until the run ends")
    except Exception:  # never let power management stop a run
        log.warning("Could not keep the computer awake: it may sleep during the run", exc_info=True)
    try:
        yield
    finally:
        if held:
            try:
                _set_state(ES_CONTINUOUS)
                log.debug("Idle sleep allowed again")
            except Exception:
                log.warning("Could not release the keep-awake request", exc_info=True)
