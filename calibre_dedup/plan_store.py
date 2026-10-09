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

"""The plan shown in Merge and Dedup, kept on disk (plans/ in the data folder) so that it
can be continued another day without analyzing again: its rows, what the user ticked and
changed, and what was executed (the executed books have left it).

A saved plan is only good for the libraries it was made from as they are now. The plan
holds their state (libraries_state: every book's id and last_modified) as the analysis
read them, brought up to date by each Execute (which writes them); anything else that
changes them (a book added, edited in Calibre) makes the plan stale: it is then not
offered, and the libraries are analyzed again. One file per source/target pair and tag
filter (selection.pair_key), replaced by each analysis of it."""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import sqlite3
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .archives import Unpack
from .config import config_dir
from .models import Action, Book, Identity, Plan, PlanItem
from .selection import pair_key

log = logging.getLogger(__name__)

PLAN_FORMAT = 1  # change when a saved plan can no longer be read back as it is
PLANS_FOLDER = "plans"
KEEP_PLANS = 10  # of different libraries: the oldest are deleted


def _folder() -> Path:
    return config_dir() / PLANS_FOLDER


# --- the libraries' state ---------------------------------------------------------
def library_state(library: str) -> dict:
    """How many books the library has and a hash of their ids and last_modified: any book
    added, removed or changed (metadata, formats, cover) changes it. Read-only, from
    metadata.db alone (a tenth of a second for 80,000 books). An empty folder is an empty
    library. Raises OSError or sqlite3.Error if the library can't be read."""
    db = Path(library, "metadata.db")
    if not db.is_file():
        if Path(library).is_dir() and not any(Path(library).iterdir()):
            return {"books": 0, "hash": ""}
        raise OSError(f"{library} is not a Calibre library (no metadata.db)")
    conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT id, last_modified FROM books ORDER BY id").fetchall()
    finally:
        conn.close()
    return {"books": len(rows), "hash": hashlib.sha1(repr(rows).encode()).hexdigest()}


def libraries_state(source: str, target: str) -> dict:
    """The state of both libraries of an analysis (see library_state); one library: read once."""
    state = library_state(source)
    same = str(Path(source).resolve()).casefold() == str(Path(target).resolve()).casefold()
    return {"source": state, "target": state if same else library_state(target)}


def analysis_state(source: str, target: str) -> dict | None:
    """libraries_state, read before an analysis; None if it can't be (the analysis goes
    on, its plan just can't be continued another day)."""
    try:
        return libraries_state(source, target)
    except (OSError, sqlite3.Error) as e:
        log.warning("Could not read the state of the libraries: %s", e)
        return None


def refresh_state(plan: Plan, before: bool) -> None:
    """Around an Execute. `before`: the libraries must still be as the plan has them, else
    something else changed them since and the plan can't be continued another day
    (plan.state = None; the Execute itself goes on: each book is checked anyway). After:
    the state the Execute left them in, the plan having been updated with what it did."""
    if plan.state is None:
        return
    try:
        now = libraries_state(plan.source_library, plan.target_library)
    except (OSError, sqlite3.Error) as e:
        log.warning("Could not read the state of the libraries: %s", e)
        plan.state = None
        return
    if before and now != plan.state:
        log.warning("The libraries changed since the analysis: the plan can't be saved to be continued")
        plan.state = None
    elif not before:
        plan.state = now


def changes(plan: Plan) -> list[str]:
    """Why the plan no longer fits its libraries (empty: it does)."""
    if plan.state is None:
        return ["the libraries were changed while the plan was open"]
    try:
        now = libraries_state(plan.source_library, plan.target_library)
    except (OSError, sqlite3.Error) as e:
        return [f"the libraries can't be read ({e})"]
    roles = [("library", "source")] if plan.same_library else [("source library", "source"),
                                                              ("target library", "target")]
    why = []
    for name, role in roles:
        then, current = plan.state.get(role) or {}, now[role]
        if then != current:
            if then.get("books") != current["books"]:
                why.append(f"the {name} has {current['books']:,} books, {then.get('books', 0):,} then")
            else:
                why.append(f"books of the {name} were changed")
    return why


# --- saving and loading -----------------------------------------------------------
@dataclass
class SavedPlan:
    plan: Plan
    signature: dict  # the analysis' settings (session.analysis_signature)
    saved: str  # when, ISO
    version: str  # of the program that saved it
    executed: int  # books executed and taken off the list
    exec_stopped: bool = False  # the last Execute was stopped with checked books left


def _path(key: str, folder: Path | None) -> Path:
    return (folder or _folder()) / f"{hashlib.sha1(key.encode()).hexdigest()[:16]}.json.gz"


def _key(plan: Plan) -> str:
    return pair_key(plan.source_library, plan.target_library, plan.tag, plan.tag_exclude)


def _plain(value):
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    raise TypeError(f"{type(value).__name__} can't be saved")


def save(plan: Plan, signature: dict, executed: int = 0, version: str = "", folder: Path | None = None,
         exec_stopped: bool = False) -> Path:
    """Write the plan (replacing the one saved for its libraries). A plan with nothing left
    is deleted instead. Raises OSError."""
    key = _key(plan)
    path = _path(key, folder)
    if not plan.items:
        forget(plan, folder)
        return path
    data = {"format": PLAN_FORMAT, "key": key, "saved": datetime.now().isoformat(timespec="seconds"),
            "version": version, "signature": signature, "executed": executed, "exec_stopped": exec_stopped,
            "plan": asdict(plan)}
    raw = gzip.compress(json.dumps(data, default=_plain, ensure_ascii=False).encode("utf-8"), 6)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix="plan_", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    _prune(path.parent)
    return path


def _prune(folder: Path) -> None:
    """Keep the plans of the KEEP_PLANS libraries used last."""
    plans = sorted(folder.glob("*.json.gz"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in plans[KEEP_PLANS:]:
        old.unlink(missing_ok=True)


def load(source: str, target: str, tag: str = "", exclude: bool = False,
         folder: Path | None = None) -> SavedPlan | None:
    """The plan saved for these libraries and tag filter; None if there is none, or it
    can't be read (damaged, or saved in another format): it is then deleted."""
    key = pair_key(source, target, tag, exclude)
    path = _path(key, folder)
    if not path.is_file():
        return None
    try:
        data = json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
        if data.get("format") != PLAN_FORMAT or data.get("key") != key:
            raise ValueError(f"format {data.get('format')}")
        return SavedPlan(_plan(data["plan"]), data["signature"], data["saved"], data.get("version", ""),
                         int(data.get("executed", 0)), bool(data.get("exec_stopped")))
    except (OSError, ValueError, KeyError, TypeError, EOFError) as e:
        log.warning("The saved plan %s can't be read (%s): deleted", path.name, e)
        path.unlink(missing_ok=True)
        return None


def forget(plan: Plan, folder: Path | None = None) -> None:
    _path(_key(plan), folder).unlink(missing_ok=True)


def _book(d: dict) -> Book:
    return Book(**{**d, "isbns": set(d["isbns"]), "asins": set(d["asins"]), "tags": set(d["tags"])})


def _identity(d: dict) -> Identity:
    return Identity(**{**d, "isbns": set(d["isbns"]), "ai_fields": set(d["ai_fields"]), "asins": set(d["asins"]),
                       "series": tuple(d["series"]) if d["series"] else None})


def _item(d: dict) -> PlanItem:
    item = PlanItem(**{**d, "source": _book(d["source"]), "match": _book(d["match"]) if d["match"] else None,
                       "identity": _identity(d["identity"]), "action": Action(d["action"]),
                       "archives": [Unpack(**u) for u in d["archives"]]})
    # __post_init__ takes the action given for the analysis' own, ticked: put back what was saved.
    item.planned_action = Action(d["planned_action"]) if d["planned_action"] else None
    item.planned_reason, item.planned_add_formats = d["planned_reason"], list(d["planned_add_formats"])
    item.selected, item.planned_selected = d["selected"], d["planned_selected"]
    return item


def _plan(d: dict) -> Plan:
    return Plan(**{**d, "items": [_item(i) for i in d["items"]],
                   "moved": {int(k): v for k, v in d["moved"].items()}})


def describe(saved: SavedPlan, todo: int) -> str:
    """For the question: when it was saved, what is left in it."""
    when = datetime.fromisoformat(saved.saved).strftime("%d %b %Y at %H:%M")
    p = saved.plan
    text = f"Saved on {when}: {len(p.items):,} books listed, {todo:,} checked to execute"
    if saved.executed:
        text += f", {saved.executed:,} executed already" + (" (the execution was stopped)" if saved.exec_stopped else "")
    return text + (" The analysis had been stopped: not every book is in it." if p.stopped else ".")
