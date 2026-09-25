import pytest

from calibre_dedup.ai import AICache
from calibre_dedup.executor import _keep_failed_in_source, plan_actions
from calibre_dedup.models import Action, Book, Identity, Plan, PlanItem
from calibre_dedup.selection import SelectionStore, actionable, blocked, can_override, override, revert


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


def test_ai_year_is_included_in_move_metadata():
    plan = make_plan()
    item = plan.items[0]
    item.identity = Identity(year=1990, ai_fields={"year"})

    assert plan_actions(plan, update_metadata=True)[0]["set"] == {"year": 1990}


def test_failed_execution_keeps_book_in_source():
    plan = make_plan()
    item = plan.items[0]

    _keep_failed_in_source(item)

    assert item.action is Action.LEAVE
    assert not item.selected
    assert not item.manual
    assert "kept in source after execution failure" in item.reason


def test_ai_cache_is_model_independent():
    assert AICache.key("C:/books/book.epub", "start", "llama3") == AICache.key("C:/books/book.epub", "start", "gpt-4o")


def test_leave_items_with_ai_metadata_are_updated_in_source():
    plan = make_plan()
    item = plan.items[3]
    item.action = Action.LEAVE
    item.ai_used = True
    item.identity = Identity(title="New title", authors=["Alice Example"], ai_fields={"title", "authors"})
    item.selected = True

    actions = plan_actions(plan, update_metadata=True)
    assert item in actionable(plan)
    assert actions[-1] == {
        "op": "update",
        "src_id": item.source.id,
        "title": item.source.title,
        "set": {"title": "New title", "authors": ["Alice Example"]},
    }


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


def test_stopped_analysis_keeps_choices_of_books_it_did_not_reach(tmp_path):
    store = SelectionStore(tmp_path / "sel.json")
    plan = make_plan()
    plan.items[1].selected = False
    override(plan.items[3], Action.MOVE)
    store.save(plan)

    partial = make_plan()
    partial.items = partial.items[:2]  # stopped before books 3 and 4
    partial.stopped = True
    partial.items[1].selected = True
    store.save(partial)

    fresh = make_plan()
    assert store.apply(fresh) == 1
    assert fresh.items[1].selected  # re-checked in the partial plan
    assert fresh.items[3].action is Action.MOVE  # untouched by the partial plan


def test_same_library_plan_cannot_force_a_move():
    plan = make_plan()
    item = plan.items[2]  # left in place, has a match
    assert can_override(item, Action.MOVE) and not can_override(item, Action.MOVE, same_library=True)
    assert can_override(item, Action.TRASH, same_library=True)
    with pytest.raises(ValueError):
        override(item, Action.MOVE, same_library=True)


def test_saved_move_is_not_restored_into_the_same_library(tmp_path):
    store = SelectionStore(tmp_path / "sel.json")
    plan = make_plan()
    override(plan.items[3], Action.MOVE)
    store.save(plan)
    fresh = make_plan()
    fresh.same_library = True
    store.apply(fresh)
    assert fresh.items[3].action is Action.LEAVE and not fresh.items[3].manual


def test_trash_is_merge_and_trash_or_trash_only():
    from calibre_dedup.selection import action_label, filter_key

    plan = make_plan()
    same_formats = PlanItem(book(5), Action.LEAVE, "edition unknown", Identity(), match=book(6))
    override(same_formats, Action.TRASH)
    assert action_label(same_formats) == "Trash only" and filter_key(same_formats) == "trash"
    assert same_formats.reason.startswith("manual: trash only (analysis: leave")

    extra = plan.items[2]  # EPUB, PDF, MOBI vs a PDF-only match
    override(extra, Action.TRASH)
    assert action_label(extra) == "Merge & Trash" and filter_key(extra) == "merge"
    assert extra.reason.startswith("manual: merge & trash EPUB, MOBI (analysis: leave")


def test_saved_choices_can_be_applied_to_the_books_just_decided(tmp_path):
    store = SelectionStore(tmp_path / "sel.json")
    plan = make_plan()
    plan.items[1].selected = False
    override(plan.items[3], Action.MOVE)
    store.save(plan)

    fresh = make_plan()
    assert store.apply(fresh, fresh.items[:2]) == 1  # only the first batch
    assert not fresh.items[1].selected and not fresh.items[3].manual


def test_saving_while_the_analysis_runs_keeps_choices_of_books_not_reached(tmp_path):
    store = SelectionStore(tmp_path / "sel.json")
    plan = make_plan()
    override(plan.items[3], Action.MOVE)
    store.save(plan)

    live = make_plan()
    live.items = live.items[:2]  # still analyzing
    live.items[1].selected = False
    store.save(live, partial=True)

    fresh = make_plan()
    assert store.apply(fresh) == 2
    assert not fresh.items[1].selected and fresh.items[3].action is Action.MOVE


def test_blocked_reason_for_one_item():
    from calibre_dedup.selection import blocked_reason

    plan = make_plan()
    by_id = {i.source.id: i for i in plan.items}
    assert blocked_reason(plan.items[1], by_id) == ""
    plan.items[0].selected = False  # the move its duplicate depends on
    assert blocked_reason(plan.items[1], by_id).startswith("blocked: #1")
