import pytest

from calibre_dedup.executor import plan_actions
from calibre_dedup.models import Action, Book, Identity, Plan, PlanItem
from calibre_dedup.selection import SelectionStore, actionable, blocked, override, revert


def book(bid, formats=("EPUB",)):
    return Book(bid, f"T{bid}", ["A"], None, None, set(), {f: f"x.{f}" for f in formats}, f"uuid{bid}", "", "lib")


def make_plan():
    target = book(100, ("PDF",))
    moved = book(1)
    items = [
        PlanItem(moved, Action.MOVE, "not in target", Identity()),
        PlanItem(book(2), Action.TRASH, "dup", Identity(), match=moved, match_planned=True),
        PlanItem(book(3, ("EPUB", "PDF", "MOBI")), Action.LEAVE, "edition unknown", Identity(), match=target),
        PlanItem(book(4), Action.LEAVE, "no title", Identity()),
    ]
    return Plan("src", "tgt", "trash", items)


def test_defaults_check_move_and_trash_only():
    plan = make_plan()
    assert [i.source.id for i in actionable(plan)] == [1, 2]


def test_unchecking_a_move_blocks_its_duplicates():
    plan = make_plan()
    plan.items[0].selected = False
    assert 2 in blocked(plan)
    assert actionable(plan) == []
    assert plan_actions(plan, update_metadata=False) == []


def test_force_trash_computes_formats_and_needs_a_match():
    plan = make_plan()
    item = plan.items[2]
    override(item, Action.TRASH)
    assert item.action is Action.TRASH and item.manual and item.selected
    assert item.add_formats == ["EPUB", "MOBI"]  # PDF never added, target already has PDF
    assert item.reason.startswith("manual:")
    with pytest.raises(ValueError):
        override(plan.items[3], Action.TRASH)


def test_force_move_and_revert():
    plan = make_plan()
    item = plan.items[3]
    override(item, Action.MOVE)
    assert item in actionable(plan)
    revert(item)
    assert item.action is Action.LEAVE and not item.manual and item.reason == "no title"
    # overriding back to the planned action is a revert
    override(plan.items[0], Action.LEAVE)
    override(plan.items[0], Action.MOVE)
    assert not plan.items[0].manual


def test_forcing_the_move_to_leave_blocks_dependents():
    plan = make_plan()
    override(plan.items[0], Action.LEAVE)
    assert 2 in blocked(plan)


def test_store_round_trip(tmp_path):
    store = SelectionStore(tmp_path / "sel.json")
    plan = make_plan()
    plan.items[1].selected = False
    override(plan.items[3], Action.MOVE)
    store.save(plan)

    fresh = make_plan()
    assert store.apply(fresh) == 2
    assert not fresh.items[1].selected
    assert fresh.items[3].action is Action.MOVE and fresh.items[3].manual

    # nothing deviating from defaults -> entry removed
    store.save(make_plan())
    assert store.apply(make_plan()) == 0
