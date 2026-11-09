"""The effect browser's catalog: categories, descriptions, capabilities and icons.

The effect classes keep their own ``category`` strings because the inspector and saved
projects depend on them. This module is the *browser-facing* layer over that metadata, so
the library can be grouped into the professional categories without renaming the engine's
effects or rewriting old projects.

Keeping the catalog in one place means the category combo, list headers, search text,
tooltips and list icons cannot disagree with each other.
"""

from __future__ import annotations

# The order is deliberate: everyday picture work first, technical/motion tools after,
# audio last. Empty categories are simply not shown by the browser.
CATEGORY_ORDER = (
    "Blur & Glow",
    "Color & Light",
    "Distortion",
    "Time & Motion",
    "Keying & Masking",
    "Tracking",
    "Generate",
    "Stylize",
    "Audio",
    "Utility",
)

# Every registered effect that is shown in the browser. ``look`` is omitted: Styles are
# their own tab and must not appear as an effect category.
CATEGORY_BY_KEY = {
    "mosaic": "Stylize",
    "ascii": "Stylize",
    "face_tracker": "Tracking",
    "hand_tracker": "Tracking",
    "blob_tracker": "Tracking",
    "color_correct": "Color & Light",
    "gaussian_blur": "Blur & Glow",
    "box_blur": "Blur & Glow",
    "directional_blur": "Blur & Glow",
    "radial_blur": "Blur & Glow",
    "zoom_blur": "Blur & Glow",
    "motion_blur": "Blur & Glow",
    "glow": "Blur & Glow",
    "chroma_key": "Keying & Masking",
    "smart_key": "Keying & Masking",
    "roto_brush": "Keying & Masking",
    "puppet_warp": "Distortion",
    "corner_pin": "Distortion",
    "perspective": "Distortion",
    "mesh_warp": "Distortion",
    "skew": "Distortion",
    "distortion": "Distortion",
    "chromatic_aberration": "Distortion",
    "glitch": "Distortion",
    "wave_warp": "Distortion",
    "bulge_pinch": "Distortion",
    "twirl": "Distortion",
    "kaleidoscope": "Distortion",
    "vhs_crt": "Stylize",
    "film_grain": "Stylize",
    "halftone": "Stylize",
    "posterize": "Stylize",
    "edge_outline": "Stylize",
    "duotone": "Color & Light",
    "invert": "Color & Light",
    "vignette": "Color & Light",
    "flash": "Color & Light",
    "exposure_flicker": "Color & Light",
    "light_leak": "Color & Light",
    "bloom_streaks": "Blur & Glow",
    "posterize_time": "Time & Motion",
    "stutter": "Time & Motion",
    "echo": "Time & Motion",
    "lut": "Color & Light",
    "simple_choker": "Keying & Masking",
    "audio_gain": "Audio",
    "audio_fade": "Audio",
    "audio_eq": "Audio",
    "audio_lowpass": "Audio",
    "audio_highpass": "Audio",
    "audio_compressor": "Audio",
    "audio_limiter": "Audio",
    "audio_reverb": "Audio",
    "audio_delay": "Audio",
    "audio_pan": "Audio",
}

# A browser-facing note, not a claim that the engine is unstable: these tools depend on
# detection in the source and are worth checking shot by shot.
EXPERIMENTAL_KEYS = {"face_tracker", "hand_tracker", "blob_tracker"}

DESCRIPTIONS = {
    "mosaic": "Turns the picture into a grid of flat colour blocks.",
    "ascii": "Rebuilds the picture from typed characters.",
    "face_tracker": "Follows a face to drive masks and effects. The reference picture has "
                    "no face or hand to detect, so the tile shows the effect icon instead.",
    "hand_tracker": "Tracks a hand so masks can follow gestures. The reference picture has "
                    "no face or hand to detect, so the tile shows the effect icon instead.",
    "blob_tracker": "Follows a high-contrast region across the shot.",
    "color_correct": "Balances exposure, contrast, saturation and colour.",
    "gaussian_blur": "Softens the picture with a smooth Gaussian falloff.",
    "box_blur": "A fast, even blur with square-edged sampling.",
    "directional_blur": "Smears the picture along a chosen direction.",
    "radial_blur": "Spins the picture around a centre point.",
    "zoom_blur": "Pulls the picture toward or away from a centre.",
    "motion_blur": "Adds shutter-style blur in the direction of movement.",
    "glow": "Adds a soft halo around the brightest areas.",
    "chroma_key": "Removes a green, blue or custom screen with a soft matte and spill control.",
    "smart_key": "Samples the edge of the shot and keys that screen automatically.",
    "roto_brush": "Stroke-guided rotoscoping: base frame, outline, temporal propagation, "
                   "refine edge, matte views and a frozen mask cache.",
    "bloom_streaks": "Stretches highlights into anamorphic streaks.",
    "puppet_warp": "Deforms the picture by pulling placed pins.",
    "corner_pin": "Moves the four corners independently for a screen or sign.",
    "perspective": "Tilts the picture in 3D space.",
    "mesh_warp": "Bends a grid of points to reshape the picture.",
    "skew": "Slants the picture horizontally or vertically.",
    "distortion": "Applies a smooth lens-style warp.",
    "chromatic_aberration": "Offsets red, green and blue for a lens fringe.",
    "glitch": "Slices and displaces rows for a digital glitch.",
    "wave_warp": "Ripples the picture with a sine wave.",
    "bulge_pinch": "Pushes the centre outward or pulls it inward.",
    "twirl": "Rotates the picture around a centre.",
    "kaleidoscope": "Mirrors the picture into a symmetrical pattern.",
    "vhs_crt": "Adds tape wobble, scanlines and CRT colour.",
    "film_grain": "Lays fine animated grain over the picture.",
    "halftone": "Rebuilds tones as a grid of dots.",
    "posterize": "Reduces each colour channel to a few flat levels.",
    "edge_outline": "Draws the picture's edges as clean lines.",
    "duotone": "Maps shadows and highlights to two colours.",
    "invert": "Flips every colour to its opposite.",
    "vignette": "Darkens the corners to draw the eye inward.",
    "flash": "Pulses the exposure like a strobe.",
    "exposure_flicker": "Adds an irregular exposure flicker.",
    "light_leak": "Washes light across the frame like a film leak.",
    "posterize_time": "Samples the picture at a lower frame rate, holding each sample.",
    "stutter": "Freezes, then jumps forward in a repeating stutter cycle.",
    "echo": "Blends the clip's earlier frames into a fading trail.",
    "lut": "Applies a colour lookup table to the whole picture.",
    "simple_choker": "Grows or shrinks an alpha edge to tighten a matte.",
    "audio_gain": "Changes the clip's level before the rest of the chain.",
    "audio_fade": "Fades the sound up at the start and down at the end.",
    "audio_eq": "Shapes low, mid and high bands with a three-band equalizer.",
    "audio_lowpass": "Rolls off everything above the cutoff frequency.",
    "audio_highpass": "Rolls off everything below the cutoff frequency.",
    "audio_compressor": "Reduces peaks dynamically so quiet details can be raised.",
    "audio_limiter": "Stops the signal from leaving above a hard ceiling.",
    "audio_reverb": "Adds the decay and reflections of a room.",
    "audio_delay": "Repeats the sound at a set interval with feedback.",
    "audio_pan": "Places the sound left or right and adjusts stereo width.",
}

# A small, coherent stroke-icon vocabulary. Audio and time effects each get their own
# glyph; picture effects share a family only where that genuinely reads as the same tool.
ICON_BY_KEY = {
    "mosaic": "mosaic",
    "ascii": "text",
    "face_tracker": "tracking_face",
    "hand_tracker": "tracking_hand",
    "blob_tracker": "tracking_blob",
    "color_correct": "color",
    "gaussian_blur": "blur",
    "box_blur": "box_blur",
    "directional_blur": "directional_blur",
    "radial_blur": "radial_blur",
    "zoom_blur": "zoom_blur",
    "motion_blur": "motion_blur",
    "glow": "glow",
    "chroma_key": "key_mask",
    "smart_key": "key_mask",
    "roto_brush": "key_mask",
    "bloom_streaks": "bloom",
    "puppet_warp": "warp",
    "corner_pin": "corner_pin",
    "perspective": "perspective",
    "mesh_warp": "mesh",
    "skew": "skew",
    "distortion": "distort",
    "chromatic_aberration": "rgb_split",
    "glitch": "glitch",
    "wave_warp": "wave",
    "bulge_pinch": "pinch",
    "twirl": "twirl",
    "kaleidoscope": "kaleidoscope",
    "vhs_crt": "vhs",
    "film_grain": "grain",
    "halftone": "halftone",
    "posterize": "posterize",
    "edge_outline": "edge",
    "duotone": "duotone",
    "invert": "invert",
    "vignette": "vignette",
    "flash": "flash",
    "exposure_flicker": "flicker",
    "light_leak": "leak",
    "posterize_time": "posterize_time",
    "stutter": "stutter",
    "echo": "echo_frames",
    "lut": "lut",
    "simple_choker": "key_mask",
    "audio_gain": "volume",
    "audio_fade": "fade",
    "audio_eq": "equalizer",
    "audio_lowpass": "lowpass",
    "audio_highpass": "highpass",
    "audio_compressor": "compressor",
    "audio_limiter": "limiter",
    "audio_reverb": "reverb",
    "audio_delay": "delay",
    "audio_pan": "pan",
}

_LEGACY_CATEGORY = {
    "Blur & Sharpen": "Blur & Glow",
    "Glow & Light": "Blur & Glow",
    "Color Correction": "Color & Light",
    "Light": "Color & Light",
    "Warp": "Distortion",
    "Glitch & Distortion": "Distortion",
    "Time": "Time & Motion",
    "Matte / Keying": "Keying & Masking",
    "Audio Effects": "Audio",
}


def key_of(effect):
    if isinstance(effect, str):
        return effect
    return str(getattr(type(effect), "key", getattr(effect, "key", "")) or "")


def category_for(effect):
    """The browser category for an effect object, class or effect key."""
    key = key_of(effect)
    category = CATEGORY_BY_KEY.get(key)
    if category:
        return category
    legacy = str(getattr(effect, "category", "") or "")
    return _LEGACY_CATEGORY.get(legacy, legacy or "Stylize")


def category_rank(category):
    try:
        return CATEGORY_ORDER.index(category)
    except ValueError:
        return len(CATEGORY_ORDER)


def available_categories(effects):
    """Categories that actually contain a browser effect, in catalog order."""
    found = {category_for(effect) for effect in effects if key_of(effect) != "look"}
    return [category for category in CATEGORY_ORDER if category in found]


def description_for(effect):
    key = key_of(effect)
    description = DESCRIPTIONS.get(key)
    if description:
        return description
    note = str(getattr(effect, "NOTE", "") or "").strip()
    return note or f"{getattr(effect, 'name', key)} effect."


def supports_keyframes(effect):
    """True when at least one numeric parameter can carry keyframes."""
    specs = getattr(effect, "PARAMS", ()) or ()
    return any(str(getattr(spec, "kind", "")) in ("float", "int") for spec in specs)


def supports_adjustment_layers(effect):
    """Adjustment layers are video composites; audio effects are not applicable."""
    media = getattr(effect, "media_types", None)
    if callable(media):
        return "video" in media()
    return "video" in tuple(getattr(effect, "MEDIA_TYPES", ()) or ())


def is_experimental(effect):
    return key_of(effect) in EXPERIMENTAL_KEYS


def icon_for(effect):
    key = key_of(effect)
    return ICON_BY_KEY.get(key, "blur" if supports_adjustment_layers(effect) else "music")


def details_for(effect):
    """Short, useful browser details for a tooltip or the selection strip."""
    key = key_of(effect)
    name = str(getattr(effect, "name", key) or key)
    category = category_for(effect)
    media = getattr(effect, "media_label", None)
    media_label = media() if callable(media) else "video"
    keyframes = supports_keyframes(effect)
    adjustment = supports_adjustment_layers(effect)
    experimental = is_experimental(effect)
    tooltip = "\n".join([
        f"{name} ({media_label})",
        description_for(effect),
        f"Category: {category}",
        f"Keyframes: {'Yes' if keyframes else 'No'}",
        f"Adjustment layers: {'Yes' if adjustment else 'No'}",
        f"Experimental: {'Yes' if experimental else 'No'}",
        "Double-click to add or remove.",
    ])
    return {"name": name, "category": category, "description": description_for(effect),
            "keyframes": keyframes, "adjustment": adjustment,
            "experimental": experimental, "media": media_label, "icon": icon_for(effect),
            "tooltip": tooltip}


def detail_line(details):
    """One compact line for the selection strip."""
    flags = [f"Keyframes: {'Yes' if details['keyframes'] else 'No'}",
             f"Adjustment: {'Yes' if details['adjustment'] else 'No'}"]
    if details["experimental"]:
        flags.append("Experimental")
    return f"{details['name']} · {details['category']}  |  " + " · ".join(flags)
