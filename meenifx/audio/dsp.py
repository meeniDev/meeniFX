"""Audio DSP: filters, dynamics, reverb and delay, all numpy.

The design goal is honest signal processing that is fast enough to rebuild whenever a
parameter changes:

* filters are FIR kernels designed from a magnitude response and applied with
  overlap-add FFT convolution (no per-sample Python loop, no scipy dependency);
* reverb and delay are impulse responses, so the same convolution engine runs them -
  a reverb is a decaying noise tail, a delay is a train of decaying taps;
* dynamics (compressor/limiter) run in 64-sample blocks with attack/release smoothing,
  which is smooth at 48 kHz (1.3 ms per step) and stays vectorised;
* pan and gain are exact per-sample operations.

Everything works on (frames, channels) float32 arrays and never modifies its input.
"""

from __future__ import annotations

import numpy as np

EPS = 1e-9


# --------------------------------------------------------------------- helpers
def as_stereo(samples):
    """(frames, channels) float32 view of any decoded audio."""
    data = np.asarray(samples, dtype=np.float32)
    if data.ndim == 1:
        data = data[:, None]
    return data


def db_to_linear(db):
    return float(10.0 ** (float(db) / 20.0))


def linear_to_db(value):
    return 20.0 * float(np.log10(max(EPS, float(value))))


def _block_size(kernel_length):
    """FFT block size: long enough to amortise the transform, short enough to stay small."""
    return 1 << 15 if kernel_length < 4096 else 1 << 16


def fft_convolve(signal, kernel):
    """Overlap-add convolution of (frames, channels) with a 1-D mono kernel."""
    data = as_stereo(signal)
    taps = np.asarray(kernel, dtype=np.float32).ravel()
    if taps.size < 2 or data.shape[0] == 0:
        return data.copy()
    frames, channels = data.shape
    block = _block_size(taps.size)
    nfft = 1
    while nfft < block + taps.size - 1:
        nfft <<= 1
    spectrum = np.fft.rfft(taps, nfft)
    out = np.zeros((frames + taps.size - 1, channels), dtype=np.float64)
    for start in range(0, frames, block):
        chunk = data[start:start + block]
        if chunk.shape[0] == 0:
            break
        for channel in range(channels):
            piece = np.fft.irfft(np.fft.rfft(chunk[:, channel], nfft) * spectrum, nfft)
            stop = min(out.shape[0], start + piece.size)
            out[start:stop, channel] += piece[:stop - start]
    return out[:frames].astype(np.float32)


# ------------------------------------------------------------ filter design
def _response_to_kernel(rate, magnitude, taps=None, normalize_at=None):
    """Linear-phase FIR kernel with the given magnitude response (0..rate/2).

    `normalize_at` is a frequency where the response is known to be 1.0 (the passband of
    a low-pass, the top of a high-pass). Normalising there keeps a filter from changing
    the level by itself - normalising by the kernel's DC sum instead would silently
    rescale shelves and high-passes into nonsense.
    """
    rate = max(8000, int(rate))
    taps = taps or 257
    taps = max(33, int(taps) | 1)
    nfft = 1 << 12
    freqs = np.fft.rfftfreq(nfft, 1.0 / rate)
    mag = np.clip(np.asarray(magnitude(freqs), dtype=np.float64), 0.0, 64.0)
    spec = np.concatenate([mag, mag[-2:0:-1]])
    impulse = np.fft.fftshift(np.fft.irfft(spec, nfft))
    centre = len(impulse) // 2
    half = taps // 2
    kernel = impulse[centre - half: centre + half + 1] * np.hanning(taps)
    if normalize_at is not None:
        frequency = float(normalize_at)
        design = float(np.asarray(magnitude(np.array([frequency]))).ravel()[0])
        # Compare the design with what the windowed kernel actually does at that
        # frequency, so the passband sits on the intended level instead of the
        # windowing loss (about 0.3 dB) quietly trimming every filter.
        probe = np.fft.rfft(kernel, 8192)
        bins = np.fft.rfftfreq(8192, 1.0 / rate)
        measured = float(np.interp(frequency, bins, np.abs(probe)))
        if design > 1e-6 and measured > 1e-6:
            kernel = kernel * (design / measured)
    return kernel.astype(np.float32)


def _butterworth_low(rate, cutoff, order):
    cutoff = float(max(20.0, min(rate * 0.49, cutoff)))
    order = max(1, int(order))

    def magnitude(freqs):
        ratio = np.maximum(freqs, 1e-6) / cutoff
        return 1.0 / np.sqrt(1.0 + ratio ** (2 * order))
    return magnitude


def _butterworth_high(rate, cutoff, order):
    cutoff = float(max(20.0, min(rate * 0.49, cutoff)))
    order = max(1, int(order))

    def magnitude(freqs):
        ratio = np.maximum(freqs, 1e-6) / cutoff
        top = ratio ** (2 * order)
        return np.sqrt(top / (1.0 + top))
    return magnitude


def lowpass_kernel(rate, cutoff, order=4, taps=None):
    return _response_to_kernel(rate, _butterworth_low(rate, cutoff, order), taps,
                               normalize_at=20.0)


def highpass_kernel(rate, cutoff, order=4, taps=None):
    return _response_to_kernel(rate, _butterworth_high(rate, cutoff, order), taps,
                               normalize_at=max(2000.0, min(rate * 0.45, 12000.0)))


def _shelf_low_db(freqs, corner, gain_db):
    """0 dB well below the corner, `gain_db` well above it (log-frequency S curve)."""
    ratio = np.maximum(freqs, 1.0) / max(10.0, corner)
    return gain_db / (1.0 + ratio ** 2)


def _shelf_high_db(freqs, corner, gain_db):
    ratio = np.maximum(freqs, 1.0) / max(10.0, corner)
    return gain_db * (ratio ** 2) / (1.0 + ratio ** 2)


def _bell_db(freqs, centre, gain_db, q):
    centre = max(20.0, centre)
    width = max(0.05, centre / max(0.1, q))
    return gain_db * np.exp(-0.5 * ((freqs - centre) / width) ** 2)


def eq_kernel(rate, low_freq=120.0, low_gain=0.0, mid_freq=1000.0, mid_gain=0.0, mid_q=1.0,
              high_freq=6000.0, high_gain=0.0, taps=None):
    """Low shelf + bell + high shelf in one linear-phase kernel."""
    def magnitude(freqs):
        total_db = (_shelf_low_db(freqs, low_freq, low_gain)
                    + _bell_db(freqs, mid_freq, mid_gain, mid_q)
                    + _shelf_high_db(freqs, high_freq, high_gain))
        return 10.0 ** (total_db / 20.0)
    return _response_to_kernel(rate, magnitude, taps or 1025, normalize_at=1000.0)


# ------------------------------------------------------------------- dynamics
def compress(signal, rate, threshold_db=-18.0, ratio=3.0, attack_ms=12.0, release_ms=150.0,
             makeup_db=0.0, knee_db=6.0, state=None):
    """Feed-forward compressor with a soft knee, in 64-sample steps.

    `state` keeps the envelope across calls, so a chain can feed the clip in chunks
    (with the parameters resolved per chunk) and still sound continuous.
    """
    data = as_stereo(signal)
    if data.shape[0] == 0:
        return data.copy()
    ratio = max(1.0, float(ratio))
    threshold_db = float(np.clip(threshold_db, -60.0, 0.0))
    knee_db = max(0.0, float(knee_db))
    attack = float(np.clip(attack_ms, 0.1, 500.0)) / 1000.0
    release = float(np.clip(release_ms, 5.0, 3000.0)) / 1000.0
    block = 64
    frames = data.shape[0]
    out = np.empty_like(data)
    # One-pole coefficients per block: the block is the sample rate of this loop, so a
    # 12 ms attack must be exp(-(block/rate)/0.012), not exp(-1/(0.012*rate)).
    block_seconds = block / float(rate)
    attack_coeff = float(np.exp(-block_seconds / max(1e-6, attack)))
    release_coeff = float(np.exp(-block_seconds / max(1e-6, release)))
    envelope = float((state or {}).get("envelope", 0.0))   # gain reduction, dB (<= 0)
    for start in range(0, frames, block):
        chunk = data[start:start + block]
        peak = float(np.max(np.abs(chunk)))
        level_db = linear_to_db(peak)
        over = level_db - threshold_db
        if over <= -knee_db / 2.0:
            reduction = 0.0
        elif over >= knee_db / 2.0:
            reduction = over * (1.0 - 1.0 / ratio)
        else:
            # Quadratic soft knee: no abrupt gain change where the knee starts.
            x = over + knee_db / 2.0
            reduction = (1.0 - 1.0 / ratio) * x * x / (2.0 * knee_db)
        target = -reduction
        step = attack_coeff if target < envelope else release_coeff
        envelope = step * envelope + (1.0 - step) * target
        gain = db_to_linear(envelope + makeup_db)
        out[start:start + block] = chunk * gain
    if state is not None:
        state["envelope"] = envelope
    return out


def limit(signal, rate, ceiling_db=-1.0, release_ms=80.0, lookahead_ms=3.0, state=None):
    """Brick-wall limiter: fast attack, hard ceiling, no inter-sample overs."""
    data = as_stereo(signal)
    if data.shape[0] == 0:
        return data.copy()
    ceiling = db_to_linear(np.clip(ceiling_db, -24.0, 0.0))
    release = float(np.clip(release_ms, 5.0, 2000.0)) / 1000.0
    lookahead = int(max(0, min(data.shape[0] - 1, round(float(lookahead_ms) * rate / 1000.0))))
    peak = np.max(np.abs(data), axis=1)
    if lookahead > 0:
        # Look at the next few milliseconds too, so the gain is already down when the
        # transient arrives instead of letting it through and ducking afterwards.
        ahead = np.empty_like(peak)
        ahead[:peak.size - lookahead] = peak[lookahead:]
        ahead[peak.size - lookahead:] = peak[-1]
        peak = np.maximum(peak, ahead)
    wanted = np.minimum(1.0, ceiling / np.maximum(peak, EPS))
    step = float(np.exp(-1.0 / max(1e-6, release * rate)))
    smoothed = np.empty_like(wanted)
    current = float((state or {}).get("gain", 1.0))
    for index in range(wanted.size):
        target = float(wanted[index])
        # Attack instantly (that is the point of a limiter), release smoothly.
        current = target if target < current else step * current + (1.0 - step) * target
        smoothed[index] = current
    if state is not None:
        state["gain"] = current
    return np.clip(data * smoothed[:, None], -ceiling, ceiling)


# ------------------------------------------------------------ reverb / delay
def reverb_kernel(rate, room_size=0.5, decay=1.6, damping=0.4, seed=7):
    """A decaying noise tail: room length from `room_size`, brightness from `damping`."""
    rate = max(8000, int(rate))
    length = int(max(0.05, min(4.0, 0.2 + 1.8 * float(room_size))) * rate)
    rng = np.random.default_rng(int(seed))
    tail = rng.standard_normal(length).astype(np.float32)
    seconds = np.arange(length, dtype=np.float32) / rate
    tail *= np.exp(-seconds / max(0.05, float(decay)))
    # Damping: a one-pole low pass over the tail, so the reverb darkens as it decays.
    amount = float(np.clip(damping, 0.0, 0.95))
    if amount > 0.01:
        kernel = np.ones(8, dtype=np.float32) / 8.0
        tail = np.convolve(tail, kernel, mode="same").astype(np.float32)
        tail *= 1.0 - amount * 0.5
    tail[: int(0.004 * rate)] *= np.linspace(0.0, 1.0, tail[: int(0.004 * rate)].size)
    energy = float(np.sqrt(np.sum(tail ** 2)))
    if energy > 1e-9:
        tail /= energy / 0.9
    return tail


def delay_kernel(rate, time_ms=320.0, feedback=0.35, repeats=6):
    """A train of decaying taps: the classic feedback echo, as an impulse response."""
    rate = max(8000, int(rate))
    step = int(max(1, round(np.clip(time_ms, 5.0, 2000.0) / 1000.0 * rate)))
    repeats = max(1, min(16, int(repeats)))
    feedback = float(np.clip(feedback, 0.0, 0.95))
    length = step * repeats + 1
    kernel = np.zeros(length, dtype=np.float32)
    gain = 1.0
    for index in range(repeats):
        position = step * (index + 1)
        if position >= length:
            break
        kernel[position] = gain
        gain *= feedback
    return kernel


# ------------------------------------------------------------------- mixing
def pan_gains(pan):
    """Constant-power pan gains for -1 (left) .. +1 (right)."""
    value = float(np.clip(pan, -1.0, 1.0))
    angle = (value + 1.0) * np.pi / 4.0
    return float(np.cos(angle)), float(np.sin(angle))


def apply_pan(signal, pan, width=1.0):
    data = as_stereo(signal)
    left_gain, right_gain = pan_gains(pan)
    width = float(np.clip(width, 0.0, 2.0))
    out = np.empty_like(data)
    if data.shape[1] == 1:
        mono = data[:, 0]
        out = np.stack([mono * left_gain, mono * right_gain], axis=1)
        return out
    mid = (data[:, 0] + data[:, 1]) * 0.5
    side = (data[:, 0] - data[:, 1]) * 0.5 * width
    out[:, 0] = (mid + side) * left_gain
    out[:, 1] = (mid - side) * right_gain
    return out


def apply_gain(signal, gain_db):
    return as_stereo(signal) * db_to_linear(gain_db)


def apply_fades(signal, rate, fade_in_seconds=0.0, fade_out_seconds=0.0):
    data = as_stereo(signal).copy()
    frames = data.shape[0]
    fade_in = int(max(0.0, float(fade_in_seconds)) * rate)
    fade_out = int(max(0.0, float(fade_out_seconds)) * rate)
    if fade_in > 0:
        fade_in = min(fade_in, frames)
        ramp = np.linspace(0.0, 1.0, fade_in, dtype=np.float32)
        data[:fade_in] *= ramp[:, None]
    if fade_out > 0:
        fade_out = min(fade_out, frames)
        ramp = np.linspace(1.0, 0.0, fade_out, dtype=np.float32)
        data[frames - fade_out:] *= ramp[:, None]
    return data


def soft_clip(signal, ceiling=0.98):
    """Tanh-style saturation as a final safety net, never a hard digital clip."""
    data = as_stereo(signal)
    ceiling = float(max(0.05, min(1.0, ceiling)))
    return np.tanh(data / ceiling) * ceiling


def peak_db(signal):
    data = as_stereo(signal)
    return linear_to_db(float(np.max(np.abs(data))) if data.size else 0.0)
