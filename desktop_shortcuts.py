"""Physical editing shortcuts in Orchestra's Windows UI thread."""
import tkinter as tk
from collections import deque
from tkinter import ttk


# Scan codes from WM_KEYDOWN are independent of the active layout.
SHORTCUTS = {
    0x1E: '<<SelectAll>>',  # A
    0x2E: '<<Copy>>',       # C
    0x2F: '<<Paste>>',      # V
    0x2D: '<<Cut>>',        # X
    0x15: '<<Redo>>',       # Y
    0x2C: '<<Undo>>',       # Z
}


def install_editing_shortcuts(root):
    if root.tk.call('tk', 'windowingsystem') != 'win32':
        return
    from ctypes import POINTER, WINFUNCTYPE, WinDLL, c_ssize_t, cast, get_last_error
    from ctypes.wintypes import HINSTANCE, HWND, LPARAM, MSG, WPARAM, DWORD, UINT

    user32 = WinDLL('user32', use_last_error=True)
    kernel32 = WinDLL('kernel32')
    callback_type = WINFUNCTYPE(c_ssize_t, c_ssize_t, WPARAM, LPARAM)
    user32.SetWindowsHookExW.argtypes = (c_ssize_t, callback_type, HINSTANCE, DWORD)
    user32.SetWindowsHookExW.restype = HINSTANCE
    user32.CallNextHookEx.argtypes = (HINSTANCE, c_ssize_t, WPARAM, LPARAM)
    user32.CallNextHookEx.restype = c_ssize_t
    user32.UnhookWindowsHookEx.argtypes = (HINSTANCE,)
    user32.GetAncestor.argtypes = (HWND, UINT)
    user32.GetAncestor.restype = HWND
    pending = deque()
    focused_widget = None
    focused_window = None

    def focus_in(event):
        nonlocal focused_widget, focused_window
        widget = event.widget
        if isinstance(widget, (tk.Text, tk.Entry, ttk.Entry)):
            focused_widget = widget
            focused_window = user32.GetAncestor(widget.winfo_id(), 2)  # GA_ROOT

    def focus_out(event):
        nonlocal focused_widget, focused_window
        if event.widget is focused_widget:
            focused_widget = None
            focused_window = None

    root.bind_all('<FocusIn>', focus_in, add='+')
    root.bind_all('<FocusOut>', focus_out, add='+')

    @callback_type
    def handle_message(code, removed, pointer):
        if code >= 0 and removed & 1:
            message = cast(pointer, POINTER(MSG)).contents
            if (message.message == 0x100 and user32.GetKeyState(0x11) & 0x8000
                    and not user32.GetKeyState(0x12) & 0x8000):
                scan = (message.lParam >> 16) & 0x1FF
                shortcut = SHORTCUTS.get(scan)
                if user32.GetKeyState(0x10) & 0x8000:
                    shortcut = '<<Redo>>' if scan == 0x2C else None
                if (shortcut and focused_widget is not None and focused_window
                        and user32.GetAncestor(message.hWnd, 2) == focused_window):
                    # Tk must only be called after the native hook returns.
                    pending.append((focused_widget, shortcut))
                    message.message = 0  # WM_NULL: prevent duplicate Tk handling.
        return user32.CallNextHookEx(None, code, removed, pointer)

    hook = user32.SetWindowsHookExW(3, handle_message, None, kernel32.GetCurrentThreadId())
    if not hook:
        raise OSError(get_last_error(), 'Could not install Orchestra keyboard shortcuts')

    def drain():
        nonlocal timer
        while pending:
            apply_shortcut(*pending.popleft())
        timer = root.after(10, drain)

    timer = root.after(10, drain)

    def cleanup(event):
        if event.widget is root:
            user32.UnhookWindowsHookEx(hook)
            root.after_cancel(timer)
            root._keyboard_hook = None

    root._keyboard_hook = handle_message  # Keep the native callback alive until unhooked.
    root.bind('<Destroy>', cleanup, add='+')


def apply_shortcut(widget, shortcut):
    if not widget.winfo_exists():
        return
    if shortcut == '<<SelectAll>>' and isinstance(widget, tk.Text):
        widget.tag_add('sel', '1.0', 'end-1c')
    else:
        widget.event_generate(shortcut)

