"""Which plan items get executed: checkboxes, manual overrides, dependencies,
and remembering the user's choices between analyses."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .config import config_dir
from .models import Action, Plan, PlanItem

log = logging.getLogger(__name__)

ACTION_LABELS = {Action.MOVE: "Move to target", Action.TRASH: "Trash (duplicate)", Action.LEAVE: "Leave in source"}


# --- overrides ------------------------------------------------------------------
def can_override(item: PlanItem, action: Action) -> bool:
    # Trashing needs a target copy to be a duplicate of.
    return action is not Action.TRASH or item.match is not None


def override(item: PlanItem, action: Action) -> None:
    if action is item.planned_action:
        revert(item)
        return
    if not can_override(item, action):
        raise ValueError(f"#{item.source.id} has no matching target book to be a duplicate of")
    item.action = action
    item.manual = True
    item.reason = (f"manual: {ACTION_LABELS[action].lower()} "
                   f"(analysis: {item.planned_action.value} — {item.planned_reason})")
    if action is Action.TRASH:
        existing = item.match.formats
        item.add_formats = [f for f in item.source.formats if f not in existing and f != "PDF"]
    else:
        item.add_formats = []
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
    out: dict[int, str] = {}
    for it in plan.items:
        if it.action is Action.TRASH and it.match_planned and it.match is not None:
            m = by_id.get(it.match.id)
            if m is None or m.action is not Action.MOVE or not m.selected:
                out[it.source.id] = f"blocked: #{it.match.id} (its target copy) is not being moved"
    return out


def actionable(plan: Plan) -> list[PlanItem]:
    """Items that execution will process, in plan order."""
    stuck = blocked(plan)
    return [i for i in plan.items
            if i.selected and i.action is not Action.LEAVE and i.source.id not in stuck]


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

    def apply(self, plan: Plan) -> int:
        """Re-apply saved choices to a fresh plan. Returns how many were restored."""
        entries = self._read().get(self._key(plan), {})
        restored = 0
        for item in plan.items:
            e = entries.get(item.source.uuid)
            if not e:
                continue
            if e.get("action"):
                try:
                    override(item, Action(e["action"]))
                except ValueError:
                    continue
            if "selected" in e and item.action is not Action.LEAVE:
                item.selected = bool(e["selected"])
            restored += 1
        return restored

    def save(self, plan: Plan) -> None:
        entries = {}
        for item in plan.items:
            e = {}
            if item.manual:
                e["action"] = item.action.value
            if item.action is not Action.LEAVE and not item.selected:
                e["selected"] = False
            if e and item.source.uuid:
                entries[item.source.uuid] = e
        data = self._read()
        key = self._key(plan)
        if entries:
            data[key] = entries
        else:
            data.pop(key, None)
        try:
            self.path.write_text(json.dumps(data, indent=1), encoding="utf-8")
        except OSError as e:
            log.warning("Cannot save selections: %s", e)
