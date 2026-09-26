"""Which plan items get executed: checkboxes, manual overrides, dependencies,
and remembering the user's choices between analyses."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .config import config_dir
from .models import Action, Plan, PlanItem

log = logging.getLogger(__name__)

ACTION_LABELS = {Action.MOVE: "Move to target", Action.TRASH: "Trash only", Action.LEAVE: "Leave in source"}
MERGE = "merge"  # a TRASH item that first merges formats into the kept copy
MERGE_LABEL = "Merge & Trash"
# Filter keys and labels, in display order: a duplicate is "merge" or "trash".
FILTER_LABELS = {Action.MOVE.value: ACTION_LABELS[Action.MOVE], MERGE: MERGE_LABEL,
                 Action.TRASH.value: ACTION_LABELS[Action.TRASH], Action.LEAVE.value: ACTION_LABELS[Action.LEAVE]}


def mergeable_formats(item: PlanItem) -> list[str]:
    """Formats of the book that its match lacks; PDF is never merged."""
    if item.match is None:
        return []
    return [f for f in item.source.formats if f not in item.match.formats and f != "PDF"]


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
    item.selected = action is not Action.LEAVE


def revert(item: PlanItem) -> None:
    item.action = item.planned_action
    item.reason = item.planned_reason
    item.add_formats = list(item.planned_add_formats)
    item.manual = False
    item.selected = item.action is not Action.LEAVE


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
    if it.action is Action.TRASH and it.match_planned and it.match is not None:
        m = by_id.get(it.match.id)
        if m is None or m.action is not Action.MOVE or not m.selected:
            return f"blocked: #{it.match.id} (its target copy) is not being moved"
    return ""


def actionable(plan: Plan) -> list[PlanItem]:
    """Items that execution will process, in plan order.

    A book left in source may still be updated with AI metadata when it was
    enriched but not moved/trash-ed. Those updates must be allowed without
    treating normal leave decisions as executable actions.
    """
    stuck = blocked(plan)
    return [
        i for i in plan.items
        if i.selected and i.source.id not in stuck and (
            i.action is not Action.LEAVE or (i.ai_used and bool(i.identity.ai_fields))
        )
    ]


# --- remembering choices --------------------------------------------------------
class SelectionStore:
    """Remembers unchecked items and overrides per (source, target) pair, keyed by book UUID."""

    def __init__(self, path: Path | None = None):
        self.path = path or config_dir() / "selections.json"

    @staticmethod
    def _key(plan: Plan) -> str:
        norm = [str(Path(p).resolve()).casefold() for p in (plan.source_library, plan.target_library)]
        return " -> ".join(norm)

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
            if "selected" in e and item.action is not Action.LEAVE:
                item.selected = bool(e["selected"])
            restored += 1
        return restored

    def save(self, plan: Plan, partial: bool = False) -> None:
        """`partial`: the plan doesn't cover every book yet (analysis still running)."""
        entries = {}
        for item in plan.items:
            e = {}
            if item.manual:
                e["action"] = item.action.value
                if item.action is Action.TRASH and not item.add_formats:
                    e["merge"] = False
            if item.action is not Action.LEAVE and not item.selected:
                e["selected"] = False
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
        try:
            self.path.write_text(json.dumps(data, indent=1), encoding="utf-8")
        except OSError as e:
            log.warning("Cannot save selections: %s", e)
