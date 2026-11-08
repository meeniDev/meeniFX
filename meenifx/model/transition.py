"""Automatic overlap transitions between clips on one track.

A transition is a *relationship*, not a hidden second clip: the two clips keep their
full source ranges and durations, and the overlap between them is the transition region.
Timeline keeps the records; the compositor and the audio mixer read them, so moving,
trimming or deleting either clip can never leave stale geometry behind - a record whose
overlap disappeared is simply pruned.

Only the crossfade/dissolve is implemented. The constants are shaped so another kind
(dip to black/white, wipe, slide, blur dissolve) can be added here and in the
compositor without changing the file format.
"""

from dataclasses import dataclass

KINDS = ("crossfade",)
KIND_LABELS = {"crossfade": "Crossfade"}

VIDEO_CURVES = (("linear", "Linear"), ("ease_in_out", "Ease In/Out"), ("smooth", "Smoothstep"))
AUDIO_CURVES = (("equal_power", "Equal Power"), ("linear", "Linear"))
DEFAULT_VIDEO_CURVE = "smooth"
DEFAULT_AUDIO_CURVE = "equal_power"


def normalise_kind(kind):
    key = str(kind or "").strip().lower().replace(" ", "_").replace("-", "_")
    return key if key in KINDS else KINDS[0]


def video_curve_names():
    return tuple(name for name, _label in VIDEO_CURVES)


def audio_curve_names():
    return tuple(name for name, _label in AUDIO_CURVES)


def normalise_video_curve(curve):
    key = str(curve or "").strip().lower().replace(" ", "_").replace("-", "_")
    return key if key in video_curve_names() else DEFAULT_VIDEO_CURVE


def normalise_audio_curve(curve):
    key = str(curve or "").strip().lower().replace(" ", "_").replace("-", "_")
    return key if key in audio_curve_names() else DEFAULT_AUDIO_CURVE


def video_weight(progress, curve=DEFAULT_VIDEO_CURVE):
    """Incoming clip's weight at `progress` (0 = overlap start, 1 = overlap end).

    The weight is the amount of the incoming picture; the outgoing clip gets 1 - weight,
    so an opaque pair always sums to full coverage and the mix can never dip to black.
    """
    t = max(0.0, min(1.0, float(progress)))
    curve = normalise_video_curve(curve)
    if curve == "linear":
        return t
    if curve == "ease_in_out":
        # Cubic ease-in-out: slower at both ends than smoothstep, and symmetric.
        return 4.0 * t * t * t if t < 0.5 else 1.0 - pow(-2.0 * t + 2.0, 3.0) / 2.0
    return t * t * (3.0 - 2.0 * t)


def audio_weight(progress, curve=DEFAULT_AUDIO_CURVE):
    """Incoming clip's linear gain at `progress`.

    Equal power is the default because two unrelated sources crossfaded linearly lose
    about 3 dB through the middle; sin/cos keeps the summed power constant.
    """
    t = max(0.0, min(1.0, float(progress)))
    curve = normalise_audio_curve(curve)
    if curve == "equal_power":
        import math
        return math.sin(t * math.pi / 2.0)
    return t


@dataclass
class Transition:
    """The settings for one overlapping pair on one track.

    `out_id` is the clip on the left (its picture/audio fades out), `in_id` the clip on
    the right. The overlap window is always derived from the live clips, never stored.
    """

    out_id: int
    in_id: int
    kind: str = "crossfade"
    curve: str = DEFAULT_VIDEO_CURVE
    audio_curve: str = DEFAULT_AUDIO_CURVE

    def __post_init__(self):
        self.out_id = int(self.out_id)
        self.in_id = int(self.in_id)
        self.kind = normalise_kind(self.kind)
        self.curve = normalise_video_curve(self.curve)
        self.audio_curve = normalise_audio_curve(self.audio_curve)

    def state(self):
        """Tuple form used by undo snapshots; order matches ``Transition(*state)``."""
        return (self.out_id, self.in_id, self.kind, self.curve, self.audio_curve)

    def as_dict(self):
        return {"out": self.out_id, "in": self.in_id, "kind": self.kind,
                "curve": self.curve, "audio_curve": self.audio_curve}

    @classmethod
    def from_dict(cls, row):
        return cls(int(row["out"]), int(row["in"]), row.get("kind", "crossfade"),
                   row.get("curve", DEFAULT_VIDEO_CURVE),
                   row.get("audio_curve", DEFAULT_AUDIO_CURVE))

    @property
    def label(self):
        return KIND_LABELS.get(self.kind, "Crossfade")
