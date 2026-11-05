"""Keyboard Shortcuts: a searchable list of every menu shortcut, with conflicts marked."""

from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit,
                             QTableWidget, QTableWidgetItem, QVBoxLayout)


def collect_shortcuts(window):
    """[(action text, shortcut, menu path)] from the live menu bar.

    Read from the widgets rather than from a hand-written table, so the page can never
    list a shortcut the application does not actually have.
    """
    rows = []

    def walk(menu, path):
        for action in menu.actions():
            if action.isSeparator():
                continue
            submenu = action.menu()
            label = action.text().replace("&", "").strip()
            if submenu is not None:
                walk(submenu, f"{path} > {label}" if label else path)
                continue
            if not label:
                continue
            rows.append((label, action.shortcut().toString(), path))

    for action in window.menuBar().actions():
        menu = action.menu()
        if menu is not None:
            walk(menu, action.text().replace("&", "").strip())
    return rows


def shortcut_conflicts(rows):
    """{shortcut: [action labels]} for shortcuts assigned to more than one action."""
    seen = {}
    for label, shortcut, _path in rows:
        if shortcut:
            seen.setdefault(shortcut, []).append(label)
    return {shortcut: labels for shortcut, labels in seen.items() if len(labels) > 1}


class ShortcutsDialog(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.w = window
        self.setWindowTitle("Keyboard Shortcuts")
        self.resize(760, 560)
        self.rows = collect_shortcuts(window)
        self.conflicts = shortcut_conflicts(self.rows)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 12)
        layout.setSpacing(8)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search actions, shortcuts or menus…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._fill)
        layout.addWidget(self.search)

        self.conflict_label = QLabel("")
        self.conflict_label.setWordWrap(True)
        layout.addWidget(self.conflict_label)

        self.table = QTableWidget(0, 4, self)
        self.table.setHorizontalHeaderLabels(["Action", "Shortcut", "Menu", "Note"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, 1)

        note = QLabel("Shortcuts are ignored while a text or numeric field has focus, so "
                      "typing always works normally. Conflicting entries are marked red.")
        note.setWordWrap(True)
        note.setStyleSheet("color: #8a8f9a;")
        layout.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        if self.conflicts:
            names = "; ".join(f"{shortcut}: {', '.join(labels)}"
                              for shortcut, labels in sorted(self.conflicts.items()))
            self.conflict_label.setText(f"{len(self.conflicts)} conflicting shortcut(s): {names}")
            self.conflict_label.setStyleSheet("color: #ffce6b;")
        else:
            self.conflict_label.setText("No conflicting shortcuts.")
            self.conflict_label.setStyleSheet("color: #8a8f9a;")
        self._fill()

    def _fill(self, *_args):
        needle = self.search.text().strip().lower()
        rows = []
        for label, shortcut, path in sorted(self.rows, key=lambda row: (row[2], row[0])):
            note = ""
            if shortcut and shortcut in self.conflicts:
                others = [other for other in self.conflicts[shortcut] if other != label]
                note = "also " + ", ".join(others[:3])
            if needle and not any(needle in text.lower() for text in (label, shortcut, path, note)):
                continue
            rows.append((label, shortcut or "—", path, note))
        self.table.setUpdatesEnabled(False)
        self.table.setRowCount(len(rows))
        for index, (label, shortcut, path, note) in enumerate(rows):
            for column, value in enumerate((label, shortcut, path, note)):
                item = QTableWidgetItem(value)
                if note:
                    item.setForeground(QColor("#ffce6b"))
                self.table.setItem(index, column, item)
        self.table.resizeColumnsToContents()
        self.table.setUpdatesEnabled(True)
