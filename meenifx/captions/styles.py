"""Caption style presets.

A caption is a generated text clip, so a style is nothing but a set of values for the
text model (:mod:`meenifx.model.text`). That keeps every caption fully editable afterwards:
the inspector changes the same keys the style set, and preview and export draw the same
instantly-updated picture.
"""

from ..model import text as TXT

STYLE_ORDER = ("classic", "shorts", "karaoke", "minimal", "gaming", "ymm4", "pop", "typewriter")

POSITION_ORDER = ("bottom", "lower", "center", "top")
POSITION_LABELS = {
    "bottom": "Bottom",
    "lower": "Lower third",
    "center": "Center",
    "top": "Top",
}
# Fraction of the frame height the text block is moved away from the centre.
POSITION_OFFSET = {"bottom": 0.355, "lower": 0.255, "center": 0.0, "top": -0.36}

ANIMATION_LABELS = (
    ("Fade in", "text_in_fade"),
    ("Pop in", "text_in_pop"),
    ("Scale in", "text_in_scale_overshoot"),
    ("Slide in", "text_in_slide_up"),
    ("Bounce in", "text_in_bounce"),
    ("Typewriter", "text_in_typewriter"),
    ("None", ""),
)


def _style(name, animation, position, wrap, **settings):
    return {"name": name, "animation": animation, "position": position,
            "wrap_fraction": wrap, "settings": settings}


STYLES = {
    "classic": _style(
        "Classic Subtitles", "text_in_fade", "bottom", 0.86,
        font="Segoe UI", size=54.0, bold=False, color="#FFFFFF", stroke_color="#000000",
        stroke_width=3.0, shadow_opacity=0.75, shadow_offset_x=2.0, shadow_offset_y=3.0,
        shadow_blur=4.0, bg_opacity=0.0, align="center"),
    "shorts": _style(
        "Bold YouTube Shorts", "text_in_pop", "lower", 0.90,
        font="Arial", size=78.0, bold=True, color="#FFFFFF", stroke_color="#000000",
        stroke_width=7.0, shadow_opacity=0.55, shadow_offset_y=5.0, shadow_blur=6.0,
        bg_opacity=0.0, align="center"),
    "karaoke": _style(
        "Karaoke Highlight", "text_in_fade", "lower", 0.92,
        font="Arial", size=68.0, bold=True, color="#FFFFFF", stroke_color="#101010",
        stroke_width=5.0, shadow_opacity=0.5, shadow_offset_y=4.0, shadow_blur=5.0,
        bg_opacity=0.0, align="center", highlight_color="#FFD400"),
    "minimal": _style(
        "Minimal Clean", "text_in_fade", "bottom", 0.82,
        font="Segoe UI", size=46.0, bold=False, color="#F3F5F8",
        stroke_width=0.0, shadow_opacity=0.55, shadow_offset_y=2.0, shadow_blur=4.0,
        bg_color="#000000", bg_opacity=42.0, bg_padding=20.0, align="center"),
    "gaming": _style(
        "Gaming Captions", "text_in_pop", "lower", 0.90,
        font="Trebuchet MS", size=66.0, bold=True, color="#FFE066", stroke_color="#161616",
        stroke_width=6.0, shadow_opacity=0.65, shadow_offset_y=4.0, shadow_blur=5.0,
        bg_opacity=0.0, align="center"),
    "ymm4": _style(
        "YMM4-Inspired", "text_in_slide_up", "bottom", 0.88,
        font="Segoe UI", size=60.0, bold=True, color="#FFFFFF", stroke_color="#1A2333",
        stroke_width=6.0, shadow_opacity=0.6, shadow_offset_y=4.0, shadow_blur=6.0,
        bg_opacity=0.0, align="center"),
    "pop": _style(
        "Pop Animation", "text_in_pop", "center", 0.86,
        font="Arial", size=74.0, bold=True, color="#FFFFFF", stroke_color="#000000",
        stroke_width=5.0, shadow_opacity=0.5, shadow_offset_y=5.0, shadow_blur=7.0,
        bg_opacity=0.0, align="center"),
    "typewriter": _style(
        "Typewriter", "text_in_typewriter", "bottom", 0.84,
        font="Consolas", size=52.0, bold=False, color="#EAF2FF", stroke_color="#000000",
        stroke_width=2.0, shadow_opacity=0.6, shadow_offset_y=3.0, shadow_blur=4.0,
        bg_color="#0B101B", bg_opacity=58.0, bg_padding=22.0, align="left"),
}


def style(style_id):
    return STYLES.get(style_id) or STYLES["classic"]


def style_settings(style_id, width, height, position=None):
    """Concrete text-model values for one style at this project size.

    Sizes are authored for 1080p and scaled by the frame height, so the same style looks
    right on a 720p or 4K project. The wrap width is a fraction of the frame, because a
    fixed pixel width would produce one-word lines on a narrow project.
    """
    chosen = style(style_id)
    settings = dict(chosen["settings"])
    scale = max(0.25, float(height) / 1080.0)
    settings["size"] = float(settings.get("size", 54.0)) * scale
    settings["wrap"] = float(width) * float(chosen.get("wrap_fraction", 0.86))
    settings["offset_y"] = POSITION_OFFSET.get(position or chosen["position"], 0.35) * float(height)
    return settings


def caption_text(style_id, text, width, height, position=None, animation=None, words=None,
                 caption_id="", highlight=None):
    """The full ``clip.text`` dict for one caption (style included, still editable)."""
    chosen = style(style_id)
    data = TXT.blank()
    data.update(style_settings(style_id, width, height, position))
    data["text"] = str(text or "")
    data["keys"] = {}
    if highlight:
        data["highlight_color"] = highlight
    data["caption"] = {
        "id": caption_id,
        "style": style_id if style_id in STYLES else "classic",
        "animation": chosen["animation"] if animation is None else animation,
        "words": list(words or ()),
        "highlight": highlight or data.get("highlight_color", ""),
    }
    return data


def style_of(clip):
    data = getattr(clip, "text", None) or {}
    caption = data.get("caption") or {}
    return caption.get("style") or "classic"
