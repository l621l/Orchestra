"""Native Windows window for running and resuming Orchestra tasks."""
import argparse
import asyncio
import os
import time
import subprocess
import tkinter as tk
from pathlib import Path
from tkinter import ttk

import desktop_popups

from desktop_data import DesktopData
from desktop_settings import SettingsDialog
from desktop_shortcuts import install_editing_shortcuts
from desktop_theme import BACKGROUND, HOVER, TEXT_COLORS, apply_dark_theme
from orchestra_app import OrchestraApp
from workspace_lease import workspace_lease


class DesktopWindow:
    def __init__(self, root, data, loop, workspace):
        self.root = root
        self.data = data
        self.loop = loop
        self.task = None
        self.started_at = None
        self.activity = ''
        self.stream_item = None
        self.closing = False
        self.preferences = data.load_preferences()
        self.workspaces = self.preferences.get('workspaces', [])
        self.history = data.history()
        self.hidden_workspaces = set(self.preferences.get('hidden_workspaces', []))
        initial = str(workspace) if workspace else next(iter(self.visible_workspaces()), '')
        if workspace and self.workspace_key(initial) in self.hidden_workspaces:
            initial = next(iter(self.visible_workspaces()), '')
        self.workspace = tk.StringVar(value=initial)
        self.status = tk.StringVar()
        self.running_status = tk.StringVar()
        install_editing_shortcuts(root)
        root.title('Orchestra')
        root.minsize(900, 560)
        layout = ttk.Panedwindow(root, orient='horizontal')
        layout.pack(fill='both', expand=True)
        sidebar = ttk.Frame(layout, padding=(12, 16))
        frame = ttk.Frame(layout, padding=16)
        layout.add(sidebar, weight=0)
        layout.add(frame, weight=1)
        ttk.Label(sidebar, text='Проекты').pack(anchor='w')
        self.choose = ttk.Button(sidebar, text='+ Добавить проект', command=self.choose_folder)
        self.choose.pack(fill='x', pady=8)
        ttk.Button(sidebar, text='Создать ветку', command=self.show_git_history).pack(fill='x')
        ttk.Button(sidebar, text='Ссылка Git (origin)', command=self.set_git_origin).pack(fill='x', pady=(4, 8))
        ttk.Button(sidebar, text='Файлы проекта', command=self.show_project_files).pack(fill='x')
        ttk.Button(sidebar, text='История Git', command=self.show_git_history).pack(fill='x', pady=(4, 8))
        self.projects = ttk.Treeview(sidebar, show='tree', selectmode='browse', height=18)
        scroll = ttk.Scrollbar(sidebar, command=self.projects.yview)
        self.projects.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.projects.pack(fill='both', expand=True)
        self.projects.bind('<<TreeviewSelect>>', self.select_project)
        self.projects.bind('<Button-3>', self.project_menu)
        self.project_items = {}
        self.run_items = {}
        self.selected_run = None
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(6, weight=1)
        ttk.Label(frame, textvariable=self.workspace, wraplength=600).grid(
            row=1, column=0, columnspan=2, sticky='w')
        ttk.Label(frame, textvariable=self.status, wraplength=630).grid(
            row=2, column=0, columnspan=2, sticky='w', pady=8)
        self.prompt = tk.Text(frame, height=6, wrap='word', undo=True, **TEXT_COLORS)
        self.prompt.grid(row=3, column=0, columnspan=2, sticky='ew')
        bar = ttk.Frame(frame)
        bar.grid(row=4, column=0, columnspan=2, sticky='ew', pady=8)
        self.actions = []
        for label, action in (('Новая задача', 'new_task'), ('Продолжить', 'continue_task')):
            button = ttk.Button(bar, text=label, command=lambda method=action: self.start(method))
            button.pack(side='left', padx=(0, 6))
            self.actions.append(button)
        self.stop_button = ttk.Button(bar, text='Остановить', command=self.stop, state='disabled')
        self.stop_button.pack(side='left')
        ttk.Button(bar, text='Настройки', command=self.open_settings).pack(side='right')
        self.activity_row = ttk.Frame(frame)
        self.activity_row.grid(row=5, column=0, columnspan=2, sticky='ew', pady=(0, 8))
        self.activity_row.grid_remove()
        self.indicator = ttk.Progressbar(self.activity_row, mode='indeterminate', length=95)
        self.indicator.pack(side='left', padx=(0, 8))
        ttk.Label(self.activity_row, textvariable=self.running_status).pack(side='left')
        tabs = ttk.Notebook(frame)
        tabs.grid(row=6, column=0, columnspan=2, sticky='nsew')
        self.output = self.text_tab(tabs, 'Журнал')
        self.reasoning = self.text_tab(tabs, 'Рассуждения')
        root.protocol('WM_DELETE_WINDOW', self.close)
        self.reload_projects()
        self.refresh()

    @staticmethod
    def text_tab(tabs, title):
        pane = ttk.Frame(tabs)
        tabs.add(pane, text=title)
        text = tk.Text(pane, wrap='word', state='disabled', **TEXT_COLORS)
        text.bind('<Button-1>', lambda event: event.widget.focus_set(), add='+')
        buttons = ttk.Frame(pane)
        buttons.pack(side='top', fill='x', pady=(4, 4))
        status = tk.StringVar(master=pane)

        def copy_all():
            content = text.get('1.0', 'end-1c')
            if not content:
                status.set('Пока пусто')
                return
            text.clipboard_clear()
            text.clipboard_append(content)
            status.set('Скопировано')

        ttk.Button(buttons, text='Скопировать всё', command=copy_all).pack(side='left')
        ttk.Label(buttons, textvariable=status).pack(side='left', padx=8)
        scroll = ttk.Scrollbar(pane, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        text.pack(fill='both', expand=True)
        return text

    def open_settings(self):
        try:
            SettingsDialog(self.root, self.data, self.refresh)
        except (OSError, ValueError, KeyError, TypeError, StopIteration) as error:
            desktop_popups.showerror('Настройки Orchestra', str(error), parent=self.root)

    @staticmethod
    def workspace_key(workspace):
        return os.path.normcase(os.path.normpath(workspace))

    def visible_workspaces(self, selected=''):
        candidates = dict.fromkeys([*self.workspaces, *self.history, *([selected] if selected else [])])
        return [workspace for workspace in candidates
                if self.workspace_key(workspace) not in self.hidden_workspaces]

    def project_menu(self, event):
        workspace = self.project_items.get(self.projects.identify_row(event.y))
        if not workspace or self.task:
            return
        desktop_popups.action_popup(self.projects, event.x_root, event.y_root,
                                    'Убрать из списка', lambda: self.hide_workspace(workspace))
        return 'break'

    def hide_workspace(self, workspace):
        if self.task:
            return
        key = self.workspace_key(workspace)
        remaining = [path for path in self.workspaces if self.workspace_key(path) != key]
        hidden = self.hidden_workspaces | {key}
        try:
            self.save_workspace_list(remaining, hidden)
        except OSError as error:
            desktop_popups.showerror('Orchestra', str(error), parent=self.root)
            return
        if self.workspace_key(self.workspace.get()) == key:
            self.workspace.set(next(iter(self.visible_workspaces()), ''))
            self.selected_run = None
            self.prompt.delete('1.0', 'end')
            self.clear_text(self.output)
            self.clear_text(self.reasoning)
        self.reload_projects()
        self.refresh()

    def save_workspace_list(self, workspaces, hidden):
        preferences = {**self.preferences, 'workspaces': workspaces,
                       'hidden_workspaces': sorted(hidden)}
        self.data.save_preferences(preferences)
        self.preferences = preferences
        self.workspaces = workspaces
        self.hidden_workspaces = hidden

    def reload_projects(self):
        self.history = self.data.history()
        self.projects.delete(*self.projects.get_children())
        self.project_items.clear()
        self.run_items.clear()
        selected = self.workspace.get()
        for workspace in self.visible_workspaces(selected):
            title = Path(workspace).name or workspace
            item = self.projects.insert('', 'end', text=title, open=False)
            self.project_items[item] = workspace
            for directory, summary in self.history.get(workspace, ()):
                title = summary.get('topic') or summary.get('request') or directory.name
                child = self.projects.insert(item, 'end', text=title)
                self.run_items[child] = (workspace, directory, summary)
            if workspace == selected:
                self.projects.selection_set(item)

    def select_project(self, _event=None):
        selection = self.projects.selection()
        if self.task or not selection:
            return
        item = selection[0]
        run = self.run_items.get(item)
        workspace = run[0] if run else self.project_items.get(item)
        if not workspace:
            return
        if workspace == self.workspace.get() and run == self.selected_run:
            return
        switched = workspace != self.workspace.get()
        self.workspace.set(workspace)
        self.selected_run = run
        if switched:
            self.prompt.delete('1.0', 'end')
        self.clear_text(self.output)
        self.clear_text(self.reasoning)
        if run:
            self.show_run(*run[1:])
        self.refresh()

    def show_run(self, directory, summary):
        self.write(summary.get('request') or summary.get('topic') or directory.name)
        self.write(f"{directory.parent.name} · {summary.get('status', '')}")
        if summary.get('answer_file') == 'answer.md':
            try:
                self.write((directory / 'answer.md').read_text(encoding='utf-8'))
            except (OSError, UnicodeError):
                self.write('Ответ недоступен.')
        if summary.get('error_message'):
            self.write(summary['error_message'])
        usage = summary.get('usage', {})
        self.write(f"Вызовы API: {usage.get('api_calls', 0)} · Токены: {usage.get('total_tokens', 0)}")
        if 'cost' in summary:
            self.write(self.run_cost_text(summary['cost']))
        self.write('Журнал: ' + str(directory))

    def remember_workspace(self, workspace):
        key = self.workspace_key(workspace)
        workspaces = [workspace, *(path for path in self.workspaces if self.workspace_key(path) != key)]
        self.save_workspace_list(workspaces, self.hidden_workspaces - {key})

    @staticmethod
    def project_git(folder, *args, check=True):
        result = subprocess.run(['git', '-C', str(folder), *args], stdin=subprocess.DEVNULL,
                                capture_output=True, encoding='utf-8', errors='replace',
                                timeout=60, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if check and result.returncode:
            raise ValueError(result.stderr.strip() or 'Команда Git завершилась ошибкой.')
        return result

    def git_prompt(self, text, initial=''):
        return desktop_popups.askstring('Git проекта', text, initialvalue=initial, parent=self.root)

    def choose_folder(self):
        if self.task:
            return
        create = desktop_popups.askyesnocancel(
            'Добавить проект', 'Создать новую папку?\n\nДа — создать. Нет — выбрать существующую.',
            parent=self.root)
        if create is None:
            return
        folder = desktop_popups.askdirectory(
            title='Где создать папку проекта' if create else 'Выберите папку проекта',
            parent=self.root)
        if not folder:
            return
        folder = Path(folder).resolve()
        created = False
        try:
            if create:
                name = desktop_popups.askstring('Новый проект', 'Имя папки проекта', parent=self.root)
                if name is None:
                    return
                name = name.strip()
                if (name in {'', '.', '..'} or name.endswith('.') or Path(name).is_reserved()
                        or any(c in name for c in '<>:"/\\|?*') or any(ord(c) < 32 for c in name)):
                    raise ValueError('Введите допустимое имя одной папки.')
                target = folder / name
                target.mkdir()
                folder = target
                created = True
            self.offer_git_setup(folder)
        except (OSError, ValueError, subprocess.SubprocessError, RuntimeError) as error:
            desktop_popups.showerror('Git проекта', str(error), parent=self.root)
            if create and not created:
                return
        workspace = str(Path(folder).resolve())
        try:
            self.remember_workspace(workspace)
        except OSError as error:
            desktop_popups.showerror('Orchestra', str(error), parent=self.root)
            return
        if workspace != self.workspace.get():
            self.prompt.delete('1.0', 'end')
        self.workspace.set(workspace)
        self.selected_run = None
        self.clear_text(self.output)
        self.clear_text(self.reasoning)
        self.reload_projects()
        self.refresh()

    def offer_git_setup(self, folder):
        if (folder / '.git').exists():
            return
        try:
            repo = self.project_git(folder, 'rev-parse', '--is-inside-work-tree', check=False)
        except FileNotFoundError:
            return
        if repo.stdout.strip() == 'true':
            return
        if not desktop_popups.askyesno('Git проекта', 'Создать Git-репозиторий в этой папке?',
                                   parent=self.root):
            return
        branch = self.git_prompt('Имя первой ветки', 'main')
        if branch is None:
            return
        branch = branch.strip()
        name = self.git_prompt('Имя автора коммитов (можно оставить пустым)')
        if name is None:
            return
        email = self.git_prompt('Email автора коммитов (можно оставить пустым)')
        if email is None:
            return
        url = self.git_prompt('Ссылка на удалённый репозиторий (необязательно)')
        if url is None:
            return
        with workspace_lease(folder):
            self.check_branch(folder, branch)
            self.project_git(folder, 'init', '--initial-branch=' + branch)
            for key, value in [('user.name', name.strip()), ('user.email', email.strip())]:
                if value:
                    self.project_git(folder, 'config', '--local', key, value)
            if url.strip():
                self.project_git(folder, 'remote', 'add', '--', 'origin', url.strip())

    def selected_git_folder(self):
        folder = self.workspace.get()
        if not folder:
            raise ValueError('Выберите проект.')
        folder = Path(folder).resolve()
        result = self.project_git(folder, 'rev-parse', '--show-toplevel', check=False)
        if result.returncode or Path(result.stdout.strip()).resolve() != folder:
            raise ValueError('Выберите корневую папку Git-репозитория.')
        return folder

    def check_branch(self, folder, branch):
        if not branch or branch.startswith(('-', '@')):
            raise ValueError('Введите допустимое имя ветки.')
        self.project_git(folder, 'check-ref-format', '--branch', branch)

    def create_branch(self, folder=None, commit=None):
        if self.task:
            return
        try:
            folder = folder if folder is not None else self.selected_git_folder()
            base = commit[:12] if commit else 'HEAD'
            branch = self.git_prompt(f'Имя новой ветки от {base}')
            if branch is None:
                return
            branch = branch.strip()
            with workspace_lease(folder):
                self.check_branch(folder, branch)
                start = [commit] if commit else []
                self.project_git(folder, 'switch', '-c', branch, *start)
            desktop_popups.showinfo('Git проекта', f'Текущая ветка: {branch}', parent=self.root)
            return True
        except (OSError, ValueError, subprocess.SubprocessError, RuntimeError) as error:
            desktop_popups.showerror('Git проекта', str(error), parent=self.root)

    def set_git_origin(self):
        if self.task:
            return
        try:
            folder = self.selected_git_folder()
            previous = self.project_git(folder, 'remote', 'get-url', 'origin', check=False)
            url = self.git_prompt('Ссылка на удалённый репозиторий', previous.stdout.strip())
            if not url or not url.strip():
                return
            with workspace_lease(folder):
                operation = 'add' if previous.returncode else 'set-url'
                self.project_git(folder, 'remote', operation, '--', 'origin', url.strip())
        except (OSError, ValueError, subprocess.SubprocessError, RuntimeError) as error:
            desktop_popups.showerror('Git проекта', str(error), parent=self.root)

    def show_project_files(self):
        workspace = self.workspace.get()
        if not workspace or not Path(workspace).is_dir():
            desktop_popups.showerror('Файлы проекта', 'Выберите папку проекта.', parent=self.root)
            return
        folder = Path(workspace).resolve()
        window = tk.Toplevel(self.root, background=BACKGROUND)
        window.title('Файлы проекта — ' + folder.name)
        window.geometry('700x500')
        toolbar = ttk.Frame(window)
        toolbar.pack(fill='x', padx=8, pady=8)
        tree = ttk.Treeview(window, show='tree', selectmode='browse')
        scroll = ttk.Scrollbar(window, command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        tree.pack(fill='both', expand=True, padx=(8, 0), pady=(0, 8))
        paths = {}
        pending = {}

        def populate(item, directory):
            try:
                entries = sorted(
                    ((path.is_dir(), path) for path in directory.iterdir() if path.name != '.git'),
                    key=lambda entry: (not entry[0], entry[1].name.casefold()),
                )
                for is_directory, path in entries:
                    child = tree.insert(item, 'end', text=path.name)
                    paths[child] = (path, is_directory)
                    if is_directory and not (path.is_symlink() or getattr(path, 'is_junction', lambda: False)()):
                        pending[child] = tree.insert(child, 'end', text='…')
            except OSError as error:
                tree.insert(item, 'end', text=str(error))

        def expand(_event):
            item = tree.focus()
            if item in pending:
                tree.delete(pending.pop(item))
                populate(item, paths[item][0])

        def reload_tree():
            tree.delete(*tree.get_children())
            paths.clear()
            pending.clear()
            populate('', folder)

        def open_folder():
            selection = tree.selection()
            path, directory = paths.get(selection[0], (folder, True)) if selection else (folder, True)
            try:
                os.startfile(path if directory else path.parent)
            except OSError as error:
                desktop_popups.showerror('Файлы проекта', str(error), parent=window)

        ttk.Button(toolbar, text='Обновить', command=reload_tree).pack(side='left')
        ttk.Button(toolbar, text='Открыть в Проводнике', command=open_folder).pack(side='left', padx=8)
        tree.bind('<<TreeviewOpen>>', expand)
        reload_tree()

    def show_git_history(self, folder=None, focus_head=False):
        try:
            folder = folder if folder is not None else self.selected_git_folder()
            branch = self.project_git(folder, 'symbolic-ref', '--quiet', '--short', 'HEAD',
                                      check=False).stdout.strip()
            history = self.project_git(
                folder, '--no-pager', 'log', '--all', '--topo-order', '--decorate=short',
                '--format=%H%x00%P%x00%h%x00%d%x00%s', '-n', '150',
            ).stdout
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            desktop_popups.showerror('История Git', str(error), parent=self.root)
            return
        window = tk.Toplevel(self.root, background=BACKGROUND)
        window.title('История Git — ' + folder.name)
        window.geometry('900x500')
        ttk.Label(window, text=f'Текущая ветка: {branch or "HEAD"} · До 150 коммитов').pack(
            anchor='w', padx=8, pady=8)
        body = ttk.Frame(window)
        body.pack(fill='both', expand=True, padx=8, pady=(0, 8))
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        canvas = tk.Canvas(body, background=TEXT_COLORS['background'], highlightthickness=0)
        canvas.grid(row=0, column=0, sticky='nsew')
        vertical = ttk.Scrollbar(body, command=canvas.yview)
        horizontal = ttk.Scrollbar(body, orient='horizontal', command=canvas.xview)
        vertical.grid(row=0, column=1, sticky='ns')
        horizontal.grid(row=1, column=0, sticky='ew')
        canvas.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        canvas.bind('<MouseWheel>', lambda event: canvas.yview_scroll(-int(event.delta / 120), 'units'))
        rows = self.draw_git_history(canvas, history) or []
        toolbar = ttk.Frame(window)
        toolbar.pack(fill='x', padx=8, pady=(0, 8))
        selection = tk.StringVar(master=window, value='Выберите коммит в графе')
        ttk.Label(toolbar, textvariable=selection).pack(side='left')
        selected = None

        def row_at(event):
            y = canvas.canvasy(event.y)
            return next((row for row in rows if abs(row[0] - y) < 16), None)

        def clear_hover(_event=None):
            canvas.delete('hover_commit')
            canvas.configure(cursor='')

        def hover_commit(event):
            clear_hover()
            row = row_at(event)
            if row is None:
                return
            canvas.configure(cursor='hand2')
            if row != selected:
                y = row[0]
                width = max(canvas.canvasx(canvas.winfo_width()), canvas.bbox('all')[2])
                canvas.create_rectangle(0, y - 15, width, y + 15, fill=HOVER,
                                        outline='', tags='hover_commit')
                canvas.tag_lower('hover_commit')

        def choose_commit(event):
            nonlocal selected
            clear_hover()
            selected = row_at(event)
            canvas.delete('selected_commit')
            button.configure(state='normal' if selected else 'disabled')
            if selected:
                canvas.configure(cursor='hand2')
                row_y, _, short, subject = selected
                width = max(canvas.winfo_width(), canvas.bbox('all')[2])
                canvas.create_rectangle(0, row_y - 15, width, row_y + 15,
                                        fill='#284663', outline='', tags='selected_commit')
                canvas.tag_lower('selected_commit')
                selection.set(f'{short} · {subject[:55]}')
            else:
                selection.set('Выберите коммит в графе')

        def create_from(commit=None):
            if self.create_branch(folder, commit):
                window.destroy()
                self.show_git_history(folder, focus_head=True)

        button = ttk.Button(toolbar, text='Создать ветку отсюда', state='disabled',
                            command=lambda: create_from(selected[1]) if selected else None)
        button.pack(side='right')
        ttk.Button(toolbar, text='Ветка от HEAD', command=create_from).pack(side='right', padx=8)
        canvas.bind('<Button-1>', choose_commit)
        canvas.bind('<Motion>', hover_commit)
        canvas.bind('<Leave>', clear_hover)
        if focus_head and rows:
            head = self.project_git(folder, 'rev-parse', '--verify', 'HEAD', check=False)
            current = next((row for row in rows if row[1] == head.stdout.strip()), None)
            if current:
                from types import SimpleNamespace
                window.update_idletasks()
                bottom = canvas.bbox('all')[3] + 12
                canvas.yview_moveto(max(0, current[0] - canvas.winfo_height() // 2) / bottom)
                choose_commit(SimpleNamespace(y=current[0] - canvas.canvasy(0)))


    @staticmethod
    def draw_git_history(canvas, history):
        rows = [line.split(chr(0), 4) for line in history.splitlines() if chr(0) in line]
        colors = ('#71b7ff', '#ffb86c', '#78dca8', '#d6a0ff', '#ff858d', '#f2d477')
        foreground = TEXT_COLORS['foreground']
        if not rows:
            canvas.create_text(18, 25, anchor='w', fill=foreground,
                               text='В репозитории пока нет коммитов.')
            return
        lanes, graph = [], []
        max_lanes = 1
        for commit, parents, short, refs, subject in rows:
            if commit not in lanes:
                lanes.append(commit)
            before = lanes.copy()
            lane = before.index(commit)
            parents = parents.split()
            lanes.pop(lane)
            for offset, parent in enumerate(parents):
                if parent not in lanes:
                    lanes.insert(min(lane + offset, len(lanes)), parent)
            edges = [(i, lanes.index(ref)) for i, ref in enumerate(before) if ref != commit]
            edges.extend((lane, lanes.index(parent)) for parent in parents)
            graph.append((commit, lane, edges, short, refs.strip().removeprefix('(').removesuffix(')'), subject))
            max_lanes = max(max_lanes, len(before), len(lanes))

        spacing = 32
        text_x = 24 + max_lanes * 22
        click_rows = []
        for row, (commit, lane, edges, short, refs, subject) in enumerate(graph):
            y = 24 + (len(graph) - 1 - row) * spacing
            click_rows.append((y, commit, short, subject))
            for start, end in edges if row < len(graph) - 1 else ():
                x1, x2 = 16 + 22 * start, 16 + 22 * end
                canvas.create_line(x1, y, x1, y - 9, x2, y - 23, x2, y - spacing,
                                   fill=colors[end % len(colors)], width=2, tags='edge')
            x = 16 + 22 * lane
            canvas.create_oval(x - 5, y - 5, x + 5, y + 5,
                               fill=colors[lane % len(colors)], outline=TEXT_COLORS['background'])
            canvas.create_text(text_x, y, text=short, fill='#91a4bc', anchor='w', font='TkFixedFont')
            label_x = text_x + 105
            if refs:
                label = canvas.create_text(label_x + 6, y, text=refs, fill='#afe2ff', anchor='w')
                left, top, right, bottom = canvas.bbox(label)
                badge = canvas.create_rectangle(left - 4, top - 2, right + 4, bottom + 2,
                                                fill='#283f55', outline='')
                canvas.tag_lower(badge, label)
                label_x = right + 14
            canvas.create_text(label_x, y, text=subject, fill=foreground, anchor='w')
        canvas.tag_lower('edge')
        _, _, right, bottom = canvas.bbox('all')
        canvas.configure(scrollregion=(0, 0, right + 20, bottom + 12))
        return click_rows

    def session(self):
        folder = Path(self.workspace.get()).expanduser()
        if not self.workspace.get().strip() or not folder.is_dir():
            raise ValueError('Выберите существующую рабочую папку.')
        return OrchestraApp.from_directory(self.data.root, folder, logs_path=self.data.logs)

    def refresh(self, _event=None):
        if self.task:
            return
        self.actions[1].configure(state='disabled' if self.selected_run else 'normal')
        if self.selected_run:
            self.status.set('История запуска. Для продолжения текущей задачи выберите проект.')
            return
        if not self.data.config.is_file():
            self.status.set('Откройте «Настройки» и заполните данные провайдеров.')
            return
        try:
            state = self.session().status()
        except (OSError, ValueError, KeyError) as error:
            self.status.set(str(error))
            return
        labels = {'new': 'Новая рабочая папка', 'pause': 'Можно продолжить',
                  'done': 'Задача завершена', 'state_error': 'Ошибка чтения сохранённой задачи'}
        if state['running']:
            self.status.set('В этой папке уже идёт работа.')
        elif state.get('stop_reason') == 'interrupted':
            self.status.set('Прошлый запуск прерван. Нажмите «Продолжить».')
        else:
            self.status.set(labels.get(state['status'], state['status']))
        if 'budget_spent' in state:
            self.status.set(self.status.get() +
                            f" · Расход по бюджету задачи: \u0024{state['budget_spent']:.5f}/\u0024{state['budget_limit']:.2f}")

    def write(self, line):
        self.append_text(self.output, line + '\n')

    @staticmethod
    def clear_text(widget):
        widget.configure(state='normal')
        widget.delete('1.0', 'end')
        widget.configure(state='disabled')

    @staticmethod
    def append_text(widget, text):
        follow = widget.yview()[1] >= 0.999
        widget.configure(state='normal')
        widget.insert('end', text)
        if follow:
            widget.see('end')
        widget.configure(state='disabled')

    def progress(self, event):
        kind = event['kind']
        if self.task and not self.task.cancelling() and not self.closing:
            if kind == 'model':
                self.activity = 'Ожидаю ответа: ' + event['model']
            elif kind == 'reasoning':
                self.activity = 'Рассуждает: ' + event['model']
            elif kind == 'model_text':
                self.activity = 'Пишет ответ'
            elif kind == 'edit_draft':
                self.activity = 'Готовит правку'
            elif kind == 'tool':
                self.activity = 'Выполняется: ' + event['name']
            elif kind in {'files', 'verification', 'usage'}:
                self.activity = 'Обрабатываю результат'
        if kind == 'started':
            self.write(event['topic'])
        elif kind == 'model':
            self.write('Модель: ' + event['model'])
        elif kind == 'reasoning':
            item = (event['request_id'], 'reasoning')
            if self.stream_item != item:
                separator = '\n\n' if self.stream_item else ''
                self.append_text(self.reasoning, separator + event['model'] + '\n')
                self.stream_item = item
            self.append_text(self.reasoning, event['text'])
        elif kind in {'model_text', 'edit_draft'}:
            item = (event['request_id'], event['item_id'])
            if self.stream_item != item:
                separator = '\n\n' if self.stream_item else ''
                heading = 'Ответ модели' if kind == 'model_text' else f"Черновик · {event['name']}"
                self.append_text(self.reasoning, separator + heading + '\n')
                self.stream_item = item
            self.append_text(self.reasoning, event['text'])
        elif kind == 'tool':
            self.write('Инструмент: ' + event['name'])
        elif kind == 'files':
            self.write('Изменены: ' + ', '.join(event['paths']))
        elif kind == 'verification':
            self.write('Проверка ' + event['name'] + (' прошла' if event['passed'] else ' не прошла'))
        elif kind == 'usage':
            self.status.set(f"Работа идёт · {event['tokens']} токенов · ${event['spent']:.4f}")
        elif kind == 'stopped':
            if event.get('reason'):
                self.write(str(event['reason']))
            if 'cost' in event:
                self.write(self.run_cost_text(event['cost']))

    @staticmethod
    def run_cost_text(cost):
        suffix = '' if cost['complete'] else ' (данные о расходе неполные)'
        return f"Расход за запуск: \u0024{cost['estimated_usd']:.5f}{suffix}"

    def start(self, method):
        if self.task:
            return
        try:
            app = self.session()
            folder = str(app.engine.files.root)
            self.remember_workspace(folder)
        except (OSError, ValueError, KeyError) as error:
            desktop_popups.showerror('Orchestra', str(error), parent=self.root)
            return
        text = self.prompt.get('1.0', 'end').strip()
        if method != 'continue_task' and not text:
            self.status.set('Введите задачу или описание ошибки.')
            return
        self.task = self.loop.create_task(self.run(app, method, text))
        self.stream_item = None
        if self.selected_run:
            self.clear_text(self.output)
        self.selected_run = None
        self.workspace.set(folder)
        self.reload_projects()
        self.clear_text(self.reasoning)
        self.started_at = time.monotonic()
        self.activity = 'Запуск'
        self.update_activity()
        self.activity_row.grid()
        self.indicator.start(60)
        self.status.set('Запуск…')
        self.projects.state(['disabled'])
        self.choose.configure(state='disabled')
        for button in self.actions:
            button.configure(state='disabled')
        self.stop_button.configure(state='normal')

    async def run(self, app, method, text):
        try:
            result = await getattr(app, method)(text, on_progress=self.progress)
            self.write(result.get('answer') or result.get('error') or result['status'])
            self.write('Журнал: ' + result['log_directory'])
        except asyncio.CancelledError:
            self.write('Работа остановлена. Сохранённую задачу можно продолжить.')
        except (OSError, ValueError, RuntimeError) as error:
            self.write(str(error))
        finally:
            await asyncio.gather(*(client.close() for client in app.engine.clients.values()),
                                 return_exceptions=True)

    def stop(self):
        if self.task and not self.task.done() and not self.task.cancelling():
            self.status.set('Остановка…')
            self.activity = 'Останавливаю работу'
            self.task.cancel()
            self.stop_button.configure(state='disabled')

    def close(self):
        self.closing = True
        self.stop()

    def update_activity(self):
        elapsed = int(time.monotonic() - self.started_at)
        minutes, seconds = divmod(elapsed, 60)
        self.running_status.set(f'{self.activity} · {minutes:02d}:{seconds:02d}')

    def tick(self):
        self.loop.call_soon(self.loop.stop)
        self.loop.run_forever()
        if self.task and self.task.done():
            if not self.task.cancelled() and self.task.exception():
                self.write('Ошибка выполнения: ' + type(self.task.exception()).__name__)
            self.task = None
            self.indicator.stop()
            self.activity_row.grid_remove()
            self.projects.state(['!disabled'])
            self.reload_projects()
            self.choose.configure(state='normal')
            for button in self.actions:
                button.configure(state='normal')
            self.stop_button.configure(state='disabled')
            self.refresh()
        elif self.task:
            self.update_activity()
        if self.closing and not self.task:
            self.root.destroy()
        else:
            self.root.after(20, self.tick)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace', type=Path)
    args = parser.parse_args()
    root = tk.Tk()
    root.withdraw()
    apply_dark_theme(root)
    data = DesktopData(Path(__file__).resolve().parent)
    try:
        data.initialize()
        preferences = data.load_preferences()
        if not preferences.get('migration_prompted') and data.has_legacy_data():
            if desktop_popups.askyesno('Orchestra', 'Импортировать настройки и сохранённые задачи?',
                                   parent=root):
                data.import_legacy()
            preferences['migration_prompted'] = True
            data.save_preferences(preferences)
    except OSError as error:
        desktop_popups.showerror('Orchestra', str(error), parent=root)
        root.destroy()
        return
    with asyncio.Runner() as runner:
        window = DesktopWindow(root, data, runner.get_loop(), args.workspace)
        root.deiconify()
        root.after(0, window.tick)
        root.mainloop()


if __name__ == '__main__':
    main()
