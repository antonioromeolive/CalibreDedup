"""Runs a plan through bridge_script.py inside calibre-debug."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Callable

from .calibre_env import CREATE_NO_WINDOW, calibre_is_running, tool
from .config import config_dir
from .models import Action, Plan, PlanItem
from .selection import actionable

log = logging.getLogger(__name__)
BRIDGE = Path(__file__).with_name("bridge_script.py")


class ExecutionError(Exception):
    pass


def _keep_failed_in_source(item: PlanItem) -> None:
    item.action = Action.LEAVE
    item.selected = False
    item.manual = False
    item.reason = f"kept in source after execution failure: {item.reason}"


class _ExecutionLock:
    def __init__(self):
        self.path = config_dir() / "execution.lock"
        self._file = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a+b")
        try:
            self._file.seek(0)
            if self._file.read(1) == b"":
                self._file.seek(0)
                self._file.write(b"0")
                self._file.flush()
            self._file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            self._file.close()
            self._file = None
            raise ExecutionError("Another CalibreDedup instance is executing a plan.") from e

    def release(self):
        if self._file is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None


def plan_actions(plan: Plan, update_metadata: bool) -> list[dict]:
    """Bridge actions for the checked, unblocked items."""
    actions = []
    for item in actionable(plan):
        if item.action is Action.MOVE:
            a = {"op": "move", "src_id": item.source.id, "title": item.source.title}
            if update_metadata and item.identity.ai_fields:
                a["set"] = _ai_values(item)
            actions.append(a)
        elif item.action is Action.TRASH and item.match is not None:
            a = {"op": "trash", "src_id": item.source.id, "title": item.source.title,
                 "add_formats": item.add_formats}
            if item.match_planned:
                a["target_src_id"] = item.match.id
            else:
                a["target_id"] = item.match.id
            actions.append(a)
    return actions


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
    execution_lock = _ExecutionLock()
    execution_lock.acquire()
    items = {i.source.id: i for i in plan.items}
    ok = failed = 0

    try:
        with tempfile.TemporaryDirectory(prefix="cdr_exec_") as tmp:
            plan_file = Path(tmp) / "plan.json"
            plan_file.write_text(json.dumps({
                "source": plan.source_library, "target": plan.target_library, "trash": plan.trash_library,
                "permanent": permanent, "actions": actions,
            }), encoding="utf-8")
            proc = subprocess.Popen(
                [str(tool(calibre_dir, "calibre-debug")), str(BRIDGE), str(plan_file)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", creationflags=CREATE_NO_WINDOW,
            )
            done = False
            assert proc.stdout is not None
            for line in proc.stdout:
                if cancel is not None and cancel.is_set():
                    Path(str(plan_file) + ".stop").touch()
                if not line.startswith("@@CDR "):
                    if line.strip():
                        log.info("calibre: %s", line.rstrip())
                    continue
                msg = json.loads(line[6:])
                if msg["event"] == "result":
                    item = items[msg["src_id"]]
                    item.status = ("OK: " if msg["ok"] else "FAILED: ") + msg["msg"]
                    if msg["ok"]:
                        ok += 1
                    else:
                        failed += 1
                        _keep_failed_in_source(item)
                        log.error("Book %s (%s): %s", item.source.id, item.source.title, msg.get("trace") or msg["msg"])
                    if on_result:
                        on_result(item, msg["ok"], msg["msg"])
                elif msg["event"] == "stopped":
                    log.warning("Execution stopped by user")
                elif msg["event"] == "done":
                    done = True
            proc.wait()
            if not done:
                raise ExecutionError(f"calibre-debug exited with code {proc.returncode}; see the log for details")
        return ok, failed
    finally:
        execution_lock.release()
