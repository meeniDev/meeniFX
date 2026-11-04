"""Write a small AVI that really contains video *and* audio, for import/drag tests.

There is no ffmpeg on the machines this project is developed on, and OpenCV cannot mux
sound, so the RIFF file is assembled by hand: MJPEG frames plus an interleaved 16-bit PCM
track. Both OpenCV (video) and Qt's audio decoder (sound) read it back, which is what makes
it useful as an end-to-end fixture rather than a stub.
"""
import struct

import cv2
import numpy as np

RATE = 48000


def _jpeg_frames(count, size, fps):
    frames = []
    for index in range(count):
        image = np.zeros((size[1], size[0], 3), np.uint8)
        image[:] = (28, 22, 16)
        cv2.rectangle(image, (10 + index * 3, 20), (60 + index * 3, 90), (60, 200, 120), -1)
        cv2.putText(image, str(index), (size[0] - 70, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                    (240, 240, 240), 2)
        ok, buffer = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if not ok:
            raise RuntimeError("JPEG encoding failed")
        frames.append(buffer.tobytes())
    return frames


def _pcm(seconds, rate=RATE):
    time_axis = np.arange(int(rate * seconds), dtype=np.float32) / rate
    return (0.4 * np.sin(2 * np.pi * 440.0 * time_axis) * 32767).astype("<i2").reshape(-1, 1)


def _chunk(tag, payload):
    return tag + struct.pack("<I", len(payload)) + payload + (b"\x00" if len(payload) % 2 else b"")


def _list(kind, payload):
    return _chunk(b"LIST", kind + payload)


def write_av_avi(path, seconds=2.0, audio=True, video=True, fps=25.0, size=(320, 180)):
    """Create `path`; returns it. audio=False makes a video-only file, video=False audio-only."""
    frames = _jpeg_frames(int(round(seconds * fps)), size, fps) if video else []
    samples = _pcm(seconds) if audio else None
    block = 2 * samples.shape[1] if samples is not None else 0
    per_frame = (len(samples) // max(1, len(frames))) if (audio and frames) else 0
    if block:
        per_frame -= per_frame % block
    movi, taken, biggest_audio = b"", 0, 0
    for data in frames:
        movi += _chunk(b"00dc", data)
        if per_frame:
            payload = samples[taken:taken + per_frame].tobytes()
            taken += per_frame
            biggest_audio = max(biggest_audio, len(payload))
            movi += _chunk(b"01wb", payload)
    if audio and taken < len(samples):
        payload = samples[taken:].tobytes()
        movi += _chunk(b"01wb", payload)
        biggest_audio = max(biggest_audio, len(payload))
    biggest = max([len(f) for f in frames] + [biggest_audio] + [0])

    header = _chunk(b"avih", struct.pack("<14I", int(round(1_000_000 / fps)), 0, 0, 0x100,
                                         len(frames), 0, int(bool(video)) + int(bool(audio)),
                                         biggest, size[0], size[1], 0, 0, 0, 0))
    if video:
        stream = struct.pack("<4s4sIHHIIIIIIIIhhhh", b"vids", b"MJPG", 0, 0, 0, 0, 1,
                             int(round(fps)), 0, len(frames), biggest, 0xFFFFFFFF, 0, 0, 0,
                             size[0], size[1])
        bitmap = struct.pack("<IiiHHIIiiII", 40, size[0], size[1], 1, 24,
                             struct.unpack("<I", b"MJPG")[0], biggest, 0, 0, 0, 0)
        header += _list(b"strl", _chunk(b"strh", stream) + _chunk(b"strf", bitmap))
    if audio:
        stream = struct.pack("<4s4sIHHIIIIIIIIhhhh", b"auds", b"\x00\x00\x00\x00", 0, 0, 0, 0,
                             block, RATE * block, 0, len(samples), biggest_audio, 0xFFFFFFFF,
                             block, 0, 0, 0, 0)
        wave = struct.pack("<HHIIHH", 1, samples.shape[1], RATE, RATE * block, block, 16)
        header += _list(b"strl", _chunk(b"strh", stream) + _chunk(b"strf", wave))

    body = b"AVI " + _list(b"hdrl", header) + _list(b"movi", movi)
    with open(path, "wb") as handle:
        handle.write(b"RIFF" + struct.pack("<I", len(body)) + body)
    return path
