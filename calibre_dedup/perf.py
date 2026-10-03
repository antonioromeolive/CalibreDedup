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


"""Performance log: one JSON object per line in its own file, to compare AI
providers (calls, tokens, time). Holds no book metadata: a book is only its
number in the run, and texts and images only their sizes.

Events: "run_start" / "run_end" around an analysis or a review scan, "pause" /
"resume" when the user pauses it (the run's "seconds" leave the time paused out, given
apart as "paused_seconds"), and "call" for each AI request (the connection test's too, with "book": null). A run belongs to
the thread that started it, so that two can run at once (a review scan and the AI
asked again about some books): each call counts in its own thread's run, and names
its program."""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler

from .config import config_dir
from .version import app_version

log = logging.getLogger("calibre_dedup.perf")
log.setLevel(logging.INFO)
log.propagate = False  # never in the main log or the window

_lock = threading.Lock()
# thread id -> the run in progress, with "book": the book being worked on (its number in the run)
_runs: dict[int, dict] = {}


def configure(filename: str) -> None:
    """Write the performance log to this file of the data folder (once per process)."""
    if log.handlers:
        return
    handler = RotatingFileHandler(config_dir() / filename, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(handler)


def _write(event: str, **fields) -> None:
    if log.handlers:
        log.info(json.dumps({"ts": datetime.now().isoformat(timespec="milliseconds"), "event": event, **fields}))


def _describe(provider) -> dict | None:
    p = getattr(provider, "profile", None)
    if p is None:
        return None
    return {"provider": p.kind, "model": p.model or "(default)",
            "num_ctx": p.num_ctx if p.kind == "ollama" else None}


def run_start(program: str, books: int, text_provider=None, image_provider=None) -> None:
    """A run of `program` ("dedup", "review", or "review-ask": the AI asked again
    about some books) over `books` books begins."""
    with _lock:
        if threading.get_ident() in _runs:  # the last one ended with an exception
            _end_locked(done=None, stopped=True, aborted=True)
        _runs[threading.get_ident()] = {
            "program": program, "books": books, "started": time.monotonic(), "book": None, "calls": 0, "ok": 0,
            "paused": 0.0, "paused_at": None,
            "ai_seconds": 0.0, "books_with_ai": set(), "input_tokens": 0, "output_tokens": 0,
            "reasoning_tokens": 0}
    _write("run_start", program=program, version=app_version(), books=books,
           text_ai=_describe(text_provider), image_ai=_describe(image_provider))


def book(n: int) -> None:
    """The following AI calls (of this thread) are for the run's `n`-th book."""
    with _lock:
        r = _runs.get(threading.get_ident())
        if r is not None:
            r["book"] = n


def pause() -> None:
    """This thread's run waits for the user (Pause): until resume(), its time isn't counted."""
    with _lock:
        r = _runs.get(threading.get_ident())
        if r is None or r["paused_at"] is not None:
            return
        r["paused_at"] = time.monotonic()
        program, n = r["program"], r["book"]
    _write("pause", program=program, book=n)


def resume() -> None:
    with _lock:
        r = _runs.get(threading.get_ident())
        if r is None or r["paused_at"] is None:
            return
        seconds = time.monotonic() - r["paused_at"]
        r["paused"] += seconds
        r["paused_at"] = None
        program, n = r["program"], r["book"]
    _write("resume", program=program, book=n, paused_seconds=round(seconds, 3))


def run_end(done: int, stopped: bool = False) -> None:
    with _lock:
        _end_locked(done, stopped)


def _end_locked(done: int | None, stopped: bool, aborted: bool = False) -> None:
    r = _runs.pop(threading.get_ident(), None)
    if r is None:
        return
    now = time.monotonic()
    paused = r["paused"] + (now - r["paused_at"] if r["paused_at"] is not None else 0.0)
    _write("run_end", program=r["program"], books=r["books"], books_done=done, stopped=stopped,
           aborted=aborted, seconds=round(now - r["started"] - paused, 3), paused_seconds=round(paused, 3),
           books_with_ai=len(r["books_with_ai"]), calls=r["calls"], calls_ok=r["ok"],
           ai_seconds=round(r["ai_seconds"], 3), input_tokens=r["input_tokens"],
           output_tokens=r["output_tokens"], reasoning_tokens=r["reasoning_tokens"])


def call(provider, seconds: float, system: str, user: str, images: list[str] | None,
         info: dict, error: Exception | None = None) -> None:
    """One AI request: what was sent (sizes only), how long it took, what the server reported."""
    if error is None:
        outcome = "ok"
    elif getattr(error, "filtered", False):
        outcome = "filtered"
    elif getattr(error, "too_long", False):
        outcome = "too_long"
    else:
        outcome = "error"
    with _lock:
        r = _runs.get(threading.get_ident())
        n = r["book"] if r is not None else None
        if r is not None:
            r["calls"] += 1
            r["ok"] += outcome == "ok"
            r["ai_seconds"] += seconds
            if n is not None:
                r["books_with_ai"].add(n)
            for k in ("input_tokens", "output_tokens", "reasoning_tokens"):
                r[k] += info.get(k) or 0
    p = provider.profile
    fields = {"program": r["program"] if r is not None else None, "book": n, "provider": p.kind, "model": p.model or "(default)", "outcome": outcome,
              "http_status": getattr(error, "status", None), "seconds": round(seconds, 3),
              "system_chars": len(system), "text_chars": len(user), "images": len(images or []),
              # base64 -> bytes
              "image_bytes": sum(len(i) * 3 // 4 for i in images or [])}
    fields.update({k: v for k, v in info.items() if k in _SERVER_FIELDS and v is not None})
    _write("call", **fields)


# What the servers report, as Provider.last_info names it.
_SERVER_FIELDS = ("finish", "input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens",
                  "thinking_chars", "load_seconds", "prompt_seconds", "eval_seconds", "server_seconds")
