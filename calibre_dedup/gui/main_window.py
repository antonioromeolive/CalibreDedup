from __future__ import annotations

import logging
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, QSortFilterProxyModel, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QGridLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QSplitter, QTableView, QVBoxLayout, QWidget,
)

from ..calibre_env import calibre_is_running, known_libraries
from ..config import Settings, config_dir
from ..executor import execute_plan
from ..models import Action, Plan, PlanItem
from ..normalize import strip_accents
from ..planner import Cancelled, build_plan
from ..report import write_csv
from ..selection import ACTION_LABELS, SelectionStore, actionable, blocked, can_override, override, revert
from ..session import make_resolver, require_calibre_dir
from .settings_dialog import SettingsDialog

log = logging.getLogger("calibre_dedup")

ACTION_COLORS = {Action.MOVE: "#2e7d32", Action.TRASH: "#c62828", Action.LEAVE: "#8d6e00"}


# --- logging into the GUI -----------------------------------------------------
class _LogSignal(QObject):
    message = Signal(str)


class QtLogHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.signal = _LogSignal()
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))

    def emit(self, record):
        self.signal.message.emit(self.format(record))


# --- table model ----------------------------------------------------------------
SORT_ROLE = Qt.UserRole


def _is_checked(value) -> bool:
    if isinstance(value, int):
        return value == Qt.Checked.value
    return value == Qt.Checked


class PlanModel(QAbstractTableModel):
    HEADERS = ["", "ID", "Title", "Authors", "Action", "Reason", "Match in target", "Add formats", "AI", "Result"]
    COL_CHECK, COL_ID, COL_ACTION, COL_REASON, COL_RESULT = 0, 1, 4, 5, 9
    selection_changed = Signal()

    def __init__(self):
        super().__init__()
        self.plan: Plan | None = None
        self.items: list[PlanItem] = []
        self.blocked: dict[int, str] = {}
        self.locked = False  # no edits while running or after execution
        self._rows: dict[int, int] = {}
        self.italic_font = QFont()  # set from the view, marks manual overrides

    def set_plan(self, plan: Plan | None):
        self.beginResetModel()
        self.plan = plan
        self.items = list(plan.items) if plan else []
        self._rows = {id(it): row for row, it in enumerate(self.items)}
        self.blocked = blocked(plan) if plan else {}
        self.locked = False
        self.endResetModel()

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
            ACTION_LABELS[it.action] + (" (manual)" if it.manual else ""),
            f"{why_blocked} | {it.reason}" if why_blocked else it.reason,
            (it.match.label() + (" (moving now)" if it.match_planned else "")) if it.match else "",
            ", ".join(it.add_formats),
            ", ".join(sorted(ident.ai_fields)) or ("nothing found" if it.ai_used else ""),
            it.status,
        ]

    def data(self, index, role=Qt.DisplayRole):
        it = self.items[index.row()]
        col = index.column()
        if role in (Qt.DisplayRole, Qt.ToolTipRole):
            return self.values(it)[col]
        if role == Qt.CheckStateRole and col == self.COL_CHECK and it.action is not Action.LEAVE:
            return Qt.Checked if it.selected else Qt.Unchecked
        if role == SORT_ROLE:
            if col == self.COL_CHECK:
                return (2 if it.selected else 1) if it.action is not Action.LEAVE else 0
            v = self.values(it)[col]
            return v if isinstance(v, int) else str(v).casefold()
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
        self.setSortRole(SORT_ROLE)
        self.terms: list[str] = []
        self.action = ""
        self.ai_only = self.formats_only = self.checked_only = self.failed_only = False

    def update(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
        self.invalidateRowsFilter()

    def filterAcceptsRow(self, row, parent):
        model: PlanModel = self.sourceModel()
        it = model.items[row]
        if self.action and it.action.value != self.action:
            return False
        if self.ai_only and not it.ai_used:
            return False
        if self.formats_only and not it.add_formats:
            return False
        if self.checked_only and not (it.selected and it.action is not Action.LEAVE):
            return False
        if self.failed_only and not it.status.startswith("FAILED"):
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
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(self, settings: Settings, source: str, target: str, trash: str):
        super().__init__()
        self.settings, self.source, self.target, self.trash = settings, source, target, trash
        self.cancel = threading.Event()

    def run(self):
        resolver = None
        try:
            resolver = make_resolver(self.settings)
            plan = build_plan(self.source, self.target, self.trash, resolver,
                              self.settings.ignore_subtitle, self.progress.emit, self.cancel)
            self.finished_ok.emit(plan)
        except Cancelled:
            self.failed.emit("Analysis stopped.")
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
        self.setWindowTitle("Calibre Duplicate Remover")
        self.resize(1300, 800)

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
            box = QComboBox()
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
        self.use_ai = QCheckBox("Use AI for missing metadata")
        self.use_ai.setChecked(settings.use_ai)
        self.profile_box = QComboBox()
        self._fill_profiles()
        settings_btn = QPushButton("Settings…")
        settings_btn.clicked.connect(self._open_settings)
        ai_row = QHBoxLayout()
        ai_row.addWidget(self.use_ai)
        ai_row.addWidget(QLabel("AI profile:"))
        ai_row.addWidget(self.profile_box, 1)
        ai_row.addWidget(settings_btn)

        # actions
        self.analyze_btn = QPushButton("1. Analyze (dry run)")
        self.execute_btn = QPushButton("2. Execute checked")
        self.stop_btn = QPushButton("Stop")
        self.export_btn = QPushButton("Export CSV…")
        self.analyze_btn.clicked.connect(self._analyze)
        self.execute_btn.clicked.connect(self._execute)
        self.stop_btn.clicked.connect(self._stop)
        self.export_btn.clicked.connect(self._export)
        self.summary = QLabel()
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
        self.action_filter = QComboBox()
        self.action_filter.addItem("All actions", "")
        for a in Action:
            self.action_filter.addItem(ACTION_LABELS[a], a.value)
        self.action_filter.currentIndexChanged.connect(
            lambda: self.proxy.update(action=self.action_filter.currentData()))
        self.toggles = {}
        for attr, label in [("ai_only", "AI used"), ("formats_only", "Adds formats"),
                            ("checked_only", "Only checked"), ("failed_only", "Only failed")]:
            box = QCheckBox(label)
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
        self.check_btn = QPushButton("Check visible")
        self.uncheck_btn = QPushButton("Uncheck visible")
        self.invert_btn = QPushButton("Invert visible")
        self.check_btn.clicked.connect(lambda: self.model.set_checked(self._visible_items(), True))
        self.uncheck_btn.clicked.connect(lambda: self.model.set_checked(self._visible_items(), False))
        self.invert_btn.clicked.connect(lambda: self.model.set_checked(self._visible_items(), None))
        self.checked_label = QLabel()
        bulk_row = QHBoxLayout()
        for w in (self.check_btn, self.uncheck_btn, self.invert_btn):
            bulk_row.addWidget(w)
        bulk_row.addWidget(QLabel("  Space toggles selected rows · right-click to change the action"))
        bulk_row.addStretch(1)
        bulk_row.addWidget(self.checked_label)

        # table + log
        self.store = SelectionStore()
        self.model.selection_changed.connect(self._selection_changed)
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
        for col, width in enumerate([34, 50, 260, 180, 150, 420, 260, 80, 80, 300]):
            self.table.setColumnWidth(col, width)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        log_handler.signal.message.connect(self.log_view.appendPlainText)
        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self.table)
        splitter.addWidget(self.log_view)
        splitter.setSizes([600, 150])

        self.progress = QProgressBar()
        self.status_label = QLabel()

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
    def _fill_profiles(self):
        self.profile_box.clear()
        for p in self.settings.profiles:
            model = p.model or "no model"
            self.profile_box.addItem(f"{p.name}  ({p.kind}: {model})", p.name)
        self.profile_box.setCurrentIndex(max(0, self.profile_box.findData(self.settings.active_profile)))

    def _browse(self, box: QComboBox, label: str):
        d = QFileDialog.getExistingDirectory(self, label, box.currentText())
        if d:
            box.setEditText(d)

    def _library(self, key: str) -> str:
        return self.lib_boxes[key].currentText().strip()

    def _sync_settings(self):
        s = self.settings
        s.source_library, s.target_library, s.trash_library = (self._library(k) for k in ("source", "target", "trash"))
        s.use_ai = self.use_ai.isChecked()
        if self.profile_box.currentData():
            s.active_profile = self.profile_box.currentData()
        s.save()

    def _set_busy(self, busy: bool):
        self._busy = busy
        self.analyze_btn.setEnabled(not busy)
        self.export_btn.setEnabled(not busy and self.plan is not None)
        self.stop_btn.setEnabled(busy)
        for box in self.lib_boxes.values():
            box.setEnabled(not busy)
        editable = not busy and self.plan is not None and not self.model.locked
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
            return
        self.summary.setText(
            f"<b style='color:{ACTION_COLORS[Action.MOVE]}'>{p.count(Action.MOVE)} move</b> · "
            f"<b style='color:{ACTION_COLORS[Action.TRASH]}'>{p.count(Action.TRASH)} trash</b> · "
            f"<b style='color:{ACTION_COLORS[Action.LEAVE]}'>{p.count(Action.LEAVE)} leave</b>")
        todo = actionable(p)
        possible = sum(1 for i in p.items if i.action is not Action.LEAVE)
        n_blocked = len(self.model.blocked)
        text = f"{len(todo)} of {possible} checked"
        if n_blocked:
            text += f" · <b style='color:#e65100'>{n_blocked} blocked</b>"
        self.checked_label.setText(text)
        self.execute_btn.setText(f"2. Execute checked ({len(todo)})")
        self.execute_btn.setEnabled(not self._busy and not self.model.locked and bool(todo))

    # --- selection ------------------------------------------------------------------
    def _visible_items(self) -> list[PlanItem]:
        return [self.model.items[self.proxy.mapToSource(self.proxy.index(r, 0)).row()]
                for r in range(self.proxy.rowCount())]

    def _selected_items(self) -> list[PlanItem]:
        rows = self.table.selectionModel().selectedRows()
        return [self.model.items[self.proxy.mapToSource(i).row()] for i in rows]

    def _selection_changed(self):
        self._update_summary()
        if self.plan is not None and not self.model.locked:
            self.store.save(self.plan)

    def _toggle_selected_rows(self):
        items = [i for i in self._selected_items() if i.action is not Action.LEAVE]
        if items:
            # Mixed selection: check all. All checked: uncheck all.
            self.model.set_checked(items, not all(i.selected for i in items))

    def _context_menu(self, pos):
        items = self._selected_items()
        if not items or self.model.locked or self._busy:
            return
        menu = QMenu(self)
        choices = [
            (Action.MOVE, "Force move to target"),
            (Action.TRASH, "Force trash (duplicate of the match)"),
            (Action.LEAVE, "Keep in source"),
        ]
        for action, label in choices:
            n = sum(1 for i in items if i.action is not action and can_override(i, action))
            act = menu.addAction(f"{label} ({n})" if len(items) > 1 else label)
            act.setEnabled(n > 0)
            act.triggered.connect(lambda _=False, a=action: self._override(items, a))
        menu.addSeparator()
        n = sum(1 for i in items if i.manual)
        act = menu.addAction(f"Revert to analysis decision ({n})" if len(items) > 1 else "Revert to analysis decision")
        act.setEnabled(n > 0)
        act.triggered.connect(lambda: self._revert(items))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _override(self, items: list[PlanItem], action: Action):
        skipped = 0
        for it in items:
            if it.action is action:
                continue
            if can_override(it, action):
                override(it, action)
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

    def _open_settings(self):
        self._sync_settings()
        if SettingsDialog(self.settings, self).exec():
            self.use_ai.setChecked(self.settings.use_ai)
            self._fill_profiles()

    # --- analyze --------------------------------------------------------------------
    def _analyze(self):
        self._sync_settings()
        self.plan = None
        self.model.set_plan(None)
        self._update_summary()
        worker = AnalyzeWorker(self.settings, *(self._library(k) for k in ("source", "target", "trash")))
        worker.progress.connect(self._on_progress)
        worker.finished_ok.connect(self._analysis_done)
        worker.failed.connect(self._worker_failed)
        self._start(worker)
        log.info("Analysis started")

    def _on_progress(self, done: int, total: int, msg: str):
        self.progress.setMaximum(max(total, 1))
        self.progress.setValue(done)
        self.status_label.setText(msg)

    def _analysis_done(self, plan: Plan):
        restored = self.store.apply(plan)
        self.plan = plan
        self.model.set_plan(plan)
        self._finish()
        log.info("Analysis complete: %d move, %d trash, %d leave",
                 plan.count(Action.MOVE), plan.count(Action.TRASH), plan.count(Action.LEAVE))
        if restored:
            log.info("Restored %d manual choice(s) from a previous analysis of these libraries", restored)

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
        self._done_count = 0
        self.progress.setMaximum(total)
        self.progress.setValue(0)
        worker.result.connect(self._on_result)
        worker.finished_ok.connect(self._execution_done)
        worker.failed.connect(self._worker_failed)
        self._start(worker)
        log.info("Execution started")

    def _on_result(self, item: PlanItem):
        self._done_count += 1
        self.progress.setValue(self._done_count)
        self.status_label.setText(f"{item.source.label()}: {item.status}")
        self.model.item_changed(item)

    def _execution_done(self, ok: int, failed: int):
        self._finish()  # the model stays locked: the plan is spent; analyze again
        self.model.refresh()
        msg = f"Execution finished: {ok} succeeded, {failed} failed."
        log.info(msg)
        (QMessageBox.warning if failed else QMessageBox.information)(self, "Done", msg)

    # --- worker plumbing ----------------------------------------------------------------
    def _start(self, worker: QThread):
        self.worker = worker
        self._set_busy(True)
        worker.start()

    def _finish(self):
        self.worker = None
        self._set_busy(False)
        self.status_label.setText("")

    def _worker_failed(self, message: str):
        if isinstance(self.worker, ExecuteWorker) and not self._done_count:
            self.model.locked = False  # nothing was executed; the plan is still usable
        self._finish()
        log.error(message)
        QMessageBox.warning(self, "Error", message)

    def _stop(self):
        if self.worker is not None:
            self.worker.cancel.set()
            self.status_label.setText("Stopping after the current book…")

    def _export(self):
        if not self.plan:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export plan", str(Path.home() / "dedup_plan.csv"), "CSV (*.csv)")
        if path:
            write_csv(self.plan, path)
            log.info("Plan exported to %s", path)

    def closeEvent(self, event):
        if self.worker is not None and self.worker.isRunning():
            if QMessageBox.question(self, "Quit", "An operation is running. Stop it and quit?") != QMessageBox.Yes:
                event.ignore()
                return
            self.worker.cancel.set()
            self.worker.wait(60_000)
        self._sync_settings()
        event.accept()


def run_gui() -> int:
    handler = QtLogHandler()
    file_handler = RotatingFileHandler(config_dir() / "calibre_dedup.log", maxBytes=5_000_000,
                                       backupCount=3, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    root.addHandler(file_handler)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Calibre Duplicate Remover")
    window = MainWindow(Settings.load(), handler)
    window.show()
    return app.exec()
