"""Qt settings dialog for Orchestra."""
import copy
import json
import math
from urllib.parse import urlsplit

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QLineEdit,
    QInputDialog, QLabel, QMessageBox, QPlainTextEdit, QPushButton, QTabWidget, QVBoxLayout, QWidget,
)

from orchestra_core.desktop.desktop_options import DEFAULT_CONFIG, LIMITS, MODEL_PRESETS
from orchestra_core.workspace.interface_notes import atomic_text
from orchestra_core.models.model_routing import role_settings, reasoning_levels
from orchestra_core.runtime.orchestra_config import (
    completion_review_enabled, normalize_settings, project_configuration, project_settings_payload,
    set_completion_review, validate_settings,
)


REVIEW_LIMITS = (
    ('direct', 'completion_review_budget_percent', 'Maximum review share (%)', float, 0, 20),
    ('direct', 'completion_review_output_tokens', 'Output tokens per review call', int, 2, 8192),
)


class SettingsComboBox(QComboBox):
    def __init__(self):
        super().__init__()
        self.popup_pressed = False
        self.view().viewport().installEventFilter(self)
        self.activated.connect(self.hidePopup)

    def showPopup(self):
        self.popup_pressed = False
        super().showPopup()

    def eventFilter(self, watched, event):
        if (watched is not self.view().viewport()
                or event.type() not in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease)
                or event.button() != Qt.MouseButton.LeftButton):
            return super().eventFilter(watched, event)
        if event.type() == QEvent.Type.MouseButtonPress:
            self.popup_pressed = True
        else:
            # Windows can deliver the release that opened the popup to its viewport.
            if not self.popup_pressed:
                return True
            self.popup_pressed = False
            index = self.view().indexAt(event.position().toPoint())
            selectable = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
            if index.isValid() and index.flags() & selectable == selectable:
                self.hidePopup()
                self.setCurrentIndex(index.row())
                self.activated.emit(self.currentIndex())
                self.textActivated.emit(self.currentText())
                return True
        return super().eventFilter(watched, event)


class SettingsDialog(QDialog):
    def __init__(self, parent, data, *, project_path=None, global_only=False):
        super().__init__(parent)
        self.data = data
        self.project_path = project_path
        self.global_only = global_only
        self.setWindowTitle('Project Settings' if project_path else 'Orchestra Settings')
        self.resize(570, 440)
        self.settings_path = data.root / 'settings.json'
        self.settings = normalize_settings(json.loads(self.settings_path.read_text(encoding='utf-8')))
        self.config = (json.loads(data.config.read_text(encoding='utf-8'))
                       if data.config.exists() else copy.deepcopy(DEFAULT_CONFIG))
        self.config, self.settings = project_configuration(project_path, self.config, self.settings)
        for name, provider in DEFAULT_CONFIG['providers'].items():
            self.config['providers'].setdefault(name, copy.deepcopy(provider))
        self.pricing_path = data.root / 'pricing.json'
        self.pricing = json.loads(self.pricing_path.read_text(encoding='utf-8'))
        self.catalog = self.pricing['providers']
        self.pricing_changed = False
        bundled = json.loads((data.source / 'pricing.json').read_text(encoding='utf-8'))
        for provider, models in bundled['providers'].items():
            catalog = self.catalog.setdefault(provider, {})
            for model, rates in models.items():
                if model not in catalog:
                    catalog[model] = rates
                    self.pricing_changed = True
        layout = QVBoxLayout(self)
        tabs = self.tabs = QTabWidget()
        layout.addWidget(tabs)

        limits = self.form(tabs, 'Limits')
        self.limits = {}
        for section, key, label, _, _, default in LIMITS:
            field = QLineEdit(str(self.settings.get(section, {}).get(key, default)))
            self.limits[section, key] = field
            if section != 'api':
                limits.addRow(label, field)

        direct = self.settings['direct']
        self.review_enabled = QCheckBox('Run a completion review')
        self.review_enabled.setChecked(completion_review_enabled(direct))
        limits.addRow(self.review_enabled)
        review_panel = QWidget()
        review = QFormLayout(review_panel)
        review.setContentsMargins(0, 0, 0, 0)
        configured_reviewer = self.config['roles'].get('reviewer', {})
        self.review_provider = self.combo(['Same as executor', *self.config['providers']])
        if configured_reviewer.get('provider') in self.config['providers']:
            self.review_provider.setCurrentText(configured_reviewer['provider'])
        self.review_model = self.combo([])
        review.addRow('Review provider', self.review_provider)
        review.addRow('Review model', self.review_model)
        self.review_effort = self.combo([])
        review.addRow('Review reasoning effort', self.review_effort)
        for section, key, label, _, _, default in REVIEW_LIMITS:
            field = QLineEdit(str(direct.get(key, default)))
            self.limits[section, key] = field
            review.addRow(label, field)
        self.review_cost = QLabel()
        self.review_cost.setWordWrap(True)
        review.addRow(self.review_cost)
        self.review_instructions = QPlainTextEdit(direct.get('completion_review_instructions', ''))
        self.review_instructions.setPlaceholderText('Optional. For example: check failed requests and edge cases.')
        self.review_instructions.setFixedHeight(80)
        review.addRow('Review priorities', self.review_instructions)
        limits.addRow(review_panel)
        review_panel.setVisible(self.review_enabled.isChecked())
        self.review_enabled.toggled.connect(review_panel.setVisible)

        writer = role_settings(self.config, 'writer')
        models = self.form(tabs, 'Model Catalog' if global_only else 'Models')
        self.writer_provider = self.combo(self.config['providers'], writer['provider'])
        self.model = self.combo([])
        self.update_models(self.writer_provider.currentText())
        if self.model.findText(writer['model']) < 0:
            self.model.addItem(writer['model'])
        self.model.setCurrentText(writer['model'])
        self.thinking = QCheckBox('Enable model reasoning (Chat Completions)')
        self.thinking.setChecked(writer.get('thinking', 'enabled') == 'enabled')
        self.stream = QCheckBox('Show model reasoning as it streams')
        self.stream.setToolTip('Applies to the executor and the Debug reviewer.')
        self.stream.setChecked(writer.get('stream_reasoning', writer['model'].lower() == 'gpt-6.1-sol') is True)
        add_model = self.action('Add Model', self.add_model)
        models.addRow(add_model)
        models.addRow('Provider' if global_only else 'Executor provider', self.writer_provider)
        models.addRow('Model', self.model)
        self.writer_effort = self.combo([])
        models.addRow('Executor reasoning effort', self.writer_effort)
        self.pricing_group = QGroupBox()
        pricing_form = QFormLayout(self.pricing_group)
        self.rate_fields = {}
        for key, label in (
            ('input_cache_miss', 'Uncached input'),
            ('input_cache_hit', 'Cached input'),
            ('input_cache_write', 'Cache write (optional)'),
            ('output', 'Output'),
        ):
            field = self.rate_fields[key] = QLineEdit()
            field.setPlaceholderText('Optional' if key == 'input_cache_write' else 'Required')
            pricing_form.addRow(label, field)
        self.rate_drafts = {}
        self.rate_selection = None
        self.model.currentTextChanged.connect(self.show_model_rates)
        self.writer_provider.currentTextChanged.connect(self.update_models)
        self.show_model_rates()
        self.update_review_models(configured_reviewer.get('model'))
        self.review_provider.currentIndexChanged.connect(lambda _: self.update_review_models())
        self.review_model.currentTextChanged.connect(self.update_review_cost)
        for field in (*self.limits.values(), self.rate_fields['output']):
            field.textChanged.connect(self.update_review_cost)
        self.writer_provider.currentTextChanged.connect(self.update_review_cost)
        self.update_review_cost()
        models.addRow(self.pricing_group)
        models.addRow(self.thinking)
        models.addRow(self.stream)
        self.effort_drafts = {}
        for role in ('writer', 'reviewer'):
            saved = self.config['roles'].get(role, {})
            self.effort_drafts[role, saved.get('provider'), saved.get('model')] = saved.get('reasoning_effort', 'default')
        self.effort_selection = {}
        self.model.currentTextChanged.connect(self.update_reasoning)
        self.writer_provider.currentTextChanged.connect(self.update_reasoning)
        self.review_model.currentTextChanged.connect(self.update_reasoning)
        self.review_provider.currentTextChanged.connect(self.update_reasoning)
        self.update_reasoning()

        providers = self.form(tabs, 'Providers')
        self.provider = self.combo(self.config['providers'])
        self.address = QLineEdit()
        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.editing_provider = self.provider.currentText()
        self.load_provider()
        self.provider.currentTextChanged.connect(self.switch_provider)
        providers.addRow('Provider', self.provider)
        providers.addRow('API URL', self.address)
        providers.addRow('API key', self.key)
        providers.addRow('API timeout (seconds)', self.limits['api', 'timeout_seconds'])

        providers.addRow(self.action('Add Provider', self.add_provider))

        git = self.form(tabs, 'Git')
        self.git_flags = {}
        for key, label in (
            ('git_tools_enabled', 'Repository status and diff'),
            ('git_history_enabled', 'Read history and commits'),
            ('allow_git_mutations', 'Automatically commit verified changes'),
            ('allow_git_push', 'Automatically push verified commits'),
        ):
            flag = QCheckBox(label)
            flag.setChecked(bool(self.settings.get('tools', {}).get(key, key == 'allow_git_push')))
            self.git_flags[key] = flag
            git.addRow(flag)
        push = self.git_flags['allow_git_push']
        push.setToolTip('Requires repository tools and local commits to be enabled.')
        def update_push_enabled():
            enabled = (self.git_flags['git_tools_enabled'].isChecked()
                       and self.git_flags['allow_git_mutations'].isChecked())
            if not enabled:
                push.setChecked(False)
            push.setEnabled(enabled)
        self.git_flags['git_tools_enabled'].toggled.connect(update_push_enabled)
        self.git_flags['allow_git_mutations'].toggled.connect(update_push_enabled)
        update_push_enabled()

        if project_path is not None:
            tabs.setTabVisible(tabs.indexOf(providers.parentWidget()), False)
            models.setRowVisible(add_model, False)
            models.setRowVisible(self.pricing_group, False)
        elif global_only:
            for section in (limits, git):
                tabs.setTabVisible(tabs.indexOf(section.parentWidget()), False)
            for field in (self.writer_effort, self.thinking, self.stream):
                models.setRowVisible(field, False)
            tabs.setCurrentWidget(providers.parentWidget())

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText('Save')
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('Cancel')
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def action(self, text, callback):
        button = QPushButton(text)
        button.clicked.connect(callback)
        return button

    def update_reasoning(self, *_):
        for role, field, provider, model in (
            ('writer', self.writer_effort, self.writer_provider.currentText(), self.model.currentText()),
            ('reviewer', self.review_effort, self.review_provider.currentText(), self.review_model.currentText()),
        ):
            previous = self.effort_selection.get(role)
            if previous:
                self.effort_drafts[previous] = field.currentText()
            key = (role, provider, model)
            levels = reasoning_levels(provider, model)
            field.clear()
            field.addItems(['default', *levels])
            field.setEnabled(bool(levels))
            field.setToolTip('default uses the API default. Same as executor inherits its effort.'
                             if role == 'reviewer' else 'default uses the API default.')
            selected = self.effort_drafts.get(key, 'default')
            field.setCurrentText(selected if selected in levels else 'default')
            self.effort_selection[role] = key

    def update_review_cost(self):
        def value(section, key):
            return self.limits[section, key].text().strip().replace(',', '.')

        try:
            budget = float(value('direct', 'max_task_cost_usd'))
            percent = float(value('direct', 'completion_review_budget_percent'))
            if not math.isfinite(budget) or budget <= 0 or not math.isfinite(percent) or not 0 < percent < 100:
                raise ValueError
        except (ValueError, OverflowError):
            self.review_cost.setText('Enter a positive task budget and a review percentage between 0 and 100.')
            return
        review_budget = budget * percent / 100
        self.review_cost.setText(
            f'Total: ${budget:.4f}. Review: up to ${review_budget:.4f} ({percent:g}%) from funds left after implementation. '
            'Input and output both count. Output limits shrink to fit the estimated remaining funds. '
            'Token estimates can differ from actual billing; unused reserve is not a spending target.')

    def add_provider(self):
        name, accepted = QInputDialog.getText(self, 'Provider', 'Provider name')
        name = name.strip().lower()
        if not accepted:
            return
        if not name or name in self.config['providers']:
            QMessageBox.warning(self, 'Provider', 'Enter a unique name.')
            return
        transport, accepted = QInputDialog.getItem(
            self, 'Provider', 'Compatible API', ['chat', 'responses'], editable=False)
        if not accepted:
            return
        url, accepted = QInputDialog.getText(self, 'Provider', 'API base URL')
        if not accepted:
            return
        if not self.valid_address(url.strip()):
            QMessageBox.warning(self, 'Provider', 'Enter an HTTP(S) API URL without credentials.')
            return
        self.config['providers'][name] = {'transport': transport, 'base_url': url.strip(), 'api_key': ''}
        self.provider.addItem(name)
        self.writer_provider.addItem(name)
        self.review_provider.addItem(name)
        self.provider.setCurrentText(name)

    @staticmethod
    def valid_address(address):
        try:
            url = urlsplit(address)
            return url.scheme in {'http', 'https'} and bool(url.hostname) and not (url.username or url.password)
        except ValueError:
            return False

    def add_model(self):
        provider = self.writer_provider.currentText()
        model, accepted = QInputDialog.getText(
            self, 'Model', f'API model ID ({provider})')
        model = model.strip()
        if not accepted or not model:
            return
        if self.model.findText(model) < 0:
            self.rate_drafts.setdefault((provider, model), {})
            self.model.addItem(model)
        self.model.setCurrentText(model)
        self.update_review_models()
        self.rate_fields['input_cache_miss'].setFocus()

    def remember_rates(self):
        if self.rate_selection is None:
            return
        provider, model = self.rate_selection
        existing = self.catalog.get(provider, {}).get(model, {})
        values = {key: field.text().strip() for key, field in self.rate_fields.items()}
        if values != {key: str(existing.get(key, '')) for key in values}:
            self.rate_drafts[self.rate_selection] = values
        elif existing:
            self.rate_drafts.pop(self.rate_selection, None)
        elif self.rate_selection in self.rate_drafts:
            self.rate_drafts[self.rate_selection] = {}

    def show_model_rates(self):
        self.remember_rates()
        provider = self.writer_provider.currentText()
        model = self.model.currentText()
        self.rate_selection = (provider, model) if model else None
        title = f'{provider} / {model} — USD per million tokens' if model else 'Model pricing'
        self.pricing_group.setTitle(title.replace('&', '&&'))
        self.pricing_group.setEnabled(bool(model))
        existing = self.rate_drafts.get(self.rate_selection, self.catalog.get(provider, {}).get(model, {}))
        for key, field in self.rate_fields.items():
            field.setText(str(existing.get(key, '')))

    def collect_rates(self, provider, model):
        self.remember_rates()
        drafts = dict(self.rate_drafts)
        if model not in self.catalog.get(provider, {}):
            drafts.setdefault((provider, model), {})
        updates = {}
        for (name, model_id), values in drafts.items():
            if not any(values.values()) and (name, model_id) != (provider, model):
                if model_id not in self.catalog.get(name, {}):
                    continue
            rates = {}
            for key in self.rate_fields:
                raw = values.get(key, '').replace(',', '.')
                if not raw and key == 'input_cache_write':
                    continue
                try:
                    rate = float(raw)
                    if math.isfinite(rate) and rate >= 0:
                        rates[key] = rate
                        continue
                except ValueError:
                    pass
                self.writer_provider.setCurrentText(name)
                self.model.setCurrentText(model_id)
                self.tabs.setCurrentWidget(self.pricing_group.parentWidget())
                self.rate_fields[key].setFocus()
                raise ValueError(f'Enter valid nonnegative rates for {name} / {model_id}.')
            updates[name, model_id] = rates
        for (name, model_id), rates in updates.items():
            self.catalog.setdefault(name, {})[model_id] = rates
        self.pricing_changed |= bool(updates)

    @staticmethod
    def form(tabs, name):
        page = QWidget()
        tabs.addTab(page, name)
        return QFormLayout(page)

    @staticmethod
    def combo(values, current=None, *, editable=False):
        combo = SettingsComboBox()
        combo.setEditable(editable)
        combo.addItems(list(values))
        if current is not None:
            combo.setCurrentText(current)
        return combo

    def update_models(self, provider):
        self.model.clear()
        drafts = (model for name, model in getattr(self, 'rate_drafts', {}) if name == provider)
        self.model.addItems(list(dict.fromkeys((*MODEL_PRESETS.get(provider, ()),
                                               *self.catalog.get(provider, {}), *drafts))))

    def update_review_models(self, preferred=None):
        provider = self.review_provider.currentText()
        preferred = preferred or self.review_model.currentText()
        separate = self.review_provider.currentIndex() > 0
        self.review_model.setEnabled(separate)
        self.review_model.clear()
        if separate:
            drafts = (model for name, model in self.rate_drafts if name == provider)
            self.review_model.addItems(list(dict.fromkeys(
                (*self.catalog.get(provider, {}), *MODEL_PRESETS.get(provider, ()), *drafts))))
            self.review_model.setCurrentText(preferred)
        self.update_review_cost()

    def load_provider(self):
        provider = self.config['providers'][self.editing_provider]
        address = provider.get('base_url') or ''
        if self.editing_provider == 'gpt' and not address:
            address = DEFAULT_CONFIG['providers']['gpt']['base_url']
        self.address.setText(address)
        self.key.setText(provider.get('api_key') or '')

    def store_provider(self):
        self.config['providers'][self.editing_provider].update(
            base_url=self.address.text().strip(), api_key=self.key.text().strip())

    def switch_provider(self, name):
        self.store_provider()
        self.editing_provider = name
        self.load_provider()

    def save(self):
        settings = copy.deepcopy(self.settings)
        try:
            fields = (*LIMITS, *REVIEW_LIMITS) if self.review_enabled.isChecked() else LIMITS
            for section, key, label, parse, minimum, _ in fields:
                if self.global_only and section != 'api' or self.project_path is not None and section == 'api':
                    continue
                raw = self.limits[section, key].text().strip()
                try:
                    value = parse(raw.replace(',', '.') if parse is float else raw)
                except ValueError:
                    raise ValueError(label + ' — enter a number.') from None
                if not math.isfinite(value) or value < minimum or (minimum == 0 and value == 0):
                    raise ValueError(label + ' — value out of range.')
                settings.setdefault(section, {})[key] = value
            if self.global_only:
                validate_settings(settings)
                self.store_provider()
                for name, provider in self.config['providers'].items():
                    address = provider.get('base_url')
                    if (address or name not in DEFAULT_CONFIG['providers']) and not self.valid_address(address):
                        raise ValueError('Check the API URL for ' + name + '.')
                self.remember_rates()
                if self.rate_drafts:
                    self.collect_rates(self.writer_provider.currentText(), self.model.currentText())
                self.save_shared(settings)
                self.accept()
                return
            direct = settings['direct']
            enabled = self.review_enabled.isChecked()
            set_completion_review(direct, enabled)
            direct['completion_review_instructions'] = self.review_instructions.toPlainText().strip()
            settings.setdefault('tools', {}).update({key: flag.isChecked() for key, flag in self.git_flags.items()})
            if not 0 < direct.get('completion_review_budget_percent', 20) < 100:
                raise ValueError('Review budget percentage must be between 0 and 100.')
            validate_settings(settings)
            if self.project_path is None:
                self.store_provider()
            provider = self.writer_provider.currentText()
            model = self.model.currentText().strip()
            if not model:
                raise ValueError('Choose a model for ' + provider + '.')
            address = self.config['providers'][provider].get('base_url')
            if (address or provider not in DEFAULT_CONFIG['providers']) and not self.valid_address(address):
                raise ValueError('Check the API URL for ' + provider + '.')
            writer = dict(self.config['roles'].get('writer', {}))
            writer.pop('transport', None)
            writer.update(provider=provider, model=model,
                          reasoning_effort=self.writer_effort.currentText(),
                          thinking='enabled' if self.thinking.isChecked() else 'disabled',
                          stream_reasoning=self.stream.isChecked())
            self.config['roles']['writer'] = writer
            role_settings(self.config, 'writer')
            if self.project_path is None:
                self.collect_rates(provider, model)
            elif model not in self.catalog.get(provider, {}):
                raise ValueError(f'Add prices for {provider} / {model} in Global Settings.')
            if self.review_provider.currentIndex() > 0:
                reviewer_provider = self.review_provider.currentText()
                reviewer_model = self.review_model.currentText()
                if not reviewer_model:
                    raise ValueError('Choose a model for completion review.')
                if enabled and reviewer_model not in self.catalog.get(reviewer_provider, {}):
                    raise ValueError(f'Add prices for {reviewer_provider} / {reviewer_model} in the model catalog.')
                self.config['roles']['reviewer'] = {'provider': reviewer_provider, 'model': reviewer_model,
                                                   'reasoning_effort': self.review_effort.currentText()}
                role_settings(self.config, 'reviewer')
            else:
                self.config['roles'].pop('reviewer', None)
            if self.project_path is None:
                self.save_shared(settings)
            else:
                self.project_path.parent.mkdir(parents=True, exist_ok=True)
                payload = project_settings_payload(self.config, settings)
                atomic_text(self.project_path, json.dumps(payload, ensure_ascii=False, indent=2))
        except (OSError, ValueError, OverflowError) as error:
            QMessageBox.warning(self, 'Orchestra Settings', str(error))
            return
        self.accept()

    def save_shared(self, settings):
        if self.pricing_changed:
            atomic_text(self.pricing_path, json.dumps(self.pricing, ensure_ascii=False, indent=2))
        atomic_text(self.data.config, json.dumps(self.config, ensure_ascii=False, indent=2))
        atomic_text(self.settings_path, json.dumps(settings, ensure_ascii=False, indent=2))
