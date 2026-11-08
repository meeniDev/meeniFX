"""Release notes for the What's New dialog.

Notes live in ``docs/releases/<version>.md`` so they ship inside the installer and can be
read offline. GitHub's release body is preferred when it is available (the update was just
downloaded from there), with the bundled file as the dependable fallback.
"""

from __future__ import annotations

from ..version import VERSION, read_release_notes


def parse_notes(text):
    """[(heading, [bullet lines])] from the user-facing markdown notes.

    Notes are deliberately simpler than the changelog: a heading and bullets. HTML and
    level-1 headings are dropped, because the dialog supplies its own version title.
    """
    sections = []
    heading = None
    lines = []

    def flush():
        if heading is not None or lines:
            sections.append((heading or "Highlights", [line for line in lines if line]))

    for raw in str(text or "").splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if stripped.startswith("#"):
            flush()
            heading = stripped.lstrip("#").strip() or "Highlights"
            lines = []
        elif stripped.startswith(("- ", "* ")):
            lines.append(stripped[2:].strip())
        elif stripped:
            lines.append(stripped)
    flush()
    return [(name, items) for name, items in sections if items]


def notes_text(version_text=VERSION, release_notes=None):
    """The notes to show: the release body when it is real, else the bundled file."""
    body = str(release_notes or "").strip()
    if body:
        return body
    return read_release_notes(version_text).strip()


def notes_url(version_text=VERSION):
    return f"https://github.com/meeniDev/meeniFX/releases/tag/v{version_text}"
