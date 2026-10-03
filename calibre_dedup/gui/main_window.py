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

from __future__ import annotations

import html
import logging
import os
import sys
import threading
import time
from collections import Counter
from dataclasses import replace
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import (
    QAbstractTableModel, QByteArray, QEvent, QModelIndex, QObject, QSortFilterProxyModel, Qt, QThread, QTimer,
    QUrl, Signal,
)
from PySide6.QtGui import QColor, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QGridLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QSizePolicy, QSplitter, QTableView, QVBoxLayout, QWidget,
)

from .. import perf, tempdirs
from ..awake import keep_awake
from ..ai import AICache, AIError, make_provider
from ..calibre_env import calibre_is_running, known_libraries
from ..config import Settings, config_dir, library_cache_dir
from ..eta import Eta
from ..executor import execute_plan
from ..library import library_tags, tag_filter_text
from ..library_use import Conflict, LibraryInUse, LibraryUse, execution_conflicts, same_library
from ..extract import TextExtractor
from ..models import Action, Plan, PlanItem
from ..normalize import strip_accents
from ..pause import Pause
from ..judge import Judgement, apply_judgement, judge_pair
from ..library_cache import TextPrints
from ..planner import (
    AI_OFF, MAX_CONSECUTIVE_AI_ERRORS, RETRY, SKIP_BOOK, build_plan, check_libraries, redecide_ids,
    run_summary,
)
from ..report import write_csv
from ..selection import (
    FILTER_LABELS, MERGE, MERGE_LABEL, SelectionStore, action_label, actionable, blocked, blocked_reason,
    can_override, checkable, is_changed, mark_reviewed, needs_review, runs_main_action, filter_key,
    mergeable_formats, override, revert, revert_all,
)
from ..session import analysis_signature, changed_settings, make_resolver, preflight, require_calibre_dir
from ..version import app_version
from .cover_preview import CoverPreview
from .filters import STATUS_ENTRIES, STATUS_TIP, Entry, FilterButton, showing_text, status_keys
from .filters import filter_row as make_filter_row
from .icons import DEDUP, app_icon, set_taskbar_identity
from .settings_dialog import SettingsDialog
from .shutdown_box import ShutdownWhenDone
from .style import BLUE, GREEN, RED, SLATE, button_css, mark_inactive, set_running, style_none_item

log = logging.getLogger("calibre_dedup")

ACTION_COLORS = {Action.MOVE: "#2e7d32", Action.TRASH: "#c62828", Action.LEAVE: "#8d6e00"}


def _compact(combo: QComboBox, chars: int) -> QComboBox:
    """Don't let the longest entry set the width (long paths or profile names pushed
    the window off-screen). The list still shows entries in full; the tooltip too."""
    combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
    combo.setMinimumContentsLength(chars)
    view = combo.view()
    view.setTextElideMode(Qt.ElideNone)

    def fit_list(*_):
        view.setMinimumWidth(view.sizeHintForColumn(0) + 30)
    combo.model().rowsInserted.connect(fit_list)
    combo.currentTextChanged.connect(combo.setToolTip)
    return combo


TAG_PLACEHOLDER = "all books"


def tag_box(value: str, tip: str) -> QComboBox:
    """The "Only books tagged" box: type a tag or pick one of the library's (see fill_tags)."""
    box = QComboBox()
    box.setEditable(True)
    box.setInsertPolicy(QComboBox.NoInsert)
    box.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
    box.setMinimumContentsLength(20)
    box.lineEdit().setPlaceholderText(TAG_PLACEHOLDER)
    box.lineEdit().setClearButtonEnabled(True)
    box.setEditText(value)
    box.setToolTip(tip)
    box.setProperty("tags_of", "")
    return box


def tag_mode_box(exclude: bool, books: str, tip: str) -> QComboBox:
    """In front of the tag box: "Only <books> tagged" or "All <books> except tagged"."""
    box = QComboBox()
    box.addItem(f"Only {books} tagged", False)
    box.addItem(f"All {books} except tagged", True)
    box.setCurrentIndex(1 if exclude else 0)
    box.setToolTip(tip)
    return box


def fill_tags(box: QComboBox, library: str) -> None:
    """List the tags of `library` in the box (once per library), keeping what is typed."""
    library = library.strip()
    if box.property("tags_of") == library:
        return
    box.setProperty("tags_of", library)
    text = box.currentText()
    box.blockSignals(True)
    box.clear()
    box.addItem("")  # all books
    box.addItems(library_tags(library) if library else [])
    box.setEditText(text)
    box.blockSignals(False)


def _elastic(label: QLabel) -> QLabel:
    """A label whose text never widens the window; long text is cut at the edge."""
    label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
    label.setMinimumWidth(1)
    return label


# --- logging into the GUI -----------------------------------------------------
# Lines from or about the AI in the log panel: purple, dark on a light background,
# light on a dark one (Windows dark mode), so they stay readable in both.
AI_LOG_COLOR = {"light": "#7b1fa2", "dark": "#d7a6ff"}


class _LogSignal(QObject):
    message = Signal(str, bool)  # text, from/about the AI


def is_ai_record(record: logging.LogRecord) -> bool:
    """AI calls and replies, and what the analysis says about them ("AI reading…",
    "AI error on…", "image AI disabled…", the "AI: text = …" setup line)."""
    return record.name.endswith(".ai") or record.getMessage().startswith(("AI ", "AI:", "image AI"))


class QtLogHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.signal = _LogSignal()
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))

    def emit(self, record):
        self.signal.message.emit(self.format(record), is_ai_record(record))


def ask_ai_down(parent, message: str, what: str, more_help: str) -> str | None:
    """The "AI not responding" dialog: RETRY, SKIP_BOOK, AI_OFF, or None for Stop."""
    box = QMessageBox(QMessageBox.Warning, "AI not responding", message, parent=parent)
    box.setInformativeText(
        "Retry: try the same request again (e.g. after starting the server).\n"
        f"Skip this book: go on to the next book with the {what}; you are asked again only "
        f"if it fails {MAX_CONSECUTIVE_AI_ERRORS} more times in a row.\n" + more_help)
    retry = box.addButton("Retry", QMessageBox.AcceptRole)
    skip = box.addButton("Skip this book", QMessageBox.AcceptRole)
    go_on = box.addButton(f"Continue without the {what}", QMessageBox.RejectRole)
    stop = box.addButton("Stop", QMessageBox.DestructiveRole)
    box.setDefaultButton(skip)
    box.setEscapeButton(skip)
    box.exec()
    clicked = box.clickedButton()
    choice = {retry: RETRY, skip: SKIP_BOOK, go_on: AI_OFF}.get(clicked, SKIP_BOOK if clicked is not stop else None)
    log.info("AI not responding: user chose %s", {RETRY: "retry", SKIP_BOOK: "skip this book",
                                                    AI_OFF: f"continue without the {what}", None: "stop"}[choice])
    return choice


def ask_unpack(parent, count: int) -> bool:
    """The "Unpack the archives?" question, asked once before the first book."""
    books = "1 book is" if count == 1 else f"{count} books are"
    box = QMessageBox(QMessageBox.Question, "Unpack the archives?",
                      f"{books} stored as a RAR, ZIP or 7Z archive. Unpack them?", parent=parent)
    box.setInformativeText(
        "Nothing is written now: the books are analyzed with the files inside, and the archives "
        "are unpacked on Execute. Unclear archives (several books, nothing readable, password…) "
        "are kept and flagged. Right-click a book to change your mind.")
    yes = box.addButton("Unpack", QMessageBox.YesRole)
    box.addButton("Keep the archives", QMessageBox.NoRole)
    box.setDefaultButton(yes)
    box.exec()
    return box.clickedButton() is yes


def ask_other_trash(parent, program: str, conflicts: list[Conflict], trash_box: QComboBox) -> bool:
    """Another execution writes a library of ours. Only the trash library can be changed
    without analyzing again (the analysis never reads it): offer that. True = a new
    trash library was put in `trash_box`, check again."""
    text = "\n\n".join(c.describe(program) for c in conflicts)
    if any(c.role != "trash" for c in conflicts):
        QMessageBox.warning(parent, "Library in use", f"{text}\n\nWait until that execution has finished, "
                                                      "then execute again.")
        return False
    box = QMessageBox(QMessageBox.Warning, "Trash library in use", text, parent=parent)
    box.setInformativeText("Choose another trash library to execute now, without analyzing again, "
                           "or wait until that execution has finished.")
    choose = box.addButton("Choose another trash library…", QMessageBox.AcceptRole)
    box.addButton(QMessageBox.Cancel)
    box.setDefaultButton(choose)
    box.exec()
    if box.clickedButton() is not choose:
        return False
    d = QFileDialog.getExistingDirectory(parent, "Trash library", trash_box.currentText())
    if not d:
        return False
    trash_box.setEditText(d)
    return True


def no_cache_box() -> QCheckBox:
    """"Don't use the saved AI answers": for tests. Red while on; not remembered (off at each
    start), so that it can't stay on by mistake and have every analysis ask the AI again."""
    box = QCheckBox("No AI cache")
    box.setToolTip("For tests: the saved answers are not used, every question goes to the AI.\n"
                   "Its answers are still saved, replacing the old ones for the same questions.\n"
                   "Not remembered: off each time the program starts.")
    box.setStyleSheet(f"QCheckBox:checked {{ color: {RED[0]}; font-weight: bold; }}")
    return box


class UnpackQuestion:
    """Mixed into the analysis workers: asks the window, once before the first book,
    whether to unpack the archives (the worker waits)."""
    unpack_asked: Signal  # the number of books stored as an archive

    def _init_unpack(self) -> None:
        self._unpack_yes = False
        self._unpack_answered = threading.Event()

    def ask_unpack(self, count: int) -> bool:
        self._unpack_answered.clear()
        self._unpack_yes = False
        self.unpack_asked.emit(count)
        while not self._unpack_answered.wait(0.2):
            if self.cancel.is_set():  # Stop, or the window is closing
                return False
        return self._unpack_yes

    def answer_unpack(self, yes: bool) -> None:
        self._unpack_yes = yes
        self._unpack_answered.set()


def configure_logging(handler: QtLogHandler, filename: str) -> None:
    """Log to the window and to a rotating file in the data folder; the AI's
    performance to its own file beside it (calibre_dedup.log -> calibre_dedup_perf.log)."""
    perf.configure(Path(filename).stem + "_perf.log")
    file_handler = RotatingFileHandler(config_dir() / filename, maxBytes=5_000_000,
                                       backupCount=3, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    requested_level = os.environ.get("CALIBRE_DEDUP_LOG_LEVEL", "").upper()
    level = getattr(logging, requested_level, logging.DEBUG if "--debug" in sys.argv[1:] else logging.INFO)
    root.setLevel(level)
    root.addHandler(handler)
    root.addHandler(file_handler)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    log.info("Logging configured at %s", logging.getLevelName(level))
    tempdirs.sweep()  # folders left by a run that was killed


def open_file(path: str) -> None:
    """Open with the associated app, off the GUI thread: on Windows the shell
    can block until the viewer answers, and a busy viewer would freeze us."""
    log.info("Opening %s", path)
    if sys.platform != "win32":  # Qt's opener belongs on the GUI thread; it doesn't block there
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(path)):
            log.warning("Could not open %s: no associated application", path)
        return

    def run():
        try:
            os.startfile(path)
        except OSError as e:
            log.warning("Could not open %s: %s", path, e)
    threading.Thread(target=run, daemon=True, name="open-file").start()


def with_eta(status: str, eta: str) -> str:
    """"Book 12 of 1500: Analyzing …" -> "Book 12 of 1500 · about 3 h 10 min left: Analyzing …"
    (before the title, which is cut first when the window is narrow)."""
    if not eta or not status.startswith("Book "):
        return status
    head, sep, rest = status.partition(": ")
    return f"{head} · {eta}{sep}{rest}"


# --- table model ----------------------------------------------------------------


def _is_checked(value) -> bool:
    if isinstance(value, int):
        return value == Qt.Checked.value
    return value == Qt.Checked


class PlanModel(QAbstractTableModel):
    HEADERS = ["✓", "ID", "Title", "Authors", "Action", "Reason", "Match in target", "Formats", "AI", "Result"]
    COL_CHECK, COL_ID, COL_TITLE, COL_AUTHORS, COL_ACTION, COL_REASON, COL_RESULT = 0, 1, 2, 3, 4, 5, 9
    selection_changed = Signal()

    def __init__(self):
        super().__init__()
        self.plan: Plan | None = None
        self.items: list[PlanItem] = []
        self.blocked: dict[int, str] = {}
        self.locked = False  # no edits while executing or after execution
        self._rows: dict[int, int] = {}
        self._by_id: dict[int, PlanItem] = {}  # source book id -> item, for blocked markers
        self._sort_column, self._sort_order = self.COL_ID, Qt.AscendingOrder
        self._sorted = True  # items are in (_sort_column, _sort_order) order
        self.italic_font = QFont()  # set from the view, marks manual overrides

    def set_plan(self, plan: Plan | None):
        self.beginResetModel()
        self.plan = plan
        self.items = list(plan.items) if plan else []
        self._by_id = {it.source.id: it for it in self.items}
        self.blocked = blocked(plan) if plan else {}
        self._sort_items()  # keep the column the user sorted by
        self.locked = False
        self.endResetModel()

    def start_live(self, plan: Plan):
        """An empty table that rows are appended to while the analysis runs. It can be
        edited (ticks, overrides) but not sorted until the end."""
        self.beginResetModel()
        self.plan = plan
        self.items, self._rows, self._by_id, self.blocked = [], {}, {}, {}
        self.locked = False
        self.endResetModel()

    def append_items(self, items: list[PlanItem]):
        """Add books as they are decided, in arrival order (unsorted until the end)."""
        if not items:
            return
        n = len(self.items)
        self.beginInsertRows(QModelIndex(), n, n + len(items) - 1)
        self.items.extend(items)
        self.plan.items.extend(items)
        for row, it in enumerate(items, n):
            self._rows[id(it)] = row
            self._by_id[it.source.id] = it
        for it in items:  # e.g. a duplicate of a move the user already unticked
            why = blocked_reason(it, self._by_id)
            if why:
                self.blocked[it.source.id] = why
        self._sorted = False
        self.endInsertRows()

    def remove_done(self) -> int:
        """Drop the books executed successfully: they have left the source library.
        Failed, unticked and Leave rows stay (a row may be "OK" when only its cleanup
        ran: unreadable formats, archive). Returns how many were removed."""
        if not self.plan:
            return 0

        def gone(it: PlanItem) -> bool:
            return it.done
        keep = [it for it in self.plan.items if not gone(it)]
        removed = len(self.plan.items) - len(keep)
        if removed:
            self.beginResetModel()
            self.plan.items = keep
            self.items = [it for it in self.items if not gone(it)]
            self._rows = {id(it): row for row, it in enumerate(self.items)}
            self._by_id = {it.source.id: it for it in self.items}
            self.blocked = blocked(self.plan)
            self.endResetModel()
        return removed

    def refresh(self):
        """Recompute dependencies and repaint every row after a selection/override change."""
        self.blocked = blocked(self.plan) if self.plan else {}
        if self.items:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.items) - 1, len(self.HEADERS) - 1))
        self.selection_changed.emit()

    def item_changed(self, item: PlanItem):
        row = self._rows[id(item)]
        self.dataChanged.emit(self.index(row, 0), self.index(row, len(self.HEADERS) - 1))

    def set_checked(self, items: list[PlanItem], value: bool | None):
        """Check (True), uncheck (False) or invert (None) items; those that can't be ticked are skipped."""
        if self.locked:
            return
        for it in items:
            if checkable(it):
                it.selected = (not it.selected) if value is None else value
        self.refresh()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.items)

    def columnCount(self, parent=QModelIndex()):
        return len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.HEADERS[section]
        return None

    def flags(self, index):
        f = super().flags(index)
        it = self.items[index.row()]
        if index.column() == self.COL_CHECK and checkable(it) and not self.locked:
            f |= Qt.ItemIsUserCheckable
        return f

    def setData(self, index, value, role=Qt.EditRole):
        if role == Qt.CheckStateRole and index.column() == self.COL_CHECK:
            self.set_checked([self.items[index.row()]], _is_checked(value))
            return True
        return False

    def values(self, it: PlanItem) -> list:
        ident = it.identity
        why_blocked = self.blocked.get(it.source.id)
        reason = f"To review: {it.review} | {it.reason}" if needs_review(it) else it.reason
        return [
            "\u26a0" if why_blocked else "?" if needs_review(it) else ("\u2013" if not checkable(it) else ""),
            it.source.id,
            ident.title or it.source.title,
            " & ".join(ident.authors or it.source.authors),
            action_label(it) + (" (manual)" if it.manual else ""),
            f"{why_blocked} | {reason}" if why_blocked else reason,
            (it.match.label() + (" (moving now)" if it.match_planned else "")
             + (" (kept in source)" if it.match_in_source else "")
             + (" (different edition)" if it.different and it.action is not Action.TRASH else ""))
            if it.match else "",
            _formats_text(it),
            ", ".join(sorted(ident.ai_fields)) or ("nothing found" if it.ai_used else ""),
            it.status,
        ]

    def sort_key(self, it: PlanItem, col: int):
        if col == self.COL_CHECK:
            return (2 if it.selected else 1) if checkable(it) else 0
        v = self.values(it)[col]
        return v if isinstance(v, int) else str(v).casefold()

    def _sort_items(self):
        # One key per row and one Python sort: letting Qt sort through the proxy
        # calls back into Python millions of times (20 s for 80,000 books).
        col = self._sort_column
        self.items.sort(key=lambda it: self.sort_key(it, col), reverse=self._sort_order == Qt.DescendingOrder)
        self._rows = {id(it): row for row, it in enumerate(self.items)}
        self._sorted = True

    def sort(self, column, order=Qt.AscendingOrder):
        if self._sorted and (column, order) == (self._sort_column, self._sort_order):
            return  # re-enabling sorting after a run asks again; nothing to do
        self._sort_column, self._sort_order = column, order
        self.layoutAboutToBeChanged.emit()
        old = self.persistentIndexList()  # keeps the selection on the same books
        moved = [(self.items[i.row()], i.column()) for i in old]
        self._sort_items()
        self.changePersistentIndexList(old, [self.index(self._rows[id(it)], c) for it, c in moved])
        self.layoutChanged.emit()

    def data(self, index, role=Qt.DisplayRole):
        it = self.items[index.row()]
        col = index.column()
        if role in (Qt.DisplayRole, Qt.ToolTipRole):
            return self.values(it)[col]
        if role == Qt.CheckStateRole and col == self.COL_CHECK and checkable(it):
            return Qt.Checked if it.selected else Qt.Unchecked
        if role == Qt.FontRole and it.manual:
            return self.italic_font
        if role == Qt.ForegroundRole:
            if col == self.COL_ACTION:
                return QColor(ACTION_COLORS[it.action])
            if col in (self.COL_CHECK, self.COL_REASON) and (it.source.id in self.blocked or needs_review(it)):
                return QColor("#e65100")
            if col == self.COL_RESULT and it.status.startswith("FAILED"):
                return QColor("#c62828")
        return None


LEAVE_UNIQUE = "leave_unique"
ACTION_ENTRIES: list[Entry] = [
    (Action.MOVE.value, FILTER_LABELS[Action.MOVE.value], "Books copied to the target library on Execute."),
    (MERGE, FILTER_LABELS[MERGE], "Duplicates whose missing formats are added to the kept copy, then trashed."),
    (Action.TRASH.value, FILTER_LABELS[Action.TRASH.value], "Duplicates moved to the trash library on Execute."),
    (LEAVE_UNIQUE, "Leave: no duplicate", "Left in place: no other book has the same title and authors "
                                          "(or only other editions)."),
    (Action.LEAVE.value, "Leave: to check", "Left in place but undecided: a possible duplicate, or a record to "
                                            "fix with the Metadata Review first (no title or authors, a file "
                                            "name for title, title and author swapped)."),
]
BOOK_ENTRIES: list[Entry] = [
    ("ai", "AI used", "The AI read the book (or compared covers or authors) to decide it."),
    ("formats", "Adds formats", "Its formats the kept copy lacks are added to it before the trash."),
    ("reduced", "Reduced checks", "Decided with fewer checks than the settings ask for (no Image AI, AI off or "
                                  "stopped): analyze again once that is fixed."),
    ("cover", "Decided by cover", "Duplicates proven by the same cover: check those whose metadata differ."),
    ("no_edition", "No edition data", "Duplicates only because nothing tells the copies apart: no edition data to "
                                      "compare, and the covers don't differ (one missing, not a real cover, or the "
                                      "AI unsure). Check them before executing."),
    ("file_name", "File-name title", "The title in Calibre is a file name: the book was matched with the title the "
                                     "AI read in it, or is not moved without one."),
    ("unreadable", "Unreadable files", "Files Calibre can't open (a format it doesn't read, a fake PDF), or no "
                                       "file at all (an empty record)."),
    ("archives", "Archives", "Books stored as RAR/ZIP/7Z: their archive can be unpacked on Execute."),
    ("swapped", "Title/author swapped", "Title and author are swapped in Calibre: the list shows them put "
                                        "right, for the matching; the record is not moved until the Metadata "
                                        "Review fixes it."),
    ("other", "Other books", "Books that match none of the entries above."),
]


def action_key(it: PlanItem) -> str:
    key = filter_key(it)
    return LEAVE_UNIQUE if key == Action.LEAVE.value and has_no_duplicate(it) else key


def book_kinds(it: PlanItem) -> set[str]:
    kinds = {k for k, on in (("ai", it.ai_used), ("formats", it.add_formats), ("reduced", it.skipped),
                             ("cover", it.by_cover), ("no_edition", it.no_edition),
                             ("file_name", it.file_name_title),
                             ("unreadable", it.bad_formats or not it.source.formats),
                             ("archives", it.archives), ("swapped", it.swapped)) if on}
    return kinds or {"other"}


def is_checked(it: PlanItem) -> bool:
    return it.selected and checkable(it)


class PlanFilter(QSortFilterProxyModel):
    """The search (all words must match) and the Actions, Books and Status lists (see filters.py)."""

    def __init__(self):
        super().__init__()
        self.terms: list[str] = []
        self.actions: set[str] = set()
        self.kinds: set[str] = set()
        self.states: set[str] = set()

    def update(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
        self.invalidateRowsFilter()

    def sort(self, column, order=Qt.AscendingOrder):
        # The source model sorts itself (much faster); this proxy only filters.
        self.sourceModel().sort(column, order)

    def filterAcceptsRow(self, row, parent):
        model: PlanModel = self.sourceModel()
        it = model.items[row]
        if self.actions and action_key(it) not in self.actions:
            return False
        if self.kinds and not (self.kinds & book_kinds(it)):
            return False
        if self.states and not (self.states & status_keys(is_checked(it), it.status, needs_review(it))):
            return False
        if self.terms:
            hay = strip_accents(" ".join(str(x) for x in model.values(it)[1:7])).casefold()
            return all(t in hay for t in self.terms)
        return True


def _formats_text(it: PlanItem) -> str:
    """Formats added to the match, and what happens to the ones Calibre can't open."""
    parts = [f"add {', '.join(it.add_formats)}"] if it.add_formats else []
    if it.bad_formats and not it.unreadable:
        going = it.bad_formats_to_trash
        kept = sorted(set(it.bad_formats) - set(going))
        parts += [f"{', '.join(going)} to trash"] if going else []
        parts += [f"{', '.join(kept)} unreadable, kept"] if kept else []
    parts += archive_texts(it.archives)
    return " · ".join(parts)


ARCHIVES_TIP = ("Books stored as an archive (RAR, ZIP, 7Z). The Formats column says what Execute does:\n"
                "unpack (the new formats are added, the archive goes to the trash library inside a copy\n"
                "of the record), kept, or unclear (several books, nothing readable, password…: left as it\n"
                "is). Right-click to unpack or keep.")


def archive_texts(archives: list) -> list[str]:
    """What happens to each archive on Execute, for the list."""
    return [f"{u.format}: unclear, kept" if u.problem
            else f"{u.format}: unpack ({', '.join(sorted(u.add)) or 'nothing new'})" if u.unpack
            else f"{u.format}: kept" for u in archives]


def add_unpack_actions(menu: QMenu, items: list, set_unpack) -> None:
    """Right-click: unpack the selected books' clear archives on Execute, or keep them."""
    clear = [i for i in items if any(not u.problem for u in i.archives)]
    if not clear:
        return
    menu.addSeparator()
    for value, label in ((True, "Unpack the archive on Execute"), (False, "Keep the archive")):
        n = sum(1 for i in clear if any(u.unpack != value for u in i.archives if not u.problem))
        act = menu.addAction(f"{label} ({n})" if len(items) > 1 else label)
        act.setEnabled(n > 0)
        act.triggered.connect(lambda _=False, v=value: set_unpack(clear, v))


def add_mark_reviewed(menu: QMenu, items: list, needs, mark) -> None:
    """Right-click: the books to review were looked at, and keep the analysis' choices."""
    todo = [i for i in items if needs(i)]
    act = menu.addAction(f"Mark reviewed ({len(todo)})" if len(items) > 1 else "Mark reviewed")
    act.setToolTip("You checked these books and keep what the list shows: they leave Status → Needs review.")
    act.setEnabled(bool(todo))
    act.triggered.connect(lambda: mark(todo))


def has_no_duplicate(it: PlanItem) -> bool:
    """Left in place because no other book shares its title and authors, or every
    one that does is a different edition. The other Leave books need a look:
    undecided pairs, and books whose title/authors couldn't be read."""
    return it.action is Action.LEAVE and (it.match is None or it.different) and it.identity.has_title_authors


class PlanTable(QTableView):
    space_pressed = Signal()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Space:
            self.space_pressed.emit()
            return
        super().keyPressEvent(event)


# What a window is busy with, for the other windows (see shutdown.py).
OPERATION_NAMES = {"analyze": "analyzing", "scan": "analyzing", "execute": "executing",
                   "ask": "asking the AI again", "judge": "asking the Judge AI"}


# --- workers --------------------------------------------------------------------
class AnalyzeWorker(QThread, UnpackQuestion):
    progress = Signal(int, int, str)
    items_ready = Signal(object)  # list of books just decided, for the live table
    finished_ok = Signal(object)
    failed = Signal(str)
    ai_down = Signal(str, bool)  # message, image AI: the GUI asks the user, then calls answer()
    unpack_asked = Signal(int)  # the GUI asks, then calls answer_unpack()
    pause_waiting = Signal(bool)  # the analysis waits (Pause), or goes on
    BATCH_SECONDS = 0.3  # books with no duplicate go by by the thousand: send them in batches

    def __init__(self, settings: Settings, source: str, target: str, trash: str, no_cache: bool = False,
                 only: set[int] | None = None, moving: set[int] = frozenset(), leaving: set[int] = frozenset(),
                 unpack: bool | None = None):
        """`only`, `moving`, `leaving`: the AI asked again about some books of a plan (see
        planner.build_plan); `unpack`: then, whether their archives are unpacked (not asked)."""
        super().__init__()
        self.settings, self.source, self.target, self.trash = settings, source, target, trash
        self.no_cache = no_cache
        self.only, self.moving, self.leaving, self.unpack = only, moving, leaving, unpack
        self.cancel = threading.Event()
        self.pause = Pause(on_wait=self.pause_waiting.emit)
        self._answered = threading.Event()
        self._choice = AI_OFF
        self._init_unpack()

    def ask(self, message: str, image: bool) -> str:
        """Called on the worker thread when an AI keeps failing: wait for the user.
        RETRY, SKIP_BOOK, or AI_OFF: go on without that AI (also when Stop was chosen)."""
        self._answered.clear()
        self._choice = AI_OFF
        self.ai_down.emit(message, image)
        while not self._answered.wait(0.2):
            if self.cancel.is_set():  # e.g. the window is closing
                return AI_OFF
        return self._choice

    def answer(self, choice: str) -> None:
        self._choice = choice
        self._answered.set()

    def run(self):
        with keep_awake():  # no idle sleep halfway through the run
            self._run()

    def _run(self):
        resolver = None
        try:
            resolver = make_resolver(self.settings, on_down=self.ask,
                                     cache=AICache(off=True) if self.no_cache else None)
            batch: list = []
            sent = time.monotonic()

            def on_item(item):
                nonlocal sent
                batch.append(item)
                if time.monotonic() - sent >= self.BATCH_SECONDS:
                    self.items_ready.emit(batch.copy())
                    batch.clear()
                    sent = time.monotonic()

            plan = build_plan(self.source, self.target, self.trash, resolver,
                              self.settings.ignore_subtitle, self.progress.emit, self.cancel,
                              similar_matching=self.settings.similar_matching,
                              cover_check=self.settings.cover_check,
                              recheck_years=self.settings.recheck_years,
                              same_series=self.settings.same_series,
                              similar_titles=self.settings.similar_titles,
                              always_cover=self.settings.always_cover,
                              author_variants=self.settings.author_variants,
                              fix_swapped=self.settings.fix_swapped,
                              trash_unreadable=self.settings.trash_unreadable,
                              cleanup_only=self.settings.cleanup_only,
                              tag=self.settings.only_tag, tag_exclude=self.settings.only_tag_exclude,
                              on_item=on_item,
                              unpack=self.ask_unpack if self.unpack is None else (lambda n: self.unpack),
                              generic_check=not self.settings.skip_generic_covers,
                              library_cache=library_cache_dir(), pause=self.pause,
                              only=self.only, moving=self.moving, leaving=self.leaving)
            if batch:
                self.items_ready.emit(batch.copy())
            self.finished_ok.emit(plan)
        except Exception as e:
            log.exception("Analysis failed")
            self.failed.emit(str(e))
        finally:
            if resolver:
                resolver.cache.save()
                resolver.extractor.close()


class ExecuteWorker(QThread):
    result = Signal(object)
    finished_ok = Signal(int, int)
    failed = Signal(str)

    def __init__(self, settings: Settings, plan: Plan):
        super().__init__()
        self.settings, self.plan = settings, plan
        self.cancel = threading.Event()

    def run(self):
        with keep_awake():  # no idle sleep halfway through the run
            self._run()

    def _run(self):
        try:
            ok, failed = execute_plan(
                self.plan, require_calibre_dir(self.settings), self.settings.delete_permanently,
                lambda item, *_: self.result.emit(item), self.cancel,
            )
            self.finished_ok.emit(ok, failed)
        except Exception as e:
            log.exception("Execution failed")
            self.failed.emit(str(e))


class JudgeWorker(QThread):
    """Asks the Judge AI about some rows, one by one (judge.judge_pair)."""
    judged = Signal(object, object)  # the item, its Judgement
    progress = Signal(int, int, str)
    finished_ok = Signal(int)  # rows judged
    failed = Signal(str)
    MAX_ERRORS = 3  # in a row: the judge is down, stop

    def __init__(self, settings: Settings, items: list[PlanItem]):
        super().__init__()
        self.settings, self.items = settings, items
        self.cancel = threading.Event()

    def run(self):
        with keep_awake():
            self._run()

    def _run(self):
        extractor = None
        try:
            profile = self.settings.profile(self.settings.judge_profile)
            if profile is None:
                raise RuntimeError("Choose a Judge AI in Settings.")
            provider = make_provider(profile)
            extractor = TextExtractor(require_calibre_dir(self.settings), self.settings.pdf_pages,
                                      self.settings.text_chars)
            prints, cache = TextPrints(library_cache_dir(), extractor), AICache()
            done = errors = 0
            perf.run_start("dedup-judge", len(self.items), provider)
            try:
                for n, item in enumerate(self.items):
                    if self.cancel.is_set():
                        break
                    perf.book(n + 1)
                    self.progress.emit(n, len(self.items), f"Judge AI: {item.source.label()}")
                    try:
                        self.judged.emit(item, judge_pair(provider, item, extractor, cache, prints))
                        done, errors = done + 1, 0
                    except AIError as e:
                        errors += 1
                        log.warning("Judge AI on %s: %s", item.source.label(), e)
                        if errors >= self.MAX_ERRORS:
                            raise RuntimeError(f"The Judge AI failed {errors} times in a row: {e}") from e
            finally:
                perf.run_end(done, self.cancel.is_set())
                prints.save()
            self.finished_ok.emit(done)
        except Exception as e:
            log.exception("Judge AI failed")
            self.failed.emit(str(e))
        finally:
            if extractor is not None:
                extractor.close()


# --- main window ------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self, settings: Settings, log_handler: QtLogHandler):
        super().__init__()
        self.settings = settings
        self.plan: Plan | None = None
        self.worker: QThread | None = None
        self._stopping = False  # Stop pressed; the worker ends after the current book
        self._paused = False  # the analysis waits (Pause), see pause.py
        self._exec_pausing = False  # Pause pressed while executing: it stops after the current book
        self._exec_paused = False  # an execution paused: Continue executes the remaining checked books
        self._close_pending = False  # quit requested; close once the worker has ended
        self._library_use: LibraryUse | None = None  # the libraries of the plan shown (see library_use)
        self._operation = ""  # "analyze" or "execute" while a worker thread exists
        self._done_count = self._exec_total = 0
        self._executed_removed = 0  # executed books taken off the list
        self._restored = 0  # remembered choices re-applied to this analysis' books
        self._progress_max = 1
        self._status_msg, self._status_since = "", 0.0  # current book, for the seconds counter
        self._eta = Eta()  # time left of the analysis
        self.setWindowTitle(f"Calibre Merge & Dedup {app_version()}")
        self._restore_geometry()
        # Warnings about the shown plan: settings changed since, an AI that stopped
        # responding, checks that could not run. Hidden when there is nothing to say.
        self.notice = QLabel()
        self.notice.setWordWrap(True)
        self.notice.setTextFormat(Qt.RichText)
        self.notice.setVisible(False)
        self._plan_signature: dict | None = None  # analysis settings of the shown plan

        # libraries
        libs = QGroupBox("Libraries")
        grid = QGridLayout(libs)
        known = known_libraries()
        self.lib_boxes: dict[str, QComboBox] = {}
        rows = [
            ("source", "Source library", "Books are moved out of this library", settings.source_library),
            ("target", "Target library", "Books that aren't duplicates go here", settings.target_library),
            ("trash", "Trash library", "Duplicates of target books go here (created if empty)", settings.trash_library),
        ]
        for r, (key, label, tip, value) in enumerate(rows):
            box = _compact(QComboBox(), 40)
            box.setEditable(True)
            box.addItems(known)
            box.setEditText(value)
            box.setToolTip(tip)
            box.lineEdit().setPlaceholderText(tip)
            box.editTextChanged.connect(self._update_notices)
            browse = QPushButton("Browse…")
            browse.clicked.connect(lambda _=False, b=box, l=label: self._browse(b, l))
            grid.addWidget(QLabel(label), r, 0)
            grid.addWidget(box, r, 1)
            grid.addWidget(browse, r, 2)
            self.lib_boxes[key] = box
        tip = ("Analyze only the source books with this tag, or all of them except those with it\n"
               "(empty: all of them). The target is always read whole: the books analyzed are\n"
               "still compared with every book in it, or, in one library, with the books the\n"
               "filter leaves out, which are never moved or trashed.\n"
               "Their ticks are remembered apart from those of the whole library.")
        self.tag_mode = tag_mode_box(settings.only_tag_exclude, "source books", tip)
        self.tag_mode.currentIndexChanged.connect(self._update_notices)
        self.tag_box = tag_box(settings.only_tag, tip)
        self.tag_box.editTextChanged.connect(self._update_notices)
        source_box = self.lib_boxes["source"]
        source_box.editTextChanged.connect(lambda text: fill_tags(self.tag_box, text))
        fill_tags(self.tag_box, source_box.currentText())
        tag_row = QHBoxLayout()
        tag_row.addWidget(self.tag_box)
        tag_row.addStretch(1)
        grid.addWidget(self.tag_mode, len(rows), 0)
        grid.addLayout(tag_row, len(rows), 1, 1, 2)
        self.cleanup_box = QCheckBox("Cleanup source only (don't copy anything to the target)")
        self.cleanup_box.setToolTip(
            "Only the source books already in the target are handled: they go to the trash library.\n"
            "Nothing is written to the target: books not in it stay in the source, and so do\n"
            "duplicates whose target copy lacks one of their formats (trashing them would drop that\n"
            "format from both libraries): right-click to Merge & Trash them, or leave them.")
        self.cleanup_box.setChecked(settings.cleanup_only)
        self.cleanup_box.toggled.connect(self._update_notices)
        grid.addWidget(self.cleanup_box, len(rows) + 1, 1, 1, 2)
        grid.setColumnStretch(1, 1)

        # AI row
        self.text_box = _compact(QComboBox(), 24)
        self.image_box = _compact(QComboBox(), 24)
        # After _compact's own tooltip update (same signal, connected earlier).
        self.text_box.currentTextChanged.connect(self._ai_choice_changed)
        self.image_box.currentTextChanged.connect(self._ai_choice_changed)
        self._fill_profiles()
        settings_btn = QPushButton("Settings…")
        settings_btn.clicked.connect(self._open_settings)
        ai_row = QHBoxLayout()
        ai_row.addWidget(QLabel("Text AI:"))
        ai_row.addWidget(self.text_box, 1)
        ai_row.addWidget(QLabel("Image AI:"))
        ai_row.addWidget(self.image_box, 1)
        self.no_cache = no_cache_box()
        ai_row.addWidget(self.no_cache)
        ai_row.addWidget(settings_btn)

        # actions
        self.analyze_btn = QPushButton("1. Analyze (dry run)")
        self.execute_btn = QPushButton("2. Execute checked")
        self.stop_btn = QPushButton("Stop")
        self.pause_btn = QPushButton("Pause")
        self.pause_btn.setStyleSheet(button_css(*SLATE))
        self.pause_btn.clicked.connect(self._toggle_pause)
        self.analyze_btn.setStyleSheet(button_css(*BLUE))
        self.execute_btn.setStyleSheet(button_css(*GREEN))
        # Red while something can be stopped, grey otherwise.
        self.stop_btn.setStyleSheet(button_css(*RED))
        self.export_btn = QPushButton("Export CSV…")
        self.analyze_btn.clicked.connect(self._analyze)
        self.execute_btn.clicked.connect(self._execute)
        self.stop_btn.clicked.connect(self._stop)
        self.export_btn.clicked.connect(self._export)
        self.summary = _elastic(QLabel())
        self.shutdown = ShutdownWhenDone(self, "dedup", lambda text: self.status_label.setText(text))
        btn_row = QHBoxLayout()
        for w in (self.analyze_btn, self.execute_btn, self.pause_btn, self.stop_btn, self.export_btn):
            btn_row.addWidget(w)
        btn_row.addSpacing(12)
        btn_row.addWidget(self.shutdown.box)
        btn_row.addStretch(1)
        btn_row.addWidget(self.summary)

        # filter bar
        self.model = PlanModel()
        self.proxy = PlanFilter()
        self.proxy.setSourceModel(self.model)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search title, authors, reason, match… (all words must match)")
        self.search.setClearButtonEnabled(True)
        self._search_timer = QTimer(self, singleShot=True, interval=200)
        self._search_timer.timeout.connect(
            lambda: self.proxy.update(terms=strip_accents(self.search.text()).casefold().split()))
        self.search.textChanged.connect(self._search_timer.start)
        items = lambda: self.model.items  # noqa: E731
        self.action_filter = FilterButton(
            "Actions", "Actions: show the books with any of the actions ticked here. Nothing ticked: all books.\n"
                       "Same library: 'Leave: no duplicate' (usually most books) starts unticked.",
            ACTION_ENTRIES, lambda: Counter(action_key(it) for it in items()))
        self.book_filter = FilterButton(
            "Books", "Books: show the kinds of book ticked here (any of them). Nothing ticked: all books.\n"
                     "A book can be of several kinds: it is shown if any of them is ticked.",
            BOOK_ENTRIES, lambda: Counter(k for it in items() for k in book_kinds(it)))
        self.status_filter = FilterButton(
            "Status", STATUS_TIP, STATUS_ENTRIES,
            lambda: Counter(k for it in items() for k in status_keys(is_checked(it), it.status, needs_review(it))))
        self._filter_same_library: bool | None = None
        self._set_action_entries(False)
        self.action_filter.changed.connect(lambda: self.proxy.update(actions=self.action_filter.selected()))
        self.book_filter.changed.connect(lambda: self.proxy.update(kinds=self.book_filter.selected()))
        self.status_filter.changed.connect(lambda: self.proxy.update(states=self.status_filter.selected()))
        clear = QPushButton("Clear filters")
        clear.setToolTip("Show every book: clear the search and untick all three lists.")
        clear.clicked.connect(self._clear_filters)
        self.showing_label = QLabel()
        for signal in (self.proxy.rowsInserted, self.proxy.rowsRemoved, self.proxy.modelReset,
                       self.proxy.layoutChanged):
            signal.connect(self._update_showing)
        filter_row = make_filter_row(self.search, [self.action_filter, self.book_filter, self.status_filter],
                                     clear, self.showing_label)

        # bulk selection
        self.check_btn = QPushButton("Check visible")
        self.uncheck_btn = QPushButton("Uncheck visible")
        self.invert_btn = QPushButton("Invert visible")
        self.check_btn.setToolTip("Check rows currently shown after filtering")
        self.uncheck_btn.setToolTip("Uncheck rows currently shown after filtering")
        self.invert_btn.setToolTip("Invert checks for rows currently shown after filtering")
        self.check_btn.clicked.connect(lambda: self.model.set_checked(self._visible_items(), True))
        self.uncheck_btn.clicked.connect(lambda: self.model.set_checked(self._visible_items(), False))
        self.invert_btn.clicked.connect(lambda: self.model.set_checked(self._visible_items(), None))
        self.revert_all_btn = QPushButton("Revert all changes…")
        self.revert_all_btn.setToolTip(
            "Put every book back to the analysis' decision: actions, ticks, unreadable formats and archives,\n"
            "and forget the choices remembered for these libraries (also of books not analyzed now)")
        self.revert_all_btn.clicked.connect(self._revert_all)
        self.checked_label = _elastic(QLabel())
        bulk_row = QHBoxLayout()
        for w in (self.check_btn, self.uncheck_btn, self.invert_btn, self.revert_all_btn):
            bulk_row.addWidget(w)
        bulk_row.addWidget(_elastic(QLabel("  Space toggles selected rows · right-click to change the action")), 1)
        bulk_row.addStretch(1)
        bulk_row.addWidget(self.checked_label)

        # table + log
        self.store = SelectionStore()
        self.model.selection_changed.connect(self._selection_changed)
        for sig in (self.proxy.rowsInserted, self.proxy.rowsRemoved, self.proxy.modelReset):
            sig.connect(self._filter_changed)
        self.table = PlanTable()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(PlanModel.COL_ID, Qt.AscendingOrder)
        self.table.setSelectionBehavior(QTableView.SelectRows)
        self.table.setWordWrap(False)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.table.space_pressed.connect(self._toggle_selected_rows)
        self.model.italic_font = QFont(self.table.font())
        self.model.italic_font.setItalic(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.sectionClicked.connect(self._header_clicked)
        for col, width in enumerate([34, 50, 260, 180, 150, 420, 260, 80, 80, 300]):
            self.table.setColumnWidth(col, width)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        log_handler.signal.message.connect(self._append_log)
        # The selected book's cover and its match's, stacked, to compare them.
        self.cover = CoverPreview()
        self.table.selectionModel().currentRowChanged.connect(self._show_covers)
        top = QSplitter(Qt.Horizontal)
        top.addWidget(self.table)
        top.addWidget(self.cover)
        top.setStretchFactor(0, 1)
        top.setSizes([1100, 200])
        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(top)
        splitter.addWidget(self.log_view)
        splitter.setSizes([600, 150])

        self.progress = QProgressBar()
        self.status_label = _elastic(QLabel())
        self._ticker = QTimer(self, interval=1000)  # keeps the seconds counter moving
        self._ticker.timeout.connect(self._show_status)
        self._ticker.start()

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(self.shutdown.banner)  # shown only while "shut down when done" is on
        layout.addWidget(libs)
        layout.addLayout(ai_row)
        layout.addLayout(btn_row)
        layout.addLayout(filter_row)
        layout.addLayout(bulk_row)
        layout.addWidget(self.notice)
        layout.addWidget(splitter, 1)
        layout.addWidget(self.progress)
        layout.addWidget(self.status_label)
        self.setCentralWidget(central)
        self._set_busy(False)

    # --- helpers ------------------------------------------------------------------
    @staticmethod
    def _profile_label(p) -> str:
        return f"{p.name}  ({p.kind}: {p.model or 'no model'})"

    def _fill_profiles(self):
        self.text_box.blockSignals(True)
        self.text_box.clear()
        self.text_box.addItem("None (metadata only)", "")
        for p in self.settings.profiles:
            self.text_box.addItem(self._profile_label(p), p.name)
        self.text_box.setCurrentIndex(max(0, self.text_box.findData(self.settings.text_profile)))
        style_none_item(self.text_box)
        self.text_box.blockSignals(False)
        self.image_box.clear()  # refilled from the saved setting
        self._fill_image_box()

    def _fill_image_box(self):
        """Image AI choices: profiles that support images. The choice is kept when the
        text AI is None (it is only inactive then; see _ai_choice_changed)."""
        current = self.image_box.currentData() if self.image_box.count() else self.settings.image_profile
        self.image_box.blockSignals(True)
        self.image_box.clear()
        self.image_box.addItem("None (no cover check, skip scanned PDFs)", "")
        for p in self.settings.profiles:
            if p.vision:
                self.image_box.addItem(self._profile_label(p), p.name)
        self.image_box.setCurrentIndex(max(0, self.image_box.findData(current)))
        style_none_item(self.image_box)
        self.image_box.blockSignals(False)
        self._ai_choice_changed()

    def _ai_choice_changed(self, *_):
        """Amber italic for a choice that won't take effect: "None", or an Image AI
        while the text AI is None. The choice itself is never changed."""
        text_on = bool(self.text_box.currentData())
        image_on = bool(self.image_box.currentData())
        mark_inactive(self.text_box, not text_on)
        self.text_box.setToolTip(
            "Reads book text to find missing metadata.\n"
            + ("None: metadata only, no AI at all (no year re-check, no cover check)." if not text_on
               else self.text_box.currentText()))
        mark_inactive(self.image_box, not (text_on and image_on))
        tip = ("A model that reads text and images: compares covers and reads scanned PDFs.\n"
               "Only profiles marked 'Supports images' are listed.\n")
        if not image_on:
            tip += "None: covers aren't compared and scanned PDFs aren't read."
        elif not text_on:
            tip += f"Inactive: the Text AI is None. {self.image_box.currentText()} will be used when it's set."
        else:
            tip += self.image_box.currentText()
        self.image_box.setToolTip(tip)
        self._update_notices()

    def _current_settings(self) -> Settings:
        """The settings as shown in the window, without saving them."""
        return replace(
            self.settings, source_library=self._library("source"), target_library=self._library("target"),
            trash_library=self._library("trash"), text_profile=self.text_box.currentData() or "",
            image_profile=self.image_box.currentData() or "", cleanup_only=self.cleanup_box.isChecked(),
            only_tag=self.tag_box.currentText().strip(), only_tag_exclude=bool(self.tag_mode.currentData()))

    def _update_notices(self, *_):
        """The amber bar above the table: what the user should know about the shown plan."""
        lines = []
        if self.plan is not None and self._plan_signature is not None and not self._busy:
            changed = changed_settings(self._plan_signature, analysis_signature(self._current_settings()))
            if changed:
                lines.append(f"<b>Settings changed since this analysis</b> ({', '.join(changed)}): "
                             "analyze again to apply them.")
        if self.plan is not None and not (self._busy and self._operation == "analyze"):
            _, warnings = run_summary(self.plan)
            lines += [html.escape(w) for w in warnings]
            if any(it.skipped for it in self.plan.items):
                lines.append("Tick <b>Reduced checks</b> to list these books.")
        dark = self.palette().color(self.backgroundRole()).lightness() < 128
        self.notice.setStyleSheet(
            "QLabel { background: %s; color: %s; border: 1px solid %s; border-radius: 3px; padding: 4px 8px; }"
            % (("#3d2a00", "#ffd699", "#8a5a00") if dark else ("#fff3dc", "#5c3900", "#e0a030")))
        self.notice.setText("<br>".join(f"⚠ {line}" for line in lines))
        self.notice.setVisible(bool(lines))

    def _browse(self, box: QComboBox, label: str):
        d = QFileDialog.getExistingDirectory(self, label, box.currentText())
        if d:
            box.setEditText(d)

    def _restore_geometry(self):
        """Last session's size and position, always within the screen."""
        saved = self.settings.window_geometry
        restored = bool(saved) and self.restoreGeometry(QByteArray.fromBase64(saved.encode("ascii")))
        if not restored:
            self.resize(1300, 800)
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        # Room for the title bar and frame, which aren't part of the size set here.
        w, h = min(self.width(), area.width() - 40), min(self.height(), area.height() - 60)
        self.resize(w, h)
        if not restored:
            x, y = area.x() + (area.width() - w) // 2, area.y() + (area.height() - h) // 2  # centred
        else:  # where it was, but pulled fully inside the screen (e.g. saved on a larger screen)
            x = min(max(self.x(), area.left()), area.left() + area.width() - w - 20)
            y = min(max(self.y(), area.top()), area.top() + area.height() - h - 50)
        self.move(x, y)

    def _set_action_entries(self, same_library: bool):
        """List the actions a plan can contain: a same-library plan never moves. When
        that changes, the Actions list starts again: in one library, books with no
        duplicate (usually most of them) unticked; between two, everything."""
        shown = {k for k, _, _ in ACTION_ENTRIES if not (same_library and k == Action.MOVE.value)}
        self.action_filter.set_shown(shown)
        if same_library != self._filter_same_library:
            self._filter_same_library = same_library
            self.action_filter.set_default(shown - {LEAVE_UNIQUE} if same_library else set())

    def _library(self, key: str) -> str:
        return self.lib_boxes[key].currentText().strip()

    def _sync_settings(self):
        s = self.settings
        s.source_library, s.target_library, s.trash_library = (self._library(k) for k in ("source", "target", "trash"))
        s.text_profile = self.text_box.currentData() or ""
        s.image_profile = self.image_box.currentData() or ""
        s.cleanup_only = self.cleanup_box.isChecked()
        s.only_tag = self.tag_box.currentText().strip()
        s.only_tag_exclude = bool(self.tag_mode.currentData())
        s.save()

    def _set_busy(self, busy: bool):
        self._busy = busy
        # A worker delivers its result before it ends (it still saves the AI cache):
        # nothing new may start until its thread has really finished.
        finishing = not busy and self.worker is not None
        analyzing = self.worker is not None and self._operation == "analyze"
        self.analyze_btn.setEnabled(not busy and self.worker is None)
        self.analyze_btn.setText(("Finishing…" if finishing else "Analyzing…") if analyzing
                                 else "1. Analyze (dry run)")
        self.analyze_btn.setToolTip("Saving the AI cache; available again in a moment" if finishing else "")
        set_running(self.analyze_btn, analyzing)
        if finishing:
            self.progress.setRange(0, 0)  # moving "busy" bar
        elif self.progress.maximum() == 0:
            self.progress.setRange(0, self._progress_max)
            self.progress.setValue(self._progress_max)
        self.stop_btn.setText("Stopping…" if busy and self._stopping else "Stop")
        set_running(self.stop_btn, busy and self._stopping)
        self.export_btn.setEnabled(not busy and self.plan is not None)
        self.stop_btn.setEnabled(busy and not self._stopping)
        self._update_pause_button()
        self.shutdown.set_busy(self.worker is not None, OPERATION_NAMES.get(self._operation, ""))
        self.table.setSortingEnabled(not busy)  # no sorting while rows arrive or results come in
        for box in self.lib_boxes.values():
            box.setEnabled(not busy)
        self.cleanup_box.setEnabled(not busy)
        self.tag_box.setEnabled(not busy)
        self.tag_mode.setEnabled(not busy)
        editable = self._can_edit()
        for btn in (self.check_btn, self.uncheck_btn, self.invert_btn, self.revert_all_btn):
            btn.setEnabled(editable)
        self._update_summary()
        self._update_notices()

    def _update_summary(self):
        p = self.plan
        if not p:
            self.summary.setText("")
            self.checked_label.setText("")
            self.execute_btn.setText("2. Execute checked")
            self.execute_btn.setEnabled(False)
            set_running(self.execute_btn, False)
            return
        self.summary.setText(
            f"<b style='color:{ACTION_COLORS[Action.MOVE]}'>{p.count(Action.MOVE)} move</b> · "
            f"<b style='color:{ACTION_COLORS[Action.TRASH]}'>{p.count(Action.TRASH)} trash</b> · "
            f"<b style='color:{ACTION_COLORS[Action.LEAVE]}'>{p.count(Action.LEAVE)} leave</b>")
        todo = actionable(p)
        possible = sum(1 for i in p.items if checkable(i) and not i.done)
        n_blocked = len(self.model.blocked)
        text = f"{len(todo)} of {possible} checked"
        if n_blocked:
            text += f" · <b style='color:#e65100'>{n_blocked} blocked</b>"
        if self._executed_removed:  # executed books leave the list; the others can be executed too
            text += f" · {self._executed_removed} executed, taken off the list"
        self.checked_label.setText(text)
        executing = self.worker is not None and self._operation == "execute"
        self.execute_btn.setText(f"Executing… ({self._done_count} of {self._exec_total})" if executing
                                 else f"2. Execute checked ({len(todo)})")
        set_running(self.execute_btn, executing)
        self.execute_btn.setEnabled(not self._busy and self.worker is None and not self.model.locked and bool(todo))

    # --- selection ------------------------------------------------------------------
    def _visible_items(self) -> list[PlanItem]:
        return [self.model.items[self.proxy.mapToSource(self.proxy.index(r, 0)).row()]
                for r in range(self.proxy.rowCount())]

    def _can_edit(self) -> bool:
        """Ticks and overrides: when idle and while analyzing, never while executing."""
        return (self.plan is not None and not self.model.locked
                and (not self._busy or (self.worker is not None and self._operation == "analyze")))

    def _header_clicked(self, section: int):
        if section != PlanModel.COL_CHECK or not self._can_edit():
            return
        items = [it for it in self.model.items if checkable(it)]
        if items:
            self.model.set_checked(items, not all(it.selected for it in items))

    def _selected_items(self) -> list[PlanItem]:
        rows = self.table.selectionModel().selectedRows()
        return [self.model.items[self.proxy.mapToSource(i).row()] for i in rows]

    def _selection_changed(self):
        self._update_summary()
        if self.plan is not None and not self._busy:
            self._update_analysis_recap()
        if self.plan is not None and not self.model.locked:
            self.store.save(self.plan, partial=self._busy)  # keep choices of books not analyzed yet

    def _filter_changed(self, *_):
        if self.plan is not None and not self._busy:
            self._update_analysis_recap()

    def _update_analysis_recap(self):
        if not self.plan:
            return
        move = self.plan.count(Action.MOVE)
        trash = self.plan.count(Action.TRASH)
        leave = self.plan.count(Action.LEAVE)
        total = move + trash + leave
        head = (f"Analysis stopped after {total} of {self.plan.total_books} books"
                if self.plan.stopped else "Analysis complete")
        text = f"{head}: {move} move, {trash} trash, {leave} leave"
        if leave:
            unique = sum(1 for it in self.plan.items if has_no_duplicate(it))
            text += f" ({leave - unique} to review, {unique} with no duplicate)"
        text += f" · total {total}"
        if self.plan.tag:
            text += f" {tag_filter_text(self.plan.tag, self.plan.tag_exclude)}"
        hidden = total - self.proxy.rowCount()
        if hidden:
            text += f" · {hidden} hidden by filters"
        info, _ = run_summary(self.plan)
        self.status_label.setText(f"{text} · {info}")
        self.status_label.setToolTip(info)

    def _show_covers(self, current: QModelIndex, _previous=None):
        if not current.isValid():
            self.cover.clear()
            return
        it = self.model.items[self.proxy.mapToSource(current).row()]
        if it.match is None:
            self.cover.show_books([("This book", it.source), ("Match", None)], empty="No match")
            return
        match = "Match" + (" (moving now)" if it.match_planned else "") + (
            " (different edition)" if it.different else "")
        self.cover.show_books([("This book", it.source), (match, it.match)])

    def _toggle_selected_rows(self):
        items = [i for i in self._selected_items() if checkable(i)]
        if items:
            # Mixed selection: check all. All checked: uncheck all.
            self.model.set_checked(items, not all(i.selected for i in items))

    def _context_menu(self, pos):
        index = self.table.indexAt(pos)
        if not index.isValid():
            return
        menu = QMenu(self)
        # Opening files and copying change nothing: available even while analyzing or executing.
        item = self.model.items[self.proxy.mapToSource(index).row()]
        self._add_open_actions(menu, item)
        menu.addSeparator()
        values = self.model.values(item)  # what the Title and Authors columns show
        for label, text in (("Copy title", values[PlanModel.COL_TITLE]),
                            ("Copy author", values[PlanModel.COL_AUTHORS])):
            act = menu.addAction(label)
            act.setEnabled(bool(text))
            act.triggered.connect(lambda _=False, t=text: QApplication.clipboard().setText(t))
        items = self._selected_items()
        if items and self._can_edit():
            menu.addSeparator()
            self._add_override_actions(menu, items)
        if items and self.plan is not None:
            menu.addSeparator()
            self._add_ai_actions(menu, items)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    # --- the AI asked again, the Judge AI ---------------------------------------------------
    def _ask_choices(self) -> list[tuple[str, str | None]]:
        """The entries of the Ask the AI menu: (label, profile name), None for the AIs selected above."""
        text, image = self.text_box.currentData(), self.image_box.currentData()
        above = "With the AIs selected above"
        if text:
            above += f" ({text}" + (f" + {image}" if image and image != text else "") + ")"
        return [(above, None)] + [(self._profile_label(p) + (" · text and covers" if p.vision else " · text only"),
                                   p.name) for p in self.settings.profiles]

    def _add_ai_actions(self, menu: QMenu, items: list[PlanItem]):
        """Ask the AI again about these books (and those whose decision rests on them), never
        from the cache; or ask the Judge AI about the pairs. Not while something runs."""
        items = [i for i in items if not i.done]
        why = ("something is running" if self.worker is not None
               else "executing" if self.model.locked else "no book" if not items else "")
        ask = menu.addMenu(f"Ask the AI again ({len(items)})" if len(items) > 1 else "Ask the AI again")
        ask.setToolTip("Decide these books again, with the AI asked again (not from the cache);\n"
                       "the books whose decision rests on them are decided again too.")
        ask.setEnabled(not why)
        for label, name in self._ask_choices():
            act = ask.addAction(label)
            act.triggered.connect(lambda _=False, n=name: self._ask_ai(items, n))
            if name is None:
                act.setEnabled(bool(self.text_box.currentData()))
                ask.addSeparator()
        pairs = [i for i in items if i.match is not None]
        judge = self.settings.judge_profile
        label = f"Judge AI ({judge})" if judge else "Judge AI (choose one in Settings → Analysis)"
        act = menu.addAction(f"{label} ({len(pairs)})" if len(items) > 1 else label)
        act.setToolTip("A stronger model judges the book and its match with everything known about them;\n"
                       "its answer becomes the row's suggestion, unticked, to review.")
        act.setEnabled(bool(judge) and bool(pairs) and not why)
        act.triggered.connect(lambda: self._judge(pairs))

    def _ask_settings(self, profile: str | None) -> Settings:
        if profile is None:
            return replace(self.settings)
        return replace(self.settings, text_profile=profile,
                       image_profile=profile if (p := self.settings.profile(profile)) and p.vision else "")

    def _ask_ai(self, items: list[PlanItem], profile: str | None = None):
        if self.worker is not None or self.plan is None or self.model.locked:
            return
        self._sync_settings()
        settings = self._ask_settings(profile)
        p = self.plan
        ids = redecide_ids(p, {i.source.id for i in items})
        moving = {i.source.id for i in p.items if i.action is Action.MOVE and i.selected and not i.done
                  and i.source.id not in ids}
        leaving = {i.source.id for i in p.items if i.action is Action.TRASH and i.selected and not i.done
                   and i.source.id not in ids} if p.same_library else set()
        unpack = any(u.unpack for i in p.items if i.source.id in ids for u in i.archives)
        worker = AnalyzeWorker(settings, p.source_library, p.target_library, p.trash_library, no_cache=True,
                               only=ids, moving=moving, leaving=leaving, unpack=unpack)
        worker.progress.connect(self._on_progress)
        worker.finished_ok.connect(self._redecided)
        worker.failed.connect(self._worker_failed)
        worker.ai_down.connect(self._on_ai_down)
        worker.pause_waiting.connect(self._on_pause_waiting)
        self._eta.reset()
        self._start(worker, "ask")
        ais = " + ".join(dict.fromkeys(n for n in (settings.text_profile, settings.image_profile) if n))
        log.info("Asking the AI (%s) again about %d book(s), %d with those whose decision rests on them",
                 ais or "none", len(items), len(ids))

    def _redecided(self, new: Plan):
        """The books decided again replace their rows (what the user did to those rows is not kept)."""
        by_id = {it.source.id: it for it in new.items}
        p = self.plan
        p.items = [by_id.get(it.source.id, it) for it in p.items]
        self.model.set_plan(p)
        self._finish()
        msg = f"The AI was asked again: {len(by_id)} book(s) decided again"
        log.info(msg)
        self.status_label.setText(msg)

    def _judge(self, items: list[PlanItem]):
        if self.worker is not None or self.plan is None or self.model.locked or not items:
            return
        self._sync_settings()
        worker = JudgeWorker(replace(self.settings), items)
        worker.progress.connect(self._on_progress)
        worker.judged.connect(self._on_judged)
        worker.finished_ok.connect(self._judge_done)
        worker.failed.connect(self._worker_failed)
        self._start(worker, "judge")
        log.info("Asking the Judge AI (%s) about %d book(s)", self.settings.judge_profile, len(items))

    def _on_judged(self, item: PlanItem, judgement: Judgement):
        if self.plan is not None and item in self.plan.items:
            apply_judgement(self.plan, item, judgement)
            self.model.refresh()

    def _judge_done(self, count: int):
        self._finish()
        msg = f"The Judge AI answered about {count} book(s): its suggestions are unticked, to review"
        log.info(msg)
        self.status_label.setText(msg)

    def _add_open_actions(self, menu: QMenu, item: PlanItem):
        """Open the book and its match with the apps the system associates with them."""
        def file_of(book):
            return TextExtractor.pick_format(book.formats) if book is not None else None

        mine, theirs = file_of(item.source), file_of(item.match)
        for label, book, picked in (("Open this book", item.source, mine), ("Open the match", item.match, theirs)):
            if book is None:
                act = menu.addAction(f"{label} (no match)")
            elif picked is None:
                act = menu.addAction(f"{label} (file not found)")
            else:
                act = menu.addAction(f"{label} ({picked[0]})")
                act.triggered.connect(lambda _=False, path=picked[1]: self._open_file(path))
            act.setEnabled(picked is not None)
        both = menu.addAction("Open both")
        both.setEnabled(mine is not None and theirs is not None)
        if mine and theirs:
            both.triggered.connect(lambda: self._open_both(mine[1], theirs[1]))

    OPEN_SECOND_AFTER_MS = 1500  # let a single-instance viewer start before it gets the second file

    def _open_both(self, first: str, second: str):
        self._open_file(first)
        QTimer.singleShot(self.OPEN_SECOND_AFTER_MS, lambda: self._open_file(second))

    def _open_file(self, path: str):
        open_file(path)

    def _add_override_actions(self, menu: QMenu, items: list[PlanItem]):
        same = self.plan.same_library

        def fits(i: PlanItem, action: Action, merge: bool | None) -> bool:
            # merge: None = any; True = Merge & Trash (needs a match with formats to add);
            # False = Trash only: always possible, nothing is merged
            if not can_override(i, action, same):
                return False
            if action is not Action.TRASH:
                return i.action is not action
            if merge:
                return bool(mergeable_formats(i)) and not (i.action is Action.TRASH and i.add_formats)
            return not (i.action is Action.TRASH and not i.add_formats)

        # One library: finding duplicates, so a book can only be merged/trashed or kept.
        choices = [] if same else [(Action.MOVE, None, "Force move to target")]
        choices += [
            (Action.TRASH, True, f"Force {MERGE_LABEL} (add its extra formats to the match, then trash)"),
            (Action.TRASH, False, "Force Trash only (nothing merged; with no match it will only be in the trash library)"),
            (Action.LEAVE, None, "Keep in library" if same else "Keep in source"),
        ]
        for action, merge, label in choices:
            n = sum(1 for i in items if fits(i, action, merge))
            act = menu.addAction(f"{label} ({n})" if len(items) > 1 else label)
            act.setEnabled(n > 0)
            act.triggered.connect(
                lambda _=False, a=action, m=merge: self._override([i for i in items if fits(i, a, m)], a, m))
        partial = [i for i in items if i.bad_formats and not i.unreadable]
        if partial:
            menu.addSeparator()
            for value, label in ((True, "Move the unreadable formats to the trash library"),
                                 (False, "Keep the unreadable formats")):
                n = sum(1 for i in partial if i.trash_bad != value)
                act = menu.addAction(f"{label} ({n})" if len(items) > 1 else label)
                act.setEnabled(n > 0)
                act.triggered.connect(lambda _=False, v=value: self._set_trash_bad(partial, v))
        add_unpack_actions(menu, items, self._set_unpack)
        menu.addSeparator()
        add_mark_reviewed(menu, items, needs_review, self._mark_reviewed)
        n = sum(1 for i in items if i.manual)
        act = menu.addAction(f"Revert to analysis decision ({n})" if len(items) > 1 else "Revert to analysis decision")
        act.setEnabled(n > 0)
        act.triggered.connect(lambda: self._revert(items))

    def _override(self, items: list[PlanItem], action: Action, merge: bool | None = None):
        skipped = 0
        same = self.plan.same_library
        for it in items:
            if can_override(it, action, same):
                override(it, action, same, merge is not False)
            else:
                skipped += 1
        if skipped:
            self.status_label.setText(f"{skipped} book(s) skipped: already in the target library.")
        self.model.refresh()

    def _set_trash_bad(self, items: list[PlanItem], value: bool):
        for it in items:
            it.trash_bad = value
        self.model.refresh()

    def _set_unpack(self, items: list, value: bool):
        """Unpack the books' clear archives on Execute, or keep them."""
        for it in items:
            for u in it.archives:
                if not u.problem:
                    u.unpack = value
        self.model.refresh()

    def _mark_reviewed(self, items: list[PlanItem]):
        mark_reviewed(items)
        self.model.refresh()

    def _revert(self, items: list[PlanItem]):
        for it in items:
            if it.manual:
                revert(it)
        self.model.refresh()

    def _revert_all(self):
        """Every change made to this plan, and every choice remembered for its libraries."""
        changed = sum(1 for it in self.plan.items if is_changed(it))
        saved = self.store.saved(self.plan)
        if not changed and not saved:
            self.status_label.setText("No changes to revert: every book has the analysis' decision.")
            return
        if QMessageBox.question(
                self, "Revert all changes",
                f"Put {changed} book(s) back to the analysis' decision and forget the {saved} choice(s) "
                "remembered for these libraries?\n\nThis can't be undone.") != QMessageBox.StandardButton.Yes:
            return
        self.store.forget(self.plan)
        revert_all(self.plan)
        log.info("Reverted all changes: %d book(s); %d remembered choice(s) forgotten", changed, saved)
        self.model.refresh()  # saves the choices left: none

    def _clear_filters(self):
        self.search.clear()
        for button in (self.action_filter, self.book_filter, self.status_filter):
            button.set_selected(set())

    def _update_showing(self, *_):
        self.showing_label.setText(showing_text(self.proxy.rowCount(), self.model.rowCount()))

    def _append_log(self, text: str, ai: bool):
        if ai:  # kept as plain text (the JSON replies' indentation too), only coloured
            color = AI_LOG_COLOR["dark" if self.log_view.palette().base().color().lightness() < 128 else "light"]
            self.log_view.appendHtml(f"<span style='color:{color}; white-space:pre-wrap'>"
                                     f"{html.escape(text)}</span>")
        else:
            self.log_view.appendHtml(f"<span style='white-space:pre-wrap'>{html.escape(text)}</span>")

    def _refresh_profiles(self):
        """AI profiles edited in calibre-review (ai_profiles.json is shared): list them."""
        s = self.settings
        s.text_profile = self.text_box.currentData() or ""
        s.image_profile = self.image_box.currentData() or ""
        if s.reload_profiles():
            self._fill_profiles()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.ActivationChange and self.isActiveWindow():
            self._refresh_profiles()

    def _open_settings(self):
        self._refresh_profiles()
        self._sync_settings()
        dialog = SettingsDialog(self.settings, self,
                                cache_busy=self.worker is not None and self._operation == "analyze")
        try:
            if dialog.exec():
                self._fill_profiles()
        finally:
            # Delete it now: a closed dialog left alive as a hidden child of the window
            # crashed the next analysis (Python freed memory Qt still used).
            dialog.deleteLater()

    # --- analyze --------------------------------------------------------------------
    def _preflight_ok(self) -> bool:
        """Settings that are on but won't take effect, and AIs that can't be reached:
        tell the user before the analysis starts. False = don't start."""
        QApplication.setOverrideCursor(Qt.WaitCursor)  # pinging a local AI server takes up to a few seconds
        try:
            issues = [i for i in preflight(self.settings)
                      if not (i.dismissable and i.key in self.settings.dismissed_warnings)]
        finally:
            QApplication.restoreOverrideCursor()
        if not issues:
            return True
        box = QMessageBox(QMessageBox.Warning, "Before analyzing",
                          "Some settings won't take effect in this analysis:", parent=self)
        box.setInformativeText("\n\n".join(f"• {i.message}" for i in issues))
        fix = next((i.fix_image_profile for i in issues if i.fix_image_profile), "")
        fix_btn = box.addButton(f"Use {fix!r} as Image AI and analyze", QMessageBox.AcceptRole) if fix else None
        go_btn = box.addButton("Analyze anyway", QMessageBox.AcceptRole)
        box.addButton(QMessageBox.Cancel)
        box.setDefaultButton(fix_btn or go_btn)
        dismissable = [i.key for i in issues if i.dismissable]
        dont_ask = None
        if dismissable:
            dont_ask = QCheckBox("Don't warn me again about these settings (unreachable AIs are always shown)"
                                 if len(dismissable) < len(issues) else "Don't warn me again about these")
            box.setCheckBox(dont_ask)
        box.exec()
        clicked = box.clickedButton()
        if clicked not in (fix_btn, go_btn):
            return False
        if dont_ask is not None and dont_ask.isChecked():
            self.settings.dismissed_warnings = sorted(set(self.settings.dismissed_warnings) | set(dismissable))
        if clicked is fix_btn:
            self.image_box.setCurrentIndex(max(0, self.image_box.findData(fix)))
        self._sync_settings()
        return True

    def _on_ai_down(self, message: str, image: bool):
        """The worker waits for this answer: Retry, skip the book, go on without that AI, or Stop."""
        worker = self.worker
        if not isinstance(worker, AnalyzeWorker):
            return
        if self._close_pending:
            worker.answer(AI_OFF)
            return
        what = "image AI" if image else "text AI"
        choice = ask_ai_down(self, message, what,
                             f"Continue without the {what}: the rest of this analysis is decided without it; "
                             "those books are marked (filter: Reduced checks). The setting isn't changed: "
                             "the next analysis tries it again.\n"
                             "Stop: keep the books analyzed so far.")
        if choice is None:
            self._stop()
        worker.answer(choice or AI_OFF)

    def _on_unpack_asked(self, count: int):
        """The worker waits for this answer (see UnpackQuestion)."""
        worker = self.worker
        if not isinstance(worker, AnalyzeWorker):
            return
        if self._close_pending:
            worker.answer_unpack(False)
            return
        worker.answer_unpack(ask_unpack(self, count))

    def _analyze(self):
        self._sync_settings()
        if not self._preflight_ok():
            return
        self._plan_signature = analysis_signature(self.settings)
        source, target, trash = (self._library(k) for k in ("source", "target", "trash"))
        if not self._claim_libraries(LibraryUse("dedup"), [source, target], trash):
            return
        same = bool(source) and str(Path(source).resolve()).casefold() == str(Path(target).resolve()).casefold()
        # Rows appear as books are decided; the table is read-only and unsorted until the end.
        self.plan = Plan(source, target, trash, same_library=same, tag=self.settings.only_tag,
                         tag_exclude=self.settings.only_tag_exclude and bool(self.settings.only_tag))
        self._eta.reset()
        self.model.start_live(self.plan)
        self._restored = 0
        self._set_action_entries(same)
        worker = AnalyzeWorker(self.settings, source, target, trash, self.no_cache.isChecked())
        worker.progress.connect(self._on_progress)
        worker.items_ready.connect(self._on_items)
        worker.finished_ok.connect(self._analysis_done)
        worker.failed.connect(self._analysis_failed)
        worker.ai_down.connect(self._on_ai_down)
        worker.unpack_asked.connect(self._on_unpack_asked)
        worker.pause_waiting.connect(self._on_pause_waiting)
        self._exec_paused = False
        self._executed_removed = 0
        self._start(worker, "analyze")
        log.info("Analysis started")

    def _on_items(self, items: list):
        # Before the rows are shown: remembered choices can't overwrite what the user
        # changes during the run (they used to be applied at the end).
        self._restored += self.store.apply(self.plan, items)
        self.model.append_items(items)
        self._update_summary()

    def _claim_libraries(self, use: LibraryUse, analyzed: list[str], trash: str) -> bool:
        """Mark the libraries of the new analysis as used, replacing the previous
        plan's. False (and told) if another running program stands in the way."""
        try:
            use.claim(analyzed, trash, replacing=self._library_use)
        except (LibraryInUse, OSError) as e:
            QMessageBox.warning(self, "Library in use", str(e))
            return False
        self._release_libraries()
        self._library_use = use
        return True

    def _release_libraries(self):
        if self._library_use is not None:
            self._library_use.release()
            self._library_use = None

    def _analysis_failed(self, message: str):
        self._release_libraries()
        self.plan = None
        self._plan_signature = None
        self.model.set_plan(None)
        self._worker_failed(message)

    def _on_progress(self, done: int, total: int, msg: str):
        self._progress_max = max(total, 1)
        self.progress.setMaximum(self._progress_max)
        self.progress.setValue(done)
        if msg.startswith("Analyzing ") and total:
            msg = f"Book {min(done + 1, total)} of {total}: {msg}"
            self._eta.update(done, total)
        self._status_msg, self._status_since = msg, time.monotonic()
        self._show_status()

    def _show_status(self):
        """Current book, plus seconds spent on it: an AI answer can take minutes."""
        if not self._busy or not self._status_msg:
            return
        text = with_eta(self._status_msg, self._eta.text() if self._operation == "analyze" else "")
        pause = getattr(self.worker, "pause", None)
        if self._stopping:
            text = f"Stopping after the current book… · {text}"
        elif self._exec_pausing:
            text = f"Pausing after the current book… · {text}"
        elif pause is not None and pause.paused:
            text = f"{'Paused' if self._paused else 'Pausing after the current book…'} · {text}"
            if self._paused:  # the seconds counter would count the pause
                self.status_label.setText(text)
                return
        elapsed = int(time.monotonic() - self._status_since)
        if elapsed >= 3:
            text += f" · {elapsed} s"
        self.status_label.setText(text)

    def _analysis_done(self, plan: Plan):
        log.debug("Analysis result received on GUI thread")
        restored = self._restored  # applied batch by batch as books were decided
        self.plan = plan
        self.model.set_plan(plan)
        self._set_action_entries(plan.same_library)
        log.debug("Plan model populated: items=%d", len(plan.items))
        self._finish()
        log.debug("Worker marked finished")
        self._update_analysis_recap()
        log.debug("Analysis recap updated")
        log.info("Analysis %s: %d move, %d trash, %d leave",
                 f"stopped after {len(plan.items)} of {plan.total_books} books" if plan.stopped else "complete",
                 plan.count(Action.MOVE), plan.count(Action.TRASH), plan.count(Action.LEAVE))
        if restored:
            log.info("Restored %d manual choice(s) from a previous analysis of these libraries", restored)
        info, warnings = run_summary(plan)
        log.info("Analysis checks: %s", info)
        for w in warnings:
            log.warning("Analysis checks: %s", w)
        self._update_notices()
        if plan.stop_reason and not self._close_pending:
            QMessageBox.warning(
                self, "Analysis stopped",
                f"The analysis stopped by itself after {len(plan.items)} of {plan.total_books} books:\n\n"
                f"{plan.stop_reason}\n\nThe books analyzed so far are listed. Check the drive, "
                "then analyze again (fast: AI answers are cached).")

    # --- execute --------------------------------------------------------------------
    def _execute(self, continuing: bool = False):
        """`continuing`: Continue after Pause, the remaining checked books without asking again."""
        if not self.plan:
            return
        if calibre_is_running():
            QMessageBox.warning(self, "Calibre is running",
                                "Close Calibre (and calibre-server) before executing: two programs "
                                "must not change a library at the same time.")
            return
        if not self._execution_libraries_ok():
            return
        p = self.plan
        todo = actionable(p)
        main = [i for i in todo if runs_main_action(i, self.model.blocked)]
        moves = sum(1 for i in main if i.action is Action.MOVE)
        trashes = sum(1 for i in main if i.action is Action.TRASH)
        where = "permanently deleted" if self.settings.delete_permanently else "moved to Calibre's recycle bin"
        n_blocked = len(self.model.blocked)
        unreadable = sum(1 for i in main if i.action is Action.TRASH and i.unreadable)
        empty = sum(1 for i in main if i.action is Action.TRASH and not i.source.formats)
        no_copy = sum(1 for i in main if i.action is Action.TRASH and i.match is None) - unreadable - empty
        trashed_whole = {id(i) for i in main if i.action is Action.TRASH}
        bad = sum(1 for i in todo if i.bad_formats_to_trash and id(i) not in trashed_whole)
        unpacked = sum(1 for i in todo if i.archives_to_unpack)
        to_review = sum(1 for i in p.items if needs_review(i))
        answer = QMessageBox.Yes if continuing else QMessageBox.question(
            self, "Execute checked books",
            f"{moves} books will be moved to the target library.\n"
            f"{trashes} books will be moved to the trash library ({p.trash_library}).\n"
            + (f"   {unreadable} of them have no file Calibre can open.\n" if unreadable else "")
            + (f"   {empty} of them are empty records (no file).\n" if empty else "")
            + (f"   {no_copy} of them (forced) have no copy in the target: "
               "they will only be in the trash library.\n" if no_copy else "")
            + (f"{bad} books have formats Calibre can't open: each whole record is copied to the trash "
               "library, then those formats are removed from the source.\n" if bad else "")
            + (f"{unpacked} books have their archive unpacked: the formats they lack are added, and the "
               "archive goes to the trash library inside a copy of the whole record.\n" if unpacked else "") +
            f"{len(p.items) - len(main)} books stay in the source library"
            + (f" (including {n_blocked} blocked)" if n_blocked else "") + ".\n"
            + (f"{to_review} books need review (Status → Needs review): those not ticked are left as they "
               "are.\n" if to_review else "")
            + f"\nAfter a verified copy, each book is {where} in the source library. A copy of each library's "
            "metadata.db is saved first, and a journal of what is done (data folder: snapshots, journal)."
            "\n\nContinue?")
        if answer != QMessageBox.Yes:
            return
        self._sync_settings()
        self.store.save(p)
        self.model.locked = True
        self._exec_paused = self._exec_pausing = False
        worker = ExecuteWorker(self.settings, p)
        total = len(todo)
        self._done_count = 0
        self.progress.setMaximum(total)
        self.progress.setValue(0)
        worker.result.connect(self._on_result)
        worker.finished_ok.connect(self._execution_done)
        worker.failed.connect(self._worker_failed)
        self._exec_total = total
        self._start(worker, "execute")
        log.info("Execution started")

    def _execution_libraries_ok(self) -> bool:
        """The trash library chosen now is the one used (see _use_trash), and no library
        of the plan is being written by another execution; if only the trash is, the
        user may choose another. False = don't execute."""
        p = self.plan
        while True:
            if not self._use_trash(self._library("trash")):
                return False
            conflicts = execution_conflicts("dedup", {"source": p.source_library, "target": p.target_library,
                                                      "trash": p.trash_library})
            if not conflicts:
                return True
            if not ask_other_trash(self, "dedup", conflicts, self.lib_boxes["trash"]):
                return False

    def _use_trash(self, trash: str) -> bool:
        """Make `trash` the plan's trash library, checked as before an analysis. It may
        differ from the analysis' without analyzing again: the analysis never reads it."""
        p = self.plan
        if same_library(trash, p.trash_library):
            return True
        try:
            check_libraries(p.source_library, p.target_library, trash)
        except ValueError as e:
            QMessageBox.warning(self, "Trash library", str(e))
            return False
        if not self._claim_libraries(LibraryUse("dedup"), [p.source_library, p.target_library], trash):
            return False
        log.info("Trash library changed after the analysis: %s (was %s)", trash, p.trash_library)
        p.trash_library = trash
        return True

    def _on_result(self, item: PlanItem):
        self._done_count += 1
        self.execute_btn.setText(f"Executing… ({self._done_count} of {self._exec_total})")
        self.progress.setValue(self._done_count)
        prefix = "Pausing after the current book… · " if self._exec_pausing else ""
        self.status_label.setText(f"{prefix}{item.source.label()}: {item.status}")
        self.model.item_changed(item)

    def _execution_done(self, ok: int, failed: int):
        """The executed books leave the list; the plan can be executed again for the others
        (executor.apply_result wrote what was done into it): no new analysis needed."""
        paused, self._exec_pausing = self._exec_pausing and not self._stopping, False
        self._exec_paused = paused and bool(actionable(self.plan))
        self.model.locked = False
        self._finish()
        self._remove_executed()
        self.model.refresh()
        if self._exec_paused:
            msg = (f"Execution paused: {ok} succeeded, {failed} failed. Continue executes the remaining "
                   f"{len(actionable(self.plan))} checked books.")
            log.info(msg)
            self.status_label.setText(msg)
            return
        msg = f"Execution finished: {ok} succeeded, {failed} failed."
        if failed:
            msg += " The failed books stay in the list, unticked: tick them to try again."
        log.info(msg)
        if not self._close_pending:
            (QMessageBox.warning if failed else QMessageBox.information)(self, "Done", msg)

    # --- worker plumbing ----------------------------------------------------------------
    def _start(self, worker: QThread, operation: str):
        if self.worker is not None:  # the buttons prevent this; never drop a running thread
            log.warning("Previous operation still finishing; try again in a moment")
            return
        self.worker, self._operation = worker, operation
        self._status_msg, self._stopping = "", False
        worker.finished.connect(lambda w=worker: self._worker_finished(w))
        self._set_busy(True)
        worker.start()

    def _finish(self):
        self._set_busy(False)
        self.status_label.setText("")

    def _worker_finished(self, worker: QThread):
        if self.worker is worker:
            self.worker, self._operation = None, ""
        worker.deleteLater()
        self._set_busy(self._busy)  # re-enable Analyze/Execute now that the thread is gone
        if self.worker is None and not self._exec_paused and not self._close_pending:
            self.shutdown.run_ended()
        if self._close_pending and self.worker is None:
            QTimer.singleShot(0, self.close)

    def _remove_executed(self):
        removed = self.model.remove_done()
        self._executed_removed += removed
        if removed:
            log.info("Removed %d executed book(s) from the list", removed)
        self._update_summary()

    def _worker_failed(self, message: str):
        if isinstance(self.worker, ExecuteWorker):
            # What was done is in the plan (executor.apply_result): the rest can be executed again.
            self.model.locked = False
            self._exec_pausing = self._exec_paused = False
            self._remove_executed()
        self._finish()
        log.error(message)
        if not self._close_pending:
            QMessageBox.warning(self, "Error", message)

    def _stop(self):
        if self.worker is not None:
            self.worker.cancel.set()
            self._stopping = True
            self._set_busy(self._busy)  # "Stopping…", no second click
            if self._status_msg:
                self._show_status()
            else:
                self.status_label.setText("Stopping after the current book…")

    # --- pause ------------------------------------------------------------------------
    def _update_pause_button(self):
        """Pause an analysis between two books, or an execution (it stops after the current
        book; Continue executes the remaining checked books). Continue goes on."""
        worker = self.worker
        if isinstance(worker, AnalyzeWorker) and self._busy:
            paused = worker.pause.paused
            self.pause_btn.setText("Continue" if paused else "Pause")
            self.pause_btn.setToolTip("Go on with the analysis" if paused else
                                      "Pause the analysis after the current book (Stop still keeps what is done)")
            self.pause_btn.setEnabled(not self._stopping)
            set_running(self.pause_btn, paused)
        elif isinstance(worker, ExecuteWorker) and self._busy:
            self.pause_btn.setText("Pausing…" if self._exec_pausing else "Pause")
            self.pause_btn.setToolTip("Stop after the current book; Continue then executes the remaining checked books")
            self.pause_btn.setEnabled(not self._exec_pausing and not self._stopping)
            set_running(self.pause_btn, self._exec_pausing)
        else:
            can_continue = (self._exec_paused and worker is None and self.plan is not None
                            and bool(actionable(self.plan)))
            self.pause_btn.setText("Continue" if can_continue else "Pause")
            self.pause_btn.setToolTip("Execute the remaining checked books" if can_continue else "")
            self.pause_btn.setEnabled(can_continue)
            set_running(self.pause_btn, can_continue)

    def _toggle_pause(self):
        worker = self.worker
        if isinstance(worker, AnalyzeWorker):
            if worker.pause.paused:
                worker.pause.resume()
                log.info("Analysis continued")
            else:
                worker.pause.pause()
                log.info("Pausing the analysis after the current book")
        elif isinstance(worker, ExecuteWorker):
            self._exec_pausing = True
            worker.cancel.set()  # it stops after the current book; the plan stays executable
            log.info("Pausing the execution after the current book")
            self.status_label.setText("Pausing after the current book…")
        elif self._exec_paused:
            self._execute(continuing=True)
            return
        self._set_busy(self._busy)
        self._show_status()

    def _on_pause_waiting(self, waiting: bool):
        """The analysis has stopped between two books (Pause), or goes on."""
        self._paused = waiting
        if waiting:
            self._eta.pause()
        else:
            self._eta.resume()
        self._set_busy(self._busy)
        self._show_status()

    def _export(self):
        if not self.plan:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export plan", str(Path.home() / "dedup_plan.csv"), "CSV (*.csv)")
        if path:
            write_csv(self.plan, path)
            log.info("Plan exported to %s", path)

    def closeEvent(self, event):
        log.info("Close requested; worker running=%s", self.worker is not None and self.worker.isRunning())
        if self.worker is not None:
            # Never quit under a running worker: the window closes itself once the
            # current book is finished (see _worker_finished).
            event.ignore()
            if self._close_pending:
                return
            if QMessageBox.question(self, "Quit", "An operation is running. Stop after the current book "
                                    "and quit?") != QMessageBox.Yes:
                return
            self._close_pending = True
            self._stop()
            self.status_label.setText("Closing after the current book is finished…")
            return
        if not self._close_pending and QMessageBox.question(
                self, "Quit", "Close Calibre Merge and Dedup?") != QMessageBox.Yes:
            event.ignore()
            return
        self.settings.window_geometry = bytes(self.saveGeometry().toBase64()).decode("ascii")
        self._sync_settings()
        self._release_libraries()
        self.shutdown.close()
        event.accept()


def run_gui() -> int:
    handler = QtLogHandler()
    configure_logging(handler, "calibre_dedup.log")

    set_taskbar_identity(DEDUP)
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Calibre Merge and Dedup")
    app.setWindowIcon(app_icon(DEDUP))
    log.info("Calibre Merge and Dedup %s", app_version())

    def _excepthook(exc_type, exc, tb):
        log.critical("Unhandled exception", exc_info=(exc_type, exc, tb))
        msg = f"Calibre Merge and Dedup hit an unexpected error:\n\n{exc_type.__name__}: {exc}"
        if QApplication.instance() is not None:
            QMessageBox.critical(None, "Unexpected error", msg)

    sys.excepthook = _excepthook

    window = MainWindow(Settings.load(), handler)
    window.show()
    return app.exec()
