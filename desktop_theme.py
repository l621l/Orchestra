"""Dark theme for the Tk desktop window and its dialogs."""
from tkinter import TclError, ttk

BACKGROUND = '#171a21'
SURFACE = '#222731'
TEXT = '#edf0f7'
MUTED = '#aab4c5'
ACCENT = '#84aaff'
BORDER = '#414c60'
SELECTION = '#35558e'
HOVER = '#293b56'


def apply_dark_theme(root):
    root.configure(background=BACKGROUND)
    style = ttk.Style(root)
    style.theme_use('clam')
    style.configure('.', background=BACKGROUND, foreground=TEXT, bordercolor=BORDER,
                    lightcolor=BORDER, darkcolor=BORDER, fieldbackground=SURFACE,
                    selectbackground=SELECTION, selectforeground=TEXT, troughcolor=SURFACE,
                    arrowcolor=TEXT)
    for name in ('TButton', 'TCheckbutton', 'TRadiobutton', 'TCombobox', 'TScrollbar', 'TNotebook.Tab'):
        style.configure(name, background=SURFACE)
        style.map(name, background=[('disabled', BACKGROUND), ('selected', SELECTION),
                                    ('active', HOVER)], foreground=[('disabled', MUTED)])
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

    install_pointer_feedback(root)


def install_pointer_feedback(root):
    buttons = {'TButton', 'TCheckbutton', 'TRadiobutton'}
    hovered = {}

    def clear_tree(widget):
        item = hovered.pop(widget, '')
        if item and widget.exists(item):
            tags = widget.item(item, 'tags')
            widget.item(item, tags=tuple(tag for tag in tags if tag != 'pointer_hover'))

    def move(event):
        widget = event.widget
        if isinstance(widget, str):
            return
        kind = widget.winfo_class()
        if kind in buttons:
            clickable = not widget.instate(('disabled',))
        elif kind == 'TCombobox':
            clickable = not widget.instate(('disabled',)) and (
                widget.instate(('readonly',)) or 'arrow' in widget.identify(event.x, event.y))
        elif kind == 'TNotebook':
            try:
                tab = widget.index(f'@{event.x},{event.y}')
                clickable = widget.tab(tab, 'state') != 'disabled'
            except TclError:
                clickable = False
        elif kind == 'Treeview':
            item = widget.identify_row(event.y)
            clickable = bool(item) and not widget.instate(('disabled',))
            if hovered.get(widget) != item or not clickable:
                clear_tree(widget)
                if clickable:
                    widget.tag_configure('pointer_hover', background=HOVER)
                    widget.item(item, tags=(*widget.item(item, 'tags'), 'pointer_hover'))
                    hovered[widget] = item
        else:
            return
        widget.configure(cursor='hand2' if clickable else '')

    def leave(event):
        widget = event.widget
        if isinstance(widget, str):
            return
        kind = widget.winfo_class()
        if kind == 'Treeview':
            clear_tree(widget)
        if kind in buttons or kind in {'TCombobox', 'TNotebook', 'Treeview'}:
            widget.configure(cursor='')

    root.bind_all('<Enter>', move, add='+')
    root.bind_all('<Motion>', move, add='+')
    root.bind_all('<Leave>', leave, add='+')
    root.bind_all('<Destroy>', lambda event: hovered.pop(event.widget, None) and None, add='+')


TEXT_COLORS = {
    'background': SURFACE, 'foreground': TEXT, 'insertbackground': TEXT,
    'selectbackground': SELECTION, 'selectforeground': TEXT, 'highlightbackground': BORDER,
    'highlightcolor': ACCENT, 'highlightthickness': 1, 'borderwidth': 0,
}
