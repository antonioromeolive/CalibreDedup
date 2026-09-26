from __future__ import annotations

import copy
import html
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QMessageBox,
    QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from ..ai import GOOD, INFO, NOTE, PROBLEM, AICache, AIError, connection_test, extra_params, make_provider
from ..calibre_env import find_calibre_dir
from ..config import (
    ANTHROPIC, AZURE, OLLAMA, OPENAI, ProviderProfile, Settings, config_dir, get_secret, set_secret,
)
from .style import GREEN, button_css, mark_inactive, set_running

KINDS = [(OLLAMA, "Ollama"), (AZURE, "Azure OpenAI"), (OPENAI, "OpenAI"), (ANTHROPIC, "Anthropic")]
ADVANCED_HINTS = {
    OLLAMA: "e.g. think = false (no thinking: much faster). Unknown names are ignored: check with Test connection.",
    AZURE: "e.g. reasoning_effort = none or low (reasoning models only). Check with Test connection.",
    OPENAI: "e.g. reasoning_effort = none or low (reasoning models only). Check with Test connection.",
    ANTHROPIC: "Usually none needed: thinking is off unless requested.",
}
REPORT_COLORS = {GOOD: "#2e7d32", PROBLEM: "#c62828", NOTE: "#e65100"}  # green, red, orange


def _report_html(lines: list[tuple[str, str]]) -> str:
    """The connection test report, each line coloured by its level."""
    out = []
    for level, text in lines:
        text = html.escape(text)
        color = REPORT_COLORS.get(level)
        out.append(f"<span style='color:{color}'>{text}</span>" if color and text else text)
    return "<br>".join(out)


ADVANCED_RULES = (
    "Only parameters your model accepts. Names are sent as written.\n"
    "A value is JSON when it parses as JSON (false, 1024, \"text\", {...}), else text (none, low).\n"
    "A dot puts a parameter inside an object: options.num_predict.\n"
    "Not accepted: fields of this form (model, temperature, context size)\n"
    "and fields the app sets itself (messages, stream, format, response_format…)."
)


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent=None, dedup_options: bool = True,
                 cache_path: Path | None = None, cache_busy: bool = False):
        """`dedup_options`: False hides the duplicate-finding options (calibre-review).
        `cache_path`: this program's AI cache (default: the duplicate remover's), which
        "Clear AI cache" empties; `cache_busy`: an analysis is running, so it can't."""
        super().__init__(parent)
        self.dedup_options = dedup_options
        self.cache_path = cache_path or config_dir() / "ai_cache.json"
        self.cache_busy = cache_busy
        self.setWindowTitle("Settings")
        self.resize(820, 640)
        self.settings = settings
        self.profiles = copy.deepcopy(settings.profiles)
        self.keys = {p.name: get_secret(p.name) for p in self.profiles}
        self._current: ProviderProfile | None = None
        self._loading = False
        self.renamed: dict[str, str] = {}  # old profile name -> new name

        tabs = QTabWidget()
        tabs.addTab(self._build_providers_tab(), "AI providers")
        tabs.addTab(self._build_analysis_tab(), "Analysis" if dedup_options else "Reading")
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addWidget(buttons)

        self._refresh_list()
        if self.profiles:
            # Open on the Text AI chosen in the main window (the first profile if AI is off).
            names = [p.name for p in self.profiles]
            self.list.setCurrentRow(names.index(settings.text_profile) if settings.text_profile in names else 0)

    # --- providers tab --------------------------------------------------------
    def _build_providers_tab(self) -> QWidget:
        w = QWidget()
        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._select_profile)
        add, dup, rem = QPushButton("Add"), QPushButton("Duplicate"), QPushButton("Remove")
        add.clicked.connect(self._add_profile)
        dup.clicked.connect(self._duplicate_profile)
        rem.clicked.connect(self._remove_profile)
        left = QVBoxLayout()
        left.addWidget(self.list)
        row = QHBoxLayout()
        for b in (add, dup, rem):
            row.addWidget(b)
        left.addLayout(row)

        self.f_name = QLineEdit()
        self.f_kind = QComboBox()
        for kind, label in KINDS:
            self.f_kind.addItem(label, kind)
        self.f_url = QLineEdit()
        self.f_model = QComboBox()
        self.f_model.setEditable(True)
        self.b_models = QPushButton("Refresh models")
        self.b_models.clicked.connect(self._refresh_models)
        model_row = QHBoxLayout()
        model_row.addWidget(self.f_model, 1)
        model_row.addWidget(self.b_models)
        self.f_key = QLineEdit()
        self.f_key.setEchoMode(QLineEdit.Password)
        self.f_version = QLineEdit()
        self.f_temp_on = QCheckBox("Send temperature")
        self.f_temp = QDoubleSpinBox()
        self.f_temp.setRange(0, 2)
        self.f_temp.setSingleStep(0.1)
        temp_row = QHBoxLayout()
        temp_row.addWidget(self.f_temp_on)
        temp_row.addWidget(self.f_temp, 1)
        self.f_timeout = QSpinBox()
        self.f_timeout.setRange(10, 3600)
        self.f_timeout.setSuffix(" s")
        self.f_ctx = QSpinBox()
        self.f_ctx.setRange(2048, 1_048_576)
        self.f_ctx.setSingleStep(2048)
        self.f_vision = QCheckBox("Supports images (can be the Image AI: covers, scanned PDFs)")
        self.test_btn = test = QPushButton("Test connection")
        test.setStyleSheet(button_css(*GREEN))
        test.setToolTip("Sends one real metadata request with all the settings of this profile,\n"
                        "advanced parameters included, on a made-up copyright page, and reports\n"
                        "refused parameters, empty or cut-off replies, slow answers and misread values.\n"
                        "With 'Supports images', also checks that the model sees a test image.")
        test.clicked.connect(self._test)

        self.f_params = QTableWidget(0, 2)
        self.f_params.setHorizontalHeaderLabels(["Parameter name", "Value"])
        self.f_params.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.f_params.verticalHeader().setVisible(False)
        self.f_params.setMinimumHeight(90)
        self.f_params.setToolTip(ADVANCED_RULES)
        p_add, p_rem = QPushButton("Add parameter"), QPushButton("Remove parameter")
        p_add.clicked.connect(self._add_param)
        p_rem.clicked.connect(self._remove_param)
        p_buttons = QHBoxLayout()
        p_buttons.addWidget(p_add)
        p_buttons.addWidget(p_rem)
        p_buttons.addStretch(1)
        self.params_hint = QLabel()
        self.params_hint.setWordWrap(True)
        self.params_hint.setStyleSheet("color: gray")
        self.params_hint.setToolTip(ADVANCED_RULES)
        advanced = QGroupBox("Advanced parameters (added to every request; hover for the rules)")
        a_layout = QVBoxLayout(advanced)
        a_layout.addWidget(self.f_params)
        a_layout.addLayout(p_buttons)
        a_layout.addWidget(self.params_hint)

        self.form = QFormLayout()
        self.form.addRow("Name", self.f_name)
        self.form.addRow("Type", self.f_kind)
        self.l_url = QLabel()
        self.form.addRow(self.l_url, self.f_url)
        self.l_model = QLabel()
        self.form.addRow(self.l_model, model_row)
        self.form.addRow("API key", self.f_key)
        self.form.addRow("API version", self.f_version)
        self.form.addRow("Temperature", temp_row)
        self.form.addRow("Timeout", self.f_timeout)
        self.form.addRow("Context size", self.f_ctx)
        self.form.addRow("", self.f_vision)
        self.form.addRow(advanced)
        self.form.addRow(test)  # full width
        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("color: gray")
        self.form.addRow(self.hint)

        self.f_kind.currentIndexChanged.connect(self._kind_changed)
        self.f_temp_on.toggled.connect(self.f_temp.setEnabled)

        right = QWidget()
        right.setLayout(self.form)
        layout = QHBoxLayout(w)
        layout.addLayout(left, 1)
        layout.addWidget(right, 2)
        return w

    def _refresh_list(self):
        self.list.blockSignals(True)
        self.list.clear()
        for p in self.profiles:
            self.list.addItem(p.name)
        self.list.blockSignals(False)

    def _select_profile(self, row: int):
        self._store_current()
        self._current = self.profiles[row] if 0 <= row < len(self.profiles) else None
        p = self._current
        if p is None:
            return
        self._loading = True
        self.f_name.setText(p.name)
        self.f_kind.setCurrentIndex(max(0, self.f_kind.findData(p.kind)))
        self.f_url.setText(p.base_url)
        self.f_model.clear()
        self.f_model.setEditText(p.model)
        self.f_key.setText(self.keys.get(p.name, ""))
        self.f_version.setText(p.api_version)
        self.f_temp_on.setChecked(p.temperature is not None)
        self.f_temp.setValue(p.temperature or 0.0)
        self.f_temp.setEnabled(p.temperature is not None)
        self.f_timeout.setValue(p.timeout)
        self.f_ctx.setValue(p.num_ctx)
        self.f_vision.setChecked(p.vision)
        self.f_params.setRowCount(0)
        for name, value in ((list(pair) + ["", ""])[:2] for pair in p.extra_params):
            self._add_param(name, value)
        self._loading = False
        self._kind_changed()

    def _store_current(self):
        p = self._current
        if p is None:
            return
        old_name = p.name
        p.name = self.f_name.text().strip() or old_name
        p.kind = self.f_kind.currentData()
        p.base_url = self.f_url.text().strip()
        p.model = self.f_model.currentText().strip()
        p.api_version = self.f_version.text().strip()
        p.temperature = self.f_temp.value() if self.f_temp_on.isChecked() else None
        p.timeout = self.f_timeout.value()
        p.num_ctx = self.f_ctx.value()
        p.vision = self.f_vision.isChecked()
        p.extra_params = []
        for row in range(self.f_params.rowCount()):
            name, value = (self.f_params.item(row, col).text().strip() if self.f_params.item(row, col) else ""
                           for col in (0, 1))
            if name or value:
                p.extra_params.append([name, value])
        if old_name != p.name:
            self.keys[old_name] = ""  # clears the key stored under the old name
            self.renamed[old_name] = p.name
        self.keys[p.name] = self.f_key.text()
        row = self.profiles.index(p)
        if self.list.item(row) is not None:
            self.list.item(row).setText(p.name)

    def _kind_changed(self):
        azure = self.f_kind.currentData() == AZURE
        local = self.f_kind.currentData() == OLLAMA
        fixed_api = self.f_kind.currentData() in (OPENAI, ANTHROPIC)
        self.l_url.setText("Endpoint" if azure or fixed_api else "Server URL")
        self.l_model.setText("Deployment" if azure else "Model")
        self.f_url.setEnabled(not fixed_api)
        self.f_key.setEnabled(not local)
        self.f_version.setEnabled(azure)
        self.f_ctx.setEnabled(local)
        self.b_models.setEnabled(local)
        if not self._loading and self._current is not None:
            if azure and "11434" in self.f_url.text():
                self.f_url.setText("https://<resource>.openai.azure.com")
            elif local and "azure" in self.f_url.text():
                self.f_url.setText("http://localhost:11434")
            elif self.f_kind.currentData() == OPENAI:
                self.f_url.setText("https://api.openai.com")
            elif self.f_kind.currentData() == ANTHROPIC:
                self.f_url.setText("https://api.anthropic.com")
        self.params_hint.setText(ADVANCED_HINTS.get(self.f_kind.currentData(), ""))
        self.hint.setText(
            "Azure: enter the resource endpoint and the deployment name. API version is e.g. "
            "2024-10-21, or 'v1' for the new v1 API (the deployment field then holds the model); "
            "an endpoint ending in /openai/v1, as the portal shows it, also means v1. "
            "Uncheck 'Send temperature' for reasoning models (o-series, gpt-5)."
            if azure else ("Ollama: the server URL (default http://localhost:11434). 'Refresh models' lists installed "
            "models. A larger context size lets the model read more text but uses more memory."
            if local else ("OpenAI: enter a model name and API key. The endpoint is fixed to api.openai.com."
            if self.f_kind.currentData() == OPENAI else
            "Anthropic: enter a model name and API key. The endpoint is fixed to api.anthropic.com."))
        )

    def _profile_from_form(self) -> ProviderProfile:
        self._store_current()
        return self._current

    def _refresh_models(self):
        p = self._profile_from_form()
        if p is None:
            return
        try:
            models = make_provider(p).list_models()
        except AIError as e:
            QMessageBox.warning(self, "Models", str(e))
            return
        current = self.f_model.currentText()
        self.f_model.clear()
        self.f_model.addItems(models)
        self.f_model.setEditText(current or (models[0] if models else ""))

    def _test(self):
        p = self._profile_from_form()
        if p is None:
            return
        set_secret(p.name, self.keys.get(p.name, ""))
        self.setCursor(Qt.WaitCursor)
        self.test_btn.setText("Testing…")
        self.test_btn.setEnabled(False)
        set_running(self.test_btn, True)
        QApplication.processEvents()  # show it before the (blocking) request starts
        ok, report = False, []
        try:
            provider = make_provider(p)
            ok, report = connection_test(provider)
            if ok and p.vision:
                report.append((INFO, ""))
                try:
                    seen, reply = provider.test_images()
                except Exception as e:  # keep the text report, show the actual error
                    seen, reply = False, ""
                    report.append((PROBLEM, f"✗ Images: FAILED: {e}"))
                if seen:
                    report.append((GOOD, "✓ Images: the model sees them."))
                elif reply or not report[-1][1].startswith("✗ Images"):
                    report += [(PROBLEM, "✗ Images: the model did not see the test image (a red square); "
                                         "untick 'Supports images' for this profile."),
                               (INFO, f"The model replied: {reply[:500] or '(nothing)'}")]
                ok = ok and seen
        except Exception as e:  # e.g. an unknown provider type: still show the actual error
            ok = False
            report.append((PROBLEM, f"FAILED: {e}" if isinstance(e, AIError)
                           else f"FAILED (unexpected {type(e).__name__}): {e}"))
        finally:
            self.unsetCursor()
            self.test_btn.setText("Test connection")
            self.test_btn.setEnabled(True)
            set_running(self.test_btn, False)
        box = QMessageBox(QMessageBox.Information if ok else QMessageBox.Warning,
                          f"Test connection: {p.name}", _report_html(report), parent=self)
        box.setTextFormat(Qt.RichText)
        box.exec()
        box.deleteLater()  # don't leave closed windows alive (see MainWindow._open_settings)

    def _add_param(self, name: str = "", value: str = ""):
        row = self.f_params.rowCount()
        self.f_params.insertRow(row)
        self.f_params.setItem(row, 0, QTableWidgetItem(name if isinstance(name, str) else ""))
        self.f_params.setItem(row, 1, QTableWidgetItem(value))
        if not self._loading:
            self.f_params.setCurrentCell(row, 0)
            self.f_params.editItem(self.f_params.item(row, 0))

    def _remove_param(self):
        rows = sorted({i.row() for i in self.f_params.selectedIndexes()}, reverse=True)
        for row in rows or ([self.f_params.currentRow()] if self.f_params.currentRow() >= 0 else []):
            self.f_params.removeRow(row)

    def _add_profile(self):
        self._store_current()
        name = self._unique_name("New profile")
        self.profiles.append(ProviderProfile(name=name))
        self._refresh_list()
        self.list.setCurrentRow(len(self.profiles) - 1)

    def _duplicate_profile(self):
        self._store_current()
        if self._current is None:
            return
        p = copy.deepcopy(self._current)
        name = self._unique_name(p.name)
        self.keys[name] = self.keys.get(p.name, "")
        p.name = name
        self.profiles.append(p)
        self._refresh_list()
        self.list.setCurrentRow(len(self.profiles) - 1)

    def _remove_profile(self):
        row = self.list.currentRow()
        if row < 0:
            return
        self._current = None
        removed = self.profiles.pop(row)
        self.keys[removed.name] = ""  # clears the stored key on save
        self._refresh_list()
        if self.profiles:
            self.list.setCurrentRow(min(row, len(self.profiles) - 1))

    def _unique_name(self, base: str) -> str:
        names = {p.name for p in self.profiles}
        name, n = base, 2
        while name in names:
            name, n = f"{base} {n}", n + 1
        return name

    # --- analysis tab ---------------------------------------------------------
    def _build_analysis_tab(self) -> QWidget:
        s = self.settings
        w = QWidget()
        form = QFormLayout(w)
        self.a_pdf_pages = QSpinBox()
        self.a_pdf_pages.setRange(1, 50)
        self.a_pdf_pages.setValue(s.pdf_pages)
        self.a_chars = QSpinBox()
        self.a_chars.setRange(1000, 200_000)
        self.a_chars.setSingleStep(1000)
        self.a_chars.setValue(s.text_chars)
        self.a_subtitle = QCheckBox("Ignore subtitles when comparing titles")
        self.a_subtitle.setChecked(s.ignore_subtitle)
        self.a_similar = QCheckBox("Similar author matching (ignore initials; one shared author is enough)")
        self.a_similar.setChecked(s.similar_matching)
        self.a_cover = QCheckBox("Compare covers when metadata can't decide (needs an Image AI)")
        self.a_cover.setChecked(s.cover_check)
        self.a_always_cover = QCheckBox("Always compare covers: the same cover means the same book, even when "
                                        "year, publisher or edition differ")
        self.a_always_cover.setToolTip(
            "Covers are also compared when the metadata says the books are different (e.g. only the\n"
            "years differ, 2011 vs 1986). The same cover makes a duplicate, proposed as Trash only (no\n"
            "formats are copied into the other book). Because Calibre's cover can be a downloaded\n"
            "picture, the covers inside the book files must match too. Needs an Image AI; costs more\n"
            "AI calls. List these books with the 'Decided by cover' filter.")
        self.a_always_cover.setChecked(s.always_cover)
        self.a_years = QCheckBox("Re-check year differences by reading both books (AI)")
        self.a_years.setToolTip("Calibre's publication date is often the original publication, not this "
                                "edition's.\nWhen only the years differ, the AI reads the year printed in "
                                "both books and that decides.")
        self.a_years.setChecked(s.recheck_years)
        # An option that is on but won't take effect says so, and why; its value is kept.
        if not s.use_ai:
            cover_missing = years_missing = "Text AI is None (main window)"
        else:
            cover_missing = "" if s.image_ai() else "no Image AI selected (main window)"
            years_missing = ""
        self.a_cover_note = self._inactive_note(self.a_cover, cover_missing)
        self.a_years_note = self._inactive_note(self.a_years, years_missing)
        self.a_always_cover_note = self._inactive_note(self.a_always_cover, cover_missing)
        self.a_author_variants = QCheckBox("Same title, author written differently (e.g. \"Wilson Tucke\" / "
                                           "\"Wilson Tucker\")")
        self.a_author_variants.setToolTip(
            "When no book has the same title and authors: books with the same title whose authors are\n"
            "the same person written differently. One letter apart in a name is enough; otherwise the\n"
            "Text AI is asked (typos, transliterations, pen names; answers are cached). Those books are\n"
            "then compared as usual (ISBN, edition and publisher, EPUB text, cover).")
        self.a_author_variants.setChecked(s.author_variants)
        self.a_similar_titles = QCheckBox("Match similar titles by the same author (needs proof: ISBN, same text "
                                          "or same cover)")
        self.a_similar_titles.setToolTip(
            "When no book has the same title: also books by the same author whose title contains the\n"
            "other's, with only numbers, the author, the series, the publisher or a date around it,\n"
            "e.g. \"1 Abissi d'acciaio\" or \"(Urania - 0411- Supernormale - J. Hunter Holly)\" and \"Supernormale\".\n"
            "Titles like these are weaker than the same title: it's a duplicate only with the same ISBN,\n"
            "identical EPUB text or the same cover. A different cover or edition rules the book out;\n"
            "otherwise the book is left in the source for you to check.")
        self.a_similar_titles.setChecked(s.similar_titles)
        self.a_series = QCheckBox("Same author + same series + same number = same book, even if titles differ")
        self.a_series.setToolTip(
            "Only when both books have the same series name and number, and share an author.\n"
            "Number 1 is ignored: it is Calibre's default when no number was set.\n"
            "Turn it on only for libraries whose series numbers are reliable (e.g. a collection\n"
            "like Urania, numbered by issue): a wrong number would send a different book to trash.\n"
            "While it is on, source books without a series and a number are left untouched:\n"
            "run the analysis again with it off for those.")
        self.a_series.setChecked(s.same_series)
        self.a_update = QCheckBox("Write AI-found title/authors/publisher/ISBN to moved books (only empty fields)")
        self.a_update.setChecked(s.update_metadata)
        self.a_permanent = QCheckBox("Delete permanently from source (else: Calibre's recycle bin)")
        self.a_permanent.setChecked(s.delete_permanently)
        self.a_calibre = QLineEdit(s.calibre_dir or str(find_calibre_dir() or ""))
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_calibre)
        crow = QHBoxLayout()
        crow.addWidget(self.a_calibre, 1)
        crow.addWidget(browse)

        form.addRow("PDF pages to read (start/end)", self.a_pdf_pages)
        form.addRow("Characters to read (other formats)", self.a_chars)
        form.addRow(self.a_subtitle)
        form.addRow(self.a_similar)
        form.addRow(self.a_cover)
        form.addRow(self.a_cover_note)
        form.addRow(self.a_always_cover)
        form.addRow(self.a_always_cover_note)
        form.addRow(self.a_years)
        form.addRow(self.a_years_note)
        form.addRow(self.a_author_variants)
        form.addRow(self.a_similar_titles)
        form.addRow(self.a_series)
        form.addRow(self.a_update)
        form.addRow(self.a_permanent)
        form.addRow("Calibre program folder", crow)
        self._reset_warnings = False
        reset = QPushButton(f"Show dismissed warnings again ({len(s.dismissed_warnings)})")
        reset.setToolTip("Warnings before an analysis that you chose not to see again\n"
                         "(e.g. cover check on with no Image AI).")
        reset.setEnabled(bool(s.dismissed_warnings))

        def reset_clicked():
            self._reset_warnings = True
            reset.setText("Dismissed warnings will be shown again")
            reset.setEnabled(False)
        reset.clicked.connect(reset_clicked)
        form.addRow(reset)
        form.addRow(self._clear_cache_row())
        if not self.dedup_options:
            # Hidden, not left out: accept() still reads (and keeps) their values.
            for widget in (self.a_subtitle, self.a_similar, self.a_cover, self.a_cover_note, self.a_always_cover,
                           self.a_always_cover_note, self.a_years, self.a_years_note, self.a_author_variants,
                           self.a_similar_titles, self.a_series, self.a_update, reset):
                form.setRowVisible(widget, False)
            form.labelForField(self.a_pdf_pages).setText("PDF pages to read (from the start)")
            form.labelForField(self.a_chars).setText("Characters to read (other formats, from the start)")
            self.a_permanent.setText("Delete permanently from the reviewed library when trashing "
                                     "(else: Calibre's recycle bin)")
        return w

    def _clear_cache_row(self) -> QHBoxLayout:
        """A small button at the bottom right: forget this program's AI answers. Done at
        once (not on OK), and not while an analysis runs (it would write them back)."""
        path = self.cache_path
        answers, size = AICache.size(path)
        button = QPushButton(f"Clear AI cache… ({answers:,} answers, {size / 1_000_000:.1f} MB)")
        font = button.font()
        font.setPointSizeF(font.pointSizeF() * 0.85)
        button.setFont(font)
        button.setFlat(True)
        button.setEnabled(bool(answers) and not self.cache_busy)
        button.setToolTip("Unavailable while an analysis is running." if self.cache_busy else
                          f"{path}\nForget every answer the AI gave to this program: the next analysis asks again.")

        def clicked():
            if QMessageBox.question(
                    self, "Clear AI cache",
                    f"Forget all {answers:,} AI answers of this program?\n\n"
                    "The next analysis asks the AI again for every book it needs, which is slow and, with a "
                    "cloud AI, costs requests. The other program's cache is not touched.\n\n"
                    "This happens now, even if you then press Cancel.") != QMessageBox.Yes:
                return
            AICache.clear(path)
            button.setText("AI cache cleared")
            button.setEnabled(False)
        button.clicked.connect(clicked)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(button)
        return row

    @staticmethod
    def _inactive_note(box: QCheckBox, missing: str) -> QLabel:
        """"inactive: <why>" under an option while it is ticked but can't run."""
        note = QLabel(f"      inactive: {missing}")
        mark_inactive(note, True)

        def update(*_):
            note.setVisible(bool(missing) and box.isChecked())
        box.toggled.connect(update)
        update()
        return note

    def _browse_calibre(self):
        d = QFileDialog.getExistingDirectory(self, "Calibre program folder", self.a_calibre.text())
        if d:
            self.a_calibre.setText(d)

    def _follow_rename(self, name: str) -> str:
        seen = set()
        while name in self.renamed and name not in seen:
            seen.add(name)
            name = self.renamed[name]
        return name

    # --- save -----------------------------------------------------------------
    def accept(self):
        self._store_current()
        names = [p.name for p in self.profiles]
        if len(set(names)) != len(names):
            QMessageBox.warning(self, "Settings", "Profile names must be unique.")
            return
        for row, p in enumerate(self.profiles):
            _, problems = extra_params(p)
            if problems:
                self.list.setCurrentRow(row)
                QMessageBox.warning(self, "Settings", f"Advanced parameters of {p.name!r}:\n\n"
                                    + "\n".join(f"• {x}" for x in problems))
                return
        s = self.settings
        s.profiles = self.profiles
        s.text_profile = self._follow_rename(s.text_profile)
        if s.text_profile and s.text_profile not in names:
            s.text_profile = names[0] if names else ""
        s.image_profile = self._follow_rename(s.image_profile)
        if s.image_profile not in names:
            s.image_profile = ""
        s.pdf_pages = self.a_pdf_pages.value()
        s.text_chars = self.a_chars.value()
        s.ignore_subtitle = self.a_subtitle.isChecked()
        s.similar_matching = self.a_similar.isChecked()
        s.cover_check = self.a_cover.isChecked()
        s.always_cover = self.a_always_cover.isChecked()
        s.recheck_years = self.a_years.isChecked()
        s.same_series = self.a_series.isChecked()
        s.similar_titles = self.a_similar_titles.isChecked()
        s.author_variants = self.a_author_variants.isChecked()
        s.update_metadata = self.a_update.isChecked()
        s.delete_permanently = self.a_permanent.isChecked()
        s.calibre_dir = self.a_calibre.text().strip()
        if self._reset_warnings:
            s.dismissed_warnings = []
        for name, key in self.keys.items():
            set_secret(name, key if name in names else "")
        s.save()
        super().accept()
