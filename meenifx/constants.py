"""Project-wide constants and small pure helpers (no widget or I/O code)."""


def build_default_unicode_charset():
    ranges = [
        (0x0020, 0x007E),
        (0x00A0, 0x00FF),
        (0x00A1, 0x024F),
        (0x0250, 0x02AF),
        (0x02B0, 0x02FF),
        (0x0300, 0x036F),
        (0x0370, 0x052F),
        (0x0590, 0x06FF),
        (0x0900, 0x097F),
        (0x20A0, 0x20CF),
        (0x2100, 0x214F),
        (0x2000, 0x206F),
        (0x2190, 0x22FF),
        (0x2500, 0x257F),
        (0x2580, 0x259F),
        (0x25A0, 0x25FF),
        (0x2600, 0x26FF),
        (0x2700, 0x27BF),
        (0x3040, 0x309F),
        (0x30A0, 0x30FF),
        (0x3400, 0x4DBF),
        (0x4E00, 0x9FFF),
        (0xAC00, 0xD7AF),
        (0xFB00, 0xFB4F),
        (0xFF01, 0xFFEF),
    ]

    chars = []
    for start, end in ranges:
        chars.extend(chr(codepoint) for codepoint in range(start, end + 1))
    return "".join(chars)


DEFAULT_UNICODE_CHARSET = build_default_unicode_charset()


def parse_hex_color(value):
    text = value.strip()
    if text.startswith("#"):
        text = text[1:]
    if len(text) != 6:
        return None
    try:
        r = int(text[0:2], 16)
        g = int(text[2:4], 16)
        b = int(text[4:6], 16)
    except ValueError:
        return None
    return (b, g, r)


def parse_palette_colors(value):
    colors = []
    for token in value.split(","):
        color = parse_hex_color(token)
        if color is not None:
            colors.append(color)
    return colors

ZOOM_STOPS = ("Fit", "25%", "50%", "100%", "200%", "400%")

# Internal drag-and-drop payload: media-library ids, never bare file paths, so a drop
# refers to the media the project already imported instead of importing it again.
MEDIA_MIME = "application/x-meenifx-media"
EFFECT_MIME = "application/x-meenifx-effect"
PRESET_MIME = "application/x-meenifx-preset"

VIDEO_EXT = (".mp4", ".avi", ".mov", ".mkv", ".webm", ".m4v")
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif")
AUDIO_EXT = (".wav", ".mp3", ".ogg", ".flac", ".aac", ".m4a")
