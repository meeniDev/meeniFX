"""Local speech recognition (optional faster-whisper backend).

Importing this module is always safe: the heavy dependency is imported only when a
transcription runs, so a build without it still starts, edits and exports. Models are
downloaded only after the user agrees, into the per-user data folder, and can be deleted
again from Preferences. No account, no API key, and once a model is on disk the whole
pipeline works offline.
"""

import os
import shutil

import numpy as np

from ..project.storage import DATA_DIR

MODEL_DIR = os.path.join(DATA_DIR, "models", "whisper")
SAMPLE_RATE = 16000

QUALITY = {
    "fast": {"model": "tiny", "size": "~75 MB", "label": "Fast"},
    "balanced": {"model": "base", "size": "~145 MB", "label": "Balanced"},
    "accurate": {"model": "small", "size": "~480 MB", "label": "Accurate"},
}

LANGUAGES = (
    ("Auto Detect", ""),
    ("English", "en"),
    ("Japanese", "ja"),
    ("Korean", "ko"),
    ("Chinese", "zh"),
    ("Spanish", "es"),
    ("French", "fr"),
    ("German", "de"),
    ("Portuguese", "pt"),
    ("Italian", "it"),
    ("Russian", "ru"),
    ("Hindi", "hi"),
    ("Indonesian", "id"),
    ("Turkish", "tr"),
)


def backend_available():
    """True when faster-whisper is installed in this environment."""
    try:
        import faster_whisper  # noqa: F401
    except Exception:
        return False
    return True


def install_hint():
    return ("Automatic captions need the optional faster-whisper package.\n\n"
            "Install it once with:\n    pip install faster-whisper\n\n"
            "Transcription then runs locally and offline - no account, no API key.")


def model_name(quality):
    return QUALITY.get(quality, QUALITY["balanced"])["model"]


def model_folder(name):
    return os.path.join(MODEL_DIR, name)


def model_installed(name):
    folder = model_folder(name)
    if not os.path.isdir(folder):
        return False
    for root, _dirs, files in os.walk(folder):
        if any(os.path.getsize(os.path.join(root, fn)) > 0 for fn in files):
            return True
    return False


def installed_models():
    """[(name, bytes)] for the models that are already on disk."""
    result = []
    if not os.path.isdir(MODEL_DIR):
        return result
    for name in sorted(os.listdir(MODEL_DIR)):
        folder = os.path.join(MODEL_DIR, name)
        if not os.path.isdir(folder):
            continue
        payload = 0
        for root, _dirs, files in os.walk(folder):
            for fn in files:
                try:
                    payload += os.path.getsize(os.path.join(root, fn))
                except OSError:
                    pass
        if payload:
            result.append((name, payload))
    return result


def delete_model(name):
    """Remove one downloaded model. Returns True when something was deleted."""
    folder = model_folder(name)
    if not os.path.isdir(folder):
        return False
    shutil.rmtree(folder, ignore_errors=True)
    return True


def _to_16k_mono(samples, rate):
    audio = np.asarray(samples, dtype=np.float32)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if int(rate) == SAMPLE_RATE or audio.size == 0:
        return np.ascontiguousarray(audio, dtype=np.float32)
    duration = audio.size / float(rate)
    count = max(1, int(round(duration * SAMPLE_RATE)))
    positions = np.linspace(0.0, audio.size - 1.0, count, dtype=np.float64)
    index = np.floor(positions).astype(np.int64)
    nxt = np.minimum(index + 1, audio.size - 1)
    frac = (positions - index).astype(np.float32)
    return np.ascontiguousarray(audio[index] * (1.0 - frac) + audio[nxt] * frac, dtype=np.float32)


def transcribe(samples, sample_rate=SAMPLE_RATE, language="", quality="balanced",
               on_progress=None, cancelled=None):
    """Transcribe mono audio. Returns {"language", "segments"}.

    Runs on the calling (worker) thread. `on_progress(fraction, message)` is optional and
    `cancelled()` is polled between segments, so Cancel stops without leaving anything
    half-registered: the caller only writes captions after this function has returned.
    """
    if not backend_available():
        raise RuntimeError(install_hint())
    from faster_whisper import WhisperModel

    def report(fraction, message):
        if on_progress is not None:
            on_progress(fraction, message)

    name = model_name(quality)
    first_time = not model_installed(name)
    if first_time:
        report(0.0, f"Downloading the {name} model ({QUALITY.get(quality, QUALITY['balanced'])['size']})…"
                    "  This happens once.")
    else:
        report(0.0, f"Loading the {name} model…")
    os.makedirs(MODEL_DIR, exist_ok=True)
    try:
        model = WhisperModel(name, device="cpu", compute_type="int8", download_root=MODEL_DIR)
    except Exception as exc:
        raise RuntimeError(f"The speech model could not be prepared: {exc}") from exc
    if cancelled is not None and cancelled():
        raise InterruptedError("cancelled")

    audio = _to_16k_mono(samples, sample_rate)
    if audio.size == 0:
        raise RuntimeError("There is no audio to transcribe in that range.")
    seconds = audio.size / float(SAMPLE_RATE)
    report(0.02, "Transcribing…")
    try:
        segments, info = model.transcribe(
            audio,
            language=language or None,
            word_timestamps=True,
            vad_filter=True,
            beam_size=1 if quality == "fast" else 5,
        )
    except Exception as exc:
        raise RuntimeError(f"Transcription failed: {exc}") from exc

    collected = []
    for segment in segments:
        if cancelled is not None and cancelled():
            raise InterruptedError("cancelled")
        words = [{"start": float(word.start), "end": float(word.end), "word": str(word.word)}
                 for word in (getattr(segment, "words", None) or ())]
        collected.append({"start": float(segment.start), "end": float(segment.end),
                          "text": str(segment.text).strip(), "words": words})
        if seconds > 0:
            report(min(0.99, max(0.05, float(segment.end) / seconds)), "Transcribing…")
    report(1.0, "Transcribed")
    return {"language": str(getattr(info, "language", "") or language or ""),
            "language_probability": float(getattr(info, "language_probability", 0.0) or 0.0),
            "duration": seconds, "segments": collected}
