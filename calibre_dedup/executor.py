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

"""Runs a plan through bridge_script.py inside calibre-debug."""

from __future__ import annotations

import json
import logging
import subprocess
import threading
from dataclasses import replace
from pathlib import Path
from typing import Callable

from .calibre_env import CREATE_NO_WINDOW, calibre_is_running, tool
from .journal import Journal, snapshot
from .library_use import ExecutionLock
from .models import Action, Plan, PlanItem
from .selection import action_label, actionable, blocked, runs_main_action
from .tempdirs import RunDir

log = logging.getLogger(__name__)
BRIDGE = Path(__file__).with_name("bridge_script.py")
# Added by the Metadata Review to every book whose metadata it writes from what the AI read
# in it, only when a field actually changes: search it in Calibre to check the AI's work.
AI_UPDATED_TAG = "AIUpdated"


class ExecutionError(Exception):
    pass


def _keep_failed_in_source(item: PlanItem) -> None:
    """A book that failed keeps its action, unticked: the user may tick it again and execute
    the plan again (its dependents are blocked meanwhile, see selection.blocked)."""
    item.selected = False


def _same_book(book, library: str, book_id: int) -> bool:
    return book is not None and book.id == book_id and Path(book.library).resolve() == Path(library).resolve()


def apply_result(plan: Plan, item: PlanItem, msg: dict) -> None:
    """What an execution did to a book, written into the plan so that it can be executed
    again (its other books) without a new analysis:
    - a book moved or trashed has left the source (`done`); a move is remembered
      (plan.moved), and the books that were to be trashed into it now point to its copy
      in the target, with that copy's id and last_modified;
    - a book written in place (formats taken out, an archive unpacked, formats merged into
      it) gets its new last_modified and formats, in every row that shows it (as the book
      or as the match), so the bridge's "changed since the analysis" check still holds;
    - formats taken out are no longer the book's (nothing left to clean up)."""
    sent = msg.get("op")
    src = plan.source_library
    if msg.get("stamp") is not None:  # the book itself was written and stays (cleanup only)
        _refresh_book(plan, src, item.source.id, msg["stamp"], msg.get("formats"))
        item.bad_formats = {f: why for f, why in item.bad_formats.items() if f in item.source.formats}
        if item.action is Action.LEAVE:
            item.selected = item.planned_selected = False  # nothing left to do for it
    if sent in ("move", "trash"):
        item.done = True
    kept = msg.get("kept") or {}
    if not kept:
        return
    library = plan.target_library if kept["library"] == "target" else src
    if sent == "move":
        plan.moved[item.source.id] = kept["id"]
        for it in plan.items:
            if it.match_planned and it.match is not None and it.match.id == item.source.id:
                it.match = replace(it.match, id=kept["id"], library=library, path=kept["path"],
                                   formats=dict(kept["formats"]), last_modified=kept["stamp"])
                it.match_planned = False
    else:
        _refresh_book(plan, library, kept["id"], kept["stamp"], kept["formats"])


def _refresh_book(plan: Plan, library: str, book_id: int, stamp: str, formats: dict | None) -> None:
    """The book's new last_modified (and formats), wherever the plan shows it."""
    for it in plan.items:
        for book in (it.source, it.match):
            if _same_book(book, library, book_id):
                book.last_modified = stamp
                if formats is not None:
                    book.formats = dict(formats)


def plan_actions(plan: Plan) -> list[dict]:
    """Bridge actions for the ticked, unblocked items (an unticked book is left as it is).
    "trash_formats": the formats Calibre can't open, taken out of the source first (the
    whole record is copied to the trash library as it is); on its own for a book whose
    action doesn't run. "unpack": the archives to unpack (see archives.Unpack.spec),
    likewise. "stamp" (and "target_stamp", "keep_stamp" for the copy kept): the books'
    last_modified as the analysis read them, so that a book changed since is not touched."""
    actions = []
    stuck = blocked(plan)
    for item in actionable(plan):
        main = runs_main_action(item, stuck)
        a = None
        if main and item.action is Action.MOVE:
            a = {"op": "move", "src_id": item.source.id, "title": item.source.title}
        elif main and item.action is Action.TRASH:
            a = {"op": "trash", "src_id": item.source.id, "title": item.source.title,
                 "add_formats": item.add_formats}
            if item.match is None:  # forced by the user, unreadable or empty: no copy to check or merge into
                a["no_target"] = True
            elif item.match_planned:
                a["target_src_id"] = item.match.id
            elif item.match_in_source:
                a["keep_src_id"], a["keep_stamp"] = item.match.id, item.match.last_modified
            else:
                a["target_id"], a["target_stamp"] = item.match.id, item.match.last_modified
        trashed = bool(a and a["op"] == "trash")
        bad = item.bad_formats_to_trash if not trashed else []  # trashed whole anyway
        # Archives: their files are added first (a merge may need them); a book trashed
        # whole keeps its archive, since the record goes to the trash library anyway.
        unpack = [u.spec(remove=not trashed) for u in item.archives_to_unpack]
        if bad or unpack:
            a = a or {"op": "trash_formats" if bad else "unpack", "src_id": item.source.id,
                      "title": item.source.title}
        if bad:
            a["trash_formats"] = bad
        if unpack:
            a["unpack"] = unpack
        if a:
            a["stamp"] = item.source.last_modified
            actions.append(a)
    return actions


def run_bridge(calibre_dir: Path, payload: dict, on_message: Callable[[dict], None],
               cancel: threading.Event | None = None, program: str = "dedup",
               snapshots: Path | None = None) -> None:
    """Run bridge_script.py in calibre-debug with `payload` as its plan, calling
    `on_message` for each "result" / "stopped" event it prints. Locks the libraries
    it writes (see library_use.ExecutionLock): if another execution writes one of
    them, raises ExecutionError naming it. Before starting, the metadata.db of the
    source and the target (not the trash library: it only receives books) is copied
    (journal.snapshot, into `snapshots`, default the data folder); if it can't be,
    nothing is done."""
    execution_lock = ExecutionLock(program)
    conflicts = execution_lock.acquire({r: payload.get(r) or "" for r in ("source", "target", "trash")})
    if conflicts:
        raise ExecutionError("\n".join(c.describe(program) for c in conflicts))
    try:
        written = {str(Path(p).resolve()).casefold(): p for p in (payload.get("source"), payload.get("target"))
                   if p and Path(p, "metadata.db").is_file()}
        for library in written.values():
            try:
                snapshot(library, snapshots)
            except Exception as e:
                raise ExecutionError(f"Nothing was done: the copy of {library}'s metadata.db, kept to undo the "
                                     f"changes, could not be saved ({e}).") from e
        with RunDir("execute_") as tmp:
            payload = {**payload, "tmp": str(tmp)}  # where the bridge extracts archives
            plan_file = tmp / "plan.json"
            plan_file.write_text(json.dumps(payload), encoding="utf-8")
            proc = subprocess.Popen(
                [str(tool(calibre_dir, "calibre-debug")), str(BRIDGE), str(plan_file)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", creationflags=CREATE_NO_WINDOW,
            )
            if cancel is not None:
                # Signal the bridge as soon as Stop is pressed, not when it next prints:
                # it finishes the current book and skips the rest.
                def watch_cancel():
                    while proc.poll() is None:
                        if cancel.wait(0.5):
                            try:
                                Path(str(plan_file) + ".stop").touch()
                            except OSError:
                                pass  # the bridge already ended and the temp dir is gone
                            return
                threading.Thread(target=watch_cancel, daemon=True).start()
            done = False
            assert proc.stdout is not None
            for line in proc.stdout:
                if not line.startswith("@@CDR "):
                    if line.strip():
                        log.info("calibre: %s", line.rstrip())
                    continue
                msg = json.loads(line[6:])
                if msg["event"] == "stopped":
                    log.warning("Execution stopped by user")
                elif msg["event"] == "cleanup_start":
                    log.info("Removing empty source folders…")
                elif msg["event"] == "cleanup":
                    log.info("Source folder cleanup: %d removed, %d deferred",
                             msg["removed"], msg["deferred"])
                elif msg["event"] == "done":
                    done = True
                else:
                    on_message(msg)
            proc.wait()
            if not done:
                raise ExecutionError(f"calibre-debug exited with code {proc.returncode}; see the log for details")
    finally:
        execution_lock.release()


OP_LABELS = {"move": "Move to target", "trash_formats": "Unreadable formats to the trash library",
             "unpack": "Archive unpacked"}


def journal_match(item: PlanItem) -> str:
    """The book a trashed one is a copy of, for the journal."""
    m = item.match
    if m is None:
        return ""
    where = (" (moved to the target by this run)" if item.match_planned
             else " (kept in the source)" if item.match_in_source else "")
    return f"#{m.id} {m.label()}{where}"


def execute_plan(
    plan: Plan, calibre_dir: Path, permanent: bool = False,
    on_result: Callable[[PlanItem, bool, str], None] | None = None,
    cancel: threading.Event | None = None, journal: Path | None = None,
) -> tuple[int, int]:
    """Execute the ticked items. Returns (succeeded, failed). Each book acted on gets a
    row in the journal (journal.Journal, in `journal`, default the data folder)."""
    if calibre_is_running():
        raise ExecutionError("Calibre is running. Close Calibre (and calibre-server) before executing the plan.")
    actions = plan_actions(plan)
    if not actions:
        return 0, 0
    items = {i.source.id: i for i in plan.items}
    sent = {a["src_id"]: a for a in actions}
    for i in plan.items:
        if i.source.id in sent:
            i.status = ""  # a row failed before, ticked again
    ok = failed = 0

    with Journal("dedup", journal) as book_log:
        def on_message(msg: dict) -> None:
            nonlocal ok, failed
            if msg["event"] != "result":
                return
            item = items[msg["src_id"]]
            op = sent[item.source.id]["op"]
            book_log.write(library=plan.source_library, book_id=item.source.id, title=item.source.title,
                           authors=" & ".join(item.source.authors),
                           action=action_label(item) if op == "trash" else OP_LABELS.get(op, op),
                           ok="yes" if msg["ok"] else "no", result=msg["msg"], why=item.reason,
                           match=journal_match(item) if op == "trash" else "")
            item.status = ("OK: " if msg["ok"] else "FAILED: ") + msg["msg"]
            if msg["ok"]:
                ok += 1
                if item.archives_to_unpack:  # done: the book no longer has them
                    item.archives = [u for u in item.archives if not u.unpack]
                apply_result(plan, item, {**msg, "op": op})
                log.info("Book %s (%s): %s", item.source.id, item.source.title, msg["msg"])
            else:
                failed += 1
                _keep_failed_in_source(item)
                log.error("Book %s (%s): %s", item.source.id, item.source.title, msg.get("trace") or msg["msg"])
            if on_result:
                on_result(item, msg["ok"], msg["msg"])

        log.info("Journal of this execution: %s", book_log.path)
        run_bridge(calibre_dir, {
            "source": plan.source_library, "target": plan.target_library, "trash": plan.trash_library,
            "permanent": permanent, "actions": actions,
        }, on_message, cancel)
    return ok, failed
