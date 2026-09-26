"""calibre-review: check every book's metadata with the AI.

GUI by default; with --cli, a dry run that prints the differences (and, with
--execute, writes them)."""

from __future__ import annotations

import argparse
import logging
import sys


def run_cli(argv: list[str]) -> int:
    from .ai import AICache
    from .config import config_dir, load_review_settings
    from .review import (
        FIELDS, REVIEW_CACHE_FILE, ReviewAction, Reviewer, current_value, execute_review, format_value,
        review_cache, scan_library, summary,
    )
    from .session import make_resolver, require_calibre_dir

    for stream in (sys.stdout, sys.stderr):  # titles may not fit the console code page
        stream.reconfigure(errors="replace")
    settings = load_review_settings()
    ap = argparse.ArgumentParser(prog="calibre-review --cli", description=__doc__)
    ap.add_argument("--library", default=settings.review_library)
    ap.add_argument("--trash", default=settings.trash_library)
    ap.add_argument("--text-profile", help="profile of the text AI (default: as set in the GUI)")
    ap.add_argument("--image-profile", help="profile of the image AI, which reads covers; '' for none")
    ap.add_argument("--fields", default=",".join(settings.review_fields),
                    help=f"fields to change, comma-separated (of {','.join(FIELDS)})")
    ap.add_argument("--include-reviewed", action="store_true",
                    help="also read the books tagged AIReviewed (default: as set in the GUI)")
    ap.add_argument("--execute", action="store_true",
                    help="write the differences and tag every book read AIReviewed (default: dry run)")
    ap.add_argument("--clear-cache", action="store_true",
                    help="forget every saved AI answer of the review first (review_cache.json)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    if args.text_profile:
        settings.text_profile = args.text_profile
    if args.image_profile is not None:
        settings.image_profile = args.image_profile
    fields = {f.strip() for f in args.fields.split(",") if f.strip() in FIELDS}

    if args.clear_cache:
        removed = AICache.clear(config_dir() / REVIEW_CACHE_FILE)
        print(f"AI cache cleared: {removed:,} answers removed", file=sys.stderr)
    reviewer = make_resolver(settings, cls=Reviewer, cache=review_cache())
    if reviewer is None:
        print("The review needs an AI: set a text profile.", file=sys.stderr)
        return 2
    try:
        def progress(done, total, msg):
            print(f"\r[{done}/{total}] {msg[:100]:<100}", end="", file=sys.stderr, flush=True)
        result = scan_library(args.library, args.trash, reviewer, progress,
                              skip_reviewed=settings.review_skip_reviewed and not args.include_reviewed)
        print(file=sys.stderr)
    finally:
        reviewer.cache.save()
        reviewer.extractor.close()

    for item in result.items:
        if item.found is None:
            print(f"NOT READ #{item.book.id:<6} {item.book.label()}\n        {item.note}")
        elif item.changes:
            print(f"CHANGE   #{item.book.id:<6} {item.book.label()}")
            for name, value in item.changes.items():
                print(f"        {name:9} {format_value(name, current_value(item.book, name)) or '—'}"
                      f"  ->  {format_value(name, value)}")
    print(summary(result))
    if args.execute:
        for item in result.items:
            item.selected = item.action is ReviewAction.UPDATE
        ok, failed, tagged = execute_review(result, fields, require_calibre_dir(settings),
                                    on_result=lambda it, good, msg: print(f"#{it.book.id}: {msg}"))
        print(f"Done: {ok} succeeded, {failed} failed, {tagged} more books tagged AIReviewed")
    return 0


def main() -> int:
    if "--cli" in sys.argv[1:]:
        return run_cli([a for a in sys.argv[1:] if a != "--cli"])
    from .gui.review_window import run_review_gui
    return run_review_gui()


if __name__ == "__main__":
    sys.exit(main())
