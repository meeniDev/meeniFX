"""System-wide hotkeys through Win32 RegisterHotKey (works while another app has focus)."""

import ctypes
import sys
from ctypes import wintypes

from PyQt6.QtCore import QAbstractNativeEventFilter, QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication

WM_HOTKEY = 0x0312
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000

_NAMED = {"SPACE": 0x20, "ESC": 0x1B, "PAUSE": 0x13, "PRINT": 0x2C, "HOME": 0x24, "END": 0x23,
          "INS": 0x2D, "DEL": 0x2E}


def parse_hotkey(text):
    """"Ctrl+Shift+R" -> (modifiers, virtual key) or None when it is not a valid combination."""
    mods, key = 0, None
    for part in (p.strip().upper() for p in (text or "").split("+")):
        if part in ("CTRL", "CONTROL"):
            mods |= MOD_CONTROL
        elif part == "SHIFT":
            mods |= MOD_SHIFT
        elif part == "ALT":
            mods |= MOD_ALT
        elif part in ("WIN", "META"):
            mods |= MOD_WIN
        elif part in _NAMED:
            key = _NAMED[part]
        elif len(part) == 1 and part.isalnum():
            key = ord(part)
        elif part.startswith("F") and part[1:].isdigit() and 1 <= int(part[1:]) <= 24:
            key = 0x6F + int(part[1:])
        else:
            return None
    if key is None or not mods:
        return None
    return mods, key


class _Filter(QAbstractNativeEventFilter):
    def __init__(self, owner):
        super().__init__()
        self.owner = owner

    def nativeEventFilter(self, event_type, message):
        if event_type in (b"windows_generic_MSG", "windows_generic_MSG"):
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == WM_HOTKEY:
                self.owner._fire(int(msg.wParam))
        return False, 0


class GlobalHotkeys(QObject):
    """Register named hotkeys; `triggered(name)` fires on the GUI thread."""

    triggered = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._ids = {}
        self._names = {}
        self._next = 0xB100
        self._filter = None
        self.available = sys.platform == "win32"

    def register(self, name, text):
        """True when the combination is now active; False if invalid or taken by another app."""
        self.unregister(name)
        parsed = parse_hotkey(text)
        if not self.available or parsed is None:
            return False
        if self._filter is None:
            self._filter = _Filter(self)
            QApplication.instance().installNativeEventFilter(self._filter)
        hotkey_id = self._next
        self._next += 1
        mods, key = parsed
        if not ctypes.windll.user32.RegisterHotKey(None, hotkey_id, mods | MOD_NOREPEAT, key):
            return False
        self._ids[name] = hotkey_id
        self._names[hotkey_id] = name
        return True

    def unregister(self, name):
        hotkey_id = self._ids.pop(name, None)
        if hotkey_id is not None:
            self._names.pop(hotkey_id, None)
            ctypes.windll.user32.UnregisterHotKey(None, hotkey_id)

    def clear(self):
        for name in list(self._ids):
            self.unregister(name)

    def _fire(self, hotkey_id):
        name = self._names.get(hotkey_id)
        if name:
            self.triggered.emit(name)
