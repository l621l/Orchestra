"""Themed prompts and folder picker for the desktop app."""
import tkinter as tk
from pathlib import Path
from tkinter import simpledialog, ttk

from desktop_theme import BACKGROUND, HOVER, SURFACE, TEXT


class Prompt(simpledialog.Dialog):
    def __init__(self, parent, title, text, choices, *, initial=None, default=None):
        self.text = text
        self.choices = choices
        self.initial = initial
        self.default = default
        super().__init__(parent, title)

    def body(self, master):
        self.configure(background=BACKGROUND)
        master.configure(background=BACKGROUND)
        ttk.Label(master, text=self.text, wraplength=520, justify='left').pack(
            anchor='w', padx=16, pady=12)
        if self.initial is not None:
            self.entry = ttk.Entry(master, width=58)
            self.entry.insert(0, self.initial)
            self.entry.selection_range(0, 'end')
            self.entry.pack(fill='x', padx=16, pady=(0, 8))
            return self.entry

    def buttonbox(self):
        bar = ttk.Frame(self, padding=(12, 6, 12, 12))
        bar.pack(fill='x')
        for label, value in reversed(self.choices):
            ttk.Button(bar, text=label, command=lambda value=value: self.finish(value)).pack(
                side='right', padx=4)
        if self.initial is not None or self.default is not None:
            self.bind('<Return>', lambda event: self.finish(self.default))
        self.bind('<Escape>', self.cancel)

    def finish(self, value):
        if value is True and self.initial is not None:
            value = self.entry.get()
        self.result = value
        self.cancel()


def showerror(title, text, *, parent):
    Prompt(parent, title, text, [('Закрыть', True)], default=True)


def showinfo(title, text, *, parent):
    Prompt(parent, title, text, [('OK', True)], default=True)


def askyesno(title, text, *, parent):
    return Prompt(parent, title, text, [('Да', True), ('Нет', False)], default=False).result


def askyesnocancel(title, text, *, parent):
    return Prompt(parent, title, text, [('Да', True), ('Нет', False), ('Отмена', None)]).result


def askstring(title, text, *, initialvalue='', parent):
    return Prompt(parent, title, text, [('OK', True), ('Отмена', None)],
                  initial=initialvalue, default=True).result


class FolderPicker(Prompt):
    def __init__(self, parent, title):
        super().__init__(parent, title, '', [('Выбрать', True), ('Отмена', None)])

    def body(self, master):
        self.configure(background=BACKGROUND)
        master.configure(background=BACKGROUND)
        body = ttk.Frame(master, padding=12)
        body.pack(fill='both', expand=True)
        bar = ttk.Frame(body)
        bar.pack(fill='x')
        self.path = tk.StringVar(master=self)
        entry = ttk.Entry(bar, textvariable=self.path, width=62)
        entry.pack(side='left', fill='x', expand=True)
        ttk.Button(bar, text='Перейти', command=self.visit_path).pack(side='left', padx=4)
        ttk.Button(bar, text='Вверх', command=lambda: self.visit(self.folder.parent)).pack(side='left')
        entry.bind('<Return>', lambda event: self.visit_path())
        listing = ttk.Frame(body)
        listing.pack(fill='both', expand=True, pady=(8, 0))
        self.tree = ttk.Treeview(listing, show='tree', selectmode='browse', height=14)
        scroll = ttk.Scrollbar(listing, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.tree.pack(side='left', fill='both', expand=True)
        self.tree.bind('<Double-1>', self.open_selected)
        self.tree.bind('<Return>', self.open_selected)
        self.status = ttk.Label(body, wraplength=520)
        self.status.pack(anchor='w')
        self.visit(Path.home())
        return entry

    def visit_path(self):
        self.visit(Path(self.path.get()).expanduser())
        return 'break'

    def visit(self, folder):
        try:
            folder = folder.resolve(strict=True)
            children = sorted((p for p in folder.iterdir() if p.is_dir()),
                              key=lambda p: p.name.casefold())
        except (OSError, ValueError, RuntimeError) as error:
            self.status.configure(text=str(error))
            return
        self.folder = folder
        self.path.set(str(folder))
        self.status.configure(text='')
        self.tree.delete(*self.tree.get_children())
        for child in children:
            self.tree.insert('', 'end', text=child.name)

    def open_selected(self, event):
        item = self.tree.identify_row(event.y) if event.type == tk.EventType.ButtonPress else self.tree.focus()
        if item:
            self.visit(self.folder / self.tree.item(item, 'text'))
        return 'break'

    def finish(self, value):
        if value:
            if not hasattr(self, 'folder') or self.path.get() != str(self.folder):
                self.visit_path()
                if not hasattr(self, 'folder') or self.status.cget('text'):
                    return
            selection = self.tree.selection()
            folder = self.folder / self.tree.item(selection[0], 'text') if selection else self.folder
            if not folder.is_dir():
                self.status.configure(text='Эта папка больше недоступна.')
                return
            value = str(folder)
        super().finish(value)


def askdirectory(*, title, parent):
    return FolderPicker(parent, title).result


def action_popup(parent, x, y, label, action):
    popup = tk.Toplevel(parent, background=SURFACE)
    popup.overrideredirect(True)
    popup.transient(parent.winfo_toplevel())
    popup.withdraw()

    def activate():
        popup.destroy()
        action()

    button = tk.Button(popup, text=label, command=activate, cursor='hand2', anchor='w',
                       background=SURFACE, foreground=TEXT, activebackground=HOVER,
                       activeforeground=TEXT, relief='flat', borderwidth=0,
                       highlightthickness=0, padx=12, pady=7)
    button.pack(fill='x')
    button.bind('<Enter>', lambda event: button.configure(background=HOVER))
    button.bind('<Leave>', lambda event: button.configure(background=SURFACE))
    button.bind('<Return>', lambda event: activate())
    popup.bind('<Escape>', lambda event: popup.destroy())

    def dismiss_outside(event):
        if not (popup.winfo_rootx() <= event.x_root < popup.winfo_rootx() + popup.winfo_width()
                and popup.winfo_rooty() <= event.y_root < popup.winfo_rooty() + popup.winfo_height()):
            popup.destroy()

    popup.bind('<ButtonPress>', dismiss_outside)
    popup.update_idletasks()
    x = max(0, min(x, popup.winfo_screenwidth() - popup.winfo_reqwidth()))
    y = max(0, min(y, popup.winfo_screenheight() - popup.winfo_reqheight()))
    popup.geometry(f'+{x}+{y}')
    popup.deiconify()
    popup.wait_visibility()
    popup.grab_set()
    button.focus_set()
