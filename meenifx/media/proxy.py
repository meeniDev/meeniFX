"""Proxy media: small stand-ins decoded instead of heavy source files.

A proxy is an ordinary MP4 next to the project (``<project>/Cache/proxy``), generated once
with ffmpeg at a reduced width. When proxy playback is on, the editor decodes the proxy
instead of the original: same frame timing, same duration, a fraction of the decode cost.

Proxies never reach an export - the engine only swaps the *preview* source path - and the
folder is disposable: deleting it costs nothing but one regeneration.
"""

from __future__ import annotations

import hashlib
import os
import subprocess

from .hardware import ffmpeg_exe

VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".mts", ".m2ts", ".wmv", ".flv"}


def is_proxyable(path):
    return bool(path) and os.path.splitext(str(path).lower())[1] in VIDEO_EXTS and os.path.exists(str(path))


def proxy_folder(project_folder):
    if not project_folder:
        return ""
    return os.path.join(str(project_folder), "Cache", "proxy")


def proxy_path(source, folder, width):
    """Deterministic proxy name: same source + width always lands on the same file."""
    if not folder or not source:
        return ""
    try:
        stat = os.stat(source)
        stamp = f"{os.path.abspath(source)}|{stat.st_size}|{int(stat.st_mtime)}|{int(width)}"
    except OSError:
        stamp = f"{source}|{int(width)}"
    digest = hashlib.blake2b(stamp.encode("utf-8", "replace"), digest_size=6).hexdigest()
    stem = os.path.splitext(os.path.basename(str(source)))[0]
    return os.path.join(folder, f"{stem}-{int(width)}-{digest}.mp4")


def make_proxy(source, target, width, fps=None, cancelled=None):
    """Encode a proxy with ffmpeg. Returns True when the file was written."""
    exe = ffmpeg_exe()
    if not exe or not is_proxyable(source):
        return False
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    command = [exe, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
               "-an", "-vf", f"scale='min({int(width)},iw)':-2", "-c:v", "libx264",
               "-preset", "veryfast", "-crf", "26", "-pix_fmt", "yuv420p",
               "-movflags", "+faststart", str(target)]
    try:
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except OSError:
        return False
    while process.poll() is None:
        if cancelled is not None and cancelled():
            process.kill()
            break
        try:
            process.wait(timeout=0.25)
        except subprocess.TimeoutExpired:
            continue
    ok = process.returncode == 0 and os.path.exists(target) and os.path.getsize(target) > 0
    if not ok:
        try:
            os.remove(target)
        except OSError:
            pass
    return ok
