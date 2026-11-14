"""Dark theme for the Tk desktop window and its dialogs."""
from tkinter import ttk

BACKGROUND = '#171a21'
SURFACE = '#222731'
TEXT = '#edf0f7'
MUTED = '#aab4c5'
ACCENT = '#84aaff'
BORDER = '#414c60'
SELECTION = '#35558e'


def apply_dark_theme(root):
    root.configure(background=BACKGROUND)
    style = ttk.Style(root)
    style.theme_use('clam')
    style.configure('.', background=BACKGROUND, foreground=TEXT, bordercolor=BORDER,
                    lightcolor=BORDER, darkcolor=BORDER, fieldbackground=SURFACE,
                    selectbackground=SELECTION, selectforeground=TEXT, troughcolor=SURFACE,
                    arrowcolor=TEXT)
    for name in ('TButton', 'TCombobox', 'TScrollbar', 'TNotebook.Tab'):
        style.configure(name, background=SURFACE)
        style.map(name, background=[('disabled', BACKGROUND), ('selected', SELECTION),
                                    ('active', SELECTION)], foreground=[('disabled', MUTED)])
    for name in ('TEntry', 'TCombobox'):
        style.configure(name, insertcolor=TEXT)
        style.map(name, fieldbackground=[('readonly', SURFACE), ('disabled', BACKGROUND)])
    style.configure('Horizontal.TProgressbar', background=ACCENT)
    style.configure('Treeview', background=SURFACE, fieldbackground=SURFACE,
                    foreground=TEXT, rowheight=26)
    style.map('Treeview', background=[('selected', SELECTION)],
              foreground=[('selected', TEXT)])
    root.option_add('*TCombobox*Listbox.background', SURFACE)
    root.option_add('*TCombobox*Listbox.foreground', TEXT)
    root.option_add('*TCombobox*Listbox.selectBackground', SELECTION)
    root.option_add('*TCombobox*Listbox.selectForeground', TEXT)


TEXT_COLORS = {
    'background': SURFACE, 'foreground': TEXT, 'insertbackground': TEXT,
    'selectbackground': SELECTION, 'selectforeground': TEXT, 'highlightbackground': BORDER,
    'highlightcolor': ACCENT, 'highlightthickness': 1, 'borderwidth': 0,
}
