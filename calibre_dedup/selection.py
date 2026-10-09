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

"""Which plan items get executed: checkboxes, manual overrides, dependencies,
and remembering the user's choices between analyses."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .config import config_dir
from .models import Action, Plan, PlanItem
from .library import tag_list

log = logging.getLogger(__name__)

ACTION_LABELS = {Action.MOVE: "Move to target", Action.TRASH: "Trash only", Action.LEAVE: "Leave in source"}
MERGE = "merge"  # a TRASH item that first merges formats into the kept copy
MERGE_LABEL = "Merge & Trash"
# Filter keys and labels, in display order: a duplicate is "merge" or "trash".
FILTER_LABELS = {Action.MOVE.value: ACTION_LABELS[Action.MOVE], MERGE: MERGE_LABEL,
                 Action.TRASH.value: ACTION_LABELS[Action.TRASH], Action.LEAVE.value: ACTION_LABELS[Action.LEAVE]}


def mergeable_formats(item: PlanItem) -> list[str]:
    """Formats of the book that its match lacks; PDF, and formats Calibre can't open,
    are never merged."""
    if item.match is None:
        return []
    return [f for f in item.source.formats
            if f not in item.match.formats and f != "PDF" and f not in item.bad_formats]


def filter_key(item: PlanItem) -> str:
    if item.action is Action.TRASH and item.add_formats:
        return MERGE
    return item.action.value


def action_label(item: PlanItem) -> str:
    """Merge & Trash: the duplicate's extra formats go to the kept copy, then it
    goes to trash. Trash only: nothing to merge."""
    return FILTER_LABELS[filter_key(item)]


# --- overrides ------------------------------------------------------------------
def can_override(item: PlanItem, action: Action, same_library: bool = False) -> bool:
    if action is Action.MOVE:
        return not same_library  # a book can't be moved into the library it is in
    # Trash is always possible: without a match the book just leaves the source
    # for the trash library (no target copy is checked or merged into).
    return True


def override(item: PlanItem, action: Action, same_library: bool = False, merge: bool = True) -> None:
    """`merge` (Trash only): add the book's extra formats to its match first. False:
    trash it as it is ("Trash only"), whatever formats the match lacks."""
    merge = merge and action is Action.TRASH and bool(mergeable_formats(item))
    if action is item.planned_action and (action is not Action.TRASH or merge == bool(item.planned_add_formats)):
        revert(item)
        return
    if not can_override(item, action, same_library):
        if action is Action.MOVE:
            raise ValueError(f"#{item.source.id} is already in the target library")
    item.action = action
    item.manual = True
    item.add_formats = mergeable_formats(item) if merge else []
    merged = f" {', '.join(item.add_formats)}" if item.add_formats else ""
    no_copy = ", no copy in the target" if action is Action.TRASH and item.match is None else ""
    item.reason = (f"manual: {action_label(item).lower()}{merged}{no_copy} "
                   f"(analysis: {item.planned_action.value} — {item.planned_reason})")
    item.selected = action is not Action.LEAVE  # kept: untouched (tick it for its cleanup)


def revert(item: PlanItem) -> None:
    item.action = item.planned_action
    item.reason = item.planned_reason
    item.add_formats = list(item.planned_add_formats)
    item.manual = False
    item.reviewed = False
    item.selected = item.planned_selected
    for u in item.archives:
        u.unpack = u.planned


def _decided(item: PlanItem) -> bool:
    """Whether the user changed the analysis' choices for the book: its action, its
    tick, its unreadable formats or its archives."""
    return (item.manual or item.selected != item.planned_selected or item.trash_bad != item.planned_trash_bad
            or any(u.unpack != u.planned for u in item.archives))


def is_changed(item: PlanItem) -> bool:
    """The user changed the analysis' choices for the book, or marked it reviewed."""
    return _decided(item) or item.reviewed


def needs_review(item: PlanItem) -> bool:
    """The analysis isn't sure about the book (PlanItem.review: an unproven duplicate,
    files that open nowhere, a doubtful archive), and the user hasn't decided yet: ticked,
    unticked or changed anything, or marked it reviewed. Such books start unticked
    (unticked, nothing happens to them), except for a doubtful archive, which is only
    left packed."""
    return bool(item.review) and not is_changed(item)


def mark_reviewed(items: list[PlanItem]) -> int:
    """The user looked at these books and keeps the analysis' choices. Returns how many."""
    todo = [i for i in items if needs_review(i)]
    for i in todo:
        i.reviewed = True
    return len(todo)


def revert_all(plan: Plan) -> int:
    """Put every book back to the analysis' choices. Returns how many had changed."""
    changed = [i for i in plan.items if is_changed(i)]
    for i in changed:
        revert(i)
        i.trash_bad = i.planned_trash_bad
    return len(changed)


# --- dependencies ---------------------------------------------------------------
def blocked(plan: Plan) -> dict[int, str]:
    """Source id -> why the item can't run.

    A duplicate of a book that this same plan moves into the target can only
    be trashed if that move actually happens.
    """
    by_id = {i.source.id: i for i in plan.items}
    return {it.source.id: why for it in plan.items if (why := blocked_reason(it, by_id))}


def blocked_reason(it: PlanItem, by_id: dict[int, PlanItem]) -> str:
    """Why `it` can't run ("" if it can); `by_id` maps source book ids to plan items."""
    if it.action is Action.TRASH and it.match_in_source and it.match is not None:
        m = by_id.get(it.match.id)
        if m is not None and m.action is Action.TRASH and m.selected:
            return f"blocked: #{it.match.id} (the copy kept in the source) is being trashed too"
    if it.action is Action.TRASH and it.match_planned and it.match is not None:
        m = by_id.get(it.match.id)
        if m is None or m.action is not Action.MOVE or not m.selected:
            return f"blocked: #{it.match.id} (its target copy) is not being moved"
    return ""


def actionable(plan: Plan) -> list[PlanItem]:
    """Items that execution will process, in plan order: the ticked ones (an unticked
    book is left as it is), not done by an earlier execution of the plan. A blocked book,
    or one left in place, is processed only for its unreadable formats and its archives
    (has_cleanup)."""
    stuck = blocked(plan)
    return [i for i in plan.items if i.selected and not i.done and (runs_main_action(i, stuck) or has_cleanup(i))]


def runs_main_action(i: PlanItem, stuck: dict[int, str]) -> bool:
    """Whether the item's own action (move, trash) runs on Execute."""
    return i.selected and i.source.id not in stuck and i.action is not Action.LEAVE


def checkable(i: PlanItem) -> bool:
    """Whether the item can be ticked: it moves or trashes the book, or (left in place)
    takes out its unreadable formats or unpacks its archive."""
    return i.action is not Action.LEAVE or has_cleanup(i)


def has_cleanup(i: PlanItem) -> bool:
    """Formats Calibre can't open to take out, or an archive to unpack (see PlanItem)."""
    return bool(i.bad_formats_to_trash or i.archives_to_unpack)


# --- remembering choices --------------------------------------------------------
def pair_key(source: str, target: str, tag: str = "", exclude: bool = False) -> str:
    """The libraries of an analysis, and its tag filter: what its remembered choices and
    its saved plan (plan_store) are kept under."""
    norm = [str(Path(p).resolve()).casefold() for p in (source, target)]
    tag = ",".join(sorted(t.casefold() for t in tag_list(tag)))
    return " -> ".join(norm) + (f" #{'not-' if exclude else ''}tag:{tag}" if tag else "")


class SelectionStore:
    """Remembers unchecked items and overrides per (source, target) pair, keyed by book UUID.
    An analysis of only the books with a tag counts as another pair: its choices are
    kept apart, and never replace those of the whole library (or of another tag)."""

    def __init__(self, path: Path | None = None):
        self.path = path or config_dir() / "selections.json"

    @staticmethod
    def _key(plan: Plan) -> str:
        return pair_key(plan.source_library, plan.target_library, plan.tag, plan.tag_exclude)

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def apply(self, plan: Plan, items: list[PlanItem] | None = None) -> int:
        """Re-apply saved choices to a fresh plan, or to `items` of it (the books just
        decided, while the analysis runs). Returns how many were restored."""
        entries = self._read().get(self._key(plan), {})
        restored = 0
        for item in plan.items if items is None else items:
            e = entries.get(item.source.uuid)
            if not e:
                continue
            if e.get("action"):
                try:
                    override(item, Action(e["action"]), plan.same_library, e.get("merge", True))
                except ValueError:
                    continue
            if "trash_bad" in e and item.bad_formats and not item.unreadable:
                item.trash_bad = bool(e["trash_bad"])
            if "selected" in e and checkable(item):
                item.selected = bool(e["selected"])
            if e.get("reviewed"):
                item.reviewed = True
            restored += 1
        return restored

    def saved(self, plan: Plan) -> int:
        """How many books of the plan's libraries have saved choices (analyzed or not)."""
        return len(self._read().get(self._key(plan), {}))

    def forget(self, plan: Plan) -> None:
        """Drop every saved choice of the plan's libraries, also of books not analyzed."""
        data = self._read()
        if data.pop(self._key(plan), None) is not None:
            self._write(data)

    def save(self, plan: Plan, partial: bool = False) -> None:
        """`partial`: the plan doesn't cover every book yet (analysis still running)."""
        entries = {}
        for item in plan.items:
            e = {}
            if item.manual:
                e["action"] = item.action.value
                if item.action is Action.TRASH and not item.add_formats:
                    e["merge"] = False
            # Only a tick that differs from the analysis' own: books to review start
            # unticked, and follow the analysis until changed.
            default = item.planned_selected if item.action is item.planned_action else item.action is not Action.LEAVE
            if checkable(item) and item.selected != default:
                e["selected"] = item.selected
            if item.bad_formats and not item.unreadable and item.trash_bad != item.planned_trash_bad:
                e["trash_bad"] = item.trash_bad
            if item.reviewed:
                e["reviewed"] = True
            if e and item.source.uuid:
                entries[item.source.uuid] = e
        data = self._read()
        key = self._key(plan)
        if plan.stopped or partial:
            # Books the analysis didn't (yet) reach keep their saved choices.
            seen = {item.source.uuid for item in plan.items}
            old = data.get(key, {})
            entries = {**{u: e for u, e in old.items() if u not in seen}, **entries}
        if entries:
            data[key] = entries
        else:
            data.pop(key, None)
        self._write(data)

    def _write(self, data: dict) -> None:
        try:
            self.path.write_text(json.dumps(data, indent=1), encoding="utf-8")
        except OSError as e:
            log.warning("Cannot save selections: %s", e)
