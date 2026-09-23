"""Command-line interface: analyze (and optionally execute) without the GUI."""

from __future__ import annotations

import argparse
import logging
import sys

from .config import Settings
from .executor import execute_plan
from .models import Action
from .planner import build_plan
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
    ap.add_argument("--profile", help="AI profile name (default: the active one)")
    ap.add_argument("--no-ai", action="store_true", help="use metadata only")
    ap.add_argument("--report", help="write the plan as CSV to this file")
    ap.add_argument("--execute", action="store_true", help="perform the moves (default: dry run)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(message)s")
    if args.profile:
        settings.active_profile = args.profile
    if args.no_ai:
        settings.use_ai = False

    resolver = make_resolver(settings)
    try:
        def progress(done, total, msg):
            print(f"\r[{done}/{total}] {msg[:100]:<100}", end="", file=sys.stderr, flush=True)
        plan = build_plan(args.source, args.target, args.trash, resolver,
                          settings.ignore_subtitle, progress)
        print(file=sys.stderr)
    finally:
        if resolver:
            resolver.extractor.close()

    for item in plan.items:
        print(f"{item.action.value.upper():6} #{item.source.id:<6} {item.source.label()}\n        {item.reason}")
    print(f"\nMove: {plan.count(Action.MOVE)}  Trash: {plan.count(Action.TRASH)}  Leave: {plan.count(Action.LEAVE)}")

    if args.execute:
        ok, failed = execute_plan(plan, require_calibre_dir(settings), settings.update_metadata,
                                  settings.delete_permanently,
                                  on_result=lambda it, good, msg: print(f"#{it.source.id}: {msg}"))
        print(f"Done: {ok} succeeded, {failed} failed")
    if args.report:
        write_csv(plan, args.report)
        print(f"Report written to {args.report}")
    return 0
