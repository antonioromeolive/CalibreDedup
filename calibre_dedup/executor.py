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
from pathlib import Path
from typing import Callable

from .calibre_env import CREATE_NO_WINDOW, calibre_is_running, tool
from .library_use import ExecutionLock
from .models import Action, Plan, PlanItem
from .selection import actionable, blocked, has_metadata_update, runs_main_action
from .tempdirs import RunDir

log = logging.getLogger(__name__)
BRIDGE = Path(__file__).with_name("bridge_script.py")
# Added to every book whose metadata is written from what the AI read in it (both programs),
# only when a field actually changes: search it in Calibre to check the AI's work.
AI_UPDATED_TAG = "AIUpdated"
# Added to every book whose swapped title and author were put right.
SWAPPED_TAG = "TitleAuthorSwapped"


class ExecutionError(Exception):
    pass


def _keep_failed_in_source(item: PlanItem) -> None:
    item.action = Action.LEAVE
    item.selected = False
    item.manual = False
    item.reason = f"kept in source after execution failure: {item.reason}"


def plan_actions(plan: Plan, update_metadata: bool) -> list[dict]:
    """Bridge actions for the checked, unblocked items. "trash_formats": the formats
    Calibre can't open, taken out of the source first (the whole record is copied to
    the trash library as it is); on its own for a book whose action doesn't run.
    "unpack": the archives to unpack (see archives.Unpack.spec), likewise."""
    actions = []
    stuck = blocked(plan)
    for item in actionable(plan):
        main = runs_main_action(item, stuck)
        a = None
        if main and item.action is Action.MOVE:
            a = {"op": "move", "src_id": item.source.id, "title": item.source.title}
            if update_metadata and item.identity.ai_fields:
                a["set"], a["updated_tag"] = _ai_values(item), AI_UPDATED_TAG
            if update_metadata and item.swapped:
                a["swap"] = _swap_values(item)
        elif main and item.action is Action.TRASH:
            a = {"op": "trash", "src_id": item.source.id, "title": item.source.title,
                 "add_formats": item.add_formats}
            if item.match is None:  # forced by the user, or unreadable: no target copy to check or merge into
                a["no_target"] = True
            elif item.match_planned:
                a["target_src_id"] = item.match.id
            elif item.match_in_source:
                a["keep_src_id"] = item.match.id
            else:
                a["target_id"] = item.match.id
        elif main and item.action is Action.LEAVE and update_metadata and has_metadata_update(item):
            a = {"op": "update", "src_id": item.source.id, "title": item.source.title, "set": {}}
            if item.ai_used and item.identity.ai_fields:
                a["set"], a["updated_tag"] = _ai_values(item), AI_UPDATED_TAG
            if item.swapped:
                a["swap"] = _swap_values(item)
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
            actions.append(a)
    return actions


def _swap_values(item: PlanItem) -> dict:
    """Title and authors put right, for a book whose record had them swapped
    (overwritten, unlike what the AI found; tagged SWAPPED_TAG)."""
    return {"title": item.identity.title, "authors": item.identity.authors, "tag": SWAPPED_TAG,
            "was_title": item.source.title, "was_authors": item.source.authors}


def _ai_values(item: PlanItem) -> dict:
    ident, fields = item.identity, item.identity.ai_fields
    values: dict = {}
    if "title" in fields:
        values["title"] = ident.title
    if "authors" in fields:
        values["authors"] = ident.authors
    if "publisher" in fields:
        values["publisher"] = ident.publisher
    if "year" in fields and ident.year is not None:
        values["year"] = ident.year
    if "isbn" in fields and ident.isbns:
        values["isbn"] = sorted(ident.isbns)[0]
    return values


def run_bridge(calibre_dir: Path, payload: dict, on_message: Callable[[dict], None],
               cancel: threading.Event | None = None, program: str = "dedup") -> None:
    """Run bridge_script.py in calibre-debug with `payload` as its plan, calling
    `on_message` for each "result" / "stopped" event it prints. Locks the libraries
    it writes (see library_use.ExecutionLock): if another execution writes one of
    them, raises ExecutionError naming it."""
    execution_lock = ExecutionLock(program)
    conflicts = execution_lock.acquire({r: payload.get(r) or "" for r in ("source", "target", "trash")})
    if conflicts:
        raise ExecutionError("\n".join(c.describe(program) for c in conflicts))
    try:
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


def execute_plan(
    plan: Plan, calibre_dir: Path, update_metadata: bool = True, permanent: bool = False,
    on_result: Callable[[PlanItem, bool, str], None] | None = None,
    cancel: threading.Event | None = None,
) -> tuple[int, int]:
    """Execute MOVE and TRASH items. Returns (succeeded, failed)."""
    if calibre_is_running():
        raise ExecutionError("Calibre is running. Close Calibre (and calibre-server) before executing the plan.")
    actions = plan_actions(plan, update_metadata)
    if not actions:
        return 0, 0
    items = {i.source.id: i for i in plan.items}
    ok = failed = 0

    def on_message(msg: dict) -> None:
        nonlocal ok, failed
        if msg["event"] != "result":
            return
        item = items[msg["src_id"]]
        item.status = ("OK: " if msg["ok"] else "FAILED: ") + msg["msg"]
        if msg["ok"]:
            ok += 1
            if item.archives_to_unpack:  # done: the book no longer has them
                item.archives = [u for u in item.archives if not u.unpack]
            log.info("Book %s (%s): %s", item.source.id, item.source.title, msg["msg"])
        else:
            failed += 1
            _keep_failed_in_source(item)
            log.error("Book %s (%s): %s", item.source.id, item.source.title, msg.get("trace") or msg["msg"])
        if on_result:
            on_result(item, msg["ok"], msg["msg"])

    run_bridge(calibre_dir, {
        "source": plan.source_library, "target": plan.target_library, "trash": plan.trash_library,
        "permanent": permanent, "actions": actions,
    }, on_message, cancel)
    return ok, failed
