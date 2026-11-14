"""Native Windows window for running and resuming Orchestra tasks."""
import argparse
import asyncio
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from desktop_data import DesktopData
from desktop_settings import SettingsDialog
from desktop_shortcuts import install_editing_shortcuts
from desktop_theme import TEXT_COLORS, apply_dark_theme
from orchestra_app import OrchestraApp


class DesktopWindow:
    def __init__(self, root, data, loop, workspace):
        self.root = root
        self.data = data
        self.loop = loop
        self.task = None
        self.started_at = None
        self.activity = ''
        self.reasoning_request = None
        self.closing = False
        self.preferences = data.load_preferences()
        self.workspaces = self.preferences.get('workspaces', [])
        self.history = data.history()
        initial = str(workspace) if workspace else (self.workspaces or list(self.history) or [''])[0]
        self.workspace = tk.StringVar(value=initial)
        self.status = tk.StringVar()
        self.running_status = tk.StringVar()
        apply_dark_theme(root)
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
        self.projects = ttk.Treeview(sidebar, show='tree', selectmode='browse', height=18)
        scroll = ttk.Scrollbar(sidebar, command=self.projects.yview)
        self.projects.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.projects.pack(fill='both', expand=True)
        self.projects.bind('<<TreeviewSelect>>', self.select_project)
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
        for label, action in (('Новая задача', 'new_task'), ('Продолжить', 'continue_task'),
                              ('Отладить', 'debug_task')):
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
            messagebox.showerror('Настройки Orchestra', str(error), parent=self.root)

    def reload_projects(self):
        self.history = self.data.history()
        self.projects.delete(*self.projects.get_children())
        self.project_items.clear()
        self.run_items.clear()
        selected = self.workspace.get()
        for workspace in dict.fromkeys([*self.workspaces, *self.history, *([selected] if selected else [])]):
            title = Path(workspace).name or workspace
            item = self.projects.insert('', 'end', text=title, open=workspace == selected)
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
        self.workspaces = [workspace, *(item for item in self.workspaces if item != workspace)]
        self.preferences['workspaces'] = self.workspaces
        self.data.save_preferences(self.preferences)

    def choose_folder(self):
        folder = filedialog.askdirectory(parent=self.root)
        if not folder:
            return
        workspace = str(Path(folder).resolve())
        try:
            self.remember_workspace(workspace)
        except OSError as error:
            messagebox.showerror('Orchestra', str(error), parent=self.root)
            return
        if workspace != self.workspace.get():
            self.prompt.delete('1.0', 'end')
        self.workspace.set(workspace)
        self.selected_run = None
        self.clear_text(self.output)
        self.clear_text(self.reasoning)
        self.reload_projects()
        self.refresh()

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
            elif kind == 'tool':
                self.activity = 'Выполняется: ' + event['name']
            elif kind in {'files', 'verification', 'usage'}:
                self.activity = 'Обрабатываю результат'
        if kind == 'started':
            self.write(event['topic'])
        elif kind == 'model':
            self.write('Модель: ' + event['model'])
        elif kind == 'reasoning':
            if self.reasoning_request != event['request_id']:
                separator = '\n\n' if self.reasoning_request else ''
                self.append_text(self.reasoning, separator + event['model'] + '\n')
                self.reasoning_request = event['request_id']
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
            messagebox.showerror('Orchestra', str(error), parent=self.root)
            return
        text = self.prompt.get('1.0', 'end').strip()
        if method != 'continue_task' and not text:
            self.status.set('Введите задачу или описание ошибки.')
            return
        self.task = self.loop.create_task(self.run(app, method, text))
        self.reasoning_request = None
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
    data = DesktopData(Path(__file__).resolve().parent)
    try:
        data.initialize()
        preferences = data.load_preferences()
        if not preferences.get('migration_prompted') and data.has_legacy_data():
            if messagebox.askyesno('Orchestra', 'Импортировать настройки и сохранённые задачи?',
                                   parent=root):
                data.import_legacy()
            preferences['migration_prompted'] = True
            data.save_preferences(preferences)
    except OSError as error:
        messagebox.showerror('Orchestra', str(error), parent=root)
        root.destroy()
        return
    with asyncio.Runner() as runner:
        window = DesktopWindow(root, data, runner.get_loop(), args.workspace)
        root.deiconify()
        root.after(0, window.tick)
        root.mainloop()


if __name__ == '__main__':
    main()
