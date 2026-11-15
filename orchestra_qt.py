"""Qt desktop entry point for Orchestra."""
import argparse
import asyncio
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from PySide6.QtCore import QRectF, Qt, QTimer, QUrl
from PySide6.QtGui import (
    QColor, QDesktopServices, QFont, QIcon, QKeySequence, QPainter, QPalette, QPixmap,
    QShortcut, QTextCursor,
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QFileDialog, QFileSystemModel, QFormLayout,
    QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QSplitter,
    QTabWidget, QTreeView, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from desktop_data import DesktopData
from orchestra_app import OrchestraApp
from project_usage import IGNORE_RULE, read_usage
from qt_git_graph import GitGraph
from qt_settings import SettingsDialog
from qt_statistics import UsageDialog
from workspace_lease import workspace_lease


PROJECT_GITIGNORE = f"""# Файлы, которые создаёт Orchestra
/PROJECT_MAP.md
/INTERFACE_NOTES.json
/ORCHESTRA_JOURNAL.jsonl
{IGNORE_RULE}
"""


def git_web_url(remote):
    remote = remote.strip()
    if '://' not in remote:
        match = re.fullmatch(r'(?:[^@:/\s]+@)?([\w.-]{2,}):([^\\\s]+/[^\\\s]+)', remote)
        if not match:
            return None
        host, path = match.groups()
        remote = f'ssh://{host}/{path.lstrip("/")}'
    try:
        url = urlsplit(remote)
        if url.scheme not in {'http', 'https', 'ssh', 'git'} or not url.hostname or not url.path.strip('/'):
            return None
        host = f'[{url.hostname}]' if ':' in url.hostname else url.hostname
        if url.scheme in {'http', 'https'} and url.port is not None:
            host += f':{url.port}'
        scheme = 'http' if url.scheme == 'http' else 'https'
        return urlunsplit((scheme, host, url.path.rstrip('/').removesuffix('.git'), '', ''))
    except ValueError:
        return None


def orchestra_icon():
    image = QPixmap(128, 128)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QColor('#75b9ff'))
    painter.setBrush(QColor('#182b42'))
    painter.drawRoundedRect(QRectF(5, 5, 118, 118), 23, 23)
    painter.setPen(QColor('#a7d6ff'))
    font = QFont('Inter')
    font.setPixelSize(62)
    font.setWeight(QFont.Weight.DemiBold)
    painter.setFont(font)
    painter.drawText(image.rect(), Qt.AlignmentFlag.AlignCenter, 'Or')
    painter.end()
    return image


class DesktopWindow(QMainWindow):
    def __init__(self, data, loop, workspace=None):
        super().__init__()
        self.data = data
        self.loop = loop
        self.task = None
        self.closing = False
        self.preferences = data.load_preferences()
        self.workspace = Path(workspace).resolve() if workspace else None
        self.selected_run = None
        self.stream_item = None
        self.setWindowTitle('Orchestra')
        self.resize(1120, 720)
        split = QSplitter()
        self.setCentralWidget(split)
        sidebar = QVBoxLayout()
        panel = QWidget()
        panel.setLayout(sidebar)
        split.addWidget(panel)
        brand = QHBoxLayout()
        emblem = QLabel()
        icon = orchestra_icon()
        self.setWindowIcon(QIcon(icon))
        emblem.setPixmap(icon.scaled(32, 32, Qt.AspectRatioMode.KeepAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation))
        brand.addWidget(emblem)
        brand.addWidget(QLabel('Orchestra'))
        brand.addStretch()
        sidebar.addLayout(brand)
        sidebar.addWidget(QLabel('Проекты'))
        self.project_actions = []
        for label, callback in (
            ('Создать проект', self.open_project_dialog),
            ('Файлы проекта', self.show_files),
            ('GIT', self.show_git_history),
            ('Статистика', self.show_usage),
        ):
            self.project_actions.append(self.button(sidebar, label, callback))
        self.projects = QTreeWidget()
        self.projects.setHeaderHidden(True)
        self.projects.setMouseTracking(True)
        self.projects.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
        self.projects.setExpandsOnDoubleClick(False)
        self.projects.currentItemChanged.connect(self.select_project)
        self.projects.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.projects.customContextMenuRequested.connect(self.project_menu)
        sidebar.addWidget(self.projects)

        body = QWidget()
        layout = QVBoxLayout(body)
        split.addWidget(body)
        split.setSizes([240, 880])
        split.setStretchFactor(1, 1)
        self.path = QLabel()
        self.path.setTextFormat(Qt.TextFormat.PlainText)
        self.path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.path)
        self.project_cost = QLabel()
        layout.addWidget(self.project_cost)
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.prompt = QPlainTextEdit()
        self.prompt.setPlaceholderText('Введите задачу')
        self.prompt.setMaximumHeight(120)
        layout.addWidget(self.prompt)
        actions = QHBoxLayout()
        layout.addLayout(actions)
        self.start_button = self.button(actions, 'Новая задача', lambda: self.start('new_task'))
        self.continue_button = self.button(actions, 'Продолжить', lambda: self.start('continue_task'))
        self.stop_button = self.button(actions, 'Остановить', self.stop)
        self.stop_button.setEnabled(False)
        actions.addStretch()
        self.button(actions, 'Настройки', self.open_settings)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.hide()
        layout.addWidget(self.progress_bar)
        self.activity = ''
        self.activity_label = QLabel()
        layout.addWidget(self.activity_label)
        tabs = QTabWidget()
        layout.addWidget(tabs, 1)
        self.output = self.text_tab(tabs, 'Журнал')
        self.reasoning = self.text_tab(tabs, 'Рассуждения')
        self.reload_projects()
        self.refresh()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(20)

    @staticmethod
    def button(layout, label, callback):
        button = QPushButton(label)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(callback)
        layout.addWidget(button)
        return button

    @staticmethod
    def text_tab(tabs, label):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        text = QPlainTextEdit()
        text.setReadOnly(True)
        button = QPushButton('Скопировать всё')
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(lambda: QApplication.clipboard().setText(text.toPlainText()))
        layout.addWidget(button, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(text)
        tabs.addTab(panel, label)
        return text

    @staticmethod
    def append(widget, text):
        bar = widget.verticalScrollBar()
        follow = bar.value() == bar.maximum()
        cursor = QTextCursor(widget.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text)
        if follow:
            bar.setValue(bar.maximum())

    def reload_projects(self):
        history = self.data.history()
        hidden = set(self.preferences.get('hidden_workspaces', []))
        registered = self.preferences.get('workspaces', [])
        paths = dict.fromkeys([*registered, *history, *([str(self.workspace)] if self.workspace else [])])
        expanded = {
            item.data(0, Qt.ItemDataRole.UserRole)[0]
            for index in range(self.projects.topLevelItemCount())
            if (item := self.projects.topLevelItem(index)).isExpanded()
        }
        self.projects.blockSignals(True)
        self.projects.clear()
        active = None
        for path in paths:
            if path in hidden or not Path(path).is_dir():
                continue
            item = QTreeWidgetItem(self.projects, [Path(path).name or path])
            item.setToolTip(0, path)
            item.setData(0, Qt.ItemDataRole.UserRole, (path, None))
            for directory, summary in history.get(path, ()):
                run = QTreeWidgetItem(item, [summary.get('topic') or directory.name])
                run.setData(0, Qt.ItemDataRole.UserRole, (path, (directory, summary)))
            item.setExpanded(path in expanded)
            if path == str(self.workspace):
                active = item
        active = active or self.projects.topLevelItem(0)
        workspace = None
        if active:
            self.projects.setCurrentItem(active)
            workspace = Path(active.data(0, Qt.ItemDataRole.UserRole)[0])
        if workspace != self.workspace:
            self.workspace = workspace
            self.selected_run = None
            self.output.clear()
            self.reasoning.clear()
        self.projects.blockSignals(False)

    def select_project(self, item, _previous):
        if item is None or self.task:
            return
        path, self.selected_run = item.data(0, Qt.ItemDataRole.UserRole)
        self.workspace = Path(path)
        self.output.clear()
        self.reasoning.clear()
        if self.selected_run:
            directory, summary = self.selected_run
            self.output.appendPlainText(summary.get('request') or summary.get('topic', ''))
            if summary.get('answer_file') == 'answer.md':
                try:
                    self.output.appendPlainText((directory / 'answer.md').read_text(encoding='utf-8'))
                except (OSError, UnicodeError):
                    self.output.appendPlainText('Ответ недоступен.')
            usage = summary.get('usage', {})
            self.output.appendPlainText(f"Токены: {usage.get('total_tokens', 0)} · {directory}")
            if summary.get('cost'):
                self.write_cost(summary['cost'])
        self.refresh()

    def project_menu(self, position):
        item = self.projects.itemAt(position)
        if not item or item.parent() or self.task:
            return
        menu = QMenu(self)
        hide = menu.addAction('Скрыть проект')
        if menu.exec(self.projects.viewport().mapToGlobal(position)) == hide:
            path, _ = item.data(0, Qt.ItemDataRole.UserRole)
            hidden = self.preferences.setdefault('hidden_workspaces', [])
            if path not in hidden:
                hidden.append(path)
            self.data.save_preferences(self.preferences)
            if self.workspace == Path(path):
                self.workspace = None
                self.selected_run = None
                self.output.clear()
                self.reasoning.clear()
            self.reload_projects()
            self.refresh()

    def pick_folder(self, title, accept, parent=None):
        dialog = QFileDialog(parent if parent is not None else self)
        dialog.setOption(QFileDialog.Option.DontUseNativeDialog)
        dialog.setFileMode(QFileDialog.FileMode.Directory)
        dialog.setOption(QFileDialog.Option.ShowDirsOnly)
        dialog.setWindowTitle(title)
        dialog.setDirectory(str(self.workspace or Path.home()))
        dialog.setLabelText(QFileDialog.DialogLabel.Accept, accept)
        dialog.setLabelText(QFileDialog.DialogLabel.Reject, 'Отмена')
        if dialog.exec():
            return Path(dialog.selectedFiles()[0]).resolve()
        return None

    def open_project_dialog(self):
        dialog = QDialog(self)
        dialog.setWindowTitle('Создать проект')
        dialog.setMinimumWidth(500)
        form = QFormLayout(dialog)
        name = QLineEdit()
        location = QLineEdit(str(self.workspace.parent if self.workspace else Path.home()))
        location_row = QHBoxLayout()
        location_row.addWidget(location)
        initialize_git = QCheckBox('Создать Git-репозиторий')
        initialize_git.setChecked(True)
        error_label = QLabel()
        error_label.setTextFormat(Qt.TextFormat.PlainText)
        error_label.setWordWrap(True)

        def browse():
            folder = self.pick_folder('Родительская папка', 'Выбрать', dialog)
            if folder is not None:
                location.setText(str(folder))

        def create():
            try:
                folder = self.make_project(location.text(), name.text(), initialize_git.isChecked())
                self.register_project(folder)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                error_label.setText(str(error))
                return
            dialog.accept()

        def add_existing():
            folder = self.pick_folder('Добавить существующий проект', 'Добавить', dialog)
            if folder is None:
                return
            try:
                self.register_project(folder)
            except OSError as error:
                error_label.setText(str(error))
                return
            dialog.accept()

        self.button(location_row, 'Выбрать', browse)
        form.addRow('Имя проекта', name)
        form.addRow('Родительская папка', location_row)
        form.addRow(initialize_git)
        form.addRow(error_label)
        actions = QHBoxLayout()
        self.button(actions, 'Добавить существующий', add_existing).setAutoDefault(False)
        actions.addStretch()
        self.button(actions, 'Отмена', dialog.reject)
        self.button(actions, 'Создать', create).setDefault(True)
        form.addRow(actions)
        name.setFocus()
        dialog.exec()

    def make_project(self, location, name, initialize_git):
        name = name.strip()
        if not name or name in {'.', '..'} or any(c in name for c in '/\\:'):
            raise ValueError('Введите имя одной папки проекта.')
        if not location.strip():
            raise ValueError('Укажите родительскую папку.')
        parent = Path(location.strip()).expanduser().resolve()
        if not parent.is_dir():
            raise ValueError('Родительская папка не найдена.')
        folder = parent / name
        try:
            folder.mkdir()
        except FileExistsError:
            raise ValueError('Такая папка уже существует. Выберите «Добавить существующий».') from None
        if initialize_git:
            try:
                with workspace_lease(folder):
                    repo = self.git('rev-parse', '--is-inside-work-tree', check=False, folder=folder)
                    if repo.stdout.strip() != 'true' and not (folder / '.git').exists():
                        self.git('init', '--initial-branch=main', folder=folder)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                try:
                    folder.rmdir()
                except OSError:
                    raise ValueError(f'Папка создана, но Git не настроен. {error}') from error
                raise
        with (folder / '.gitignore').open('x', encoding='utf-8') as ignore:
            ignore.write(PROJECT_GITIGNORE)
        return folder

    def register_project(self, folder):
        self.workspace = folder
        path = str(self.workspace)
        workspaces = self.preferences.setdefault('workspaces', [])
        if path not in workspaces:
            workspaces.insert(0, path)
        self.preferences['hidden_workspaces'] = [
            value for value in self.preferences.get('hidden_workspaces', []) if value != path]
        self.data.save_preferences(self.preferences)
        self.selected_run = None
        self.reload_projects()
        self.output.clear()
        self.reasoning.clear()
        self.refresh()

    def session(self):
        if self.workspace is None or not self.workspace.is_dir():
            raise ValueError('Выберите существующую рабочую папку.')
        return OrchestraApp.from_directory(self.data.root, self.workspace, logs_path=self.data.logs)

    def refresh(self):
        self.path.setText(str(self.workspace or ''))
        self.refresh_project_cost()
        self.continue_button.setEnabled(self.selected_run is None)
        if self.selected_run:
            self.status.setText('История запуска. Для продолжения выберите проект.')
            return
        if not self.data.config.is_file():
            self.status.setText('Откройте «Настройки» и заполните данные провайдеров.')
            return
        try:
            state = self.session().status()
        except (OSError, ValueError, KeyError) as error:
            self.status.setText(str(error))
            return
        labels = {'new': 'Новая рабочая папка', 'pause': 'Можно продолжить',
                  'done': 'Задача завершена', 'state_error': 'Ошибка сохранённой задачи'}
        text = 'В этой папке уже идёт работа.' if state['running'] else labels.get(state['status'], state['status'])
        if 'budget_spent' in state:
            text += f" · Расход: ${state['budget_spent']:.5f}/${state['budget_limit']:.2f}"
        self.status.setText(text)

    def refresh_project_cost(self):
        try:
            data = read_usage(self.workspace) if self.workspace else None
            total = data['total'] if data else {}
            if total:
                suffix = '' if total['complete'] else ' · данные неполные'
                self.project_cost.setText(f"Всего по проекту: ${total['estimated_usd']:.5f}{suffix}")
            else:
                self.project_cost.clear()
        except (OSError, ValueError, KeyError, TypeError):
            self.project_cost.setText('Статистика проекта недоступна.')

    def show_usage(self):
        if self.workspace is None:
            QMessageBox.information(self, 'Статистика проекта', 'Выберите проект.')
            return
        try:
            data = read_usage(self.workspace)
            if not data['runs']:
                QMessageBox.information(self, 'Статистика проекта', 'Расходов пока нет.')
                return
            dialog = UsageDialog(self, data)
        except (OSError, ValueError, KeyError, TypeError) as error:
            QMessageBox.warning(self, 'Статистика проекта', str(error))
            return
        dialog.exec()
        dialog.deleteLater()

    def open_settings(self):
        try:
            if SettingsDialog(self, self.data).exec() and not self.task:
                self.refresh()
        except (OSError, ValueError, KeyError, TypeError) as error:
            QMessageBox.warning(self, 'Настройки Orchestra', str(error))

    def git(self, *args, check=True, folder=None):
        folder = self.workspace if folder is None else folder
        if folder is None:
            raise ValueError('Выберите проект.')
        result = subprocess.run(['git', '-C', str(folder), *args], stdin=subprocess.DEVNULL,
                                capture_output=True, encoding='utf-8', errors='replace', timeout=30,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if check and result.returncode:
            raise ValueError(result.stderr.strip() or 'Команда Git завершилась ошибкой.')
        return result

    def show_git_history(self):
        try:
            head = self.git('rev-parse', '--verify', 'HEAD', check=False)
            history = ''
            if head.returncode == 0:
                history = self.git('--no-pager', 'log', 'HEAD', '--all', '--topo-order',
                                   '--decorate=short', '--format=%H%x00%P%x00%h%x00%d%x00%s',
                                   '-n', '150').stdout
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            QMessageBox.warning(self, 'История Git', str(error))
            return
        dialog = QDialog(self)
        dialog.setWindowTitle('GIT')
        dialog.resize(820, 500)
        layout = QVBoxLayout(dialog)
        graph = GitGraph(history, dialog, head=head.stdout.strip() if head.returncode == 0 else None)
        layout.addWidget(graph)
        actions = QHBoxLayout()
        layout.addLayout(actions)
        self.button(actions, 'Ссылка Git (origin)', self.set_origin)
        self.button(actions, 'Показать в Git', lambda: self.open_origin(dialog))
        actions.addStretch()

        def create_from_head():
            if self.create_branch():
                dialog.accept()

        def create_from_selection():
            rows = graph.scene().selectedItems()
            if rows and self.create_branch(rows[0].commit):
                dialog.accept()

        from_head = self.button(actions, 'Создать ветку от HEAD', create_from_head)
        from_head.setEnabled(head.returncode == 0)
        selected = self.button(actions, 'Создать ветку отсюда', create_from_selection)
        selected.setEnabled(False)
        scene = graph.scene()

        def update_selection():
            selected.setEnabled(bool(scene.selectedItems()))

        scene.selectionChanged.connect(update_selection)
        try:
            dialog.exec()
        finally:
            scene.selectionChanged.disconnect(update_selection)
            dialog.deleteLater()

    def show_files(self):
        if self.workspace is None or not self.workspace.is_dir():
            QMessageBox.warning(self, 'Файлы проекта', 'Выберите папку проекта.')
            return
        dialog = QDialog(self)
        dialog.setWindowTitle('Файлы проекта')
        dialog.resize(820, 500)
        layout = QVBoxLayout(dialog)
        model = QFileSystemModel(dialog)
        model.setReadOnly(True)
        tree = QTreeView(dialog)
        tree.setModel(model)
        tree.setRootIndex(model.setRootPath(str(self.workspace)))
        tree.setColumnWidth(0, 350)
        tree.setMouseTracking(True)
        tree.entered.connect(lambda _: tree.viewport().setCursor(Qt.CursorShape.PointingHandCursor))
        tree.viewportEntered.connect(lambda: tree.viewport().unsetCursor())

        def open_file(index):
            if not index.isValid():
                return
            path = Path(model.filePath(index))
            if path.is_file() and not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
                QMessageBox.warning(dialog, 'Файлы проекта',
                                    'Не удалось открыть файл в приложении по умолчанию.')

        tree.doubleClicked.connect(open_file)
        for key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            shortcut = QShortcut(QKeySequence(key), tree)
            shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
            shortcut.activated.connect(lambda: open_file(tree.currentIndex()))
        layout.addWidget(tree)
        tree.setFocus()
        dialog.exec()

    def create_branch(self, commit=None):
        branch, accepted = QInputDialog.getText(self, 'Git', 'Имя новой ветки')
        if accepted and branch.strip():
            return self.git_change('switch', '-c', branch.strip(), '--', *([commit] if commit else []))
        return False

    def set_origin(self):
        try:
            current = self.git('remote', 'get-url', 'origin', check=False)
            url, accepted = QInputDialog.getText(self, 'Git', 'Адрес origin', text=current.stdout.strip())
            if accepted and url.strip():
                self.git_change('remote', 'set-url' if current.returncode == 0 else 'add',
                                '--', 'origin', url.strip())
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            QMessageBox.warning(self, 'Git', str(error))

    def open_origin(self, parent):
        try:
            remote = self.git('remote', 'get-url', 'origin', check=False)
            address = git_web_url(remote.stdout) if remote.returncode == 0 else None
            if address is None:
                raise ValueError('Укажите в origin ссылку на удалённый репозиторий.')
            if not QDesktopServices.openUrl(QUrl(address)):
                raise ValueError('Не удалось открыть удалённый репозиторий в браузере.')
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            QMessageBox.warning(parent, 'Git', str(error))

    def git_change(self, *args):
        try:
            if self.workspace is None:
                raise ValueError('Выберите проект.')
            with workspace_lease(self.workspace):
                self.git(*args)
            self.refresh()
            return True
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            QMessageBox.warning(self, 'Git', str(error))
            return False

    def start(self, method):
        if self.task:
            return
        text = self.prompt.toPlainText().strip()
        if method == 'new_task' and not text:
            self.status.setText('Введите задачу.')
            return
        try:
            app = self.session()
        except (OSError, ValueError, KeyError) as error:
            QMessageBox.warning(self, 'Orchestra', str(error))
            return
        if self.selected_run:
            self.output.clear()
        self.selected_run = None
        self.stream_item = None
        self.reasoning.clear()
        self.started_at = time.monotonic()
        self.activity = 'Запуск'
        self.progress_bar.show()
        self.set_busy(True)
        self.task = self.loop.create_task(self.run(app, method, text))

    def set_busy(self, busy):
        for widget in [self.projects, self.start_button, self.continue_button, *self.project_actions]:
            widget.setEnabled(not busy)
        self.stop_button.setEnabled(busy)

    async def run(self, app, method, text):
        try:
            result = await getattr(app, method)(text, on_progress=self.progress)
            self.output.appendPlainText(result.get('answer') or result.get('error') or result['status'])
            self.output.appendPlainText('Журнал: ' + result['log_directory'])
        except asyncio.CancelledError:
            self.output.appendPlainText('Работа остановлена. Задачу можно продолжить.')
        except (OSError, ValueError, RuntimeError) as error:
            self.output.appendPlainText(str(error))
        finally:
            await asyncio.gather(*(client.close() for client in app.engine.clients.values()),
                                 return_exceptions=True)

    def progress(self, event):
        kind = event['kind']
        if kind in {'reasoning', 'model_text', 'edit_draft'}:
            label = {'reasoning': event.get('model', ''), 'model_text': 'Ответ модели',
                     'edit_draft': f"Черновик · {event.get('name', '')}"}[kind]
            item = (event['request_id'], kind, event.get('item_id'))
            if self.stream_item != item:
                self.append(self.reasoning, ('\n\n' if self.stream_item else '') + label + '\n')
                self.stream_item = item
            self.append(self.reasoning, event['text'])
            self.activity = label
        elif kind == 'model':
            self.activity = 'Ожидаю ответа · ' + event['model']
            self.output.appendPlainText('Модель: ' + event['model'])
        elif kind == 'tool':
            self.activity = 'Выполняется · ' + event['name']
            self.output.appendPlainText('Инструмент: ' + event['name'])
        elif kind == 'started':
            self.output.appendPlainText(event['topic'])
        elif kind == 'files':
            self.output.appendPlainText('Изменены: ' + ', '.join(event['paths']))
        elif kind == 'verification':
            self.output.appendPlainText('Проверка ' + event['name'] + (' прошла' if event['passed'] else ' не прошла'))
        elif kind == 'usage':
            self.status.setText(f"{event['tokens']} токенов · ${event['spent']:.4f}")
        elif kind == 'project_usage_error':
            self.output.appendPlainText('Статистика проекта не сохранена: ' + event['message'])
        elif kind == 'stopped':
            if event.get('reason'):
                self.output.appendPlainText(str(event['reason']))
            if 'cost' in event:
                self.write_cost(event['cost'])

    def write_cost(self, cost):
        suffix = '' if cost['complete'] else ' (данные неполные)'
        self.output.appendPlainText(f"Расход за запуск: ${cost['estimated_usd']:.5f}{suffix}")

    def stop(self):
        if self.task and not self.task.done() and not self.task.cancelling():
            self.activity = 'Останавливаю работу'
            self.stop_button.setEnabled(False)
            self.task.cancel()

    def closeEvent(self, event):
        if self.task:
            event.ignore()
            self.closing = True
            self.stop()
        else:
            self.timer.stop()
            event.accept()

    def tick(self):
        self.loop.call_soon(self.loop.stop)
        self.loop.run_forever()
        if not self.task:
            return
        if not self.task.done():
            minutes, seconds = divmod(int(time.monotonic() - self.started_at), 60)
            self.activity_label.setText(f'{self.activity} · {minutes:02d}:{seconds:02d}')
            return
        if not self.task.cancelled() and self.task.exception():
            self.output.appendPlainText('Ошибка выполнения: ' + type(self.task.exception()).__name__)
        self.task = None
        if self.closing:
            self.close()
            return
        self.progress_bar.hide()
        self.activity_label.clear()
        self.set_busy(False)
        self.reload_projects()
        self.refresh()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace', type=Path)
    args = parser.parse_args()
    qt = QApplication([])
    qt.setStyle('Fusion')
    palette = QPalette()
    for role, color in {
        'Window': '#202326', 'Base': '#16181a', 'AlternateBase': '#25292d',
        'WindowText': '#e8eaf0', 'Text': '#e8eaf0', 'Button': '#2a2e34',
        'ButtonText': '#e8eaf0', 'Highlight': '#3b67a2', 'HighlightedText': '#ffffff',
    }.items():
        palette.setColor(getattr(QPalette.ColorRole, role), QColor(color))
    for role in ('WindowText', 'Text', 'ButtonText'):
        palette.setColor(QPalette.ColorGroup.Disabled, getattr(QPalette.ColorRole, role), QColor('#858993'))
    qt.setPalette(palette)
    qt.setStyleSheet("""
        QPushButton:enabled:hover, QToolButton:enabled:hover { background: #3a4859; }
        QPushButton:enabled:pressed, QToolButton:enabled:pressed { background: #315475; }
        QTreeView::item:hover:!selected { background: #303b48; }
        QTabBar::tab:hover:!selected { background: #3a4859; }
    """)
    data = DesktopData(Path(__file__).resolve().parent)
    try:
        data.initialize()
        preferences = data.load_preferences()
        if not preferences.get('migration_prompted') and data.has_legacy_data():
            answer = QMessageBox.question(None, 'Orchestra', 'Импортировать настройки и сохранённые задачи?')
            if answer == QMessageBox.StandardButton.Yes:
                data.import_legacy()
            preferences['migration_prompted'] = True
            data.save_preferences(preferences)
    except OSError as error:
        QMessageBox.warning(None, 'Orchestra', str(error))
        return
    with asyncio.Runner() as runner:
        window = DesktopWindow(data, runner.get_loop(), args.workspace)
        window.show()
        qt.exec()


if __name__ == '__main__':
    main()
