from __future__ import annotations

import html
import logging
import os
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import (
    QAbstractTableModel, QByteArray, QModelIndex, QObject, QSortFilterProxyModel, Qt, QThread, QTimer, QUrl,
    Signal,
)
from PySide6.QtGui import QColor, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QGridLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QSizePolicy, QSplitter, QTableView, QVBoxLayout, QWidget,
)

from ..calibre_env import calibre_is_running, known_libraries
from ..config import Settings, config_dir
from ..executor import execute_plan
from ..extract import TextExtractor
from ..models import Action, Plan, PlanItem
from ..normalize import strip_accents
from ..planner import build_plan
from ..report import write_csv
from ..selection import (
    FILTER_LABELS, MERGE_LABEL, SelectionStore, action_label, actionable, blocked, blocked_reason,
    can_override, filter_key, mergeable_formats, override, revert,
)
from ..session import make_resolver, require_calibre_dir
from .settings_dialog import SettingsDialog
from .style import BLUE, GREEN, RED, button_css, set_running

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


def _elastic(label: QLabel) -> QLabel:
    """A label whose text never widens the window; long text is cut at the edge."""
    label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
    label.setMinimumWidth(1)
    return label


# --- logging into the GUI -----------------------------------------------------
AI_LOG_COLOR = "#7b1fa2"  # purple: lines from or about the AI in the log panel


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


# --- table model ----------------------------------------------------------------


def _is_checked(value) -> bool:
    if isinstance(value, int):
        return value == Qt.Checked.value
    return value == Qt.Checked


class PlanModel(QAbstractTableModel):
    HEADERS = ["✓", "ID", "Title", "Authors", "Action", "Reason", "Match in target", "Add formats", "AI", "Result"]
    COL_CHECK, COL_ID, COL_ACTION, COL_REASON, COL_RESULT = 0, 1, 4, 5, 9
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
        Failed, unticked and Leave rows stay (a Leave row may be "OK" too: its
        metadata was updated in place). Returns how many were removed."""
        if not self.plan:
            return 0

        def gone(it: PlanItem) -> bool:
            return it.status.startswith("OK") and it.action is not Action.LEAVE
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
        """Check (True), uncheck (False) or invert (None) items; LEAVE items are skipped."""
        if self.locked:
            return
        for it in items:
            if it.action is not Action.LEAVE:
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
        if index.column() == self.COL_CHECK and it.action is not Action.LEAVE and not self.locked:
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
        return [
            "\u26a0" if why_blocked else ("\u2013" if it.action is Action.LEAVE else ""),
            it.source.id,
            ident.title or it.source.title,
            " & ".join(ident.authors or it.source.authors),
            action_label(it) + (" (manual)" if it.manual else ""),
            f"{why_blocked} | {it.reason}" if why_blocked else it.reason,
            (it.match.label() + (" (moving now)" if it.match_planned else "")) if it.match else "",
            ", ".join(it.add_formats),
            ", ".join(sorted(ident.ai_fields)) or ("nothing found" if it.ai_used else ""),
            it.status,
        ]

    def sort_key(self, it: PlanItem, col: int):
        if col == self.COL_CHECK:
            return (2 if it.selected else 1) if it.action is not Action.LEAVE else 0
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
        if role == Qt.CheckStateRole and col == self.COL_CHECK and it.action is not Action.LEAVE:
            return Qt.Checked if it.selected else Qt.Unchecked
        if role == Qt.FontRole and it.manual:
            return self.italic_font
        if role == Qt.ForegroundRole:
            if col == self.COL_ACTION:
                return QColor(ACTION_COLORS[it.action])
            if col in (self.COL_CHECK, self.COL_REASON) and it.source.id in self.blocked:
                return QColor("#e65100")
            if col == self.COL_RESULT and it.status.startswith("FAILED"):
                return QColor("#c62828")
        return None


class PlanFilter(QSortFilterProxyModel):
    """Free-text search (all words must match) plus action and quick toggles."""

    def __init__(self):
        super().__init__()
        self.terms: list[str] = []
        self.action = ""
        self.ai_only = self.formats_only = self.checked_only = self.failed_only = False
        self.hide_unique = True

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
        if self.action and filter_key(it) != self.action:
            return False
        if self.ai_only and not it.ai_used:
            return False
        if self.formats_only and not it.add_formats:
            return False
        if self.checked_only and not (it.selected and it.action is not Action.LEAVE):
            return False
        if self.failed_only and not it.status.startswith("FAILED"):
            return False
        # Same library: books with no duplicate are usually most of the list. Books
        # whose title/authors couldn't be read stay visible: they need attention.
        if (self.hide_unique and model.plan is not None and model.plan.same_library
                and it.action is Action.LEAVE and it.match is None and it.identity.has_title_authors):
            return False
        if self.terms:
            hay = strip_accents(" ".join(str(x) for x in model.values(it)[1:7])).casefold()
            return all(t in hay for t in self.terms)
        return True


class PlanTable(QTableView):
    space_pressed = Signal()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Space:
            self.space_pressed.emit()
            return
        super().keyPressEvent(event)


# --- workers --------------------------------------------------------------------
class AnalyzeWorker(QThread):
    progress = Signal(int, int, str)
    items_ready = Signal(object)  # list of books just decided, for the live table
    finished_ok = Signal(object)
    failed = Signal(str)
    BATCH_SECONDS = 0.3  # books with no duplicate go by by the thousand: send them in batches

    def __init__(self, settings: Settings, source: str, target: str, trash: str):
        super().__init__()
        self.settings, self.source, self.target, self.trash = settings, source, target, trash
        self.cancel = threading.Event()

    def run(self):
        resolver = None
        try:
            resolver = make_resolver(self.settings)
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
                              on_item=on_item)
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
        try:
            ok, failed = execute_plan(
                self.plan, require_calibre_dir(self.settings), self.settings.update_metadata,
                self.settings.delete_permanently, lambda item, *_: self.result.emit(item), self.cancel,
            )
            self.finished_ok.emit(ok, failed)
        except Exception as e:
            log.exception("Execution failed")
            self.failed.emit(str(e))


# --- main window ------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self, settings: Settings, log_handler: QtLogHandler):
        super().__init__()
        self.settings = settings
        self.plan: Plan | None = None
        self.worker: QThread | None = None
        self._stopping = False  # Stop pressed; the worker ends after the current book
        self._close_pending = False  # quit requested; close once the worker has ended
        self._operation = ""  # "analyze" or "execute" while a worker thread exists
        self._done_count = self._exec_total = 0
        self._executed_removed = 0  # executed books taken off the list
        self._restored = 0  # remembered choices re-applied to this analysis' books
        self._progress_max = 1
        self._status_msg, self._status_since = "", 0.0  # current book, for the seconds counter
        self.setWindowTitle("Calibre Duplicate Remover")
        self._restore_geometry()

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
            browse = QPushButton("Browse…")
            browse.clicked.connect(lambda _=False, b=box, l=label: self._browse(b, l))
            grid.addWidget(QLabel(label), r, 0)
            grid.addWidget(box, r, 1)
            grid.addWidget(browse, r, 2)
            self.lib_boxes[key] = box
        grid.setColumnStretch(1, 1)

        # AI row
        self.text_box = _compact(QComboBox(), 24)
        self.text_box.setToolTip("Reads book text to find missing metadata. None: metadata only, no AI at all.")
        self.image_box = _compact(QComboBox(), 24)
        self.image_box.setToolTip("A model that reads text and images: compares covers and reads scanned PDFs.\n"
                                  "Only profiles marked 'Supports images' are listed. Needs a text AI.")
        self.text_box.currentIndexChanged.connect(self._fill_image_box)
        self._fill_profiles()
        settings_btn = QPushButton("Settings…")
        settings_btn.clicked.connect(self._open_settings)
        ai_row = QHBoxLayout()
        ai_row.addWidget(QLabel("Text AI:"))
        ai_row.addWidget(self.text_box, 1)
        ai_row.addWidget(QLabel("Image AI:"))
        ai_row.addWidget(self.image_box, 1)
        ai_row.addWidget(settings_btn)

        # actions
        self.analyze_btn = QPushButton("1. Analyze (dry run)")
        self.execute_btn = QPushButton("2. Execute checked")
        self.stop_btn = QPushButton("Stop")
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
        btn_row = QHBoxLayout()
        for w in (self.analyze_btn, self.execute_btn, self.stop_btn, self.export_btn):
            btn_row.addWidget(w)
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
        self.action_filter = _compact(QComboBox(), 12)
        self._fill_action_filter(False)
        self.action_filter.currentIndexChanged.connect(
            lambda: self.proxy.update(action=self.action_filter.currentData()))
        self.toggles = {}
        for attr, label in [("ai_only", "AI used"), ("formats_only", "Adds formats"),
                            ("checked_only", "Only checked"), ("failed_only", "Only failed")]:
            box = QCheckBox(label)
            box.toggled.connect(lambda on, a=attr: self.proxy.update(**{a: on}))
            self.toggles[attr] = box
        self.hide_unique = QCheckBox("Hide books with no duplicate")
        self.hide_unique.setToolTip("Same-library analysis only: hide books left in the library because "
                                    "no other book has the same title and authors")
        self.hide_unique.setChecked(self.proxy.hide_unique)
        self.hide_unique.toggled.connect(lambda on: self.proxy.update(hide_unique=on))
        self.hide_unique.setVisible(False)
        clear = QPushButton("Clear filters")
        clear.clicked.connect(self._clear_filters)
        filter_row = QHBoxLayout()
        filter_row.addWidget(self.search, 1)
        filter_row.addWidget(self.action_filter)
        for box in self.toggles.values():
            filter_row.addWidget(box)
        filter_row.addWidget(self.hide_unique)
        filter_row.addWidget(clear)

        # bulk selection
        self.check_btn = QPushButton("Check shown rows")
        self.uncheck_btn = QPushButton("Uncheck shown rows")
        self.invert_btn = QPushButton("Invert shown rows")
        self.check_btn.setToolTip("Check rows currently shown after filtering")
        self.uncheck_btn.setToolTip("Uncheck rows currently shown after filtering")
        self.invert_btn.setToolTip("Invert checks for rows currently shown after filtering")
        self.check_btn.clicked.connect(lambda: self.model.set_checked(self._visible_items(), True))
        self.uncheck_btn.clicked.connect(lambda: self.model.set_checked(self._visible_items(), False))
        self.invert_btn.clicked.connect(lambda: self.model.set_checked(self._visible_items(), None))
        self.checked_label = _elastic(QLabel())
        bulk_row = QHBoxLayout()
        for w in (self.check_btn, self.uncheck_btn, self.invert_btn):
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
        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self.table)
        splitter.addWidget(self.log_view)
        splitter.setSizes([600, 150])

        self.progress = QProgressBar()
        self.status_label = _elastic(QLabel())
        self._ticker = QTimer(self, interval=1000)  # keeps the seconds counter moving
        self._ticker.timeout.connect(self._show_status)
        self._ticker.start()

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(libs)
        layout.addLayout(ai_row)
        layout.addLayout(btn_row)
        layout.addLayout(filter_row)
        layout.addLayout(bulk_row)
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
        self.text_box.blockSignals(False)
        self.image_box.clear()  # refilled from the saved setting
        self._fill_image_box()

    def _fill_image_box(self):
        """Image AI choices: profiles that support images. Off when the text AI is off."""
        current = self.image_box.currentData() if self.image_box.count() else self.settings.image_profile
        self.image_box.clear()
        self.image_box.addItem("None (no cover check, skip scanned PDFs)", "")
        for p in self.settings.profiles:
            if p.vision:
                self.image_box.addItem(self._profile_label(p), p.name)
        self.image_box.setCurrentIndex(max(0, self.image_box.findData(current)))
        self.image_box.setEnabled(bool(self.text_box.currentData()))

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

    def _fill_action_filter(self, same_library: bool):
        """List the actions a plan can contain: a same-library plan never moves."""
        current = self.action_filter.currentData()
        self.action_filter.blockSignals(True)
        self.action_filter.clear()
        self.action_filter.addItem("All actions", "")
        for key, label in FILTER_LABELS.items():
            if not (same_library and key == Action.MOVE.value):
                self.action_filter.addItem(label, key)
        self.action_filter.setCurrentIndex(max(0, self.action_filter.findData(current)))
        self.action_filter.blockSignals(False)
        self.proxy.update(action=self.action_filter.currentData())

    def _library(self, key: str) -> str:
        return self.lib_boxes[key].currentText().strip()

    def _sync_settings(self):
        s = self.settings
        s.source_library, s.target_library, s.trash_library = (self._library(k) for k in ("source", "target", "trash"))
        s.text_profile = self.text_box.currentData() or ""
        s.image_profile = self.image_box.currentData() or ""
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
        self.table.setSortingEnabled(not busy)  # no sorting while rows arrive or results come in
        for box in self.lib_boxes.values():
            box.setEnabled(not busy)
        editable = self._can_edit()
        for btn in (self.check_btn, self.uncheck_btn, self.invert_btn):
            btn.setEnabled(editable)
        self._update_summary()

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
        if self.model.locked and not self._busy and self.worker is None:
            # Executed (fully or stopped): the libraries have changed and this plan
            # can't run again. A new analysis is quick (AI answers are cached).
            done = self._executed_removed + sum(1 for i in p.items if i.status)
            text = f"{done} executed"
            if self._executed_removed:
                text += f" ({self._executed_removed} done, removed from the list)"
            self.checked_label.setText(text + " · analyze again for the rest")
            self.execute_btn.setText("Analyze again to continue")
            self.execute_btn.setToolTip("This plan has been executed and the libraries have changed.\n"
                                        "Analyze again (fast: AI answers are cached) to continue with the rest.")
            self.execute_btn.setEnabled(False)
            set_running(self.execute_btn, False)
            return
        self.execute_btn.setToolTip("")
        todo = actionable(p)
        possible = sum(1 for i in p.items if i.action is not Action.LEAVE)
        n_blocked = len(self.model.blocked)
        text = f"{len(todo)} of {possible} checked"
        if n_blocked:
            text += f" · <b style='color:#e65100'>{n_blocked} blocked</b>"
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
        items = [it for it in self.model.items if it.action is not Action.LEAVE]
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
        text = f"{head}: {move} move, {trash} trash, {leave} leave (total {total})"
        hidden = total - self.proxy.rowCount()
        if hidden:
            text += f" · {hidden} hidden by filters"
        self.status_label.setText(text)

    def _toggle_selected_rows(self):
        items = [i for i in self._selected_items() if i.action is not Action.LEAVE]
        if items:
            # Mixed selection: check all. All checked: uncheck all.
            self.model.set_checked(items, not all(i.selected for i in items))

    def _context_menu(self, pos):
        index = self.table.indexAt(pos)
        if not index.isValid():
            return
        menu = QMenu(self)
        # Opening files changes nothing: available even while analyzing or executing.
        self._add_open_actions(menu, self.model.items[self.proxy.mapToSource(index).row()])
        items = self._selected_items()
        if items and self._can_edit():
            menu.addSeparator()
            self._add_override_actions(menu, items)
        menu.exec(self.table.viewport().mapToGlobal(pos))

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

    def _add_override_actions(self, menu: QMenu, items: list[PlanItem]):
        same = self.plan.same_library

        def fits(i: PlanItem, action: Action, merge: bool | None) -> bool:
            # merge: None = any; True/False = the duplicate has / has no formats to merge
            return (i.action is not action and can_override(i, action, same)
                    and (merge is None or bool(mergeable_formats(i)) == merge))

        # One library: finding duplicates, so a book can only be merged/trashed or kept.
        choices = [] if same else [(Action.MOVE, None, "Force move to target")]
        choices += [
            (Action.TRASH, True, f"Force {MERGE_LABEL} (duplicate of the match, has extra formats)"),
            (Action.TRASH, False, "Force Trash only (duplicate of the match, nothing to merge)"),
            (Action.LEAVE, None, "Keep in library" if same else "Keep in source"),
        ]
        for action, merge, label in choices:
            n = sum(1 for i in items if fits(i, action, merge))
            act = menu.addAction(f"{label} ({n})" if len(items) > 1 else label)
            act.setEnabled(n > 0)
            act.triggered.connect(
                lambda _=False, a=action, m=merge: self._override([i for i in items if fits(i, a, m)], a))
        menu.addSeparator()
        n = sum(1 for i in items if i.manual)
        act = menu.addAction(f"Revert to analysis decision ({n})" if len(items) > 1 else "Revert to analysis decision")
        act.setEnabled(n > 0)
        act.triggered.connect(lambda: self._revert(items))

    def _override(self, items: list[PlanItem], action: Action):
        skipped = 0
        same = self.plan.same_library
        for it in items:
            if it.action is action:
                continue
            if can_override(it, action, same):
                override(it, action, same)
            else:
                skipped += 1
        if skipped:
            self.status_label.setText(f"{skipped} book(s) skipped: no matching target book to be a duplicate of.")
        self.model.refresh()

    def _revert(self, items: list[PlanItem]):
        for it in items:
            if it.manual:
                revert(it)
        self.model.refresh()

    def _clear_filters(self):
        self.search.clear()
        self.action_filter.setCurrentIndex(0)
        for box in self.toggles.values():
            box.setChecked(False)
        self.hide_unique.setChecked(False)

    def _append_log(self, text: str, ai: bool):
        if ai:  # kept as plain text (the JSON replies' indentation too), only coloured
            self.log_view.appendHtml(f"<span style='color:{AI_LOG_COLOR}; white-space:pre-wrap'>"
                                     f"{html.escape(text)}</span>")
        else:
            self.log_view.appendHtml(f"<span style='white-space:pre-wrap'>{html.escape(text)}</span>")

    def _open_settings(self):
        self._sync_settings()
        dialog = SettingsDialog(self.settings, self)
        try:
            if dialog.exec():
                self._fill_profiles()
        finally:
            # Delete it now: a closed dialog left alive as a hidden child of the window
            # crashed the next analysis (Python freed memory Qt still used).
            dialog.deleteLater()

    # --- analyze --------------------------------------------------------------------
    def _analyze(self):
        self._sync_settings()
        source, target, trash = (self._library(k) for k in ("source", "target", "trash"))
        same = bool(source) and str(Path(source).resolve()).casefold() == str(Path(target).resolve()).casefold()
        # Rows appear as books are decided; the table is read-only and unsorted until the end.
        self.plan = Plan(source, target, trash, same_library=same)
        self.model.start_live(self.plan)
        self._restored = 0
        self.hide_unique.setVisible(same)
        self._fill_action_filter(same)
        worker = AnalyzeWorker(self.settings, source, target, trash)
        worker.progress.connect(self._on_progress)
        worker.items_ready.connect(self._on_items)
        worker.finished_ok.connect(self._analysis_done)
        worker.failed.connect(self._analysis_failed)
        self._start(worker, "analyze")
        log.info("Analysis started")

    def _on_items(self, items: list):
        # Before the rows are shown: remembered choices can't overwrite what the user
        # changes during the run (they used to be applied at the end).
        self._restored += self.store.apply(self.plan, items)
        self.model.append_items(items)
        self._update_summary()

    def _analysis_failed(self, message: str):
        self.plan = None
        self.model.set_plan(None)
        self._worker_failed(message)

    def _on_progress(self, done: int, total: int, msg: str):
        self._progress_max = max(total, 1)
        self.progress.setMaximum(self._progress_max)
        self.progress.setValue(done)
        if msg.startswith("Analyzing ") and total:
            msg = f"Book {min(done + 1, total)} of {total}: {msg}"
        self._status_msg, self._status_since = msg, time.monotonic()
        self._show_status()

    def _show_status(self):
        """Current book, plus seconds spent on it: an AI answer can take minutes."""
        if not self._busy or not self._status_msg:
            return
        text = self._status_msg
        if self._stopping:
            text = f"Stopping after the current book… · {text}"
        elapsed = int(time.monotonic() - self._status_since)
        if elapsed >= 3:
            text += f" · {elapsed} s"
        self.status_label.setText(text)

    def _analysis_done(self, plan: Plan):
        log.debug("Analysis result received on GUI thread")
        restored = self._restored  # applied batch by batch as books were decided
        self.plan = plan
        self.model.set_plan(plan)
        self.hide_unique.setVisible(plan.same_library)
        self._fill_action_filter(plan.same_library)
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
        if plan.stop_reason and not self._close_pending:
            QMessageBox.warning(
                self, "Analysis stopped",
                f"The analysis stopped by itself after {len(plan.items)} of {plan.total_books} books:\n\n"
                f"{plan.stop_reason}\n\nThe books analyzed so far are listed. Check the drive, "
                "then analyze again (fast: AI answers are cached).")

    # --- execute --------------------------------------------------------------------
    def _execute(self):
        if not self.plan:
            return
        if calibre_is_running():
            QMessageBox.warning(self, "Calibre is running",
                                "Close Calibre (and calibre-server) before executing: two programs "
                                "must not change a library at the same time.")
            return
        p = self.plan
        todo = actionable(p)
        moves = sum(1 for i in todo if i.action is Action.MOVE)
        trashes = len(todo) - moves
        where = "permanently deleted" if self.settings.delete_permanently else "moved to Calibre's recycle bin"
        n_blocked = len(self.model.blocked)
        answer = QMessageBox.question(
            self, "Execute checked books",
            f"{moves} books will be moved to the target library.\n"
            f"{trashes} duplicates will be moved to the trash library.\n"
            f"{len(p.items) - len(todo)} books stay in the source library"
            + (f" (including {n_blocked} blocked)" if n_blocked else "") + ".\n\n"
            f"After a verified copy, each book is {where} in the source library.\n\nContinue?")
        if answer != QMessageBox.Yes:
            return
        self._sync_settings()
        self.store.save(p)
        self.model.locked = True
        worker = ExecuteWorker(self.settings, p)
        total = len(todo)
        self._done_count = self._executed_removed = 0
        self.progress.setMaximum(total)
        self.progress.setValue(0)
        worker.result.connect(self._on_result)
        worker.finished_ok.connect(self._execution_done)
        worker.failed.connect(self._worker_failed)
        self._exec_total = total
        self._start(worker, "execute")
        log.info("Execution started")

    def _on_result(self, item: PlanItem):
        self._done_count += 1
        self.execute_btn.setText(f"Executing… ({self._done_count} of {self._exec_total})")
        self.progress.setValue(self._done_count)
        self.status_label.setText(f"{item.source.label()}: {item.status}")
        self.model.item_changed(item)

    def _execution_done(self, ok: int, failed: int):
        self._finish()  # the model stays locked: the plan is spent; analyze again
        self._remove_executed()
        self.model.refresh()
        msg = f"Execution finished: {ok} succeeded, {failed} failed."
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
            if not self._done_count:
                self.model.locked = False  # nothing was executed; the plan is still usable
            else:
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
                self, "Quit", "Close Calibre Duplicate Remover?") != QMessageBox.Yes:
            event.ignore()
            return
        self.settings.window_geometry = bytes(self.saveGeometry().toBase64()).decode("ascii")
        self._sync_settings()
        event.accept()


def run_gui() -> int:
    handler = QtLogHandler()
    file_handler = RotatingFileHandler(config_dir() / "calibre_dedup.log", maxBytes=5_000_000,
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

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Calibre Duplicate Remover")

    def _excepthook(exc_type, exc, tb):
        log.critical("Unhandled exception", exc_info=(exc_type, exc, tb))
        msg = f"Calibre Duplicate Remover hit an unexpected error:\n\n{exc_type.__name__}: {exc}"
        if QApplication.instance() is not None:
            QMessageBox.critical(None, "Unexpected error", msg)

    sys.excepthook = _excepthook

    window = MainWindow(Settings.load(), handler)
    window.show()
    return app.exec()
