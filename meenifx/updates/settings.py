"""Update preferences and the remind/skip/What's-New state machine.

These values belong to the installation, never to a ``.mfxpf`` project, so they live in
the application's INI settings. The logic is kept out of the dialogs so postponement,
skipping and the once-per-version What's New rule can be tested without a window.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..version import VERSION

PREFIX = "updates/"
CHANNELS = ("alpha", "stable")
DEFAULT_INTERVAL_HOURS = 6
REMIND_HOURS = 24


def _default_store():
    from PyQt6.QtCore import QSettings
    return QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope,
                     "meeniFX", "Studios")


def _now(now=None):
    return now or datetime.now(timezone.utc)


def _parse_time(text):
    try:
        parsed = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _iso(value):
    return value.astimezone(timezone.utc).isoformat()


def _flag(value):
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


@dataclass
class UpdateSettings:
    auto_check: bool = True
    channel: str = "alpha"
    skipped: list = field(default_factory=list)
    remind_after: str = ""
    last_check: str = ""
    last_seen_version: str = ""
    last_ack_version: str = ""
    pending_whats_new: str = ""

    # ------------------------------------------------------------------ persistence
    @classmethod
    def load(cls, store=None):
        if store is None:
            store = _default_store()
        skipped = store.value(PREFIX + "skipped", "[]")
        if isinstance(skipped, str):
            try:
                skipped = json.loads(skipped)
            except ValueError:
                skipped = []
        if not isinstance(skipped, (list, tuple)):
            skipped = []
        channel = str(store.value(PREFIX + "channel", "alpha") or "alpha").lower()
        return cls(
            auto_check=_flag(store.value(PREFIX + "autoCheck", True)),
            channel=channel if channel in CHANNELS else "alpha",
            skipped=[str(item) for item in skipped if str(item).strip()],
            remind_after=str(store.value(PREFIX + "remindAfter", "") or ""),
            last_check=str(store.value(PREFIX + "lastCheck", "") or ""),
            last_seen_version=str(store.value(PREFIX + "lastSeenVersion", "") or ""),
            last_ack_version=str(store.value(PREFIX + "lastAckVersion", "") or ""),
            pending_whats_new=str(store.value(PREFIX + "pendingWhatsNew", "") or ""),
        )

    def save(self, store=None):
        if store is None:
            store = _default_store()
        store.setValue(PREFIX + "autoCheck", bool(self.auto_check))
        store.setValue(PREFIX + "channel", self.channel if self.channel in CHANNELS else "alpha")
        store.setValue(PREFIX + "skipped", json.dumps(sorted(set(self.skipped))))
        store.setValue(PREFIX + "remindAfter", self.remind_after)
        store.setValue(PREFIX + "lastCheck", self.last_check)
        store.setValue(PREFIX + "lastSeenVersion", self.last_seen_version)
        store.setValue(PREFIX + "lastAckVersion", self.last_ack_version)
        store.setValue(PREFIX + "pendingWhatsNew", self.pending_whats_new)
        sync = getattr(store, "sync", None)
        if callable(sync):
            sync()

    # ------------------------------------------------------------------ skipping
    def is_skipped(self, version_text):
        return str(version_text) in self.skipped

    def skip(self, version_text):
        text = str(version_text)
        if text and text not in self.skipped:
            self.skipped.append(text)

    def clear_skipped(self):
        self.skipped.clear()

    # ------------------------------------------------------------------ reminding
    def remind_later(self, now=None, hours=REMIND_HOURS):
        self.remind_after = _iso(_now(now) + timedelta(hours=hours))

    def reminder_active(self, now=None):
        target = _parse_time(self.remind_after)
        return target is not None and target > _now(now)

    def clear_reminder(self):
        self.remind_after = ""

    # ------------------------------------------------------------------ checking
    def should_notify(self, update, now=None, manual=False):
        """Whether the small update notice may appear (manual checks always win)."""
        if update is None:
            return False
        if manual:
            return True
        if self.reminder_active(now):
            return False
        return not self.is_skipped(getattr(update, "version_text", update))

    def should_auto_check(self, now=None, interval_hours=DEFAULT_INTERVAL_HOURS):
        if not self.auto_check:
            return False
        last = _parse_time(self.last_check)
        if last is None:
            return True
        return (_now(now) - last).total_seconds() >= float(interval_hours) * 3600

    def mark_checked(self, now=None):
        self.last_check = _iso(_now(now))

    # ------------------------------------------------------------------ first launch
    def record_launch(self, version_text=VERSION):
        """Record that this version has run; return True when What's New is due.

        The very first launch (no previous version at all) is a fresh install, not an
        update, so it adopts the version silently. A later launch of a different version
        marks What's New pending until the dialog is acknowledged - which also means the
        notes are never shown before the new build is actually running.
        """
        previous = self.last_seen_version.strip()
        if not previous:
            self.pending_whats_new = ""
            self.last_seen_version = version_text
            self.last_ack_version = version_text
            return False
        if previous == version_text:
            # Still waiting for the user to read them: keep offering the notes.
            if self.last_ack_version != version_text:
                self.pending_whats_new = version_text
                return True
            self.pending_whats_new = ""
            return False
        self.pending_whats_new = ""
        self.last_seen_version = version_text
        if self.last_ack_version != version_text:
            self.pending_whats_new = version_text
            return True
        return False

    def acknowledge_whats_new(self, version_text=None):
        text = version_text or self.pending_whats_new or self.last_seen_version
        self.last_ack_version = str(text)
        self.pending_whats_new = ""
