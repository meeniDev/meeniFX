"""Put a finished recording on the timeline: one clip per source and segment, one undo step."""

import os
import wave

from ..media.reader import probe_media


def recordings_folder(project_folder, configured):
    if configured:
        return configured
    if project_folder:
        return os.path.join(project_folder, "Recordings")
    return os.path.join(os.path.expanduser("~"), "Documents", "meeniFX Recordings")


def probe_segment(path):
    """(seconds, source_frames, source_fps) of a finished recording file."""
    if path.lower().endswith(".wav"):
        with wave.open(path) as w:
            return w.getnframes() / float(w.getframerate()), 0, 0.0
    _kind, frames, fps = probe_media(path)
    return frames / max(1e-6, fps), frames, fps


def _spans(items, fps):
    """One dict per segment, each trimmed so it ends before the next segment starts."""
    out = []
    for i, item in enumerate(items):
        frames = max(1, int(round(item["seconds"] * fps)))
        if i + 1 < len(items):
            frames = min(frames, max(1, items[i + 1]["start"] - item["start"]))
        out.append({**item, "frames": frames})
    return out


def _lane_is_free(lane, spans):
    if lane.locked:
        return False
    for span in spans:
        for clip in lane.clips:
            if clip.timeline_start < span["start"] + span["frames"] and clip.end > span["start"]:
                return False
    return True


def _free_lane(timeline, ttype, spans, used):
    lanes = [t for t in timeline.tracks_of_type(ttype) if t not in used]
    order = reversed(lanes) if ttype == "video" else lanes
    lane = next((t for t in order if _lane_is_free(t, spans)), None)
    if lane is None:
        lane = timeline.add_track(ttype)
    used.add(lane)
    return lane


def insert_recording(timeline, stack, result, probe=probe_segment):
    """Place every recorded segment; all sources begin on the same frame.

    The microphone and the system sound are *never* mixed here: each one becomes its own clip
    on its own audio lane, so they keep separate gain, effects and mute switches. The picture
    (screen or webcam) sits on a video lane and all three share one link group, so they move,
    trim and split together while still being editable on their own.

    A legacy muxed segment (its sound was written inside the file by an older version) still
    becomes a linked video+audio pair; the WAV that went into it is not inserted again, which
    would play it twice.
    """
    fps = timeline.fps
    covered = {path for segments in result.segments.values() for segment in segments
               for path in getattr(segment, "audio_paths", ()) or ()}
    plan = {}
    for kind, segments in result.segments.items():
        items = []
        for seg in segments:
            if kind in ("mic", "system") and seg.path in covered:
                continue
            try:
                seconds, src_frames, src_fps = probe(seg.path)
            except Exception:
                continue
            if seconds <= 0:
                continue
            items.append({"path": seg.path,
                          "start": result.start_frame + int(round(seg.offset * fps)),
                          "seconds": seconds, "src_frames": src_frames, "src_fps": src_fps,
                          "muxed": bool(getattr(seg, "muxed", False))})
        if items:
            plan[kind] = _spans(items, fps)

    # How many clips this take will make: a legacy muxed video adds a second sound clip.
    total = sum(2 if span["muxed"] and kind not in ("mic", "system") else 1
                for kind, spans in plan.items() for span in spans)

    def action():
        used, ids = set(), []
        # One group for the whole take: picture, voice and system sound stay together in
        # time, but each clip keeps its own lane, gain, effects and mute. A take with a
        # single clip has nothing to link to.
        link = timeline.new_link_id() if total > 1 else 0
        # Screen first, so the newest (top) video track is the webcam picture-in-picture layer.
        for kind in ("screen", "webcam", "mic", "system"):
            spans = plan.get(kind)
            if not spans:
                continue
            ttype = "audio" if kind in ("mic", "system") else "video"
            lane = _free_lane(timeline, ttype, spans, used)
            for span in spans:
                path = span["path"]
                name = os.path.splitext(os.path.basename(path))[0]
                if ttype == "audio":
                    clip = timeline.new_clip(name, path, lane, span["start"], span["frames"],
                                             link_id=link)
                    ids.append(clip.id)
                    continue
                clip = timeline.new_clip(name, path, lane, span["start"], span["frames"], 0,
                                         source_out=span["src_frames"], source_fps=span["src_fps"],
                                         link_id=link)
                ids.append(clip.id)
                if not span["muxed"]:
                    continue
                # Only an already-muxed file needs the second, silent-looking audio clip.
                sound_lane = _free_lane(timeline, "audio", [span], used)
                sound = timeline.new_clip(name, path, sound_lane, span["start"], span["frames"], 0,
                                          source_out=span["src_frames"],
                                          source_fps=span["src_fps"], link_id=link,
                                          stream="audio")
                ids.append(sound.id)
        return ids

    return stack.run("Add recording", action)
