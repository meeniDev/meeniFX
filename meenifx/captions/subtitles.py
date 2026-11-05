"""Subtitle interchange: SRT parsing/formatting and ASS export.

Times here are seconds (float) because that is what subtitle formats use; the timeline
converts them to frames when captions become clips.
"""

import re

_TIME = re.compile(r"(\d{1,3}):(\d{2}):(\d{2})[,.](\d{1,3})")
_TAG = re.compile(r"<[^>]+>")


def _seconds(hours, minutes, seconds, fraction):
    millis = int((fraction + "00")[:3]) if fraction else 0
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + millis / 1000.0


def parse_srt(payload):
    """[{"start": s, "end": s, "text": str}] from SRT text; malformed blocks are skipped."""
    cues = []
    blocks = re.split(r"\r?\n\r?\n+", str(payload or "").replace("\ufeff", "").strip())
    for block in blocks:
        lines = [line.rstrip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        timing_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing_index is None:
            continue
        left, _, right = lines[timing_index].partition("-->")
        start_match, end_match = _TIME.search(left), _TIME.search(right)
        if start_match is None or end_match is None:
            continue
        text = "\n".join(lines[timing_index + 1:]).strip()
        if not text:
            continue
        start, end = _seconds(*start_match.groups()), _seconds(*end_match.groups())
        if end <= start:
            end = start + 1.0
        cues.append({"start": start, "end": end, "text": _TAG.sub("", text).strip()})
    cues.sort(key=lambda cue: cue["start"])
    return cues


def _srt_time(seconds):
    seconds = max(0.0, float(seconds))
    whole = int(seconds)
    millis = int(round((seconds - whole) * 1000))
    if millis >= 1000:
        whole, millis = whole + 1, millis - 1000
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def format_srt(cues):
    """SRT text for [(start, end, text)] cues (plain dicts are accepted too)."""
    lines = []
    for index, cue in enumerate(cues, start=1):
        start = cue["start"] if isinstance(cue, dict) else cue[0]
        end = cue["end"] if isinstance(cue, dict) else cue[1]
        text = (cue.get("text") if isinstance(cue, dict) else cue[2]) or ""
        lines.append(str(index))
        lines.append(f"{_srt_time(start)} --> {_srt_time(end)}")
        lines.append(str(text).strip())
        lines.append("")
    return "\n".join(lines)


def _ass_time(seconds):
    seconds = max(0.0, float(seconds))
    centis = int(round(seconds * 100))
    hours, rest = divmod(centis, 360000)
    minutes, rest = divmod(rest, 6000)
    secs, cs = divmod(rest, 100)
    return f"{hours:d}:{minutes:02d}:{secs:02d}.{cs:02d}"


def _ass_colour(value, fallback="#FFFFFF"):
    text = str(value or fallback).lstrip("#")
    if len(text) != 6:
        text = fallback.lstrip("#")
    try:
        red, green, blue = int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
    except ValueError:
        red = green = blue = 255
    return f"&H00{blue:02X}{green:02X}{red:02X}"


def format_ass(cues, width=1920, height=1080, font="Segoe UI", size=54.0, color="#FFFFFF",
               outline_color="#000000", outline=3.0, shadow=2.0, alignment=2):
    """ASS text for the same cues; the header mirrors the chosen caption style."""
    head = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {int(width)}",
        f"PlayResY: {int(height)}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Captions,{font},{float(size):.0f},{_ass_colour(color)},&H00FFFFFF,"
        f"{_ass_colour(outline_color)},&H64000000,0,0,0,0,100,100,0,0,1,"
        f"{float(outline):.1f},{float(shadow):.1f},{int(alignment)},40,40,40,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    body = []
    for cue in cues:
        start = cue["start"] if isinstance(cue, dict) else cue[0]
        end = cue["end"] if isinstance(cue, dict) else cue[1]
        text = (cue.get("text") if isinstance(cue, dict) else cue[2]) or ""
        text = str(text).replace("\n", "\\N")
        body.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Captions,,0,0,0,,{text}")
    return "\n".join(head + body) + "\n"
