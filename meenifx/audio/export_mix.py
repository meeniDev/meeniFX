"""Audio for exported videos: render the timeline mix to a WAV, then mux it into the video."""

import os
import subprocess
import wave

from .mix import render, to_pcm16

CHUNK = 48000 * 10


def has_audible(segments):
    return bool(segments)


def render_wav(segments, start_seconds, seconds, rate, path, master=1.0, cancelled=None):
    total = int(round(seconds * rate))
    with wave.open(path, "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(rate)
        done = 0
        while done < total:
            if cancelled and cancelled():
                raise InterruptedError("Export cancelled")
            count = min(CHUNK, total - done)
            block = render(segments, start_seconds + done / float(rate), count, rate, 2)
            if master != 1.0:
                block = block * float(master)
            out.writeframes(to_pcm16(block))
            done += count


def ffmpeg_exe():
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def mux(video_path, wav_path, output_path, fmt):
    codec = ["-c:a", "aac", "-b:a", "192k"] if fmt == "mp4" else ["-c:a", "pcm_s16le"]
    command = [ffmpeg_exe(), "-y", "-loglevel", "error", "-i", video_path, "-i", wav_path,
               "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", *codec, "-shortest", output_path]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    result = subprocess.run(command, capture_output=True, text=True, creationflags=flags)
    if result.returncode != 0 or not os.path.exists(output_path):
        raise RuntimeError("Could not add the audio to the video: " + (result.stderr or "ffmpeg failed").strip()[-300:])
