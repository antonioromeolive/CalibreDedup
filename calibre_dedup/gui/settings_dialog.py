from __future__ import annotations

import copy

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QMessageBox,
    QPushButton, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from ..ai import AIError, make_provider
from ..calibre_env import find_calibre_dir
from ..config import ANTHROPIC, AZURE, OLLAMA, OPENAI, ProviderProfile, Settings, get_secret, set_secret

KINDS = [(OLLAMA, "Ollama"), (AZURE, "Azure OpenAI"), (OPENAI, "OpenAI"), (ANTHROPIC, "Anthropic")]


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.resize(760, 480)
        self.settings = settings
        self.profiles = copy.deepcopy(settings.profiles)
        self.keys = {p.name: get_secret(p.name) for p in self.profiles}
        self._current: ProviderProfile | None = None
        self._loading = False

        tabs = QTabWidget()
        tabs.addTab(self._build_providers_tab(), "AI providers")
        tabs.addTab(self._build_analysis_tab(), "Analysis")
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addWidget(buttons)

        self._refresh_list()
        if self.profiles:
            self.list.setCurrentRow(0)

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
        self.f_vision = QCheckBox("Model accepts images (for scanned PDFs)")
        test = QPushButton("Test connection")
        test.clicked.connect(self._test)

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
        self.form.addRow("", test)
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
        if hasattr(self, "a_vision"):
            self._fill_vision_combo()

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
        if old_name != p.name:
            self.keys[old_name] = ""  # clears the key stored under the old name
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
        self.hint.setText(
            "Azure: enter the resource endpoint and the deployment name. API version is e.g. "
            "2024-10-21, or 'v1' for the new v1 API (the deployment field then holds the model). "
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
        try:
            msg = make_provider(p).test()
            QMessageBox.information(self, "Test connection", msg)
        except AIError as e:
            QMessageBox.warning(self, "Test connection", str(e))
        finally:
            self.unsetCursor()

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
        self.a_use_ai = QCheckBox("Use AI when metadata is not enough")
        self.a_use_ai.setChecked(s.use_ai)
        self.a_vision = QComboBox()
        self.a_pdf_pages = QSpinBox()
        self.a_pdf_pages.setRange(1, 50)
        self.a_pdf_pages.setValue(s.pdf_pages)
        self.a_chars = QSpinBox()
        self.a_chars.setRange(1000, 200_000)
        self.a_chars.setSingleStep(1000)
        self.a_chars.setValue(s.text_chars)
        self.a_subtitle = QCheckBox("Ignore subtitles when comparing titles")
        self.a_subtitle.setChecked(s.ignore_subtitle)
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

        form.addRow(self.a_use_ai)
        form.addRow("Vision profile (scanned PDFs)", self.a_vision)
        form.addRow("PDF pages to read (start/end)", self.a_pdf_pages)
        form.addRow("Characters to read (other formats)", self.a_chars)
        form.addRow(self.a_subtitle)
        form.addRow(self.a_update)
        form.addRow(self.a_permanent)
        form.addRow("Calibre program folder", crow)
        self._fill_vision_combo()
        return w

    def _fill_vision_combo(self):
        current = self.a_vision.currentData() if self.a_vision.count() else self.settings.vision_profile
        self.a_vision.clear()
        self.a_vision.addItem("None (skip scanned PDFs)", "")
        for p in self.profiles:
            self.a_vision.addItem(p.name, p.name)
        self.a_vision.setCurrentIndex(max(0, self.a_vision.findData(current)))

    def _browse_calibre(self):
        d = QFileDialog.getExistingDirectory(self, "Calibre program folder", self.a_calibre.text())
        if d:
            self.a_calibre.setText(d)

    # --- save -----------------------------------------------------------------
    def accept(self):
        self._store_current()
        names = [p.name for p in self.profiles]
        if len(set(names)) != len(names):
            QMessageBox.warning(self, "Settings", "Profile names must be unique.")
            return
        s = self.settings
        s.profiles = self.profiles
        if s.active_profile not in names:
            s.active_profile = names[0] if names else ""
        s.use_ai = self.a_use_ai.isChecked()
        vision = self.a_vision.currentData()
        s.vision_profile = vision if vision in names else ""
        s.pdf_pages = self.a_pdf_pages.value()
        s.text_chars = self.a_chars.value()
        s.ignore_subtitle = self.a_subtitle.isChecked()
        s.update_metadata = self.a_update.isChecked()
        s.delete_permanently = self.a_permanent.isChecked()
        s.calibre_dir = self.a_calibre.text().strip()
        for name, key in self.keys.items():
            set_secret(name, key if name in names else "")
        s.save()
        super().accept()
