"""Executes a plan inside Calibre's own Python environment.

Run as:  calibre-debug bridge_script.py plan.json

Uses Calibre's library API (the same code as Calibre's "Copy to library"), so
all metadata, covers, formats and custom columns are preserved. A book is only
removed from the source after its copy has been verified.

Output: one line per action, prefixed with "@@CDR " and followed by JSON.
Creating "<plan.json>.stop" stops execution before the next action.
"""

import json
import os
import sys
import time
import traceback
from datetime import datetime

from calibre.db.copy_to_library import copy_one_book
from calibre.library import db as open_db

UNKNOWN = {"", "unknown", "sconosciuto", "inconnu", "unbekannt", "desconocido", "desconhecido", "onbekend"}


def emit(**kw):
    sys.stdout.write("@@CDR " + json.dumps(kw) + "\n")
    sys.stdout.flush()


def is_unknown(value):
    return value is None or str(value).strip().casefold() in UNKNOWN


def copy_verified(src, book_id, dest):
    formats = set(src.formats(book_id))
    result = copy_one_book(book_id, src, dest, duplicate_action="add")
    new_id = result.get("new_book_id")
    if not new_id or new_id not in dest.all_book_ids():
        raise RuntimeError(f"copy failed: {result}")
    missing = formats - set(dest.formats(new_id))
    if missing:
        raise RuntimeError(f"formats missing after copy: {', '.join(sorted(missing))}")
    return new_id


def cleanup_empty_dirs(root, folders, attempts=3):
    """Remove the removed books' folders (and their author folders) if empty,
    after all database work is finished. Only these are checked: walking a
    large library can take many minutes."""
    removed = 0
    deferred = set()
    root = os.path.abspath(root)
    todo = set()
    for folder in folders:
        book_dir = os.path.abspath(os.path.join(root, folder))
        for d in (book_dir, os.path.dirname(book_dir)):
            if os.path.dirname(d).startswith(root) and d != root:
                todo.add(d)
    for attempt in range(attempts):
        # Deepest first, so an author folder is tried after its book folders.
        candidates = sorted((d for d in todo if os.path.isdir(d)), key=lambda d: d.count(os.sep), reverse=True)
        if not candidates:
            break
        deferred.clear()
        for current in candidates:
            try:
                if not os.listdir(current):
                    os.rmdir(current)
                    removed += 1
                todo.discard(current)  # removed, or not empty: other books live there
            except OSError:
                deferred.add(current)
        if not deferred:
            break
        if attempt + 1 < attempts:
            time.sleep(0.5 * (attempt + 1))
    return removed, len(deferred)


def fill_metadata(db, book_id, values):
    """Set AI-found values, only where the book's field is empty/unknown."""
    changed = []
    if values.get("title") and is_unknown(db.field_for("title", book_id)):
        db.set_field("title", {book_id: values["title"]})
        changed.append("title")
    if values.get("authors") and all(is_unknown(a) for a in db.field_for("authors", book_id)):
        db.set_field("authors", {book_id: values["authors"]})
        changed.append("authors")
    if values.get("publisher") and is_unknown(db.field_for("publisher", book_id)):
        db.set_field("publisher", {book_id: values["publisher"]})
        changed.append("publisher")
    if values.get("year"):
        pubdate = db.field_for("pubdate", book_id)
        current_year = getattr(pubdate, "year", None)
        if current_year is None or current_year < 1400:
            db.set_field("pubdate", {book_id: datetime(int(values["year"]), 1, 1)})
            changed.append("year")
    if values.get("isbn"):
        ids = dict(db.field_for("identifiers", book_id) or {})
        if "isbn" not in ids:
            ids["isbn"] = values["isbn"]
            db.set_field("identifiers", {book_id: ids})
            changed.append("isbn")
    return changed


def main(plan_path):
    with open(plan_path, encoding="utf-8") as f:
        plan = json.load(f)
    for path in (plan["target"], plan["trash"]):
        os.makedirs(path, exist_ok=True)
    src = open_db(plan["source"]).new_api
    tgt = open_db(plan["target"]).new_api
    trash = open_db(plan["trash"]).new_api
    permanent = bool(plan.get("permanent"))
    moved = {}  # source id -> new id in target
    removed_folders = []  # source book folders, relative to the library
    stop_file = plan_path + ".stop"

    try:
        for action in plan["actions"]:
            if os.path.exists(stop_file):
                emit(event="stopped")
                break
            sid = action["src_id"]
            try:
                if sid not in src.all_book_ids():
                    raise RuntimeError("book is no longer in the source library")
                if src.field_for("title", sid) != action["title"]:
                    raise RuntimeError("book changed since the analysis; re-run the analysis")

                if action["op"] == "move":
                    new_id = copy_verified(src, sid, tgt)
                    changed = fill_metadata(tgt, new_id, action.get("set") or {})
                    moved[sid] = new_id
                    msg = f"moved to target (id {new_id})"
                    if changed:
                        msg += f"; filled {', '.join(changed)}"
                elif action["op"] == "update":
                    changed = fill_metadata(src, sid, action.get("set") or {})
                    msg = f"updated metadata in source"
                    if changed:
                        msg += f"; filled {', '.join(changed)}"
                elif action["op"] == "trash":
                    tid = action.get("target_id") or moved.get(action.get("target_src_id"))
                    if tid is None or tid not in tgt.all_book_ids():
                        raise RuntimeError("the matching target book was not found")
                    added = []
                    for fmt in action.get("add_formats") or []:
                        path = src.format_abspath(sid, fmt)
                        if path and fmt not in tgt.formats(tid):
                            tgt.add_format(tid, fmt, path, replace=False)
                            added.append(fmt)
                    new_id = copy_verified(src, sid, trash)
                    msg = f"moved to trash (id {new_id})"
                    if added:
                        msg += f"; added {', '.join(added)} to target book {tid}"
                else:
                    raise RuntimeError(f"unknown op {action['op']!r}")

                removed_folders.append(src.field_for("path", sid))
                try:
                    src.remove_books([sid], permanent=permanent)
                except PermissionError:
                    # Calibre can remove the database record and files, then fail
                    # only while deleting an empty folder locked by Windows.
                    if sid in src.all_book_ids():
                        raise
                    msg += "; source record removed, empty folder cleanup deferred"
                emit(event="result", src_id=sid, ok=True, msg=msg)
            except Exception as e:
                emit(event="result", src_id=sid, ok=False, msg=str(e), trace=traceback.format_exc())
    finally:
        for db in (src, tgt, trash):
            db.close()
    emit(event="cleanup_start")
    removed, deferred = cleanup_empty_dirs(plan["source"], removed_folders)
    emit(event="cleanup", removed=removed, deferred=deferred)
    emit(event="done")


if __name__ == "__main__":
    main(sys.argv[1])
