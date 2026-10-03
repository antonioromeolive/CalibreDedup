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

from dataclasses import replace

import pytest

from calibre_dedup.ai import AICache
from calibre_dedup.executor import _keep_failed_in_source, apply_result, plan_actions
from calibre_dedup.models import Action, Book, Identity, Plan, PlanItem
from calibre_dedup.selection import (
    SelectionStore, action_label, actionable, blocked, can_override, checkable, is_changed, mark_reviewed,
    needs_review, override, revert, revert_all,
)


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


def test_a_move_writes_no_metadata():
    plan = make_plan()
    plan.items[0].identity = Identity(year=1990, ai_fields={"year"})  # what the AI read, to compare copies
    action = plan_actions(plan)[0]
    assert action == {"op": "move", "src_id": 1, "title": "T1", "stamp": ""}


def test_a_failed_book_keeps_its_action_unticked_and_can_be_ticked_again():
    plan = make_plan()
    item = plan.items[0]
    _keep_failed_in_source(item)
    assert item.action is Action.MOVE and not item.selected
    assert 2 in blocked(plan)  # its duplicate waits for it
    item.selected = True
    assert [i.source.id for i in actionable(plan)] == [1, 2]


def _moved_to_target(plan, new_id=77):
    apply_result(plan, plan.items[0], {"op": "move", "ok": True, "kept": {
        "id": new_id, "library": "target", "stamp": "2026-10-03T10:00:00+00:00", "path": "A/T1 (77)",
        "formats": {"EPUB": "tgt/A/T1 (77)/t1.epub"}}})


def test_after_a_move_its_duplicates_are_trashed_into_the_copy_in_the_target():
    plan = make_plan()
    plan.items[1].selected = False  # executed later
    _moved_to_target(plan)
    assert plan.items[0].done and plan.moved == {1: 77} and plan.items[0] not in actionable(plan)
    dup = plan.items[1]
    assert not dup.match_planned and dup.match.id == 77 and dup.match.library == "tgt"
    dup.selected = True
    assert 2 not in blocked(plan)
    action = next(a for a in plan_actions(plan) if a["src_id"] == 2)
    assert action["target_id"] == 77 and action["target_stamp"] == "2026-10-03T10:00:00+00:00"
    assert "target_src_id" not in action


def test_a_copy_written_by_an_execution_gets_its_new_stamp_and_formats_in_every_row():
    target = book(10)
    plan = Plan("src", "tgt", "trash", [
        PlanItem(book(1, ("EPUB", "MOBI")), Action.TRASH, "dup", Identity(), match=target, add_formats=["MOBI"]),
        PlanItem(book(2, ("EPUB", "MOBI")), Action.TRASH, "dup", Identity(), match=replace(target),
                 add_formats=["MOBI"])])
    for b in (plan.items[0].match, plan.items[1].match):
        b.library = "tgt"
    apply_result(plan, plan.items[0], {"op": "trash", "ok": True, "kept": {
        "id": 10, "library": "target", "stamp": "2026-10-03T11:00:00+00:00", "path": "A/T10 (10)",
        "formats": {"EPUB": "e", "MOBI": "m"}}})
    other = plan.items[1].match
    assert other.last_modified == "2026-10-03T11:00:00+00:00" and set(other.formats) == {"EPUB", "MOBI"}
    assert [a["target_stamp"] for a in plan_actions(plan)] == ["2026-10-03T11:00:00+00:00"]


def test_a_book_cleaned_up_in_place_is_not_sent_again():
    plan = make_plan()
    plan.source_library = "lib"  # where book() puts the books
    item = plan.items[2]
    item.bad_formats = {"PDF": "empty file"}
    item.selected = True
    assert item in actionable(plan)
    apply_result(plan, item, {"op": "trash_formats", "ok": True, "stamp": "2026-10-03T12:00:00+00:00",
                              "formats": {"EPUB": "e", "MOBI": "m"}})
    assert item.source.last_modified == "2026-10-03T12:00:00+00:00" and set(item.source.formats) == {"EPUB", "MOBI"}
    assert item.bad_formats == {} and not item.done and item not in actionable(plan)


def test_ai_cache_is_model_independent():
    assert AICache.key("C:/books/book.epub", "start", "llama3") == AICache.key("C:/books/book.epub", "start", "gpt-4o")


def test_a_book_left_in_place_is_never_updated():
    plan = make_plan()
    item = plan.items[3]
    item.ai_used = True
    item.identity = Identity(title="New title", authors=["Alice Example"], ai_fields={"title", "authors"})
    item.selected = True
    assert not checkable(item) and item not in actionable(plan)
    assert all(a["src_id"] != item.source.id for a in plan_actions(plan))


def test_an_unticked_book_is_left_as_it_is():
    plan = make_plan()
    item = plan.items[0]
    item.bad_formats = {"PDF": "empty file"}
    item.source.formats["PDF"] = "x.PDF"
    assert [a.get("trash_formats") for a in plan_actions(plan) if a["src_id"] == 1] == [["PDF"]]  # with the move
    item.selected = False
    assert all(a["src_id"] != 1 for a in plan_actions(plan))  # nor its unreadable formats


def test_a_book_to_review_leaves_the_list_once_decided_and_it_is_remembered(tmp_path):
    plan = make_plan()
    item = plan.items[1]
    item.review, item.selected, item.planned_selected = "no edition data", False, False
    assert needs_review(item) and not is_changed(item)
    item.selected = True
    assert not needs_review(item)
    item.selected = False
    assert needs_review(item) and mark_reviewed(plan.items) == 1 and not needs_review(item)
    store = SelectionStore(tmp_path / "sel.json")
    store.save(plan)
    fresh = make_plan()
    fresh.items[1].review, fresh.items[1].selected, fresh.items[1].planned_selected = "no edition data", False, False
    store.apply(fresh)
    assert not needs_review(fresh.items[1]) and not fresh.items[1].selected
    revert_all(fresh)
    assert needs_review(fresh.items[1])


def test_unchecking_a_move_blocks_its_duplicates():
    plan = make_plan()
    plan.items[0].selected = False
    assert 2 in blocked(plan)
    assert actionable(plan) == []
    assert plan_actions(plan) == []


def test_force_trash_computes_formats_and_works_without_a_match():
    plan = make_plan()
    item = plan.items[2]
    override(item, Action.TRASH)
    assert item.action is Action.TRASH and item.manual and item.selected
    assert item.add_formats == ["EPUB", "MOBI"]  # PDF never added, target already has PDF
    assert item.reason.startswith("manual:") and "no copy in the target" not in item.reason
    lone = plan.items[3]  # no match: it just goes to the trash library
    assert lone.match is None
    override(lone, Action.TRASH)
    assert lone.action is Action.TRASH and lone.add_formats == [] and "no copy in the target" in lone.reason
    action = next(a for a in plan_actions(plan) if a["src_id"] == lone.source.id)
    assert action["op"] == "trash" and action["no_target"] and "target_id" not in action


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


def test_revert_all_undoes_every_change_and_forget_drops_books_not_analyzed(tmp_path):
    store = SelectionStore(tmp_path / "sel.json")
    plan = make_plan()
    plan.items[1].selected = False
    override(plan.items[3], Action.MOVE)
    plan.items[2].trash_bad = True
    store.save(plan)
    other = Plan("src2", "tgt", "trash", make_plan().items)
    override(other.items[3], Action.TRASH)
    store.save(other)

    partial = make_plan()
    partial.items = partial.items[:2]  # book 4's saved move is not in this plan
    assert store.apply(partial) == 1 and store.saved(partial) == 2
    assert revert_all(partial) == 1 and not partial.items[1].manual and partial.items[1].selected
    assert revert_all(partial) == 0
    store.forget(partial)
    assert store.saved(partial) == 0 and store.apply(make_plan()) == 0
    assert store.saved(other) == 1  # other libraries keep their choices

    assert revert_all(plan) == 3
    assert plan.items[3].action is Action.LEAVE and not plan.items[2].trash_bad


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


def test_trash_only_is_possible_even_with_formats_to_merge():
    plan = make_plan()
    item = plan.items[2]  # has EPUB and MOBI that its match lacks
    override(item, Action.TRASH, merge=False)
    assert item.action is Action.TRASH and item.add_formats == [] and action_label(item) == "Trash only"
    action = next(a for a in plan_actions(plan) if a["src_id"] == item.source.id)
    assert action["add_formats"] == [] and action["target_id"] == 100
    override(item, Action.TRASH)  # and back to Merge & Trash
    assert item.add_formats == ["EPUB", "MOBI"]


def test_a_planned_merge_can_become_trash_only_and_back():
    target = book(100, ("PDF",))
    item = PlanItem(book(3, ("EPUB", "PDF")), Action.TRASH, "dup", Identity(), match=target, add_formats=["EPUB"])
    override(item, Action.TRASH, merge=False)
    assert item.manual and item.add_formats == []
    override(item, Action.TRASH, merge=True)  # the analysis' own decision again
    assert not item.manual and item.add_formats == ["EPUB"]


def test_trash_only_is_remembered(tmp_path):
    store = SelectionStore(tmp_path / "sel.json")
    plan = make_plan()
    override(plan.items[2], Action.TRASH, merge=False)
    store.save(plan)
    fresh = make_plan()
    store.apply(fresh)
    assert fresh.items[2].action is Action.TRASH and fresh.items[2].add_formats == []
