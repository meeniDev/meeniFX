"""Put recorded sound inside the recorded video.

Screen and webcam recorders write video only, so the microphone and system-sound WAVs are
mixed and muxed into each video segment afterwards. The muxed file is what goes on the
timeline, which is why a screen recording can be dragged, trimmed and moved as one linked
picture-and-sound pair. The original WAVs stay next to it as the uncompressed masters.
"""

import os
import subprocess

from ..audio.export_mix import ffmpeg_exe

VIDEO_KINDS = ("screen", "webcam")
AUDIO_KINDS = ("mic", "system")


def _run(command, timeout=600):
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                          creationflags=flags)


def mux_into_video(video_path, audio_paths, output_path):
    """Copy the video stream and mix every audio file into one AAC track.

    `normalize=0` keeps the levels the user set with the recorder's gain sliders: the default
    amix normalisation would turn a quiet voice down whenever the system sound is playing.
    """
    inputs = ["-i", video_path]
    for path in audio_paths:
        inputs += ["-i", path]
    if len(audio_paths) > 1:
        labels = "".join(f"[{index + 1}:a]" for index in range(len(audio_paths)))
        chain = (f"{labels}amix=inputs={len(audio_paths)}:duration=longest:normalize=0,"
                 "aformat=sample_fmts=fltp:channel_layouts=stereo[aout]")
    else:
        chain = "[1:a]aformat=sample_fmts=fltp:channel_layouts=stereo[aout]"
    command = [ffmpeg_exe(), "-y", "-loglevel", "error", *inputs,
               "-filter_complex", chain, "-map", "0:v:0", "-map", "[aout]",
               "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", output_path]
    result = _run(command)
    if result.returncode != 0 or not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        raise RuntimeError((result.stderr or "ffmpeg failed").strip()[-300:])
    return output_path


def mux_recording(result, progress=None):
    """Mux audio into every video segment, in place. Returns (muxed kinds, warnings)."""
    warnings = []
    muxed = set()
    audio_by_kind = {kind: result.segments.get(kind, []) for kind in AUDIO_KINDS}
    if not any(audio_by_kind.values()):
        return muxed, warnings
    for kind in VIDEO_KINDS:
        segments = result.segments.get(kind) or []
        if not segments:
            continue
        changed = False
        for index, segment in enumerate(segments):
            audio_paths = []
            for audio_kind in AUDIO_KINDS:
                tracks = audio_by_kind.get(audio_kind) or []
                if index < len(tracks) and os.path.isfile(tracks[index].path):
                    audio_paths.append(tracks[index].path)
            if not audio_paths:
                continue
            if progress:
                progress(kind, index)
            target = os.path.splitext(segment.path)[0] + " (with sound).mkv"
            try:
                mux_into_video(segment.path, audio_paths, target)
            except Exception as exc:
                warnings.append(f"Could not add the sound to the {kind} video: {exc}")
                continue
            segment.muxed = True
            segment.audio_paths = list(audio_paths)
            segment.path = target
            changed = True
        if changed:
            muxed.add(kind)
    return muxed, warnings
