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

"""Command-line interface: analyze (and optionally execute) without the GUI."""

from __future__ import annotations

import argparse
import logging
import sys

from . import perf, tempdirs
from .ai import AICache
from .config import Settings, config_dir
from .executor import execute_plan
from .models import Action
from .planner import build_plan, run_summary
from .report import write_csv
from .session import make_resolver, require_calibre_dir


def run(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):  # titles may not fit the console code page
        stream.reconfigure(errors="replace")
    settings = Settings.load()
    ap = argparse.ArgumentParser(prog="calibre-dedup --cli", description=__doc__)
    ap.add_argument("--source", default=settings.source_library)
    ap.add_argument("--target", default=settings.target_library)
    ap.add_argument("--trash", default=settings.trash_library)
    ap.add_argument("--text-profile", help="profile of the text AI (default: as set in the GUI)")
    ap.add_argument("--image-profile",
                    help="profile of the image AI, for covers and scanned PDFs; '' for none (default: as in the GUI)")
    ap.add_argument("--no-ai", action="store_true", help="use metadata only")
    ap.add_argument("--cleanup-only", action="store_true", default=None,
                    help="only send the source books already in the target to the trash library; "
                         "copy nothing to the target (default: as set in the GUI)")
    ap.add_argument("--clear-cache", action="store_true",
                    help="forget every saved AI answer first (ai_cache.json): the AI is asked again")
    ap.add_argument("--no-cache", action="store_true",
                    help="don't use the AI cache (for tests): every question goes to the AI, "
                         "ai_cache.json is neither read nor written")
    ap.add_argument("--unpack", action="store_true",
                    help="unpack every clear RAR/ZIP/7Z archive of a book (default: archives are left as they are)")
    ap.add_argument("--report", help="write the plan as CSV to this file")
    ap.add_argument("--execute", action="store_true", help="perform the moves (default: dry run)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(message)s")
    perf.configure("calibre_dedup_perf.log")
    tempdirs.sweep()
    if args.text_profile:
        settings.text_profile = args.text_profile
    if args.image_profile is not None:
        settings.image_profile = args.image_profile
    if args.no_ai:
        settings.text_profile = ""

    if args.clear_cache:
        print(f"AI cache cleared: {AICache.clear(config_dir() / 'ai_cache.json'):,} answers removed", file=sys.stderr)
    resolver = make_resolver(settings, cache=AICache(off=True) if args.no_cache else None)
    try:
        def progress(done, total, msg):
            print(f"\r[{done}/{total}] {msg[:100]:<100}", end="", file=sys.stderr, flush=True)
        plan = build_plan(args.source, args.target, args.trash, resolver,
                          settings.ignore_subtitle, progress,
                          similar_matching=settings.similar_matching, cover_check=settings.cover_check,
                          recheck_years=settings.recheck_years, same_series=settings.same_series,
                          similar_titles=settings.similar_titles, always_cover=settings.always_cover,
                          author_variants=settings.author_variants, fix_swapped=settings.fix_swapped,
                          trash_unreadable=settings.trash_unreadable,
                          cleanup_only=settings.cleanup_only if args.cleanup_only is None else True,
                          unpack=(lambda n: True) if args.unpack else None)
        print(file=sys.stderr)
    finally:
        if resolver:
            resolver.extractor.close()

    for item in plan.items:
        print(f"{item.action.value.upper():6} #{item.source.id:<6} {item.source.label()}\n        {item.reason}")
    print(f"\nMove: {plan.count(Action.MOVE)}  Trash: {plan.count(Action.TRASH)}  Leave: {plan.count(Action.LEAVE)}")
    info, warnings = run_summary(plan)
    print(f"Checks: {info}")
    for w in warnings:
        print(f"Warning: {w}", file=sys.stderr)
    if plan.stop_reason:
        print(f"Analysis stopped after {len(plan.items)} of {plan.total_books} books: {plan.stop_reason}",
              file=sys.stderr)
        if args.execute:
            print("Not executing: check the drive and analyze again.", file=sys.stderr)
            return 1

    if args.execute:
        ok, failed = execute_plan(plan, require_calibre_dir(settings), settings.update_metadata,
                                  settings.delete_permanently,
                                  on_result=lambda it, good, msg: print(f"#{it.source.id}: {msg}"))
        print(f"Done: {ok} succeeded, {failed} failed")
    if args.report:
        write_csv(plan, args.report)
        print(f"Report written to {args.report}")
    return 0
