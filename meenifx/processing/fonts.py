"""Installed-font discovery and face resolution.

The inspector lists what Windows (or the host OS) actually has installed, and the Pillow
renderer resolves the same family to a real font file. Both sides share this module, so a
font that shows in the picker is a font the preview and the export can draw.

Discovery is deliberately lazy: the first lookup scans the platform font directories with
Pillow (a fraction of a second for a few hundred files) and every later lookup is a dict
hit until :func:`refresh` runs. Qt's :class:`QFontDatabase` supplies the canonical family
names and the installed style list; Pillow's name table supplies the file each face lives
in, which is what the renderer needs.
"""

from __future__ import annotations

import os
import sys
import threading
from functools import lru_cache

from PIL import ImageFont

_FONT_SUFFIXES = (".ttf", ".otf", ".ttc", ".otc")
_INDEX = None
_INDEX_LOCK = threading.Lock()
_RESOLVED = {}
_GENERATION = 0


def _font_directories():
    """Every directory the platform keeps installed fonts in (system and per-user)."""
    directories = []
    if sys.platform.startswith("win"):
        windows = os.environ.get("WINDIR") or os.environ.get("SystemRoot") or r"C:\Windows"
        directories.append(os.path.join(windows, "Fonts"))
        local = os.environ.get("LOCALAPPDATA")
        if local:
            directories.append(os.path.join(local, "Microsoft", "Windows", "Fonts"))
    elif sys.platform == "darwin":
        directories.extend(("/System/Library/Fonts", "/Library/Fonts",
                            os.path.expanduser("~/Library/Fonts")))
    else:
        directories.extend(("/usr/share/fonts", "/usr/local/share/fonts",
                            os.path.expanduser("~/.fonts"),
                            os.path.expanduser("~/.local/share/fonts")))
    seen, result = set(), []
    for directory in directories:
        key = os.path.normcase(os.path.abspath(directory))
        if key not in seen and os.path.isdir(directory):
            seen.add(key)
            result.append(directory)
    return result


def _font_files():
    """Every candidate font file, deduplicated case-insensitively."""
    seen, files = set(), []
    for directory in _font_directories():
        for root, _dirs, names in os.walk(directory):
            for name in names:
                if not name.lower().endswith(_FONT_SUFFIXES):
                    continue
                path = os.path.join(root, name)
                key = os.path.normcase(os.path.abspath(path))
                if key in seen:
                    continue
                seen.add(key)
                files.append(path)
    return files


def _faces_in(path):
    """[(family, style, path, index)] for a font file, including every face of a .ttc."""
    faces = []
    for index in range(0, 32):
        try:
            font = ImageFont.truetype(path, 12, index=index)
        except (OSError, ValueError):
            break
        try:
            family, style = font.getname()
        except Exception:  # pragma: no cover - a malformed name table is simply skipped
            continue
        family = str(family or "").strip()
        if not family:
            continue
        faces.append((family, str(style or "Regular").strip(), path, index))
        if not path.lower().endswith((".ttc", ".otc")):
            break
    return faces


def _build_index():
    """{family_lower: {style_lower: (path, index)}} plus canonical family spellings."""
    index, canonical = {}, {}
    for path in _font_files():
        for family, style, file_path, face_index in _faces_in(path):
            key = family.casefold()
            canonical.setdefault(key, family)
            style_key = style.casefold()
            faces = index.setdefault(key, {})
            # Prefer a real vector file over a duplicate copy in another folder, and the
            # first face over a later duplicate of the same style.
            faces.setdefault(style_key, (file_path, face_index))
    return {"faces": index, "names": canonical}


def _index():
    global _INDEX
    if _INDEX is None:
        with _INDEX_LOCK:
            if _INDEX is None:
                _INDEX = _build_index()
    return _INDEX


def refresh():
    """Forget the scan and every rendered text cache; the next lookup rescans."""
    global _INDEX, _GENERATION
    with _INDEX_LOCK:
        _INDEX = None
        _RESOLVED.clear()
        _GENERATION += 1
    try:
        from . import text as TEXT
        TEXT.clear_font_cache()
    except Exception:
        pass
    try:
        from . import text_units
        text_units.clear_cache()
    except Exception:
        pass


def generation():
    """Changes whenever :func:`refresh` runs; cheap cache key for UI previews."""
    return _GENERATION


def qt_families():
    """Installed families as Qt reports them (empty when no GUI application exists)."""
    try:
        from PyQt6.QtGui import QFontDatabase
        from PyQt6.QtWidgets import QApplication
        # QFontDatabase is only valid once a GUI application exists; command-line
        # rendering and pure model tests fall back to the file scan instead of aborting.
        if QApplication.instance() is None:
            return []
        names = QFontDatabase.families()
    except Exception:
        return []
    result = []
    for name in names:
        text = str(name or "").strip()
        # Vertical writing-system aliases ("@SimSun") are not separate user-facing fonts.
        if text and not text.startswith("@"):
            result.append(text)
    return result


def system_families():
    """Canonical installed family names: Qt's list first, then anything Pillow found."""
    names, seen = [], set()
    for name in qt_families():
        key = name.casefold()
        if key not in seen:
            seen.add(key)
            names.append(name)
    for key, family in _index()["names"].items():
        if key not in seen:
            seen.add(key)
            names.append(family)
    return sorted(names, key=lambda value: (value.casefold(), value))


def has_face(family):
    """True when a real font file was found for `family` (what the renderer can draw)."""
    if not family:
        return False
    return str(family).casefold() in _index()["faces"]


def installed(family):
    """True when the family is installed according to Qt or the font-file scan."""
    if not family:
        return False
    name = str(family).casefold()
    if name in _index()["faces"]:
        return True
    return any(str(item).casefold() == name for item in qt_families())


def styles(family):
    """Installed style names for a family, regular first when it exists."""
    faces = _index()["faces"].get(str(family or "").casefold())
    if not faces:
        return []
    names = [style for style in faces]
    return sorted(names, key=lambda style: (style != "regular", style))


def _style_score(style, bold, italic):
    style = style.casefold()
    is_italic = "italic" in style or "oblique" in style
    has_bold = "bold" in style
    is_black = "black" in style or "heavy" in style
    is_semibold = "semibold" in style or "demibold" in style
    score = 0.0
    score += 0.0 if is_italic == bool(italic) else 5.0
    if bold:
        if has_bold:
            score += 0.0
        elif is_semibold:
            score += 1.5
        elif is_black:
            score += 2.5
        else:
            score += 6.0
    else:
        if is_black:
            score += 4.0
        elif has_bold or is_semibold:
            score += 3.0
        else:
            score += 0.0
    if "regular" in style or "book" in style:
        score -= 0.25
    if italic and "italic" in style:
        score -= 0.25
    return score


@lru_cache(maxsize=2048)
def _resolve_cached(family_key, bold, italic, generation_key):
    faces = _index()["faces"].get(family_key)
    if not faces:
        return None
    style = min(faces, key=lambda name: _style_score(name, bold, italic))
    return faces[style]


def resolve(family, bold=False, italic=False):
    """(path, face_index) for the closest installed face, or None when the family is absent."""
    if not family:
        return None
    key = str(family).casefold()
    return _resolve_cached(key, bool(bold), bool(italic), _GENERATION)


def fallback_chain(family, bold=False, italic=False):
    """The face for `family`, then the usual safe fallbacks, in rendering order."""
    chain = []
    candidate = resolve(family, bold, italic)
    if candidate is not None:
        chain.append(candidate)
    for name in ("Arial", "Segoe UI", "DejaVu Sans", "Liberation Sans"):
        if str(name).casefold() == str(family or "").casefold():
            continue
        other = resolve(name, bold, italic)
        if other is not None and other not in chain:
            chain.append(other)
    return chain


def family_display(family):
    """Canonical spelling for a family the user typed or a project stored."""
    if not family:
        return ""
    key = str(family).casefold()
    return _index()["names"].get(key, str(family))
