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

"""The Judge AI (judge.py, review.judge_ai): what it is told, and what its answer does."""

import json

from calibre_dedup.ai import AICache
from calibre_dedup.judge import JUDGE_REVIEW, Judgement, apply_judgement, judge_pair, pair_request
from calibre_dedup.library_cache import TextPrints
from calibre_dedup.models import Action
from calibre_dedup.planner import build_plan
from calibre_dedup.review import ReviewItem, Reviewer, judge_ai
from calibre_dedup.selection import needs_review
from tests.test_planner import libs  # noqa: F401  (a fixture)
from tests.test_review import REPLY, FakeExtractor, FakeProvider, book, meta

ANSWER = json.dumps({"verdict": "duplicate", "same_work": "yes", "same_edition": "yes", "better": "TARGET",
                     "confidence": 95, "evidence": ["same text", "both covers are a text page"],
                     "covers": {"source": "text page", "target": "text page"}})


class WholeText:
    """An extractor reading EPUBs without Calibre (extract.whole_text)."""

    def whole_text(self, fmt, path):
        from calibre_dedup.extract import whole_text
        return whole_text(fmt, path) or ""


def test_the_answer_is_read_and_summed_up():
    j = Judgement.from_json("thinking... " + ANSWER, "DeepSeek")
    assert (j.verdict, j.confidence, j.better, j.model) == ("duplicate", 95, "TARGET", "DeepSeek")
    assert j.covers == "source: text page; target: text page"
    assert j.summary() == "duplicate (95%): same text; both covers are a text page"
    assert Judgement.from_json("no json").verdict == "unsure"
    assert Judgement.from_json('{"verdict": "maybe", "confidence": "high"}').summary() == "unsure"


def _undecided(libs):
    src, tgt, trash = libs(source=[{"title": "Dune", "year": 1965, "publisher": "Ace", "prose": "one",
                                    "formats": ["EPUB", "MOBI"]}],
                           target=[{"title": "Dune", "year": 2005, "publisher": "Ace", "prose": "two"}])
    from tests.test_planner import LibraryResolver
    plan = build_plan(src, tgt, trash, LibraryResolver({}), recheck_years=True)
    assert plan.items[0].action is Action.LEAVE and plan.items[0].match is not None
    return plan


def test_the_judge_is_told_everything_known_about_both_books(libs, tmp_path):
    plan = _undecided(libs)
    item = plan.items[0]
    text, images = pair_request(item, WholeText(), AICache(tmp_path / "c.json"), TextPrints(None, WholeText()))
    assert text.startswith("FACTS:") and "the analysis left it as: same title/authors" in text
    assert "share of the same text, both ways:" in text  # different texts: a low share
    assert "=== BOOK SOURCE" in text and "=== BOOK TARGET" in text and '"year": 1965' in text
    assert "first pages of its EPUB" in text and images == []


def test_a_duplicate_verdict_becomes_an_unticked_merge_and_trash_to_review(libs):
    plan = _undecided(libs)
    item = plan.items[0]
    provider = FakeProvider(ANSWER, name="DeepSeek")
    apply_judgement(plan, item, judge_pair(provider, item, WholeText()))
    assert item.action is Action.TRASH and item.add_formats == ["MOBI"] and not item.selected
    assert item.review == JUDGE_REVIEW and needs_review(item) and "per the Judge AI (DeepSeek)" in item.reason
    item.selected = True  # the user agrees
    assert not needs_review(item)


def test_a_different_verdict_moves_the_book_unticked(libs):
    plan = _undecided(libs)
    item = plan.items[0]
    apply_judgement(plan, item, Judgement(verdict="different", model="J"))
    assert item.action is Action.MOVE and item.different and not item.selected and needs_review(item)


def test_an_unsure_verdict_only_adds_to_the_reason(libs):
    plan = _undecided(libs)
    item = plan.items[0]
    before = item.action
    apply_judgement(plan, item, Judgement(verdict="unsure", confidence=40, model="J"))
    assert item.action is before and item.reason.endswith("[Judge AI (J): unsure (40%)]")


def test_a_record_never_moved_is_not_moved_by_the_judge(libs):
    plan = _undecided(libs)
    item = plan.items[0]
    item.file_name_title = "ITABOOK 0052 - Herbert"
    apply_judgement(plan, item, Judgement(verdict="different", model="J"))
    assert item.action is Action.LEAVE


def test_the_review_judge_reads_again_with_hints_and_suggests_unticked(tmp_path):
    provider = FakeProvider(REPLY, name="DeepSeek")
    reviewer = Reviewer(provider, FakeExtractor("FRANK HERBERT\nDUNE\n1965"), AICache(tmp_path / "c.json"))
    old = ReviewItem(book(), meta())
    [(was, new)] = judge_ai(reviewer, [old])
    user, _ = provider.calls[0]
    assert user.startswith("Hints, which may be wrong") and "Calibre's metadata now:" in user
    assert "A first reading by a smaller model:" in user and "Text of the last pages:" in user
    assert was is old and new.changes.get("title") == "Dune" and not new.selected
    assert new.judged == "DeepSeek" and new.needs_review and "read by the Judge AI (DeepSeek)" in new.note
    assert reviewer.hints is None  # only for the judge
