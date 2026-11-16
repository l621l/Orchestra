"""Qt desktop entry point for Orchestra."""
import argparse
import asyncio
import json
import re
import shutil
import sys
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from PySide6.QtCore import QByteArray, QRectF, Qt, QTimer, QUrl
from PySide6.QtGui import (
    QColor, QDesktopServices, QFont, QIcon, QKeySequence, QPainter, QPalette, QPixmap,
    QShortcut, QTextCursor,
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QFileDialog, QFileSystemModel, QFormLayout,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QSplitter,
    QTabWidget, QTreeView, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from orchestra_core.desktop.desktop_data import DesktopData
from orchestra_core.workspace.interface_notes import atomic_text
from orchestra_core.runtime.orchestra_app import OrchestraApp
from orchestra_core.runtime.orchestra_config import completion_review_enabled, normalize_settings, project_configuration, set_completion_review
from orchestra_core.runtime.turn_trace import TurnTrace
from orchestra_core.workspace.tool_errors import ToolError
from orchestra_core.workspace.project_usage import IGNORE_RULE, read_usage
from orchestra_core.workspace.project_data import SETTINGS, preserve_legacy_checkout_changes, project_data_path
from orchestra_core.desktop.qt_git_graph import GitGraph
from orchestra_core.desktop.qt_settings import SettingsDialog
from orchestra_core.desktop.qt_statistics import UsageDialog
from orchestra_core.workspace.workspace_lease import workspace_lease


PROJECT_GITIGNORE = f"""# Local Orchestra data
/INTERFACE_NOTES.json
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


class OutputText(QPlainTextEdit):
    def contextMenuEvent(self, event):
        menu = QMenu(self)
        copy = menu.addAction('Copy', self.copy)
        copy.setEnabled(self.textCursor().hasSelection())
        copy.setShortcut(QKeySequence.StandardKey.Copy)
        select = menu.addAction('Select All', self.selectAll)
        select.setEnabled(not self.document().isEmpty())
        select.setShortcut(QKeySequence.StandardKey.SelectAll)
        menu.exec(event.globalPos())


class TaskPrompt(QPlainTextEdit):
    def __init__(self):
        super().__init__()
        self.history = []
        self.history_index = -1
        self.history_draft = ''
        self.textChanged.connect(self.reset_history_navigation)

    def reset_history_navigation(self):
        self.history_index = -1

    def set_history(self, history):
        self.history = history
        self.reset_history_navigation()

    def draft_text(self):
        return self.history_draft if self.history_index >= 0 else self.toPlainText()

    def keyPressEvent(self, event):
        key = event.key()
        if (not self.history or event.modifiers() not in (
                Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.KeypadModifier)
                or key not in (Qt.Key.Key_Up, Qt.Key.Key_Down)):
            return super().keyPressEvent(event)

        older = key == Qt.Key.Key_Up
        index = self.history_index
        if index < 0:
            cursor = self.textCursor()
            if not older or cursor.hasSelection() or cursor.movePosition(QTextCursor.MoveOperation.Up):
                return super().keyPressEvent(event)
            self.history_draft = self.toPlainText()
            if self.history_draft == self.history[0] and len(self.history) > 1:
                index = 0

        self.history_index = min(index + 1, len(self.history) - 1) if older else index - 1
        text = self.history[self.history_index] if self.history_index >= 0 else self.history_draft
        blocked = self.blockSignals(True)
        try:
            self.setPlainText(text)
            self.moveCursor(QTextCursor.MoveOperation.End)
        finally:
            self.blockSignals(blocked)
        event.accept()


class ProjectFilesModel(QFileSystemModel):
    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole or index.column() != 1:
            return super().data(index, role)
        if self.isDir(index):
            return ''
        size = self.size(index)
        if size < 0:
            return ''
        if size < 1000:
            return f'{size} b'
        for factor, unit in ((10**12, 'TB'), (10**9, 'GB'), (10**6, 'MB'), (1000, 'KB')):
            if size >= factor:
                value = f'{size / factor:,.1f}'.removesuffix('.0')
                return f'{value} {unit}'


class ProjectTree(QTreeWidget):
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            item = self.itemAt(event.position().toPoint())
            if item is not None and item.childCount():
                if item.parent() is None:
                    current = self.currentItem()
                    while current is not None and current.parent() is not None:
                        current = current.parent()
                    if current is not item:
                        self.setCurrentItem(item)
                        event.accept()
                        return
                item.setExpanded(not item.isExpanded())
                if item.flags() & Qt.ItemFlag.ItemIsSelectable:
                    self.setCurrentItem(item)
                event.accept()
                return
        super().mousePressEvent(event)


def run_timestamp(directory, summary):
    try:
        stamp = datetime.fromisoformat(summary.get('started_at') or '')
    except (TypeError, ValueError):
        try:
            stamp = datetime.strptime(directory.name.split('_')[1], '%Y%m%dT%H%M%SZ')
        except (IndexError, ValueError):
            return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone()


@dataclass
class ProjectRun:
    started_at: float = field(default_factory=time.monotonic)
    activity: str = 'Starting'
    task: asyncio.Task | None = None
    view: tuple[str, str] = ('', '')
    pending: list = field(default_factory=list)
    stream_item: tuple | None = None
    reasoning_escape: str = ''
    status: str = ''


class DesktopWindow(QMainWindow):
    def __init__(self, data, loop, workspace=None):
        super().__init__()
        self.data = data
        self.loop = loop
        self.runs = {}
        self.closing = False
        self.preferences = data.load_preferences()
        saved_view = self.preferences.get('window_view', {})
        if not isinstance(saved_view, dict):
            saved_view = {}
        self.task_prompts = self.preferences.setdefault('task_prompts', {})
        if not isinstance(self.task_prompts, dict):
            self.task_prompts = self.preferences['task_prompts'] = {}
        self.task_prompt_history = self.preferences.setdefault('task_prompt_history', {})
        if not isinstance(self.task_prompt_history, dict):
            self.task_prompt_history = self.preferences['task_prompt_history'] = {}
        saved_workspace = saved_view.get('workspace')
        legacy_draft = saved_view.get('draft')
        if isinstance(saved_workspace, str) and saved_workspace and isinstance(legacy_draft, str) and legacy_draft.strip():
            self.task_prompts.setdefault(self.task_prompt_key(saved_workspace), legacy_draft)
        if workspace is None and isinstance(saved_view.get('workspace'), str):
            workspace = saved_view['workspace']
        self.workspace = Path(workspace).resolve() if workspace else None
        self.selected_run = None
        self.setWindowTitle('Orchestra')
        self.resize(1120, 720)
        split = self.main_split = QSplitter()
        split.setHandleWidth(12)
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
        self.button(brand, 'Global Settings', self.open_global_settings)
        sidebar.addLayout(brand)
        sidebar.addWidget(QLabel('Projects'))
        self.project_actions = []
        for label, callback in (
            ('Create Project', self.open_project_dialog),
            ('GIT', self.show_git_history),
            ('Usage', self.show_usage),
        ):
            self.project_actions.append(self.button(sidebar, label, callback))
        self.projects = ProjectTree()
        self.projects.setColumnCount(2)
        self.projects.setTreePosition(1)
        self.projects.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.projects.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.projects.setHeaderHidden(True)
        self.projects.setMouseTracking(True)
        self.projects.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
        self.projects.setExpandsOnDoubleClick(False)
        self.projects.setAnimated(False)
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
        stack = self.task_split = QSplitter(Qt.Orientation.Vertical)
        stack.setHandleWidth(12)
        stack.setChildrenCollapsible(False)
        layout.addWidget(stack, 1)
        editor = QWidget()
        editor_layout = QVBoxLayout(editor)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        stack.addWidget(editor)
        self.prompt = TaskPrompt()
        self.prompt.setToolTip('Up/Down browse task history at the first line.')
        self.prompt.setPlaceholderText('Enter a task')
        editor_layout.addWidget(self.prompt, 1)
        actions = QHBoxLayout()
        editor_layout.addLayout(actions)
        self.start_button = self.button(actions, 'New Task', lambda: self.start('new_task'))
        self.continue_button = self.button(actions, 'Continue', lambda: self.start('continue_task'))
        self.with_debug = QCheckBox('With Debug')
        self.with_debug.setToolTip('Run a completion review after New Task or Continue.')
        actions.addWidget(self.with_debug)
        self.stop_button = self.button(actions, 'Stop', self.stop)
        self.stop_button.setEnabled(False)
        actions.addStretch()
        self.review_button = self.button(actions, 'Debug Only', lambda: self.start('review_task'))
        self.review_button.setToolTip('Audit the current project using the prompt and what_to_do.md.')
        self.project_settings_button = self.button(actions, 'Project Settings', self.open_settings)
        self.with_debug.toggled.connect(self.save_debug_checkbox)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.hide()
        editor_layout.addWidget(self.progress_bar)
        self.activity_label = QLabel()
        editor_layout.addWidget(self.activity_label)
        tabs = self.tabs = QTabWidget()
        stack.addWidget(tabs)
        stack.setSizes([380, 230])
        self.output = self.text_tab(tabs, 'Log')
        self.reasoning = self.text_tab(tabs, 'Reasoning')
        tabs.addTab(self.files_tab(), 'Project Files')
        self.reload_projects()
        self.restore_view(saved_view)
        self.restore_task_prompt()
        self.refresh()
        fullscreen = QShortcut(QKeySequence('F11'), self)
        fullscreen.activated.connect(self.toggle_fullscreen)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(20)

    def toggle_fullscreen(self):
        self.setWindowState(self.windowState() ^ Qt.WindowState.WindowFullScreen)

    def restore_view(self, view):
        for key, restore in (('geometry', self.restoreGeometry),
                             ('main_split', self.main_split.restoreState),
                             ('task_split', self.task_split.restoreState)):
            encoded = view.get(key)
            if isinstance(encoded, str):
                restore(QByteArray.fromBase64(encoded.encode('utf-8')))
        tab = view.get('tab')
        if type(tab) is int and 0 <= tab < self.tabs.count():
            self.tabs.setCurrentIndex(tab)
        expanded = view.get('expanded_projects')
        if isinstance(expanded, list):
            for index in range(self.projects.topLevelItemCount()):
                item = self.projects.topLevelItem(index)
                item.setExpanded(item.data(0, Qt.ItemDataRole.UserRole)[0] in expanded)

    def save_view(self):
        self.remember_task_prompt()
        self.preferences['window_view'] = {
            'geometry': bytes(self.saveGeometry().toBase64()).decode('ascii'),
            'main_split': bytes(self.main_split.saveState().toBase64()).decode('ascii'),
            'task_split': bytes(self.task_split.saveState().toBase64()).decode('ascii'),
            'tab': self.tabs.currentIndex(),
            'workspace': str(self.workspace) if self.workspace else None,
            'expanded_projects': [
                item.data(0, Qt.ItemDataRole.UserRole)[0]
                for index in range(self.projects.topLevelItemCount())
                if (item := self.projects.topLevelItem(index)).isExpanded()
            ],
        }
        self.data.save_preferences(self.preferences)

    def task_prompt_key(self, workspace):
        return project_data_path(self.data.logs, workspace).name

    def remember_task_prompt(self):
        text = self.prompt.draft_text()
        if self.workspace and text.strip():
            self.task_prompts[self.task_prompt_key(self.workspace)] = text

    def restore_task_prompt(self):
        history = self.task_history()
        self.prompt.set_history(history)
        text = ''
        if self.workspace:
            key = self.task_prompt_key(self.workspace)
            text = self.task_prompts.get(key, '')
            if not isinstance(text, str) or not text.strip():
                text = history[0] if history else ''
        self.prompt.setPlainText(text)

    def task_history(self):
        if not self.workspace:
            return []
        records = self.task_prompt_history.get(self.task_prompt_key(self.workspace), [])
        saved = [text for text in records if isinstance(text, str) and text.strip()] if isinstance(records, list) else []
        previews = {TurnTrace.preview(text) for text in saved}
        requests = (summary.get('request') for _, summary in self.project_history.get(str(self.workspace), ()))
        previous = (text for text in requests if isinstance(text, str) and text.strip()
                    and text not in previews and text not in {'continue', 'review'})
        return list(dict.fromkeys((*saved, *previous)))[:100]

    def select_workspace(self, workspace):
        if workspace != self.workspace:
            self.remember_task_prompt()
            self.workspace = workspace
            self.restore_task_prompt()

    @staticmethod
    def button(layout, label, callback):
        button = QPushButton(label)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(callback)
        layout.addWidget(button)
        return button

    @staticmethod
    def text_tab(tabs, label):
        text = OutputText()
        text.setReadOnly(True)
        tabs.addTab(text, label)
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
        history = self.project_history = self.data.history()
        hidden = set(self.preferences.get('hidden_workspaces', []))
        registered = self.preferences.get('workspaces', [])
        activity = self.preferences.get('workspace_activity', {})
        paths = dict.fromkeys([*registered, *history, *([str(self.workspace)] if self.workspace else [])])
        paths = sorted(paths, key=lambda path: max(
            activity.get(path, ''), history[path][0][0].name[:21] if history.get(path) else ''),
            reverse=True)
        expanded = {
            item.data(0, Qt.ItemDataRole.UserRole)[0]
            for index in range(self.projects.topLevelItemCount())
            if (item := self.projects.topLevelItem(index)).isExpanded()
        }
        collapsed_days = {
            (item.data(0, Qt.ItemDataRole.UserRole)[0], day.text(1))
            for index in range(self.projects.topLevelItemCount())
            if (item := self.projects.topLevelItem(index))
            for child in range(item.childCount())
            if not (day := item.child(child)).isExpanded()
        }
        self.projects.blockSignals(True)
        self.projects.clear()
        active = None
        selected_history = None
        for path in paths:
            if path in hidden or not Path(path).is_dir():
                continue
            item = QTreeWidgetItem(self.projects, ['', Path(path).name or path])
            if run := self.runs.get(Path(path).resolve()):
                if run.task is not None:
                    item.setText(0, '●')
            item.setToolTip(1, path)
            item.setData(0, Qt.ItemDataRole.UserRole, (path, None))
            days = {}
            runs = [(run_timestamp(directory, summary), directory, summary)
                    for directory, summary in history.get(path, ())]
            runs.sort(key=lambda run: (-run[0].date().toordinal(), run[0].timestamp())
                      if run[0] else (0, 0))
            for stamp, directory, summary in runs:
                date = stamp.strftime('%d %b %Y') if stamp else 'Unknown date'
                if date not in days:
                    day = days[date] = QTreeWidgetItem(item, ['', date])
                    day.setFlags(day.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                    day.setExpanded((path, date) not in collapsed_days)
                topic = summary.get('topic') or directory.name
                run = QTreeWidgetItem(days[date], [stamp.strftime('%H:%M') if stamp else '', topic])
                run.setToolTip(1, topic)
                run.setData(0, Qt.ItemDataRole.UserRole, (path, (directory, summary)))
                if path == str(self.workspace) and self.selected_run and directory == self.selected_run[0]:
                    selected_history = run
            item.setExpanded(path in expanded)
            if path == str(self.workspace):
                active = item
        active = active or self.projects.topLevelItem(0)
        workspace = None
        if active:
            self.projects.setCurrentItem(selected_history or active)
            workspace = Path(active.data(0, Qt.ItemDataRole.UserRole)[0])
        if workspace != self.workspace:
            self.store_run_view()
            self.select_workspace(workspace)
            self.selected_run = None
            self.output.clear()
            self.reasoning.clear()
            self.restore_run_view()
        self.projects.blockSignals(False)

    def select_project(self, item, _previous):
        if item is None or item.data(0, Qt.ItemDataRole.UserRole) is None:
            return
        self.store_run_view()
        path, self.selected_run = item.data(0, Qt.ItemDataRole.UserRole)
        self.select_workspace(Path(path))
        self.output.clear()
        self.reasoning.clear()
        self.restore_run_view()
        if self.selected_run:
            directory, summary = self.selected_run
            self.output.appendPlainText(summary.get('request') or summary.get('topic', ''))
            if summary.get('answer_file') == 'answer.md':
                try:
                    self.output.appendPlainText((directory / 'answer.md').read_text(encoding='utf-8'))
                except (OSError, UnicodeError):
                    self.output.appendPlainText('Answer unavailable.')
            usage = summary.get('usage', {})
            self.output.appendPlainText(f"Tokens: {usage.get('total_tokens', 0)} · {directory}")
            if summary.get('cost'):
                self.write_cost(summary['cost'])
        self.refresh()

    def project_menu(self, position):
        item = self.projects.itemAt(position)
        if not item or item.parent():
            return
        menu = QMenu(self)
        hide = menu.addAction('Hide Project')
        path, _ = item.data(0, Qt.ItemDataRole.UserRole)
        run = self.runs.get(Path(path).resolve())
        hide.setEnabled(run is None or run.task is None)
        if menu.exec(self.projects.viewport().mapToGlobal(position)) == hide:
            hidden = self.preferences.setdefault('hidden_workspaces', [])
            if path not in hidden:
                hidden.append(path)
            self.data.save_preferences(self.preferences)
            if self.workspace == Path(path):
                self.store_run_view()
                self.select_workspace(None)
                self.selected_run = None
                self.output.clear()
                self.reasoning.clear()
            self.reload_projects()
            self.refresh()

    def pick_folder(self, title, accept, parent=None, *, initial_directory=''):
        dialog = QFileDialog(parent if parent is not None else self)
        dialog.setOption(QFileDialog.Option.DontUseNativeDialog)
        dialog.setFileMode(QFileDialog.FileMode.Directory)
        dialog.setOption(QFileDialog.Option.ShowDirsOnly)
        dialog.setWindowTitle(title)
        initial = Path(initial_directory.strip()).expanduser() if initial_directory.strip() else None
        if initial is None or not initial.is_dir():
            initial = self.workspace if self.workspace and self.workspace.is_dir() else Path.home()
        dialog.setDirectory(str(initial))
        dialog.setLabelText(QFileDialog.DialogLabel.Accept, accept)
        dialog.setLabelText(QFileDialog.DialogLabel.Reject, 'Cancel')
        if dialog.exec():
            return Path(dialog.selectedFiles()[0]).resolve()
        return None

    def open_project_dialog(self):
        dialog = QDialog(self)
        dialog.setWindowTitle('Create Project')
        dialog.setMinimumWidth(500)
        form = QFormLayout(dialog)
        name = QLineEdit()
        location = QLineEdit(str(self.workspace.parent if self.workspace else Path.home()))
        location_row = QHBoxLayout()
        location_row.addWidget(location)
        initialize_git = QCheckBox('Initialize Git repository')
        initialize_git.setChecked(True)
        error_label = QLabel()
        error_label.setTextFormat(Qt.TextFormat.PlainText)
        error_label.setWordWrap(True)

        def browse():
            folder = self.pick_folder('Parent folder', 'Select', dialog, initial_directory=location.text())
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
            folder = self.pick_folder('Add Existing Project', 'Add', dialog, initial_directory=location.text())
            if folder is None:
                return
            try:
                self.register_project(folder)
            except OSError as error:
                error_label.setText(str(error))
                return
            dialog.accept()

        self.button(location_row, 'Select', browse)
        form.addRow('Project name', name)
        form.addRow('Parent folder', location_row)
        form.addRow(initialize_git)
        form.addRow(error_label)
        actions = QHBoxLayout()
        self.button(actions, 'Add Existing', add_existing).setAutoDefault(False)
        actions.addStretch()
        self.button(actions, 'Cancel', dialog.reject)
        self.button(actions, 'Create', create).setDefault(True)
        form.addRow(actions)
        name.setFocus()
        dialog.exec()

    def make_project(self, location, name, initialize_git):
        name = name.strip()
        if not name or name in {'.', '..'} or any(c in name for c in '/\\:'):
            raise ValueError('Enter a single folder name for the project.')
        if not location.strip():
            raise ValueError('Select a parent folder.')
        parent = Path(location.strip()).expanduser().resolve()
        if not parent.is_dir():
            raise ValueError('Parent folder not found.')
        folder = parent / name
        try:
            folder.mkdir()
        except FileExistsError:
            raise ValueError('That folder already exists. Choose Add Existing.') from None
        if initialize_git:
            try:
                with workspace_lease(folder):
                    repo = self.git('rev-parse', '--is-inside-work-tree', check=False, folder=folder)
                    if repo.stdout.strip() != 'true' and not (folder / '.git').exists():
                        self.git('init', folder=folder)
                        self.git('symbolic-ref', 'HEAD', 'refs/heads/main', folder=folder)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                try:
                    folder.rmdir()
                except OSError:
                    raise ValueError(f'The folder was created, but Git could not be initialized. {error}') from error
                raise
        with (folder / '.gitignore').open('x', encoding='utf-8') as ignore:
            ignore.write(PROJECT_GITIGNORE)
        return folder

    def register_project(self, folder):
        self.store_run_view()
        self.select_workspace(folder)
        path = str(self.workspace)
        workspaces = self.preferences.setdefault('workspaces', [])
        if path not in workspaces:
            workspaces.insert(0, path)
        self.preferences['hidden_workspaces'] = [
            value for value in self.preferences.get('hidden_workspaces', []) if value != path]
        self.touch_project()
        self.selected_run = None
        self.reload_projects()
        self.output.clear()
        self.reasoning.clear()
        self.restore_run_view()
        self.refresh()

    def touch_project(self):
        stamp = datetime.now(timezone.utc).strftime('turn_%Y%m%dT%H%M%SZ')
        self.preferences.setdefault('workspace_activity', {})[str(self.workspace)] = stamp
        self.data.save_preferences(self.preferences)

    def session(self):
        if self.workspace is None or not self.workspace.is_dir():
            raise ValueError('Select an existing workspace folder.')
        return OrchestraApp.from_directory(self.data.root, self.workspace, logs_path=self.data.logs)

    def refresh(self):
        self.path.setText(str(self.workspace or ''))
        self.refresh_files()
        self.refresh_project_cost()
        self.refresh_run_controls()
        self.sync_debug_checkbox()
        if self.task and not self.selected_run:
            self.status.setText(self.current_run.status or 'Task running')
            return
        if self.selected_run:
            self.status.setText('Viewing a past run. Select the project to continue.')
            return
        if not self.data.config.is_file():
            self.status.setText('Open Global Settings and configure a provider.')
            return
        try:
            state = self.session().status()
        except (OSError, ValueError, KeyError) as error:
            self.status.setText(str(error))
            return
        labels = {'new': 'New workspace', 'pause': 'Ready to resume',
                  'done': 'Task complete', 'state_error': 'Could not read saved task'}
        text = 'A task is already running in this workspace.' if state['running'] else labels.get(state['status'], state['status'])
        if 'budget_spent' in state:
            text += f" · Cost: ${state['budget_spent']:.5f}/${state['budget_limit']:.2f}"
        self.status.setText(text)

    def read_project_usage(self):
        return read_usage(project_data_path(self.data.logs, self.workspace),
                          legacy_workspace=self.workspace)

    def refresh_project_cost(self):
        try:
            data = self.read_project_usage() if self.workspace else None
            total = data['total'] if data else {}
            if total:
                suffix = '' if total['complete'] else ' · incomplete data'
                self.project_cost.setText(f"Project total: ${total['estimated_usd']:.5f}{suffix}")
            else:
                self.project_cost.clear()
        except (OSError, ValueError, KeyError, TypeError):
            self.project_cost.setText('Project usage is unavailable.')

    def show_usage(self):
        if self.workspace is None:
            QMessageBox.information(self, 'Project Usage', 'Select a project.')
            return
        try:
            data = self.read_project_usage()
            if not data['runs']:
                QMessageBox.information(self, 'Project Usage', 'No usage recorded yet.')
                return
            dialog = UsageDialog(self, data)
        except (OSError, ValueError, KeyError, TypeError) as error:
            QMessageBox.warning(self, 'Project Usage', str(error))
            return
        dialog.exec()
        dialog.deleteLater()

    def project_settings_path(self):
        return project_data_path(self.data.logs, self.workspace) / SETTINGS if self.workspace else None

    def sync_debug_checkbox(self):
        path = self.project_settings_path()
        try:
            defaults = normalize_settings(json.loads((self.data.root / 'settings.json').read_text(encoding='utf-8')))
            _, settings = project_configuration(path, {}, defaults)
            enabled = path is not None and completion_review_enabled(settings['direct'])
        except (OSError, ValueError, AttributeError):
            self.with_debug.setEnabled(False)
            return
        signals_blocked = self.with_debug.blockSignals(True)
        self.with_debug.setChecked(enabled)
        self.with_debug.blockSignals(signals_blocked)
        self.with_debug.setEnabled(path is not None and not self.task)

    def save_debug_checkbox(self, enabled):
        path = self.project_settings_path()
        if path is None:
            return
        try:
            defaults = normalize_settings(json.loads((self.data.root / 'settings.json').read_text(encoding='utf-8')))
            try:
                project = json.loads(path.read_text(encoding='utf-8'))
            except FileNotFoundError:
                project = {}
            direct = project.setdefault('settings', {}).setdefault('direct', defaults['direct'])
            set_completion_review(direct, enabled)
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_text(path, json.dumps(project, ensure_ascii=False, indent=2))
        except (OSError, ValueError, AttributeError) as error:
            self.sync_debug_checkbox()
            QMessageBox.warning(self, 'Orchestra Settings', str(error))

    def open_settings(self):
        path = self.project_settings_path()
        if path is not None and not self.task:
            self.show_settings(project_path=path)

    def open_global_settings(self):
        self.show_settings(global_only=True)

    def show_settings(self, **scope):
        try:
            if SettingsDialog(self, self.data, **scope).exec():
                self.refresh()
        except (OSError, ValueError, KeyError, TypeError) as error:
            QMessageBox.warning(self, 'Orchestra Settings', str(error))

    def git(self, *args, check=True, folder=None):
        folder = self.workspace if folder is None else folder
        if folder is None:
            raise ValueError('Select a project.')
        if shutil.which('git') is None:
            install = ('Install Git for Windows from https://git-scm.com/install/windows'
                       if sys.platform == 'win32' else
                       'On Ubuntu or Debian, run: sudo apt install git. '
                       'On other distributions, install Git through your package manager.')
            raise ValueError(f'Git was not found on PATH. {install} Then restart Orchestra.')
        result = subprocess.run(['git', '-C', str(folder), *args], stdin=subprocess.DEVNULL,
                                capture_output=True, encoding='utf-8', errors='replace', timeout=30,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if check and result.returncode:
            raise ValueError(result.stderr.strip() or 'Git command failed.')
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
            QMessageBox.warning(self, 'Git History', str(error))
            return
        dialog = QDialog(self)
        dialog.setWindowTitle('GIT')
        dialog.resize(820, 500)
        layout = QVBoxLayout(dialog)
        graph = GitGraph(history, dialog, head=head.stdout.strip() if head.returncode == 0 else None)
        layout.addWidget(graph)
        actions = QHBoxLayout()
        layout.addLayout(actions)
        self.button(actions, 'Set Git Remote (origin)', self.set_origin)
        self.button(actions, 'Open Remote', lambda: self.open_origin(dialog))
        actions.addStretch()

        def create_from_head():
            if self.create_branch():
                dialog.accept()

        def create_from_selection():
            rows = graph.scene().selectedItems()
            if rows and self.create_branch(rows[0].commit):
                dialog.accept()

        from_head = self.button(actions, 'Branch from HEAD', create_from_head)
        from_head.setEnabled(head.returncode == 0)
        selected = self.button(actions, 'Branch from Selected Commit', create_from_selection)
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

    def files_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        self.files_hint = QLabel('Select a project folder.')
        layout.addWidget(self.files_hint)
        model = self.files_model = ProjectFilesModel(panel)
        model.setReadOnly(True)
        tree = self.files_tree = QTreeView(panel)
        tree.setModel(model)
        self.files_root = None
        tree.setColumnWidth(0, 350)
        tree.setMouseTracking(True)
        tree.entered.connect(lambda _: tree.viewport().setCursor(Qt.CursorShape.PointingHandCursor))
        tree.viewportEntered.connect(lambda: tree.viewport().unsetCursor())

        def open_file(index):
            if not index.isValid():
                return
            path = Path(model.filePath(index))
            if path.is_file() and not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
                QMessageBox.warning(panel, 'Project Files',
                                    'Could not open the file in its default application.')

        tree.doubleClicked.connect(open_file)
        for key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            shortcut = QShortcut(QKeySequence(key), tree)
            shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
            shortcut.activated.connect(lambda: open_file(tree.currentIndex()))
        layout.addWidget(tree)
        return panel

    def refresh_files(self):
        root = str(self.workspace) if self.workspace and self.workspace.is_dir() else None
        self.files_tree.setVisible(root is not None)
        self.files_hint.setVisible(root is None)
        if root is not None and root != self.files_root:
            self.files_tree.setRootIndex(self.files_model.setRootPath(root))
        self.files_root = root

    def create_branch(self, commit=None):
        branch, accepted = QInputDialog.getText(self, 'Git', 'New branch name')
        if accepted and branch.strip():
            return self.git_change('checkout', '-b', branch.strip(), *([commit] if commit else []))
        return False

    def set_origin(self):
        try:
            current = self.git('remote', 'get-url', 'origin', check=False)
            url, accepted = QInputDialog.getText(self, 'Git', 'Remote URL (origin)', text=current.stdout.strip())
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
                raise ValueError('Set origin to a remote repository URL.')
            if not QDesktopServices.openUrl(QUrl(address)):
                raise ValueError('Could not open the remote repository in the browser.')
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            QMessageBox.warning(parent, 'Git', str(error))

    def git_change(self, *args):
        try:
            if self.workspace is None:
                raise ValueError('Select a project.')
            with workspace_lease(self.workspace):
                if args and args[0] in {'switch', 'checkout'}:
                    preserve_legacy_checkout_changes(
                        self.workspace, project_data_path(self.data.logs, self.workspace), self.git)
                self.git(*args)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            QMessageBox.warning(self, 'Git', str(error))
            return False
        try:
            self.touch_project()
        except OSError as error:
            QMessageBox.warning(self, 'Orchestra', str(error))
        self.reload_projects()
        self.refresh()
        return True

    def ensure_git_author(self, options):
        if not (options.get('git_tools_enabled') and options.get('allow_git_mutations')):
            return True
        if self.git('rev-parse', '--git-dir', check=False).returncode:
            return True
        identities = ('GIT_AUTHOR_IDENT', 'GIT_COMMITTER_IDENT')
        if all(self.git('var', identity, check=False).returncode == 0 for identity in identities):
            return True
        values = {}
        for key, label in (('user.name', 'Name shown on commits'), ('user.email', 'Email shown on commits')):
            existing = self.git('config', '--get', key, check=False).stdout.strip()
            value, accepted = QInputDialog.getText(self, 'Git author for this repository', label, text=existing)
            if not accepted:
                return False
            if not value.strip():
                raise ValueError(label + ' is required.')
            values[key] = value.strip()
        for key, value in values.items():
            self.git('config', '--local', '--', key, value)
        if any(self.git('var', identity, check=False).returncode for identity in identities):
            raise ValueError('Git still cannot determine the author or committer.')
        return True

    def ensure_git_push(self, engine, options):
        if not (options.get('git_tools_enabled') and options.get('allow_git_mutations')
                and options.get('allow_git_push', True) and engine.repository.available):
            return True
        try:
            engine.repository.check_push()
        except ToolError as error:
            prompt = QMessageBox(self)
            prompt.setWindowTitle('Git push unavailable')
            prompt.setTextFormat(Qt.TextFormat.PlainText)
            prompt.setText(str(error))
            skip = prompt.addButton('Run without push', QMessageBox.ButtonRole.AcceptRole)
            prompt.addButton(QMessageBox.StandardButton.Cancel)
            prompt.exec()
            if prompt.clickedButton() is not skip:
                return False
            options['allow_git_push'] = False
        return True

    def start(self, method):
        if self.task or self.closing:
            return
        text = self.prompt.toPlainText().strip()
        if method == 'new_task' and not text:
            self.status.setText('Enter a task.')
            return
        try:
            app = self.session()
            options = app.engine.settings.get('tools', {})
            if not self.ensure_git_author(options) or not self.ensure_git_push(app.engine, options):
                return
            if text:
                key = self.task_prompt_key(self.workspace)
                previous = self.task_prompt_history.get(key, [])
                if not isinstance(previous, list):
                    previous = []
                self.task_prompt_history[key] = [text, *(item for item in previous if item != text)][:100]
                self.task_prompts[key] = self.prompt.toPlainText()
                self.prompt.set_history(self.task_history())
            self.data.save_preferences(self.preferences)
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
            QMessageBox.warning(self, 'Orchestra', str(error))
            return
        if self.selected_run:
            self.output.clear()
        self.selected_run = None
        workspace = self.workspace.resolve()
        run = ProjectRun(view=(self.output.toPlainText(), ''))
        self.runs[workspace] = run
        self.reasoning.clear()
        run.task = self.loop.create_task(self.run(app, method, text, workspace))
        self.reload_projects()
        self.refresh_run_controls()

    @property
    def current_run(self):
        return self.runs.get(self.workspace.resolve()) if self.workspace else None

    @property
    def task(self):
        run = self.current_run
        return run.task if run else None

    def refresh_run_controls(self):
        task = self.task
        busy = task is not None
        for widget in (self.start_button, self.review_button):
            widget.setEnabled(not busy and not self.closing)
        self.continue_button.setEnabled(not busy and not self.closing and self.selected_run is None)
        self.stop_button.setEnabled(busy and not task.done() and not task.cancelling())
        self.with_debug.setEnabled(self.workspace is not None and not busy)
        self.project_settings_button.setEnabled(self.workspace is not None and not busy)
        self.progress_bar.setVisible(busy)
        self.update_activity()

    def viewing_run(self, workspace):
        return (self.workspace is not None and self.workspace.resolve() == workspace
                and self.selected_run is None)

    def store_run_view(self):
        if self.current_run and self.selected_run is None:
            self.current_run.view = (self.output.toPlainText(), self.reasoning.toPlainText())

    def restore_run_view(self):
        run = self.current_run
        if not run or self.selected_run is not None:
            return
        output, reasoning = run.view
        self.output.setPlainText(output)
        self.reasoning.setPlainText(reasoning)
        pending, run.pending = run.pending, []
        workspace = self.workspace.resolve()
        for kind, payload in pending:
            if kind == 'progress':
                self.progress(payload, workspace)
            else:
                self.append_run_output(payload, workspace)
        if run.task is None:
            self.flush_reasoning_escape(run)

    def append_run_output(self, text, workspace):
        if self.viewing_run(workspace):
            self.output.appendPlainText(text)
        else:
            self.runs[workspace].pending.append(('text', text))

    async def run(self, app, method, text, workspace):
        try:
            result = await getattr(app, method)(text, on_progress=lambda event: self.progress(event, workspace))
            self.append_run_output(result.get('answer') or result.get('error') or result['status'], workspace)
            self.append_run_output('Log: ' + result['log_directory'], workspace)
        except asyncio.CancelledError:
            self.append_run_output('Task stopped. You can resume it.', workspace)
        except (OSError, ValueError, RuntimeError) as error:
            self.append_run_output(str(error), workspace)
        finally:
            await asyncio.gather(*(client.close() for client in app.engine.clients.values()),
                                 return_exceptions=True)

    def progress(self, event, workspace):
        run = self.runs[workspace]
        if not self.viewing_run(workspace):
            run.pending.append(('progress', event))
            return
        kind = event['kind']
        if kind in {'reasoning', 'model_text', 'edit_draft'}:
            label = {'reasoning': event.get('model', ''), 'model_text': 'Model response',
                     'edit_draft': f"Draft · {event.get('name', '')}"}[kind]
            item = (event['request_id'], kind, event.get('item_id'))
            if run.stream_item != item:
                self.flush_reasoning_escape(run)
                self.append(self.reasoning, ('\n\n' if run.stream_item else '') + label + '\n')
                run.stream_item = item
            text = event['text']
            if kind in {'reasoning', 'edit_draft'}:
                text = self.format_reasoning_chunk(run, text, json_preview=kind == 'edit_draft')
            self.append(self.reasoning, text)
            run.activity = label
        elif kind == 'model':
            run.activity = 'Waiting for · ' + event['model']
            self.output.appendPlainText('Model: ' + event['model'])
        elif kind == 'tool':
            run.activity = 'Running · ' + event['name']
            self.output.appendPlainText('Tool: ' + event['name'])
        elif kind == 'started':
            self.output.appendPlainText(event['topic'])
        elif kind == 'files':
            self.output.appendPlainText('Changed files: ' + ', '.join(event['paths']))
        elif kind == 'verification':
            self.output.appendPlainText('Check ' + event['name'] + (' passed' if event['passed'] else ' failed'))
        elif kind == 'usage':
            run.status = f"{event['tokens']} tokens · ${event['spent']:.4f}"
            self.status.setText(run.status)
        elif kind == 'project_usage_error':
            self.output.appendPlainText('Could not save project usage: ' + event['message'])
        elif kind == 'stopped':
            if event.get('reason'):
                self.output.appendPlainText(str(event['reason']))
            if 'cost' in event:
                self.write_cost(event['cost'])

    def format_reasoning_chunk(self, run, text, *, json_preview=False):
        text = run.reasoning_escape + text
        run.reasoning_escape = ''

        def decode(match):
            escape = match.group()
            if len(escape) == 1:
                run.reasoning_escape = escape
                return ''
            if json_preview and escape[1] in ('"', '\\'):
                return escape[1]
            return {'n': '\n', 't': '\t', 'r': '\r'}.get(escape[1], escape)

        return re.sub(r'\\(?:[ntr"\\]|\Z)', decode, text)

    def flush_reasoning_escape(self, run):
        if run.reasoning_escape:
            self.append(self.reasoning, run.reasoning_escape)
            run.reasoning_escape = ''

    def write_cost(self, cost):
        suffix = '' if cost['complete'] else ' (incomplete data)'
        self.output.appendPlainText(f"Run cost: ${cost['estimated_usd']:.5f}{suffix}")

    def stop(self):
        run = self.current_run
        if run and run.task and not run.task.done() and not run.task.cancelling():
            run.activity = 'Stopping'
            run.task.cancel()
            self.refresh_run_controls()

    def closeEvent(self, event):
        active = [run.task for run in self.runs.values() if run.task is not None]
        if active:
            event.ignore()
            self.closing = True
            for task in active:
                if not task.done() and not task.cancelling():
                    task.cancel()
            self.refresh_run_controls()
        else:
            self.timer.stop()
            try:
                self.save_view()
            except OSError as error:
                QMessageBox.warning(self, 'Orchestra', f'Could not save window state.\n{error}')
            event.accept()

    def tick(self):
        self.loop.call_soon(self.loop.stop)
        self.loop.run_forever()
        finished = False
        for workspace, run in self.runs.items():
            if run.task is None or not run.task.done():
                continue
            if not run.task.cancelled() and run.task.exception():
                self.append_run_output('Run failed: ' + type(run.task.exception()).__name__, workspace)
            if self.viewing_run(workspace):
                self.flush_reasoning_escape(run)
            run.task = None
            finished = True
        if self.closing and not any(run.task is not None for run in self.runs.values()):
            self.close()
            return
        if finished:
            self.reload_projects()
            self.refresh()
        self.update_activity()

    def update_activity(self):
        run = self.current_run
        active_count = sum(project.task is not None for project in self.runs.values())
        if run and run.task is not None:
            minutes, seconds = divmod(int(time.monotonic() - run.started_at), 60)
            text = f'{run.activity} · {minutes:02d}:{seconds:02d}'
            active_count -= 1
        else:
            text = ''
        if active_count:
            text += (' · ' if text else '') + f'Other running projects: {active_count}'
        self.activity_label.setText(text)


def apply_desktop_theme(qt):
    qt.setStyle('Fusion')
    palette = QPalette()
    for role, color in {
        'Window': '#202326', 'Base': '#16181a', 'AlternateBase': '#25292d',
        'WindowText': '#e8eaf0', 'Text': '#e8eaf0', 'Button': '#2a2e34',
        'ButtonText': '#e8eaf0', 'Highlight': '#3b67a2', 'HighlightedText': '#ffffff',
        'PlaceholderText': '#a3aab7', 'ToolTipBase': '#25292d', 'ToolTipText': '#e8eaf0',
    }.items():
        palette.setColor(getattr(QPalette.ColorRole, role), QColor(color))
    for role in ('WindowText', 'Text', 'ButtonText'):
        palette.setColor(QPalette.ColorGroup.Disabled, getattr(QPalette.ColorRole, role), QColor('#858993'))
    qt.setPalette(palette)
    qt.setStyleSheet("""
        QPlainTextEdit, QTextEdit, QLineEdit {
            color: #e8eaf0;
            background-color: #16181a;
            selection-color: #ffffff;
            selection-background-color: #3b67a2;
        }
        QPlainTextEdit:disabled, QTextEdit:disabled, QLineEdit:disabled { color: #858993; }
        QComboBox QAbstractItemView {
            color: #e8eaf0;
            background-color: #16181a;
            selection-color: #ffffff;
            selection-background-color: #3b67a2;
        }
        QPushButton:enabled:hover, QToolButton:enabled:hover { background: #3a4859; }
        QPushButton:enabled:pressed, QToolButton:enabled:pressed { background: #315475; }
        QTreeView::item:hover:!selected { background: #303b48; }
        QTabBar::tab:hover:!selected { background: #3a4859; }
    """)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace', type=Path)
    parser.add_argument('--import-legacy', action='store_true',
                        help='import repository settings and saved tasks on startup')
    args = parser.parse_args()
    qt = QApplication([])
    apply_desktop_theme(qt)
    data = DesktopData(Path(__file__).resolve().parent)
    try:
        data.initialize()
        preferences = data.load_preferences()
        if args.import_legacy or (not preferences.get('migration_prompted') and data.has_legacy_data()):
            if args.import_legacy or QMessageBox.question(
                None, 'Orchestra', 'Import settings and saved tasks?'
            ) == QMessageBox.StandardButton.Yes:
                data.import_legacy()
            preferences['migration_prompted'] = True
            data.save_preferences(preferences)
        data.initialize_connection()
    except OSError as error:
        QMessageBox.warning(None, 'Orchestra', str(error))
        return
    with asyncio.Runner() as runner:
        window = DesktopWindow(data, runner.get_loop(), args.workspace)
        window.show()
        qt.exec()


if __name__ == '__main__':
    main()
