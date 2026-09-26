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
from calibre.utils.date import as_local_time, local_tz

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


def set_metadata(db, book_id, values):
    """Overwrite fields with the reviewed values (calibre-review). The year keeps
    the date's month and day. Returns the changed fields, the book's folder and
    its formats' paths (a new title or author renames them)."""
    changed = []
    for name in ("title", "authors", "publisher", "series", "series_index"):
        if name in values:
            db.set_field(name, {book_id: values[name]})
            changed.append(name)
    if values.get("year"):
        year = int(values["year"])
        pubdate = db.field_for("pubdate", book_id)
        if getattr(pubdate, "year", 0) >= 1400:
            # In local time, as Calibre shows it: 1 January 1977 00:00 in Italy is stored
            # as 1976-12-31 23:00 UTC; changing the UTC year would leave it showing 1977.
            pubdate = as_local_time(pubdate)
            try:
                new = pubdate.replace(year=year)
            except ValueError:  # 29 February
                new = pubdate.replace(year=year, day=28)
        else:
            new = datetime(year, 1, 1, 12, tzinfo=local_tz)  # noon: the same year in every time zone
        db.set_field("pubdate", {book_id: new})
        changed.append("year")
    formats = {fmt: db.format_abspath(book_id, fmt) for fmt in db.formats(book_id)}
    return changed, db.field_for("path", book_id), formats


def add_tag(db, book_ids, tag):
    """Add `tag` to these books (calibre-review's "reviewed" mark), keeping their other tags."""
    values = {}
    for book_id in book_ids:
        tags = tuple(db.field_for("tags", book_id) or ())
        if tag.casefold() not in {t.casefold() for t in tags}:
            values[book_id] = tags + (tag,)
    if values:
        db.set_field("tags", values)


def main(plan_path):
    with open(plan_path, encoding="utf-8") as f:
        plan = json.load(f)
    # calibre-review has no target library; a trash library only when books go to it
    for path in (plan.get("target"), plan.get("trash")):
        if path:
            os.makedirs(path, exist_ok=True)
    src = open_db(plan["source"]).new_api
    tgt = open_db(plan["target"]).new_api if plan.get("target") else None
    trash = open_db(plan["trash"]).new_api if plan.get("trash") else None
    permanent = bool(plan.get("permanent"))
    moved = {}  # source id -> new id in target
    removed_folders = []  # source book folders, relative to the library
    stop_file = plan_path + ".stop"

    try:
        for action in plan["actions"]:
            if os.path.exists(stop_file):
                emit(event="stopped")
                break
            if action["op"] == "tag":  # calibre-review: the reviewed books with nothing to write
                try:
                    ids = [i for i in action["src_ids"] if i in src.all_book_ids()]
                    add_tag(src, ids, action["tag"])
                    emit(event="tagged", src_ids=ids, ok=True, msg=f"tagged {action['tag']}")
                except Exception as e:
                    emit(event="tagged", src_ids=action["src_ids"], ok=False, msg=str(e),
                         trace=traceback.format_exc())
                continue
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
                elif action["op"] == "set":  # calibre-review: the book stays, its metadata changes
                    changed, path, formats = set_metadata(src, sid, action["set"])
                    if action.get("tag"):  # only once the update is written
                        add_tag(src, [sid], action["tag"])
                    msg = f"updated {', '.join(changed) or 'nothing'}" + (f"; tagged {action['tag']}"
                                                                          if action.get("tag") else "")
                    emit(event="result", src_id=sid, ok=True, msg=msg, path=path, formats=formats)
                    continue
                elif action["op"] == "trash" and action.get("no_target"):
                    # Forced by the user for a book with no copy in the target.
                    new_id = copy_verified(src, sid, trash)
                    msg = f"moved to trash (id {new_id})" + ("; no copy in the target" if tgt is not None else "")
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
            if db is not None:
                db.close()
    emit(event="cleanup_start")
    removed, deferred = cleanup_empty_dirs(plan["source"], removed_folders)
    emit(event="cleanup", removed=removed, deferred=deferred)
    emit(event="done")


if __name__ == "__main__":
    main(sys.argv[1])
