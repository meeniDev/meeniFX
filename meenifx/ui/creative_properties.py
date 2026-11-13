"""Properties card for masks, track mattes and motion paths.

Kept out of the already-large panels module so the controls can reuse its helpers without
duplicating a second inspector. Every edit goes through TimelineSession, which means the
same operations are undoable and serialize with the clip's transform metadata.
"""

from __future__ import annotations

from PyQt6.QtWidgets import (QCheckBox, QComboBox, QHBoxLayout, QLabel, QLineEdit,
                             QToolButton, QWidget)

from .. import tokens
from ..model import alpha as ALPHA
from ..model import channels as CH
from ..model import mask as MASK
from ..model import matte as MATTE
from ..model import motion as MOTION
from ..model import path as PATHS
from .widgets import ScrubField


def creative_signature(clip):
    """Structure key fragment so a new/removed or renamed mask rebuilds the card once."""
    motion = MOTION.current(clip) or {}
    return (tuple((m.get("id"), m.get("name"), m.get("mode"), bool(m.get("invert")),
                   bool(m.get("visible", True)), bool(m.get("show_outline", True)))
                  for m in MASK.masks(clip)),
            (MATTE.ensure(clip).get("source_id"), MATTE.ensure(clip).get("mode"),
             bool(MATTE.ensure(clip).get("invert"))),
            bool(ALPHA.inverted(clip)),
            (bool(motion.get("enabled")), motion.get("mode") or "replace",
             len(motion.get("path", {}).get("points") or ())))


def _note(text):
    label = QLabel(text)
    label.setWordWrap(True)
    label.setProperty("quiet", True)
    return label


def add_creative_card(panel, clip):
    frame, outer = panel._card("creative")
    header = QHBoxLayout()
    header.setContentsMargins(0, 0, 0, 0)
    title = QLabel("Masks, Mattes & Motion")
    title.setStyleSheet(f"font-size: {tokens.FONT_TITLE}px; font-weight: 600;")
    header.addWidget(title)
    header.addStretch(1)
    outer.addLayout(header)
    body, form = panel._form()

    def changed():
        panel._notify()

    def mask_edit(mask_id, **values):
        panel.engine.session.update_clip_mask(clip.id, mask_id, values)
        panel.refresh_properties()
        changed()

    # --------------------------------------------------------------------- masks
    panel._section_label(form, "Masks")
    masks = MASK.masks(clip)
    if not masks:
        form.addWidget(_note("No masks. Choose Pen > Draw Mask (G) and draw on this clip."))
    for mask in masks:
        mask_id = int(mask.get("id", 0))
        name = QLineEdit(mask.get("name") or f"Mask {mask_id}")
        name.setToolTip("Rename this mask; the name is saved with the project")
        name.editingFinished.connect(
            lambda field=name, mid=mask_id: mask_edit(mid, name=field.text().strip()))
        row = QWidget()
        line = QHBoxLayout(row)
        line.setContentsMargins(0, 0, 0, 0)
        mode = QComboBox()
        for key in MASK.MODES:
            mode.addItem(MASK.MODE_LABELS.get(key, key.title()), key)
        mode.setCurrentIndex(max(0, mode.findData(mask.get("mode"))))
        mode.activated.connect(lambda index, box=mode, mid=mask_id:
                               mask_edit(mid, mode=box.itemData(index)))
        space = QComboBox()
        for key in MASK.SPACES:
            space.addItem(MASK.SPACE_LABELS.get(key, key.title()), key)
        space.setCurrentIndex(max(0, space.findData(mask.get("space", "composition"))))
        space.setToolTip("Layer space follows this clip's position, scale and rotation; "
                         "composition space stays fixed in the frame. Switching keeps the "
                         "mask's look at the playhead")
        space.activated.connect(lambda index, box=space, mid=mask_id:
                                _set_mask_space(panel, clip, mid, box.itemData(index)))
        invert = QCheckBox("Invert Mask")
        invert.setChecked(bool(mask.get("invert")))
        invert.setToolTip("Invert this mask's feathered/expanded coverage (before mask "
                          "opacity and the clip's other alpha operations)")
        invert.toggled.connect(lambda on, mid=mask_id: mask_edit(mid, invert=bool(on)))
        visible = QCheckBox("Visible")
        visible.setChecked(bool(mask.get("visible", True)))
        visible.setToolTip("Hide the mask effect without deleting or disabling it")
        visible.toggled.connect(lambda on, mid=mask_id: mask_edit(mid, visible=bool(on)))
        outline = QCheckBox("Outline")
        outline.setChecked(bool(mask.get("show_outline", True)))
        outline.setToolTip("Hide only the preview guide; the mask still renders")
        outline.toggled.connect(lambda on, mid=mask_id: mask_edit(mid, show_outline=bool(on)))
        edit = QToolButton()
        edit.setText("Edit Mask")
        edit.setToolTip("Edit this mask's points and Bezier handles in the preview")
        edit.clicked.connect(lambda _=False, mid=mask_id: _edit_mask(panel, clip, mid))
        duplicate = QToolButton()
        duplicate.setText("Duplicate")
        duplicate.clicked.connect(lambda _=False, mid=mask_id: _duplicate_mask(panel, clip, mid))
        key_path = QToolButton()
        key_path.setText("Key path")
        key_path.setToolTip("Store the current mask path as a keyframe at the playhead")
        key_path.clicked.connect(lambda _=False, mid=mask_id: _key_mask(panel, clip, mid))
        remove = QToolButton()
        remove.setText("Remove")
        remove.clicked.connect(lambda _=False, mid=mask_id: _remove_mask(panel, clip, mid))
        line.addWidget(mode, 1)
        line.addWidget(invert)
        line.addWidget(visible)
        line.addWidget(outline)
        line.addWidget(edit)
        line.addWidget(duplicate)
        line.addWidget(key_path)
        line.addWidget(remove)
        form.addWidget(panel._labelled("Name", name, "Mask name"))
        form.addWidget(panel._labelled("Operation", row, "Mask operation on this clip"))
        form.addWidget(panel._labelled("Space", space, "Mask coordinate space"))
        for field_name, label, low, high, unit in (("feather", "Feather", 0.0, 200.0, "px"),
                                                   ("expansion", "Expansion", -200.0, 200.0, "px"),
                                                   ("opacity", "Opacity", 0.0, 100.0, "%")):
            field = ScrubField(label, float(mask.get(field_name, 0.0)), low, high,
                               default=float(mask.get(field_name, 0.0)), step=1.0, unit=unit,
                               decimals=0, tooltip=f"{label} for {mask.get('name')}")
            field.committed.connect(lambda value, mid=mask_id, key=field_name:
                                    mask_edit(mid, **{key: float(value)}))
            form.addWidget(panel._labelled(label, field, f"Mask {label.lower()}"))

    # --------------------------------------------------------------------- matte
    panel._section_label(form, "Track Matte")
    matte = MATTE.ensure(clip)
    sources = [candidate for candidate in panel.engine.timeline.all_clips()
               if candidate is not clip and candidate.track is not None
               and candidate.track.type == "video"]
    source = QComboBox()
    source.addItem("None", 0)
    for candidate in sources:
        source.addItem(candidate.name, candidate.id)
    index = source.findData(int(matte.get("source_id", 0)))
    source.setCurrentIndex(index if index >= 0 else 0)
    source.activated.connect(lambda at, box=source:
                             _set_matte(panel, clip, box.itemData(at), matte.get("mode")))
    form.addWidget(panel._labelled("Source layer", source,
                                   "Another layer supplies this clip's transparency"))
    mode = QComboBox()
    for key, label in MATTE.MODE_LABELS.items():
        mode.addItem(label, key)
    mode.setCurrentIndex(max(0, mode.findData(matte.get("mode"))))
    mode.activated.connect(
        lambda at, box=mode: _set_matte(
            panel, clip, matte.get("source_id"), box.itemData(at),
            invert=(False if str(box.itemData(at)).endswith("_inverted")
                    else bool(matte.get("invert")))))
    form.addWidget(panel._labelled("Matte type", mode, "Alpha or luma, normal or inverted"))
    matte_invert = QCheckBox("Invert Matte")
    matte_invert.setChecked(MATTE.inverted(matte))
    matte_invert.setToolTip("Invert the selected matte's coverage once. A legacy "
                            "inverted matte type and this switch never double-invert.")
    matte_invert.toggled.connect(
        lambda on: _set_matte(panel, clip, matte.get("source_id"),
                              MATTE.base_mode(matte.get("mode")), invert=bool(on)))
    form.addWidget(matte_invert)

    # -------------------------------------------------------------------- alpha
    panel._section_label(form, "Clip Alpha")
    clip_invert = QCheckBox("Invert Clip Alpha")
    clip_invert.setChecked(ALPHA.inverted(clip))
    clip_invert.setToolTip("Invert the clip's finished alpha after masks and track matte "
                           "(before opacity and blending)")
    clip_invert.toggled.connect(
        lambda on: _set_clip_alpha(panel, clip, bool(on)))
    form.addWidget(clip_invert)

    # -------------------------------------------------------------------- motion
    panel._section_label(form, "Motion Path")
    motion = MOTION.ensure(clip)
    path = motion.get("path", {})
    points = len(path.get("points") or ())
    form.addWidget(_note(f"{points} path point(s). Draw with Pen > Motion Path (G)."
                         if points else "No motion path. Choose Pen > Motion Path (G)."))
    enabled = QCheckBox("Enabled")
    enabled.setChecked(bool(motion.get("enabled")))
    enabled.toggled.connect(lambda on: _set_motion(panel, clip, enabled=bool(on)))
    orient = QCheckBox("Orient along path")
    orient.setChecked(bool(motion.get("orient")))
    orient.toggled.connect(lambda on: _set_motion(panel, clip, orient=bool(on)))
    reverse = QCheckBox("Reverse")
    reverse.setChecked(bool(motion.get("reverse")))
    reverse.toggled.connect(lambda on: _set_motion(panel, clip, reverse=bool(on)))
    flags = QWidget()
    flag_row = QHBoxLayout(flags)
    flag_row.setContentsMargins(0, 0, 0, 0)
    flag_row.addWidget(enabled)
    flag_row.addWidget(orient)
    flag_row.addWidget(reverse)
    form.addWidget(flags)
    motion_mode = QComboBox()
    motion_mode.addItem("Additive Offset", "offset")
    motion_mode.addItem("Replace Position", "replace")
    motion_mode.setCurrentIndex(max(0, motion_mode.findData(motion.get("mode", "replace"))))
    motion_mode.setToolTip("Offset adds the path to existing position keyframes; "
                           "Replace intentionally overrides authored position")
    motion_mode.activated.connect(lambda at, box=motion_mode:
                                  _set_motion(panel, clip, mode=box.itemData(at)))
    form.addWidget(panel._labelled("Combine", motion_mode, "How the path combines with position"))
    easing = QComboBox()
    for key in MOTION.EASINGS:
        easing.addItem(key.replace("_", " ").title(), key)
    easing.setCurrentIndex(max(0, easing.findData(motion.get("easing"))))
    easing.activated.connect(lambda at, box=easing:
                             _set_motion(panel, clip, easing=box.itemData(at)))
    form.addWidget(panel._labelled("Easing", easing, "Motion-path easing"))
    loop = QComboBox()
    for key in MOTION.LOOPS:
        loop.addItem(key.replace("_", " ").title(), key)
    loop.setCurrentIndex(max(0, loop.findData(motion.get("loop"))))
    loop.activated.connect(lambda at, box=loop:
                           _set_motion(panel, clip, loop=box.itemData(at)))
    form.addWidget(panel._labelled("Loop", loop, "None, loop or ping-pong"))
    for name, label, low, high, step in (("duration", "Duration", 0.0, 200000.0, 1.0),
                                         ("progress", "Progress", -1.0, 1.0, 0.01),
                                         ("speed", "Speed", 0.01, 100.0, 0.1),
                                         ("start_offset", "Start offset", 0.0, 10000.0, 0.1),
                                         ("end_offset", "End offset", 0.0, 10000.0, 0.1),
                                         ("rotation_offset", "Rotation offset", -720.0, 720.0, 1.0)):
        field = ScrubField(label, float(motion.get(name, 0.0)), low, high,
                           default=float(motion.get(name, 0.0)), step=step,
                           decimals=2, tooltip=label)
        field.committed.connect(lambda value, key=name:
                                _set_motion(panel, clip, **{key: float(value)}))
        form.addWidget(panel._labelled(label, field, label))
    path_actions = QWidget()
    path_row = QHBoxLayout(path_actions)
    path_row.setContentsMargins(0, 0, 0, 0)
    key_path = QToolButton()
    key_path.setText("Key path")
    key_path.setToolTip("Key the motion path's shape at the playhead")
    key_path.clicked.connect(lambda _=False: _key_motion(panel, clip))
    edit_path = QToolButton()
    edit_path.setText("Edit path")
    edit_path.setToolTip("Redraw the motion path in the preview")
    edit_path.clicked.connect(lambda _=False: _edit_motion(panel, clip))
    path_row.addWidget(key_path)
    path_row.addWidget(edit_path)
    path_row.addStretch(1)
    form.addWidget(path_actions)
    outer.addWidget(body)
    panel.properties_layout.addWidget(frame)


def _remove_mask(panel, clip, mask_id):
    panel.engine.session.remove_clip_mask(clip.id, mask_id)
    panel.refresh_properties()
    panel._notify()


def _duplicate_mask(panel, clip, mask_id):
    panel.engine.session.duplicate_clip_mask(clip.id, mask_id)
    panel.refresh_properties()
    panel._notify()


def _set_mask_space(panel, clip, mask_id, space):
    panel.engine.session.set_clip_mask_space(clip.id, mask_id, space)
    panel.refresh_properties()
    panel._notify()


def _edit_mask(panel, clip, mask_id):
    panel.engine.session.select_clip_mask(clip.id, mask_id)
    host = panel.window()
    overlay = getattr(host, "overlay", None)
    if overlay is not None and overlay.pen.edit_mask(clip.id, mask_id):
        if hasattr(host, "_set_overlay_mode"):
            host._set_overlay_mode("pen")
        else:
            overlay.mode = "pen"
        panel._notify()


def _edit_motion(panel, clip):
    host = panel.window()
    overlay = getattr(host, "overlay", None)
    if overlay is not None and overlay.pen.set_mode("motion"):
        if hasattr(host, "_set_overlay_mode"):
            host._set_overlay_mode("pen")
        panel._notify()


def _key_mask(panel, clip, mask_id):
    frame = panel._clip_frame(clip)
    panel.engine.session.add_keyframe(clip.id, CH.mask_path_channel(mask_id), frame)
    panel.refresh_properties()
    panel._notify()


def _key_motion(panel, clip):
    frame = panel._clip_frame(clip)
    panel.engine.session.add_keyframe(clip.id, CH.motion_path_channel(), frame)
    panel.refresh_properties()
    panel._notify()


def _set_matte(panel, clip, source_id, mode, invert=None):
    panel.engine.session.set_clip_matte(clip.id, int(source_id or 0), mode or "alpha",
                                       invert=invert)
    panel.refresh_properties()
    panel._notify()


def _set_clip_alpha(panel, clip, invert):
    panel.engine.session.set_clip_alpha_invert(clip.id, bool(invert))
    panel.refresh_properties()
    panel._notify()


def _set_motion(panel, clip, **values):
    panel.engine.session.update_motion_settings(clip.id, values)
    panel.refresh_properties()
    panel._notify()
