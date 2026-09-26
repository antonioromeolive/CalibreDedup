"""calibre-review main window: one library to review, one trash library.

The table shows two lines per book where the AI read something different: the
current value on the first line, the proposed one ("→ …") on the second.
"""

from __future__ import annotations

import html
import logging
import sys
import threading
import time
from dataclasses import replace

from PySide6.QtCore import (
    QAbstractTableModel, QByteArray, QModelIndex, QRect, QSortFilterProxyModel, Qt, QThread, QTimer, Signal,
)
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSplitter, QStyle,
    QStyledItemDelegate, QStyleOptionViewItem, QTableView, QVBoxLayout, QWidget,
)

from ..calibre_env import calibre_is_running, known_libraries
from ..config import OLLAMA, Settings, config_dir, load_review_settings
from ..eta import Eta
from ..extract import TextExtractor
from ..normalize import strip_accents
from ..review import (
    ACTION_LABELS, FIELD_LABELS, FIELDS, ReviewAction, ReviewItem, ReviewResult, Reviewer, book_cover_file,
    REVIEW_CACHE_FILE, REVIEWED_TAG, current_value, execute_review, format_value, is_reviewed, review_actions,
    review_cache, scan_library, summary,
)
from ..session import _ollama_problem, make_resolver, require_calibre_dir
from .main_window import (
    AI_LOG_COLOR, PlanTable, QtLogHandler, _compact, _elastic, _is_checked, configure_logging, open_file,
    with_eta,
)
from .cover_preview import CoverPreview
from .settings_dialog import SettingsDialog
from .style import BLUE, GREEN, RED, button_css, mark_inactive, set_running, style_none_item

log = logging.getLogger("calibre_dedup")

ACTION_COLORS = {ReviewAction.UPDATE: "#2e7d32", ReviewAction.TRASH: "#c62828", ReviewAction.KEEP: "#8d6e00"}
NEW_COLOR = {"light": "#2e7d32", "dark": "#81c784"}  # the proposed value
MUTED_COLOR = {"light": "#8a8a8a", "dark": "#9a9a9a"}  # a change that won't be written
CELL_ROLE = Qt.UserRole + 1  # (current text, proposed text or None, will be written)


# --- table model ----------------------------------------------------------------
class ReviewModel(QAbstractTableModel):
    HEADERS = ["✓", "ID", "Action", "Title", "Authors", "Publisher", "Year", "Series", "Read", "Result"]
    COL_CHECK, COL_ID, COL_ACTION = 0, 1, 2
    FIELD_COLS = {3: "title", 4: "authors", 5: "publisher", 6: "year", 7: "series"}
    COL_READ, COL_RESULT = 8, 9
    selection_changed = Signal()

    def __init__(self, fields_on: set[str]):
        super().__init__()
        self.items: list[ReviewItem] = []
        self.fields_on = fields_on  # shared with the window: the "Change:" check boxes
        self.locked = False  # no edits while executing
        self._sort_column, self._sort_order = self.COL_ID, Qt.AscendingOrder
        self._sorted = True
        self._rows: dict[int, int] = {}  # id(item) -> row, so a row can be repainted without a search
        self.italic_font = QFont()

    def reset(self, items: list[ReviewItem]):
        self.beginResetModel()
        self.items = list(items)
        self._sort_items()
        self.endResetModel()

    def append_items(self, items: list[ReviewItem]):
        if not items:
            return
        n = len(self.items)
        self.beginInsertRows(QModelIndex(), n, n + len(items) - 1)
        self.items.extend(items)
        for row, it in enumerate(items, n):
            self._rows[id(it)] = row
        self._sorted = False
        self.endInsertRows()

    def remove(self, gone: list[ReviewItem]):
        if not gone:
            return
        ids = {id(i) for i in gone}
        self.beginResetModel()
        self.items = [i for i in self.items if id(i) not in ids]
        self._rows = {id(it): row for row, it in enumerate(self.items)}
        self.endResetModel()

    def refresh(self):
        if self.items:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.items) - 1, len(self.HEADERS) - 1))
        self.selection_changed.emit()

    def item_changed(self, item: ReviewItem):
        row = self._rows.get(id(item))
        if row is None:
            return
        self.dataChanged.emit(self.index(row, 0), self.index(row, len(self.HEADERS) - 1))

    def set_checked(self, items: list[ReviewItem], value: bool | None):
        if self.locked:
            return
        for it in items:
            if it.action is not ReviewAction.KEEP:
                it.selected = (not it.selected) if value is None else value
        self.refresh()

    # --- Qt ---------------------------------------------------------------------
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
        if (index.column() == self.COL_CHECK and not self.locked
                and self.items[index.row()].action is not ReviewAction.KEEP):
            f |= Qt.ItemIsUserCheckable
        return f

    def setData(self, index, value, role=Qt.EditRole):
        if role == Qt.CheckStateRole and index.column() == self.COL_CHECK:
            self.set_checked([self.items[index.row()]], _is_checked(value))
            return True
        return False

    def cell(self, it: ReviewItem, name: str) -> tuple[str, str | None, bool]:
        old = format_value(name, current_value(it.book, name))
        if name not in it.changes:
            return old, None, False
        written = (name in self.fields_on and name not in it.excluded and it.action is ReviewAction.UPDATE)
        return old, format_value(name, it.changes[name]), written

    def text(self, it: ReviewItem, col: int) -> str:
        if col in self.FIELD_COLS:
            old, new, _ = self.cell(it, self.FIELD_COLS[col])
            return old if new is None else f"{old or '—'}\n→ {new}"
        if col == self.COL_CHECK:
            return ""
        if col == self.COL_ID:
            return str(it.book.id)
        if col == self.COL_ACTION:
            label = ACTION_LABELS[it.action]
            if it.action is ReviewAction.UPDATE and not it.to_write(self.fields_on):
                label += " (nothing to write)"
            return label + (" (manual)" if it.manual else "")
        if col == self.COL_READ:
            return it.note
        return it.status

    def data(self, index, role=Qt.DisplayRole):
        it = self.items[index.row()]
        col = index.column()
        if role == Qt.DisplayRole:
            return it.book.id if col == self.COL_ID else self.text(it, col)
        if role == Qt.ToolTipRole:
            return self.text(it, col) or None
        if role == CELL_ROLE and col in self.FIELD_COLS:
            return self.cell(it, self.FIELD_COLS[col])
        if role == Qt.CheckStateRole and col == self.COL_CHECK and it.action is not ReviewAction.KEEP:
            return Qt.Checked if it.selected else Qt.Unchecked
        if role == Qt.FontRole and it.manual and col == self.COL_ACTION:
            return self.italic_font
        if role == Qt.TextAlignmentRole and col not in self.FIELD_COLS:
            return int(Qt.AlignLeft | Qt.AlignTop)
        if role == Qt.ForegroundRole:
            if col == self.COL_ACTION:
                return QColor(ACTION_COLORS[it.action])
            if col == self.COL_RESULT and it.status.startswith("FAILED"):
                return QColor("#c62828")
            if col == self.COL_READ and it.found is None:
                return QColor("#e65100")
        return None

    # --- sorting (in Python, see main_window.PlanModel) ----------------------------
    def sort_key(self, it: ReviewItem, col: int):
        if col == self.COL_CHECK:
            return (2 if it.selected else 1) if it.action is not ReviewAction.KEEP else 0
        if col == self.COL_ID:
            return it.book.id
        return strip_accents(self.text(it, col)).casefold()

    def _sort_items(self):
        col = self._sort_column
        self.items.sort(key=lambda it: self.sort_key(it, col), reverse=self._sort_order == Qt.DescendingOrder)
        self._rows = {id(it): row for row, it in enumerate(self.items)}
        self._sorted = True

    def sort(self, column, order=Qt.AscendingOrder):
        if self._sorted and (column, order) == (self._sort_column, self._sort_order):
            return
        self._sort_column, self._sort_order = column, order
        self.layoutAboutToBeChanged.emit()
        old = self.persistentIndexList()
        moved = [(self.items[i.row()], i.column()) for i in old]
        self._sort_items()
        self.changePersistentIndexList(old, [self.index(self._rows[id(it)], c) for it, c in moved])
        self.layoutChanged.emit()


class ReviewFilter(QSortFilterProxyModel):
    def __init__(self):
        super().__init__()
        self.terms: list[str] = []
        self.action = ""
        self.changes_only = True
        self.unread_only = self.checked_only = self.failed_only = False

    def update(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
        self.invalidateRowsFilter()

    def sort(self, column, order=Qt.AscendingOrder):
        self.sourceModel().sort(column, order)

    def filterAcceptsRow(self, row, parent):
        model: ReviewModel = self.sourceModel()
        it = model.items[row]
        if self.action and it.action.value != self.action:
            return False
        # A book the user sent to the trash stays visible even without differences.
        if self.changes_only and not it.changes and it.action is not ReviewAction.TRASH:
            return False
        if self.unread_only and it.found is not None:
            return False
        if self.checked_only and not (it.selected and it.action is not ReviewAction.KEEP):
            return False
        if self.failed_only and not it.status.startswith("FAILED"):
            return False
        if self.terms:
            hay = strip_accents(" ".join(model.text(it, c) for c in range(1, len(model.HEADERS)))).casefold()
            return all(t in hay for t in self.terms)
        return True


class TwoLineDelegate(QStyledItemDelegate):
    """Current value on the first line; the proposed value, if different, on the
    second: green when it will be written, grey and struck through when not."""

    def paint(self, painter, option, index):
        old, new, written = index.data(CELL_ROLE) or ("", None, False)
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        style = opt.widget.style() if opt.widget else QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, opt.widget)

        selected = bool(opt.state & QStyle.State_Selected)
        dark = opt.palette.color(QPalette.Base).lightness() < 128
        theme = "dark" if dark else "light"
        normal = opt.palette.color(QPalette.HighlightedText if selected else QPalette.Text)
        muted = QColor(MUTED_COLOR[theme])
        rect = opt.rect.adjusted(4, 2, -4, -2)
        fm = opt.fontMetrics
        h = fm.lineSpacing()
        painter.save()
        painter.setFont(opt.font)
        painter.setPen(muted if (new is not None and not old) else normal)
        painter.drawText(QRect(rect.x(), rect.y(), rect.width(), h), Qt.AlignLeft | Qt.AlignVCenter,
                         fm.elidedText(old or ("—" if new is not None else ""), Qt.ElideRight, rect.width()))
        if new is not None:
            font = QFont(opt.font)
            font.setBold(written)
            font.setStrikeOut(not written)
            painter.setFont(font)
            painter.setPen(normal if selected else (QColor(NEW_COLOR[theme]) if written else muted))
            painter.drawText(QRect(rect.x(), rect.y() + h, rect.width(), h), Qt.AlignLeft | Qt.AlignVCenter,
                             painter.fontMetrics().elidedText(f"→ {new}", Qt.ElideRight, rect.width()))
        painter.restore()


# --- workers --------------------------------------------------------------------
class ScanWorker(QThread):
    progress = Signal(int, int, str)
    items_ready = Signal(object)
    finished_ok = Signal(object)
    failed = Signal(str)
    ai_down = Signal(str, bool)
    BATCH_SECONDS = 0.3

    def __init__(self, settings: Settings, library: str, trash: str):
        super().__init__()
        self.settings, self.library, self.trash = settings, library, trash
        self.cancel = threading.Event()
        self._answered = threading.Event()
        self._retry = False

    def ask(self, message: str, image: bool) -> bool:
        """See main_window.AnalyzeWorker.ask."""
        self._answered.clear()
        self._retry = False
        self.ai_down.emit(message, image)
        while not self._answered.wait(0.2):
            if self.cancel.is_set():
                return False
        return self._retry

    def answer(self, retry: bool) -> None:
        self._retry = retry
        self._answered.set()

    def run(self):
        reviewer = None
        try:
            reviewer = make_resolver(self.settings, on_down=self.ask, cls=Reviewer, cache=review_cache())
            if reviewer is None:
                raise RuntimeError("The review needs an AI: choose a Text AI (or an Image AI).")
            batch: list = []
            sent = time.monotonic()

            def on_item(item):
                nonlocal sent
                batch.append(item)
                if time.monotonic() - sent >= self.BATCH_SECONDS:
                    self.items_ready.emit(batch.copy())
                    batch.clear()
                    sent = time.monotonic()

            result = scan_library(self.library, self.trash, reviewer, self.progress.emit, self.cancel, on_item,
                                  skip_reviewed=self.settings.review_skip_reviewed)
            if batch:
                self.items_ready.emit(batch.copy())
            self.finished_ok.emit(result)
        except Exception as e:
            log.exception("Review failed")
            self.failed.emit(str(e))
        finally:
            if reviewer:
                reviewer.cache.save()
                reviewer.extractor.close()


class ExecuteWorker(QThread):
    result = Signal(object)
    finished_ok = Signal(int, int, int)
    failed = Signal(str)

    def __init__(self, settings: Settings, result: ReviewResult, fields_on: set[str]):
        super().__init__()
        self.settings, self.review, self.fields_on = settings, result, set(fields_on)
        self.cancel = threading.Event()

    def run(self):
        try:
            ok, failed, tagged = execute_review(
                self.review, self.fields_on, require_calibre_dir(self.settings), self.settings.delete_permanently,
                lambda item, *_: self.result.emit(item), self.cancel)
            self.finished_ok.emit(ok, failed, tagged)
        except Exception as e:
            log.exception("Execution failed")
            self.failed.emit(str(e))


# --- main window ------------------------------------------------------------------
class ReviewWindow(QMainWindow):
    TITLE = "Calibre Metadata Review"

    def __init__(self, settings: Settings, log_handler: QtLogHandler):
        super().__init__()
        self.settings = settings
        self.result: ReviewResult | None = None
        self.worker: QThread | None = None
        self._operation = ""
        self._stopping = self._close_pending = False
        self._busy = False
        self._done_count = self._exec_total = 0
        self._status_msg, self._status_since = "", 0.0
        self._eta = Eta()  # time left of the scan
        self.fields_on: set[str] = {f for f in settings.review_fields if f in FIELDS}
        self.setWindowTitle(self.TITLE)
        self._restore_geometry()

        # libraries
        libs = QGroupBox("Libraries")
        grid = QGridLayout(libs)
        known = known_libraries()
        self.lib_boxes: dict[str, QComboBox] = {}
        rows = [
            ("library", "Library to review", "The books whose metadata is checked and corrected",
             settings.review_library),
            ("trash", "Trash library", "Removed books go here (created if empty)", settings.trash_library),
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

        # AI
        self.text_box = _compact(QComboBox(), 24)
        self.image_box = _compact(QComboBox(), 24)
        self.text_box.currentTextChanged.connect(self._ai_choice_changed)
        self.image_box.currentTextChanged.connect(self._ai_choice_changed)
        self._fill_profiles()
        settings_btn = QPushButton("Settings…")
        settings_btn.clicked.connect(self._open_settings)
        ai_row = QHBoxLayout()
        ai_row.addWidget(QLabel("Text AI:"))
        ai_row.addWidget(self.text_box, 1)
        ai_row.addWidget(QLabel("Image AI (reads the cover):"))
        ai_row.addWidget(self.image_box, 1)
        ai_row.addWidget(settings_btn)

        # actions
        self.scan_btn = QPushButton("1. Scan with AI")
        self.execute_btn = QPushButton("2. Execute checked")
        self.stop_btn = QPushButton("Stop")
        self.scan_btn.setStyleSheet(button_css(*BLUE))
        self.execute_btn.setStyleSheet(button_css(*GREEN))
        self.stop_btn.setStyleSheet(button_css(*RED))
        self.scan_btn.clicked.connect(self._scan)
        self.execute_btn.clicked.connect(self._execute)
        self.stop_btn.clicked.connect(self._stop)
        self.summary_label = _elastic(QLabel())
        btn_row = QHBoxLayout()
        for w in (self.scan_btn, self.execute_btn, self.stop_btn):
            btn_row.addWidget(w)
        self.skip_reviewed = QCheckBox(f"Skip books tagged {REVIEWED_TAG}")
        self.skip_reviewed.setChecked(settings.review_skip_reviewed)
        self.skip_reviewed.setToolTip(
            f"On Execute, every book of the scan is tagged {REVIEWED_TAG} (updated or not):\n"
            "the next scan leaves them out, also on another computer.\n"
            "To review a book again, remove the tag in Calibre.")
        btn_row.addSpacing(16)
        btn_row.addWidget(self.skip_reviewed)
        btn_row.addSpacing(16)
        btn_row.addWidget(QLabel("Change:"))
        self.field_boxes: dict[str, QCheckBox] = {}
        for name in FIELDS:
            box = QCheckBox(FIELD_LABELS[name])
            box.setChecked(name in self.fields_on)
            box.setToolTip(f"Write the {FIELD_LABELS[name].lower()} read by the AI to the checked books.\n"
                           "Unticked: shown, but never written.")
            box.toggled.connect(lambda on, n=name: self._field_toggled(n, on))
            self.field_boxes[name] = box
            btn_row.addWidget(box)
        btn_row.addStretch(1)
        btn_row.addWidget(self.summary_label)

        # filters
        self.model = ReviewModel(self.fields_on)
        self.proxy = ReviewFilter()
        self.proxy.setSourceModel(self.model)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search title, authors, publisher, series… (all words must match)")
        self.search.setClearButtonEnabled(True)
        self._search_timer = QTimer(self, singleShot=True, interval=200)
        self._search_timer.timeout.connect(
            lambda: self.proxy.update(terms=strip_accents(self.search.text()).casefold().split()))
        self.search.textChanged.connect(self._search_timer.start)
        self.action_filter = _compact(QComboBox(), 10)
        self.action_filter.addItem("All actions", "")
        for action, label in ACTION_LABELS.items():
            self.action_filter.addItem(label, action.value)
        self.action_filter.currentIndexChanged.connect(
            lambda: self.proxy.update(action=self.action_filter.currentData()))
        self.toggles: dict[str, QCheckBox] = {}
        for attr, label, tip in [
            ("changes_only", "Only with differences", "Hide books where the AI read the same metadata"),
            ("unread_only", "Not read", "Books the AI could not read (no file, no text, errors)"),
            ("checked_only", "Only checked", ""),
            ("failed_only", "Only failed", ""),
        ]:
            box = QCheckBox(label)
            box.setToolTip(tip)
            box.setChecked(getattr(self.proxy, attr))
            box.toggled.connect(lambda on, a=attr: self.proxy.update(**{a: on}))
            self.toggles[attr] = box
        clear = QPushButton("Clear filters")
        clear.clicked.connect(self._clear_filters)
        filter_row = QHBoxLayout()
        filter_row.addWidget(self.search, 1)
        filter_row.addWidget(self.action_filter)
        for box in self.toggles.values():
            filter_row.addWidget(box)
        filter_row.addWidget(clear)

        # bulk selection
        self.check_btn = QPushButton("Check shown rows")
        self.uncheck_btn = QPushButton("Uncheck shown rows")
        self.check_btn.clicked.connect(lambda: self.model.set_checked(self._visible_items(), True))
        self.uncheck_btn.clicked.connect(lambda: self.model.set_checked(self._visible_items(), False))
        self.checked_label = _elastic(QLabel())
        bulk_row = QHBoxLayout()
        bulk_row.addWidget(self.check_btn)
        bulk_row.addWidget(self.uncheck_btn)
        bulk_row.addWidget(_elastic(QLabel(
            "  Space toggles selected rows · right-click: Update / Keep / Trash, or don't change a field")), 1)
        bulk_row.addWidget(self.checked_label)

        # table, cover, log
        self.model.selection_changed.connect(self._update_summary)
        self.table = PlanTable()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(ReviewModel.COL_ID, Qt.AscendingOrder)
        self.table.setSelectionBehavior(QTableView.SelectRows)
        self.table.setWordWrap(False)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.table.space_pressed.connect(self._toggle_selected_rows)
        self.model.italic_font = QFont(self.table.font())
        self.model.italic_font.setItalic(True)
        delegate = TwoLineDelegate(self.table)
        for col in ReviewModel.FIELD_COLS:
            self.table.setItemDelegateForColumn(col, delegate)
        rows_header = self.table.verticalHeader()
        rows_header.setSectionResizeMode(QHeaderView.Fixed)
        rows_header.setDefaultSectionSize(2 * self.table.fontMetrics().lineSpacing() + 8)  # two lines per book
        rows_header.setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.sectionClicked.connect(self._header_clicked)
        for col, width in enumerate([34, 50, 70, 280, 200, 170, 60, 180, 260, 240]):
            self.table.setColumnWidth(col, width)
        self.table.selectionModel().currentRowChanged.connect(self._show_cover)

        self.cover = CoverPreview()
        top = QSplitter(Qt.Horizontal)
        top.addWidget(self.table)
        top.addWidget(self.cover)
        top.setStretchFactor(0, 1)
        top.setSizes([1100, 220])

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        log_handler.signal.message.connect(self._append_log)
        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(top)
        splitter.addWidget(self.log_view)
        splitter.setSizes([600, 150])

        self.progress = QProgressBar()
        self.status_label = _elastic(QLabel())
        self._ticker = QTimer(self, interval=1000)
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

    # --- settings and AI choice -------------------------------------------------------
    @staticmethod
    def _profile_label(p) -> str:
        return f"{p.name}  ({p.kind}: {p.model or 'no model'})"

    def _fill_profiles(self):
        self.text_box.blockSignals(True)
        self.text_box.clear()
        self.text_box.addItem("None", "")
        for p in self.settings.profiles:
            self.text_box.addItem(self._profile_label(p), p.name)
        self.text_box.setCurrentIndex(max(0, self.text_box.findData(self.settings.text_profile)))
        style_none_item(self.text_box)
        self.text_box.blockSignals(False)
        current = self.image_box.currentData() if self.image_box.count() else self.settings.image_profile
        self.image_box.blockSignals(True)
        self.image_box.clear()
        self.image_box.addItem("None (covers and scanned PDFs are not read)", "")
        for p in self.settings.profiles:
            if p.vision:
                self.image_box.addItem(self._profile_label(p), p.name)
        self.image_box.setCurrentIndex(max(0, self.image_box.findData(current)))
        style_none_item(self.image_box)
        self.image_box.blockSignals(False)
        self._ai_choice_changed()

    def _ai_choice_changed(self, *_):
        text_on, image_on = bool(self.text_box.currentData()), bool(self.image_box.currentData())
        mark_inactive(self.text_box, not text_on)
        mark_inactive(self.image_box, not (text_on and image_on))
        self.text_box.setToolTip("Reads the first pages of each book (when there is no Image AI).\n"
                                 + ("None: the review can't run." if not text_on else self.text_box.currentText()))
        self.image_box.setToolTip(
            "A model that reads text and images: with one, every book is sent to it with its cover.\n"
            + ("None: covers and scanned PDFs are not read." if not image_on
               else "Inactive: the Text AI is None." if not text_on else self.image_box.currentText()))

    def _library(self, key: str) -> str:
        return self.lib_boxes[key].currentText().strip()

    def _sync_settings(self):
        s = self.settings
        s.review_library, s.trash_library = self._library("library"), self._library("trash")
        s.text_profile = self.text_box.currentData() or ""
        s.image_profile = self.image_box.currentData() or ""
        s.review_fields = [f for f in FIELDS if f in self.fields_on]
        s.review_skip_reviewed = self.skip_reviewed.isChecked()
        s.save()

    def _open_settings(self):
        self._sync_settings()
        dialog = SettingsDialog(self.settings, self, dedup_options=False,
                                cache_path=config_dir() / REVIEW_CACHE_FILE,
                                cache_busy=self.worker is not None and self._operation == "scan")
        try:
            if dialog.exec():
                self._fill_profiles()
        finally:
            dialog.deleteLater()

    def _field_toggled(self, name: str, on: bool):
        if on:
            self.fields_on.add(name)
        else:
            self.fields_on.discard(name)
        self.model.refresh()

    def _browse(self, box: QComboBox, label: str):
        d = QFileDialog.getExistingDirectory(self, label, box.currentText())
        if d:
            box.setEditText(d)

    def _restore_geometry(self):
        saved = self.settings.review_window_geometry
        if not (saved and self.restoreGeometry(QByteArray.fromBase64(saved.encode("ascii")))):
            self.resize(1400, 850)
        screen = self.screen() or QApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            self.resize(min(self.width(), area.width() - 40), min(self.height(), area.height() - 60))
            self.move(min(max(self.x(), area.left()), area.left() + area.width() - self.width() - 20),
                      min(max(self.y(), area.top()), area.top() + area.height() - self.height() - 50))

    # --- state ---------------------------------------------------------------------------
    def _set_busy(self, busy: bool):
        self._busy = busy
        finishing = not busy and self.worker is not None
        scanning = self.worker is not None and self._operation == "scan"
        self.scan_btn.setEnabled(not busy and self.worker is None)
        self.scan_btn.setText(("Finishing…" if finishing else "Scanning…") if scanning else "1. Scan with AI")
        set_running(self.scan_btn, scanning)
        self.stop_btn.setText("Stopping…" if busy and self._stopping else "Stop")
        set_running(self.stop_btn, busy and self._stopping)
        self.stop_btn.setEnabled(busy and not self._stopping)
        self.table.setSortingEnabled(not busy)
        for box in self.lib_boxes.values():
            box.setEnabled(not busy)
        self.skip_reviewed.setEnabled(not busy)
        for btn in (self.check_btn, self.uncheck_btn):
            btn.setEnabled(self._can_edit())
        self._update_summary()

    def _can_edit(self) -> bool:
        """Ticks and actions: when idle and while scanning, never while executing."""
        return not self.model.locked and (not self._busy or self._operation == "scan")

    def _counts(self) -> tuple[int, int, int]:
        """(updates, trashes, books only tagged as reviewed) that Execute would do."""
        actions = review_actions(self.model.items, self.fields_on)
        updates = sum(1 for a in actions if a["op"] == "set")
        trashes = sum(1 for a in actions if a["op"] == "trash")
        tags = sum(len(a["src_ids"]) for a in actions if a["op"] == "tag")
        return updates, trashes, tags

    def _update_summary(self):
        items = self.model.items
        if self.result is None and not items:
            self.summary_label.setText("")
            self.checked_label.setText("")
            self.execute_btn.setText("2. Execute checked")
            self.execute_btn.setEnabled(False)
            return
        changed = sum(1 for i in items if i.changes)
        unread = sum(1 for i in items if i.found is None)
        skipped = self.result.skipped if self.result is not None else 0
        self.summary_label.setText(
            f"<b style='color:{ACTION_COLORS[ReviewAction.UPDATE]}'>{changed} with differences</b> · "
            f"<b style='color:#e65100'>{unread} not read</b> · {len(items)} books"
            + (f" · {skipped} already {REVIEWED_TAG}" if skipped else ""))
        updates, trashes, tags = self._counts()
        self.checked_label.setText(f"{updates} to update · {trashes} to trash · "
                                   f"{updates + tags} to tag {REVIEWED_TAG}")
        executing = self.worker is not None and self._operation == "execute"
        self.execute_btn.setText(f"Executing… ({self._done_count} of {self._exec_total})" if executing
                                 else f"2. Execute and mark reviewed ({updates + trashes + tags})")
        set_running(self.execute_btn, executing)
        self.execute_btn.setEnabled(not self._busy and self.worker is None and self.result is not None
                                    and bool(updates + trashes + tags))

    # --- table interaction ------------------------------------------------------------------
    def _visible_items(self) -> list[ReviewItem]:
        return [self.model.items[self.proxy.mapToSource(self.proxy.index(r, 0)).row()]
                for r in range(self.proxy.rowCount())]

    def _selected_items(self) -> list[ReviewItem]:
        return [self.model.items[self.proxy.mapToSource(i).row()]
                for i in self.table.selectionModel().selectedRows()]

    def _header_clicked(self, section: int):
        if section != ReviewModel.COL_CHECK or not self._can_edit():
            return
        items = [it for it in self.model.items if it.action is not ReviewAction.KEEP]
        if items:
            self.model.set_checked(items, not all(it.selected for it in items))

    def _toggle_selected_rows(self):
        if not self._can_edit():
            return
        items = [i for i in self._selected_items() if i.action is not ReviewAction.KEEP]
        if items:
            self.model.set_checked(items, not all(i.selected for i in items))

    def _show_cover(self, current: QModelIndex, _previous=None):
        if not current.isValid():
            self.cover.clear()
            return
        it = self.model.items[self.proxy.mapToSource(current).row()]
        self.cover.show_books([("", it.book)])

    def _context_menu(self, pos):
        index = self.table.indexAt(pos)
        if not index.isValid():
            return
        item = self.model.items[self.proxy.mapToSource(index).row()]
        menu = QMenu(self)
        picked = TextExtractor.pick_format(item.book.formats)
        act = menu.addAction(f"Open this book ({picked[0]})" if picked else "Open this book (file not found)")
        act.setEnabled(picked is not None)
        if picked:
            act.triggered.connect(lambda: open_file(picked[1]))
        cover = book_cover_file(item.book)
        act = menu.addAction("Open the cover" if cover else "Open the cover (none)")
        act.setEnabled(cover is not None)
        if cover:
            act.triggered.connect(lambda: open_file(str(cover)))
        if "title" in item.changes:
            act = menu.addAction("Copy proposed title")
            act.triggered.connect(lambda: QApplication.clipboard().setText(item.changes["title"]))
        items = self._selected_items()
        if items and self._can_edit():
            menu.addSeparator()
            for action, label in ((ReviewAction.UPDATE, "Update metadata"), (ReviewAction.KEEP, "Keep as it is"),
                                  (ReviewAction.TRASH, "Move to the trash library")):
                fits = [i for i in items if i.action is not action and (action is not ReviewAction.UPDATE
                                                                          or i.changes)]
                act = menu.addAction(f"{label} ({len(fits)})" if len(items) > 1 else label)
                act.setEnabled(bool(fits))
                act.triggered.connect(lambda _=False, a=action, f=fits: self._set_action(f, a))
            menu.addSeparator()
            for name in FIELDS:
                having = [i for i in items if name in i.changes]
                if not having:
                    continue
                excluded = all(name in i.excluded for i in having)
                label = (f"Change {FIELD_LABELS[name].lower()} again" if excluded
                         else f"Don't change {FIELD_LABELS[name].lower()}")
                act = menu.addAction(f"{label} ({len(having)})" if len(items) > 1 else label)
                act.triggered.connect(lambda _=False, n=name, h=having, e=excluded: self._exclude(h, n, not e))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _set_action(self, items: list[ReviewItem], action: ReviewAction):
        for it in items:
            it.set_action(action)
        self.model.refresh()

    def _exclude(self, items: list[ReviewItem], name: str, exclude: bool):
        for it in items:
            if exclude:
                it.excluded.add(name)
            else:
                it.excluded.discard(name)
        self.model.refresh()

    def _clear_filters(self):
        self.search.clear()
        self.action_filter.setCurrentIndex(0)
        for box in self.toggles.values():
            box.setChecked(False)

    def _append_log(self, text: str, ai: bool):
        style = "white-space:pre-wrap"
        if ai:
            dark = self.log_view.palette().base().color().lightness() < 128
            style += f"; color:{AI_LOG_COLOR['dark' if dark else 'light']}"
        self.log_view.appendHtml(f"<span style='{style}'>{html.escape(text)}</span>")

    # --- scan ------------------------------------------------------------------------------
    def _preflight_ok(self) -> bool:
        s = self.settings
        if not s.use_ai or s.profile() is None:
            QMessageBox.warning(self, "No AI", "The review reads every book with the AI: choose a Text AI "
                                               "(and, to read covers, an Image AI).")
            return False
        problems = []
        if s.image_ai() is None:
            problems.append("No Image AI: covers and scanned PDFs won't be read, only the text of the first pages.")
        checked = set()
        for role, p in (("Text AI", s.profile()), ("Image AI", s.image_ai())):
            if p is not None and p.kind == OLLAMA and p.name not in checked:
                checked.add(p.name)
                QApplication.setOverrideCursor(Qt.WaitCursor)
                try:
                    problem = _ollama_problem(p)
                finally:
                    QApplication.restoreOverrideCursor()
                if problem:
                    problems.append(f"{role} {p.name!r}: {problem}.")
        if not problems:
            return True
        return QMessageBox.question(
            self, "Before scanning", "\n\n".join(f"• {p}" for p in problems) + "\n\nScan anyway?"
        ) == QMessageBox.Yes

    def _scan(self):
        self._sync_settings()
        if not self._preflight_ok():
            return
        library, trash = self._library("library"), self._library("trash")
        self.result = ReviewResult(library, trash)
        self._eta.reset()
        self.model.locked = False
        self.model.reset([])
        worker = ScanWorker(replace(self.settings), library, trash)
        worker.progress.connect(self._on_progress)
        worker.items_ready.connect(self._on_items)
        worker.finished_ok.connect(self._scan_done)
        worker.failed.connect(self._worker_failed)
        worker.ai_down.connect(self._on_ai_down)
        self._start(worker, "scan")
        log.info("Review started")

    def _on_items(self, items: list):
        self.model.append_items(items)
        self._update_summary()

    def _on_progress(self, done: int, total: int, msg: str):
        self.progress.setMaximum(max(total, 1))
        self.progress.setValue(done)
        if msg.startswith("Reading ") and total:
            msg = f"Book {min(done + 1, total)} of {total}: {msg}"
            self._eta.update(done, total)
        self._status_msg, self._status_since = msg, time.monotonic()
        self._show_status()

    def _show_status(self):
        if not self._busy or not self._status_msg:
            return
        status = with_eta(self._status_msg, self._eta.text() if self._operation == "scan" else "")
        text = f"Stopping after the current book… · {status}" if self._stopping else status
        elapsed = int(time.monotonic() - self._status_since)
        self.status_label.setText(text + (f" · {elapsed} s" if elapsed >= 3 else ""))

    def _scan_done(self, result: ReviewResult):
        # Keep the rows already shown (and what the user did with them during the scan).
        shown = {id(i) for i in self.model.items}
        self.model.append_items([i for i in result.items if id(i) not in shown])
        result.items = list(self.model.items)
        self.result = result
        self.model.reset(result.items)
        self._finish()
        text = summary(result)
        self.status_label.setText(text)
        log.info("Review: %s", text)
        for reason in result.ai_down:
            log.warning("Review: %s", reason)

    def _on_ai_down(self, message: str, image: bool):
        worker = self.worker
        if not isinstance(worker, ScanWorker):
            return
        if self._close_pending:
            worker.answer(False)
            return
        what = "image AI" if image else "text AI"
        box = QMessageBox(QMessageBox.Warning, "AI not responding", message, parent=self)
        box.setInformativeText(f"Retry: try the same request again (e.g. after starting the server).\n"
                               f"Continue without the {what}: the rest of the books are read without it.\n"
                               "Stop: keep the books read so far.")
        retry = box.addButton("Retry", QMessageBox.AcceptRole)
        go_on = box.addButton(f"Continue without the {what}", QMessageBox.RejectRole)
        stop = box.addButton("Stop", QMessageBox.DestructiveRole)
        box.setDefaultButton(retry)
        box.setEscapeButton(go_on)
        box.exec()
        clicked = box.clickedButton()
        if clicked is stop:
            self._stop()
        worker.answer(clicked is retry)

    # --- execute ----------------------------------------------------------------------------
    def _execute(self):
        if self.result is None:
            return
        if calibre_is_running():
            QMessageBox.warning(self, "Calibre is running", "Close Calibre (and calibre-server) before executing: "
                                                            "two programs must not change a library at the same time.")
            return
        self._sync_settings()
        self.result.trash_library = self._library("trash")
        updates, trashes, tags = self._counts()
        if trashes and not self.result.trash_library:
            QMessageBox.warning(self, "No trash library", "Choose a trash library to move books to it.")
            return
        fields = ", ".join(FIELD_LABELS[f].lower() for f in FIELDS if f in self.fields_on) or "none"
        where = "permanently deleted" if self.settings.delete_permanently else "moved to Calibre's recycle bin"
        unread = sum(1 for i in self.model.items if i.found is None and not i.manual)
        if QMessageBox.question(
                self, "Execute and mark reviewed",
                f"{updates} books will have their metadata updated (fields: {fields}).\n"
                f"{trashes} books will be moved to the trash library; after a verified copy, each is "
                f"{where} in the reviewed library.\n"
                f"The {updates} updated books and {tags} more (unchecked or nothing to change) will be tagged "
                f"{REVIEWED_TAG}: the next scan skips them.\n"
                + (f"The {unread} books the AI could not read are not tagged: the next scan tries them again.\n"
                   if unread else "")
                + "\nContinue?") != QMessageBox.Yes:
            return
        self.model.locked = True
        self._done_count, self._exec_total = 0, updates + trashes + tags
        self.progress.setMaximum(max(self._exec_total, 1))
        self.progress.setValue(0)
        worker = ExecuteWorker(self.settings, self.result, self.fields_on)
        worker.result.connect(self._on_result)
        worker.finished_ok.connect(self._execution_done)
        worker.failed.connect(self._worker_failed)
        self._start(worker, "execute")
        log.info("Execution started")

    def _on_result(self, item: ReviewItem):
        self._done_count += 1
        self.progress.setValue(self._done_count)
        self.status_label.setText(f"{item.book.label()}: {item.status}")
        self.execute_btn.setText(f"Executing… ({self._done_count} of {self._exec_total})")
        self.model.item_changed(item)

    def _after_execution(self):
        """Trashed books have left the library, and tagged ones are done: take them off the list."""
        self.model.locked = False
        gone = [i for i in self.model.items if i.status.startswith("OK")
                and (i.action is ReviewAction.TRASH or is_reviewed(i.book))]
        self.model.remove(gone)
        if self.result is not None:
            self.result.items = list(self.model.items)
        self.model.refresh()

    def _execution_done(self, ok: int, failed: int, tagged: int):
        self._after_execution()
        self._finish()
        msg = (f"Execution finished: {ok} succeeded, {failed} failed, {tagged} more books tagged "
               f"{REVIEWED_TAG}. Tagged and trashed books were taken off the list.")
        log.info(msg)
        if not self._close_pending:
            (QMessageBox.warning if failed else QMessageBox.information)(self, "Done", msg)

    # --- worker plumbing ------------------------------------------------------------------------
    def _start(self, worker: QThread, operation: str):
        if self.worker is not None:
            log.warning("Previous operation still finishing; try again in a moment")
            return
        self.worker, self._operation = worker, operation
        self._status_msg, self._stopping = "", False
        worker.finished.connect(lambda w=worker: self._worker_finished(w))
        self._set_busy(True)
        worker.start()

    def _finish(self):
        self._set_busy(False)

    def _worker_finished(self, worker: QThread):
        if self.worker is worker:
            self.worker, self._operation = None, ""
        worker.deleteLater()
        self._set_busy(self._busy)
        if self._close_pending and self.worker is None:
            QTimer.singleShot(0, self.close)

    def _worker_failed(self, message: str):
        if isinstance(self.worker, ExecuteWorker):
            self._after_execution()
        elif self.result is not None and not self.model.items:
            self.result = None
        self._finish()
        log.error(message)
        if not self._close_pending:
            QMessageBox.warning(self, "Error", message)

    def _stop(self):
        if self.worker is not None:
            self.worker.cancel.set()
            self._stopping = True
            self._set_busy(self._busy)
            self.status_label.setText("Stopping after the current book…")

    def closeEvent(self, event):
        if self.worker is not None:
            event.ignore()
            if self._close_pending:
                return
            if QMessageBox.question(self, "Quit", "An operation is running. Stop after the current book "
                                                  "and quit?") != QMessageBox.Yes:
                return
            self._close_pending = True
            self._stop()
            return
        self.settings.review_window_geometry = bytes(self.saveGeometry().toBase64()).decode("ascii")
        self._sync_settings()
        event.accept()


def run_review_gui() -> int:
    handler = QtLogHandler()
    configure_logging(handler, "calibre_review.log")
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(ReviewWindow.TITLE)

    def _excepthook(exc_type, exc, tb):
        log.critical("Unhandled exception", exc_info=(exc_type, exc, tb))
        if QApplication.instance() is not None:
            QMessageBox.critical(None, "Unexpected error", f"{exc_type.__name__}: {exc}")

    sys.excepthook = _excepthook
    window = ReviewWindow(load_review_settings(), handler)
    window.show()
    return app.exec()
