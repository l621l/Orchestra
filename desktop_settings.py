"""Editor for settings used by the direct development mode."""
import copy
import json
import math
import tkinter as tk
from tkinter import ttk
from urllib.parse import urlsplit

from desktop_theme import BACKGROUND
from interface_notes import atomic_text
from model_routing import role_settings
from orchestra_config import validate_settings, normalize_settings


LIMITS = (
    ('direct', 'max_task_cost_usd', 'Порог расходов на задачу, USD', float, 0, 0.1),
    ('api', 'timeout_seconds', 'Тайм-аут API, сек.', float, 0, 180),
    ('execution', 'max_api_calls_per_turn', 'Вызовы API за запуск', int, 1, 300),
    ('execution', 'max_steps_per_agent', 'Шаги за запуск', int, 1, 100),
    ('generation', 'max_output_tokens', 'Выходные токены на запрос', int, 1, 131072),
    ('direct', 'writer_request_characters', 'Размер запроса исполнителя, символы', int, 3000, 24000),
    ('direct', 'no_progress_calls', 'Шаги без прогресса до восстановления', int, 1, 6),
    ('direct', 'recovery_calls', 'Шаги восстановления', int, 1, 4),
)

DEFAULT_CONFIG = {
    'providers': {'mimo': {'api_key': '', 'base_url': 'https://api.xiaomimimo.com/v1'},
                  'gpt': {'api_key': '', 'base_url': ''}},
    'roles': {'writer': {'provider': 'mimo', 'model': 'mimo-v2.6-pro'}},
}


class SettingsDialog:
    def __init__(self, parent, data, on_saved):
        self.data = data
        self.on_saved = on_saved
        self.settings_path = data.root / 'settings.json'
        self.settings = normalize_settings(json.loads(self.settings_path.read_text(encoding='utf-8')))
        self.config = (json.loads(data.config.read_text(encoding='utf-8'))
                       if data.config.exists() else copy.deepcopy(DEFAULT_CONFIG))
        for name, provider in DEFAULT_CONFIG['providers'].items():
            self.config['providers'].setdefault(name, copy.deepcopy(provider))
        self.window = tk.Toplevel(parent, background=BACKGROUND)
        self.window.title('Настройки Orchestra')
        self.window.transient(parent)
        self.window.columnconfigure(0, weight=1)
        tabs = ttk.Notebook(self.window)
        tabs.grid(row=0, column=0, sticky='ew', padx=16, pady=16)
        limits = ttk.Frame(tabs, padding=12)
        models = ttk.Frame(tabs, padding=12)
        providers = ttk.Frame(tabs, padding=12)
        git = ttk.Frame(tabs, padding=12)
        tabs.add(limits, text='Лимиты')
        tabs.add(models, text='Модели')
        tabs.add(providers, text='Провайдеры')
        tabs.add(git, text='Git')
        self.git_flags = {}
        for key, label in (
            ('git_tools_enabled', 'Статус и diff репозитория'),
            ('git_history_enabled', 'Чтение истории и коммитов'),
            ('allow_git_mutations', 'Автокоммиты этапов в текущей ветке'),
        ):
            variable = tk.BooleanVar(master=git, value=self.settings.get('tools', {}).get(key, False))
            ttk.Checkbutton(git, text=label, variable=variable).pack(anchor='w', pady=4)
            self.git_flags[key] = variable
        ttk.Label(git, text='Включите статус и diff для доступа к Git. Автокоммит требует '
                            'успешной проверки этапа.', wraplength=440).pack(anchor='w')
        self.limits = {}
        for row, (section, key, label, _, _, default) in enumerate(LIMITS):
            self.limits[section, key] = self.field(limits, row, label,
                                                   self.settings.get(section, {}).get(key, default))
        ttk.Label(limits, text='Применяется со следующего запуска. Порог расходов проверяется после ответа API.',
                  wraplength=440).grid(row=len(LIMITS), columnspan=2, sticky='w', pady=(8, 0))
        self.roles = {}
        catalog = json.loads((data.root / 'pricing.json').read_text(encoding='utf-8'))['providers']
        for index, (role, title) in enumerate(
                (('writer', 'Исполнитель'),)):
            current = role_settings(self.config, role)
            row = index * 3
            self.roles[role] = {
                'provider': self.field(models, row, title + ' · провайдер', current['provider'],
                                       choices=tuple(self.config['providers'])),
                'model': self.field(models, row + 1, title + ' · модель', current['model'],
                                    choices=tuple(catalog.get(current['provider'], {})) if role == 'writer' else None,
                                    editable=role == 'writer'),
            }
            if role == 'writer':
                writer = self.roles[role]
                model_box = models.grid_slaves(row=row + 1, column=1)[0]

                def update_writer_models(*_):
                    provider = writer['provider'].get()
                    choices = tuple(catalog.get(provider, {}))
                    model_box.configure(values=choices)
                    writer['model'].set(choices[0] if choices else '')

                writer['provider'].trace_add('write', update_writer_models)
        self.thinking = self.field(models, 6, 'Рассуждения MiMo',
                                   role_settings(self.config, 'writer').get('thinking', 'enabled'),
                                   choices=('enabled', 'disabled'))
        self.stream_reasoning = tk.BooleanVar(
            master=models, value=role_settings(self.config, 'writer').get('stream_reasoning') is True)
        ttk.Checkbutton(models, text='Показывать рассуждения в реальном времени',
                        variable=self.stream_reasoning).grid(row=7, columnspan=2, sticky='w', pady=4)
        ttk.Label(models, text='Для новых моделей нужны тарифы в pricing.json.',
                  wraplength=440).grid(row=8, columnspan=2, sticky='w', pady=(8, 0))
        self.provider_name = self.field(providers, 0, 'Провайдер', next(iter(self.config['providers'])),
                                        choices=tuple(self.config['providers']), on_select=self.select_provider)
        self.provider_url = self.field(providers, 1, 'Адрес API', '')
        self.provider_key = self.field(providers, 2, 'Ключ API', '', secret=True)
        self.editing_provider = self.provider_name.get()
        self.load_provider()
        self.message = tk.StringVar(master=self.window)
        ttk.Label(self.window, textvariable=self.message, wraplength=480).grid(
            row=1, column=0, sticky='w', padx=16)
        bar = ttk.Frame(self.window, padding=12)
        bar.grid(row=2, column=0, sticky='e')
        ttk.Button(bar, text='Отмена', command=self.window.destroy).pack(side='right', padx=(8, 0))
        ttk.Button(bar, text='Сохранить', command=self.save).pack(side='right')
        self.window.grab_set()

    @staticmethod
    def field(parent, row, label, value, *, choices=None, secret=False, on_select=None, editable=False):
        variable = tk.StringVar(master=parent, value=value)
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky='w', padx=(0, 12), pady=4)
        if choices is None:
            widget = ttk.Entry(parent, textvariable=variable, width=30, show='•' if secret else '')
        else:
            widget = ttk.Combobox(parent, textvariable=variable, values=choices,
                                  state='normal' if editable else 'readonly', width=28)
            if on_select:
                widget.bind('<<ComboboxSelected>>', on_select)
        widget.grid(row=row, column=1, sticky='ew')
        parent.columnconfigure(1, weight=1)
        return variable

    def load_provider(self):
        provider = self.config['providers'][self.editing_provider]
        self.provider_url.set(provider.get('base_url') or '')
        self.provider_key.set(provider.get('api_key') or '')

    def store_provider(self):
        self.config['providers'][self.editing_provider].update(
            base_url=self.provider_url.get().strip(), api_key=self.provider_key.get().strip())

    def select_provider(self, _event):
        self.store_provider()
        self.editing_provider = self.provider_name.get()
        self.load_provider()

    def save(self):
        settings = copy.deepcopy(self.settings)
        try:
            for section, key, label, parse, minimum, _ in LIMITS:
                raw = self.limits[section, key].get().strip()
                try:
                    value = parse(raw.replace(',', '.') if parse is float else raw)
                except ValueError:
                    raise ValueError(label + ' — введите число.') from None
                if not math.isfinite(value) or value < minimum or (minimum == 0 and value == 0):
                    raise ValueError(label + ' — значение вне допустимого диапазона.')
                settings.setdefault(section, {})[key] = value
            settings.setdefault('tools', {}).update({key: flag.get() for key, flag in self.git_flags.items()})
            validate_settings(settings)
            self.store_provider()
            for role, fields in self.roles.items():
                original = role_settings(self.config, role)
                updated = {**original, **{key: field.get().strip() for key, field in fields.items()}}
                updated.pop('transport', None)
                if role == 'writer':
                    updated['thinking'] = self.thinking.get()
                    updated['stream_reasoning'] = self.stream_reasoning.get()
                self.config['roles'][role] = updated
                role_settings(self.config, role)
                provider = self.config['providers'][updated['provider']]
                if not updated['model']:
                    raise ValueError('Укажите модель для ' + updated['provider'] + '.')
                address = provider.get('base_url')
                if address:
                    url = urlsplit(address)
                    if url.scheme not in {'http', 'https'} or not url.hostname or url.username or url.password:
                        raise ValueError('Проверьте адрес API для ' + updated['provider'] + '.')
            atomic_text(self.data.config, json.dumps(self.config, ensure_ascii=False, indent=2))
            atomic_text(self.settings_path, json.dumps(settings, ensure_ascii=False, indent=2))
        except (OSError, ValueError, OverflowError) as error:
            self.message.set(str(error))
            return
        self.window.destroy()
        self.on_saved()
