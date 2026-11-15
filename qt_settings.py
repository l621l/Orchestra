"""Qt settings dialog for Orchestra."""
import copy
import json
import math
from urllib.parse import urlsplit

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLineEdit,
    QMessageBox, QTabWidget, QVBoxLayout, QWidget,
)

from desktop_options import DEFAULT_CONFIG, LIMITS
from interface_notes import atomic_text
from model_routing import role_settings
from orchestra_config import normalize_settings, validate_settings


class SettingsDialog(QDialog):
    def __init__(self, parent, data):
        super().__init__(parent)
        self.data = data
        self.setWindowTitle('Настройки Orchestra')
        self.resize(540, 360)
        self.settings_path = data.root / 'settings.json'
        self.settings = normalize_settings(json.loads(self.settings_path.read_text(encoding='utf-8')))
        self.config = (json.loads(data.config.read_text(encoding='utf-8'))
                       if data.config.exists() else copy.deepcopy(DEFAULT_CONFIG))
        for name, provider in DEFAULT_CONFIG['providers'].items():
            self.config['providers'].setdefault(name, copy.deepcopy(provider))
        self.catalog = json.loads((data.root / 'pricing.json').read_text(encoding='utf-8'))['providers']
        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        layout.addWidget(tabs)

        limits = self.form(tabs, 'Лимиты')
        self.limits = {}
        for section, key, label, _, _, default in LIMITS:
            field = QLineEdit(str(self.settings.get(section, {}).get(key, default)))
            self.limits[section, key] = field
            limits.addRow(label, field)

        writer = role_settings(self.config, 'writer')
        models = self.form(tabs, 'Модели')
        self.writer_provider = self.combo(self.config['providers'], writer['provider'])
        self.model = self.combo([], editable=True)
        self.update_models(self.writer_provider.currentText())
        self.model.setCurrentText(writer['model'])
        self.writer_provider.currentTextChanged.connect(self.update_models)
        self.thinking = self.combo(('enabled', 'disabled'), writer.get('thinking', 'enabled'))
        self.stream = QCheckBox('Показывать рассуждения модели в реальном времени')
        self.stream.setChecked(writer.get('stream_reasoning', writer['model'].lower() == 'gpt-6.1-sol') is True)
        models.addRow('Провайдер исполнителя', self.writer_provider)
        models.addRow('Модель', self.model)
        models.addRow('Рассуждения модели (Chat Completions)', self.thinking)
        models.addRow(self.stream)

        providers = self.form(tabs, 'Провайдеры')
        self.provider = self.combo(self.config['providers'])
        self.address = QLineEdit()
        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.editing_provider = self.provider.currentText()
        self.load_provider()
        self.provider.currentTextChanged.connect(self.switch_provider)
        providers.addRow('Провайдер', self.provider)
        providers.addRow('Адрес API', self.address)
        providers.addRow('Ключ API', self.key)

        git = self.form(tabs, 'Git')
        self.git_flags = {}
        for key, label in (
            ('git_tools_enabled', 'Статус и diff репозитория'),
            ('git_history_enabled', 'Чтение истории и коммитов'),
            ('allow_git_mutations', 'Автокоммиты этапов в текущей ветке'),
        ):
            flag = QCheckBox(label)
            flag.setChecked(bool(self.settings.get('tools', {}).get(key)))
            self.git_flags[key] = flag
            git.addRow(flag)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText('Сохранить')
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('Отмена')
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def form(tabs, name):
        page = QWidget()
        tabs.addTab(page, name)
        return QFormLayout(page)

    @staticmethod
    def combo(values, current=None, *, editable=False):
        combo = QComboBox()
        combo.setEditable(editable)
        combo.addItems(list(values))
        if current is not None:
            combo.setCurrentText(current)
        return combo

    def update_models(self, provider):
        self.model.clear()
        self.model.addItems(list(self.catalog.get(provider, {})))

    def load_provider(self):
        provider = self.config['providers'][self.editing_provider]
        self.address.setText(provider.get('base_url') or '')
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
            for section, key, label, parse, minimum, _ in LIMITS:
                raw = self.limits[section, key].text().strip()
                try:
                    value = parse(raw.replace(',', '.') if parse is float else raw)
                except ValueError:
                    raise ValueError(label + ' — введите число.') from None
                if not math.isfinite(value) or value < minimum or (minimum == 0 and value == 0):
                    raise ValueError(label + ' — значение вне допустимого диапазона.')
                settings.setdefault(section, {})[key] = value
            settings.setdefault('tools', {}).update({key: flag.isChecked() for key, flag in self.git_flags.items()})
            validate_settings(settings)
            self.store_provider()
            provider = self.writer_provider.currentText()
            model = self.model.currentText().strip()
            if not model:
                raise ValueError('Укажите модель для ' + provider + '.')
            address = self.config['providers'][provider].get('base_url')
            if address:
                url = urlsplit(address)
                if url.scheme not in {'http', 'https'} or not url.hostname or url.username or url.password:
                    raise ValueError('Проверьте адрес API для ' + provider + '.')
            writer = dict(self.config['roles'].get('writer', {}))
            writer.pop('transport', None)
            writer.update(provider=provider, model=model, thinking=self.thinking.currentText(),
                          stream_reasoning=self.stream.isChecked())
            self.config['roles']['writer'] = writer
            role_settings(self.config, 'writer')
            atomic_text(self.data.config, json.dumps(self.config, ensure_ascii=False, indent=2))
            atomic_text(self.settings_path, json.dumps(settings, ensure_ascii=False, indent=2))
        except (OSError, ValueError, OverflowError) as error:
            QMessageBox.warning(self, 'Настройки Orchestra', str(error))
            return
        self.accept()
