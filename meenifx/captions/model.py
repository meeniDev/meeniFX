"""Captions on the timeline.

A caption is an ordinary generated text clip on a track named ``Captions``. That choice is
deliberate: text clips already render in preview and export, move, trim, split, select,
undo and serialise inside ``.mfxpf``, so captions inherit all of it instead of introducing
a second kind of timeline object with its own editing rules.

The caption-specific data lives under ``clip.text["caption"]``: a stable id, the style, the
word timings and the entrance animation. The visible words stay in ``clip.text["text"]``, so
editing a caption is exactly like editing any other text clip.
"""

import uuid

from ..model import preset_library as LIB
from ..model import presets as PM
from ..model import text as TXT
from . import styles

CAPTION_KEY = "caption"
TRACK_NAME = "Captions"
PAUSE_BREAK = 0.55          # seconds of silence that ends a phrase
SENTENCE_BREAK = 1.0


def is_caption(clip):
    data = getattr(clip, "text", None)
    return isinstance(data, dict) and isinstance(data.get(CAPTION_KEY), dict)


def caption_info(clip):
    return (getattr(clip, "text", None) or {}).get(CAPTION_KEY) if is_caption(clip) else None


def caption_clips(timeline):
    """Every caption clip, in timeline order."""
    return sorted((clip for clip in timeline.all_clips() if is_caption(clip)),
                  key=lambda clip: (clip.timeline_start, clip.id))


def caption_track(timeline, create=False):
    """The track captions live on: ``Captions`` if it exists, created on top when asked."""
    for track in timeline.tracks:
        if track.type == "video" and track.name == TRACK_NAME:
            return track
    if not create:
        return None
    return timeline.add_track("video", name=TRACK_NAME, index=0)


def _track_words(segment):
    """Word timing for one ASR segment, estimated when the engine gave none.

    Whisper usually returns word timestamps; when it does not (or the model was run without
    them) the words are spread over the segment by character count, which is the best
    information available and never invents a count of words that is not in the text.
    """
    words = [word for word in (segment.get("words") or ()) if str(word.get("word", "")).strip()]
    text = str(segment.get("text", "")).strip()
    start, end = float(segment.get("start", 0.0)), float(segment.get("end", 0.0))
    if words:
        cleaned = []
        for word in words:
            cleaned.append({"start": max(start, float(word.get("start", start))),
                            "end": min(max(end, start), float(word.get("end", end))),
                            "word": str(word.get("word", "")).strip()})
        return cleaned
    tokens = text.split()
    if not tokens:
        return []
    weights = [max(1, len(token)) for token in tokens]
    total = float(sum(weights))
    span = max(0.05, end - start)
    cursor, out = start, []
    for token, weight in zip(tokens, weights):
        length = span * weight / total
        out.append({"start": cursor, "end": cursor + length, "word": token})
        cursor += length
    return out


def cues(segments, grouping="phrase", max_words=8):
    """Caption cues from ASR segments.

    `grouping` is "word" (one caption per word), "phrase" (pauses and punctuation) or
    "sentence" (only sentence ends and long pauses). `max_words` caps every caption.
    """
    grouping = grouping if grouping in ("word", "phrase", "sentence") else "phrase"
    max_words = max(1, int(max_words or 8))
    groups = []
    current = []

    def flush():
        nonlocal current
        if current:
            groups.append(current)
            current = []

    for segment in segments or ():
        for word in _track_words(segment):
            if grouping == "word":
                groups.append([word])
                continue
            if current:
                gap = word["start"] - current[-1]["end"]
                punctuation = current[-1]["word"].rstrip("\"')")[-1:] in ".!?…,;:"
                long_pause = gap > (SENTENCE_BREAK if grouping == "sentence" else PAUSE_BREAK)
                sentence_end = grouping == "sentence" and current[-1]["word"].rstrip("\"')")[-1:] in ".!?…"
                if len(current) >= max_words or long_pause or sentence_end or (
                        grouping == "phrase" and punctuation):
                    flush()
            current.append(word)
    flush()
    result = []
    for group in groups:
        text = " ".join(word["word"] for word in group).strip()
        if not text:
            continue
        start = min(word["start"] for word in group)
        end = max(word["end"] for word in group)
        result.append({"start": start, "end": max(start + 0.2, end), "text": text,
                       "words": [dict(word) for word in group]})
    return result


def cues_from_srt(rows):
    """SRT rows become cues with evenly spread word timings (used by Word grouping)."""
    result = []
    for row in rows or ():
        start, end = float(row["start"]), float(row["end"])
        text = str(row.get("text", "")).strip()
        tokens = text.split()
        weights = [max(1, len(token)) for token in tokens] or [1]
        total = float(sum(weights))
        span = max(0.05, end - start)
        cursor, words = start, []
        for token, weight in zip(tokens, weights):
            length = span * weight / total
            words.append({"start": cursor, "end": cursor + length, "word": token})
            cursor += length
        result.append({"start": start, "end": end, "text": text, "words": words})
    return result


def apply_animation(clip, preset_id, fps=30.0):
    """Attach one of the shipped text entrance animations to a caption clip."""
    if not preset_id:
        return False
    preset = LIB.load().get(preset_id)
    if preset is None:
        return False
    category, program, args, knobs = LIB.preset_spec(preset, "whole")
    knobs = dict(knobs)
    if "unit" in knobs:
        knobs["unit"] = "whole"
    PM.add(clip, preset_id, category, program, args, knobs,
           duration=min(int(clip.duration), max(2, int(round(float(fps) * 0.35)))),
           name=preset.get("name") or preset_id)
    return True


def build_clips(timeline, track, caption_cues, *, style_id="classic", position=None,
                animation="", start_frame=0, width=1920, height=1080, fps=30.0):
    """Create the caption clips for `caption_cues`; the caller owns the undo entry."""
    created = []
    for cue in caption_cues or ():
        start = int(start_frame) + int(round(max(0.0, float(cue["start"])) * fps))
        end = int(start_frame) + int(round(max(0.0, float(cue["end"])) * fps))
        duration = max(2, end - start)
        clip = timeline.new_clip("Caption", "", track, max(0, start), duration)
        chosen_animation = (cue.get("animation") if cue.get("animation") is not None else animation)
        clip.text = styles.caption_text(
            style_id, cue.get("text", ""), width, height, position, chosen_animation or "",
            words=cue.get("words"), caption_id=cue.get("id") or uuid.uuid4().hex[:12])
        apply_animation(clip, chosen_animation or "", fps)
        created.append(clip)
    return created


def clear_range(timeline, start_frame, end_frame, track=None):
    """Remove caption clips overlapping a frame range (regenerating never touches others)."""
    start_frame, end_frame = int(min(start_frame, end_frame)), int(max(start_frame, end_frame))
    removed = []
    for clip in caption_clips(timeline):
        if track is not None and clip.track is not track:
            continue
        if clip.timeline_start < end_frame and clip.end > start_frame:
            removed.append(clip)
    for clip in removed:
        if clip in clip.track.clips:
            clip.track.clips.remove(clip)
    return removed


def export_cues(clips, fps=30.0):
    """[(start, end, text)] seconds for a list of caption clips."""
    rows = []
    for clip in sorted(clips, key=lambda c: (c.timeline_start, c.id)):
        text = TXT.content_at(clip, 0)
        if not str(text).strip():
            continue
        rows.append({"start": clip.timeline_start / max(1.0, float(fps)),
                     "end": clip.end / max(1.0, float(fps)),
                     "text": str(text)})
    return rows


def transcript_rows(timeline, fps=30.0):
    """Rows for the transcript editor: stable clip id plus editable text and times."""
    return [{"id": clip.id, "start": clip.timeline_start / max(1.0, float(fps)),
             "end": clip.end / max(1.0, float(fps)), "text": TXT.content_at(clip, 0)}
            for clip in caption_clips(timeline)]
