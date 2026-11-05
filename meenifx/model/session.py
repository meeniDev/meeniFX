"""Panel-independent timeline state.

Everything the Timeline panel needs to survive being destroyed and rebuilt in
another container lives here: the data model, the undo stack, playhead,
selection, view zoom/scroll and the signals. TimelineWidget is only a view.
"""

import copy

from . import animation as ANIM
from . import channels as CH
from . import text as TXT
from . import transform as T
from .keyframes import AnimatedValue, animation_value
from .values import Value
from .timeline import CommandStack, Signal, Timeline, clone_effects, timecode_text


class TimelineSession:
    _clipboard = []
    OVERLAP_MODES = {"Block overlaps": "snap", "Push neighbours": "push"}

    def __init__(self, timeline=None, media=None):
        self.timeline = timeline or Timeline()
        self.media = media
        self.stack = CommandStack(self.timeline)
        # Logical project resolution, published by the engine when a project is opened or
        # resized. Frame-space operations (Fill Frame) read it from here instead of
        # guessing a size, and the session stays free of engine/GUI imports.
        self.project_size = None

        self.playhead_changed = Signal()
        self.clip_selected = Signal()
        self.linked_selection_changed = Signal()
        self.marker_selection_changed = Signal()
        # One keyframe selection shared by the timeline and the Graph Editor: selecting a
        # diamond on a clip selects the same key in the curve view and the other way round.
        self.key_selection_changed = Signal()
        self.clip_moved = Signal()
        self.clip_trimmed = Signal()
        self.clip_split = Signal()
        self.loop_changed = Signal()
        self.changed = Signal()
        self.play_toggle_requested = Signal()
        self.clip_copied = Signal()      # the in-app clip clipboard changed (Smart Paste)
        self.status = Signal()
        # view-facing signals; a live TimelineWidget subscribes, a hidden panel simply has no listener
        self.redraw_requested = Signal()
        self.reveal_requested = Signal()
        self.playhead_moved = Signal()

        self.ppf = 3.0
        self.scroll_x = 0.0
        self.scroll_y = 0.0
        self.playhead = 0
        self.selected = set()
        self.marker_selected = set()   # marker ids, independent of the clip selection
        self.key_selection = set()     # (clip_id, channel, time) keyframe selection
        self._primary = None
        self.tool = Value("select")
        self.snap_enabled = Value(True)
        self.overlap_label = Value("Block overlaps")
        self.zoom_value = Value(50.0)
        self.vzoom_value = Value(64.0)
        self.needs_fit = True
        # Toolbar toggle: clicking a linked clip selects its whole group. Turning this off
        # never removes a link, it only stops the selection from following it.
        self.linked_selection = Value(True)
        # Auto-Keyframe: when off, editing an animated property shifts the whole curve
        # instead of silently inventing a keyframe at the playhead.
        self.auto_keyframe = Value(False)
        # Timeline envelopes and keyframe visibility: view preferences, not edits.
        self.show_keyframes = Value(True)
        # Anchor presets keep the picture where it is unless the user turns this off.
        self.keep_anchor_position = Value(True)
        self.show_opacity_envelope = Value(True)
        self.show_gain_envelope = Value(True)
        self.double_click_opens_graph = Value(True)
        # A linked pair keeps independent opacity and gain; nothing here ever follows a link.
        # Keyframe clipboard: [(channel, relative frame, value, interpolation, handles)]
        self._key_clipboard = []
        self._gesture_snapshot = None

        self.stack.changed.connect(self._on_stack_changed)

    @property
    def overlap_mode(self):
        return self.OVERLAP_MODES.get(self.overlap_label.get(), "snap")

    def request_redraw(self):
        self.redraw_requested.emit()

    # ----------------------------------------------------------------- playhead
    def set_playhead(self, frame, silent=False, follow=True):
        frame = max(0, int(frame))
        if frame == self.playhead:
            return
        self.playhead = frame
        self.playhead_moved.emit(frame, follow)
        if not silent:
            self.playhead_changed.emit(frame)

    # ---------------------------------------------------------------- selection
    def set_selection(self, ids, expand=True):
        """Select clips; with Linked Selection on, a linked partner joins automatically."""
        requested = {int(i) for i in ids}
        ids = set(requested)
        if expand:
            ids = self.with_links(ids)
        added = ids - self.selected
        # A click that expands to its linked partner adds two ids, so "exactly one new
        # id" alone cannot identify what the user clicked. A single requested clip is
        # always the primary one (even when it was already selected); otherwise the
        # newest addition wins.
        if len(requested) == 1 and next(iter(requested)) in ids:
            self._primary = next(iter(requested))
        elif len(added) == 1:
            self._primary = next(iter(added))
        if ids != self.selected:
            self.selected = ids
            self.clip_selected.emit(sorted(ids))
        self.request_redraw()

    def primary_clip(self):
        """The most recently clicked selected clip (falls back to any selected one)."""
        if not self.selected:
            return None
        pid = self._primary if self._primary in self.selected else min(self.selected)
        return self.timeline.find_clip(pid)

    def _prune_selection(self):
        alive = {c.id for c in self.timeline.all_clips()}
        if not self.selected <= alive:
            self.selected &= alive
            self.clip_selected.emit(sorted(self.selected))
        marker_alive = {m.id for m in self.timeline.markers}
        if not self.marker_selected <= marker_alive:
            self.marker_selected &= marker_alive
            self.marker_selection_changed.emit(sorted(self.marker_selected))

    def _on_stack_changed(self):
        self._prune_selection()
        # Undo/redo can remove keyframes; the shared selection must not point at ghosts
        # (this also keeps the timeline and the Graph Editor in step after a restore).
        self.selected_key_entries()
        self.request_redraw()
        self.changed.emit()

    def _selected_clips(self):
        clips = [self.timeline.find_clip(i) for i in self.selected]
        return [c for c in clips if c is not None]

    def _editable_ids(self, ids):
        out = []
        for cid in ids:
            clip = self.timeline.find_clip(cid)
            if clip is not None and not clip.track.locked:
                out.append(cid)
        return out

    # ---------------------------------------------------------- video/audio links
    @property
    def linked_selection_enabled(self):
        return bool(self.linked_selection.get())

    def set_linked_selection(self, enabled):
        """Turn Linked Selection on or off; links themselves are untouched."""
        enabled = bool(enabled)
        if enabled == self.linked_selection_enabled:
            return
        self.linked_selection.set(enabled)
        self.linked_selection_changed.emit(enabled)
        if enabled:
            self.set_selection(self.with_links(self.selected))
        self.request_redraw()

    def with_links(self, ids):
        """`ids` plus every clip linked to them, while Linked Selection is on."""
        ids = {int(i) for i in ids}
        if not self.linked_selection_enabled or not ids:
            return ids
        out = set(ids)
        for cid in list(ids):
            clip = self.timeline.find_clip(cid)
            if clip is not None:
                out |= {other.id for other in self.timeline.linked_pairs(clip)}
        return out

    def select(self, ids, additive=False):
        """Selection entry point for the UI: honours Linked Selection."""
        target = set(ids)
        if additive:
            target |= self.selected
        self.set_selection(self.with_links(target))

    def _clips_for(self, ids):
        ids = list(self.selected if ids is None else ids)
        return [clip for clip in (self.timeline.find_clip(i) for i in ids) if clip is not None]

    def can_link(self, ids=None):
        """Two or more unlocked clips that are not already one complete group.

        Clips do not have to share a media file: linking separately recorded video and
        audio is the point of the command. Track types may repeat too, so a whole
        multi-camera or multi-mic selection can be linked where the model allows it.
        Locked tracks are refused because linking would change their clip metadata.
        """
        clips = self._clips_for(ids)
        if len(clips) < 2:
            return False
        if any(clip.track.locked for clip in clips):
            return False
        groups = {clip.link_id for clip in clips if clip.link_id}
        if len(groups) == 1 and all(clip.link_id for clip in clips):
            # The selection is already one group: there is nothing to create.
            if len(self.timeline.link_group(next(iter(groups)))) == len(clips):
                return False
        return True

    def can_unlink(self, ids=None):
        clips = self._clips_for(ids)
        return any(clip.link_id and len(self.timeline.link_group(clip.link_id)) > 1 for clip in clips)

    def link_selected(self, ids=None):
        """Link the selected clips into one group (positions are never changed).

        A fresh id is allocated, so old groups stay valid for any members that were not
        selected. Linking is one undo step and never moves, trims or retimes a clip.
        """
        clip_ids = [clip.id for clip in self._clips_for(ids)]
        if not self.can_link(clip_ids):
            return 0
        holder = {}

        def action():
            holder["link"] = self.timeline.assign_link(clip_ids)

        self.stack.run("Link selected clips", action)
        link = holder.get("link") or 0
        if link:
            self.set_selection(set(clip_ids))
            synced = all(self.timeline.link_is_synced(c) for c in self._clips_for(clip_ids))
            self.status.emit("Clips linked" if synced else "Clips linked (out of sync - drag them together to line them up)")
        return link

    def unlink_selected(self, ids=None):
        """Remove the link for good; clips stay where they are.

        Only the selected members leave the group. In a group of three, unlinking one
        clip leaves the other two linked together; unlinking the last two dissolves the
        group. Timing, source ranges, effects and keyframes are never touched.
        """
        clip_ids = [clip.id for clip in self._clips_for(ids)]
        if not self.can_unlink(clip_ids):
            return False
        tl = self.timeline
        self.stack.run("Unlink selected clips", lambda: tl.clear_link(clip_ids))
        self.status.emit("Clips unlinked")
        self.request_redraw()
        return True

    def link_partners(self, clip):
        """Other clips that must follow `clip` (empty when Linked Selection is off)."""
        if clip is None or not self.linked_selection_enabled:
            return []
        return [other.id for other in self.timeline.linked_pairs(clip)]

    # ------------------------------------------------------------------ editing
    def add_track(self, track_type, index=None):
        self.stack.run(f"Add {track_type} track", lambda: self.timeline.add_track(track_type, index=index))

    def add_track_near(self, reference, track_type, above=True):
        """Insert a track directly above/below another track in the display order."""
        if reference is None or reference not in self.timeline.tracks:
            self.add_track(track_type)
            return
        index = self.timeline.tracks.index(reference) + (0 if above else 1)
        self.stack.run(f"Add {track_type} track", lambda: self.timeline.add_track(track_type, index=index))

    def rename_track(self, track_id, name):
        track = self.find_track(track_id)
        name = (name or "").strip()
        if track is None or not name or name == track.name:
            return
        self.stack.run("Rename track", lambda: setattr(track, "name", name))

    def set_track_attr(self, track_id, **attrs):
        """Change mute/solo/locked/visible/volume as one undoable action."""
        track = self.find_track(track_id)
        if track is None:
            return
        changes = {key: value for key, value in attrs.items()
                   if key in ("muted", "solo", "locked", "visible", "volume")}
        if not changes or all(getattr(track, key) == value for key, value in changes.items()):
            return
        label = "Change track " + ", ".join(sorted(changes))
        self.stack.run(label, lambda: [setattr(track, key, value) for key, value in changes.items()])

    def track_clip_count(self, track_id):
        track = self.find_track(track_id)
        return len(track.clips) if track is not None else 0

    def can_remove_track(self, track_id):
        """Every track can be removed, including the last track of its type.

        A type with no tracks is a valid state: the timeline shows an empty-state hint
        and imports/pastes create a fresh track of the right type when one is needed.
        """
        track = self.find_track(track_id)
        if track is None:
            return False, "Track not found."
        return True, ""

    def remove_track(self, track_id):
        track = self.find_track(track_id)
        allowed, reason = self.can_remove_track(track_id)
        if track is None or not allowed:
            if reason:
                self.status.emit(reason)
            return False
        self.stack.run("Delete track", lambda: self.timeline.remove_track(track))
        self.set_selection({c.id for c in self._selected_clips() if c.track is not track})
        return True

    def find_track(self, track_id):
        return next((track for track in self.timeline.tracks if track.id == track_id), None)

    def move_selection_to(self, ids, delta, lane_shift, mode=None):
        """Commit a finished drag: one undo entry, positions taken from stored starts."""
        ids = [clip_id for clip_id in self.with_links(ids) if self.timeline.find_clip(clip_id) is not None]
        if not ids:
            return False
        mode = mode or self.overlap_mode
        placements, valid = self.timeline.move_preview(ids, delta, lane_shift, mode)
        if not placements or not valid:
            return False
        if all(clip.timeline_start == start and target is clip.track
               for clip, target, start in placements):
            return False

        def action():
            fresh, _valid = self.timeline.move_preview(ids, delta, lane_shift, mode)
            self.timeline.apply_placements(fresh, mode)

        self.stack.run("Move clip" if len(ids) == 1 else f"Move {len(ids)} clips", action)
        return True

    def add_clip(self, path, name, duration, track=None, start=None, source_in=0):
        tl = self.timeline
        if track is None:
            candidates = tl.tracks_of_type("video")
            track = candidates[-1] if candidates else tl.add_track("video")
        if start is None:
            start = max((c.end for c in track.clips), default=0)
        start = tl.first_free_start(track, start, duration)

        entry = self.media.entry(path) if self.media is not None and path else None

        def action():
            return tl.new_clip(name, path, track, start, duration, source_in,
                               source_out=entry.frame_count if entry is not None else None,
                               source_fps=entry.fps if entry is not None else None)

        clip = self.stack.run("Add clip", action)
        self.set_selection({clip.id})
        return clip

    def _split_targets(self, ids, frame):
        """Ids that should be cut at `frame`: the clips, plus their linked partners under it."""
        found = set()
        for cid in ids:
            clip = self.timeline.find_clip(cid)
            if clip is None or not (clip.timeline_start < frame < clip.end):
                continue
            found.add(clip.id)
            for partner_id in self.link_partners(clip):
                partner = self.timeline.find_clip(partner_id)
                if partner is not None and partner.timeline_start < frame < partner.end \
                        and not partner.track.locked:
                    found.add(partner.id)
        return sorted(found)

    def _detach_split_links(self, new_ids):
        """Give a split pair's right-hand pieces their own link id.

        Splitting a linked video/audio pair used to leave all four pieces in one group, so
        dragging the new pair also dragged the old one. The left pair keeps the old id and
        every new pair (same start, at least two lanes) gets a fresh one.
        """
        created = [self.timeline.find_clip(cid) for cid in new_ids]
        created = [clip for clip in created if clip is not None and clip.link_id]
        groups = {}
        for clip in created:
            groups.setdefault((clip.link_id, clip.timeline_start), []).append(clip)
        for group in groups.values():
            if len(group) < 2:
                continue
            fresh = self.timeline.new_link_id()
            for clip in group:
                clip.link_id = fresh

    def split_at_playhead(self):
        frame = self.playhead
        ids = self._editable_ids(self.selected) or self._editable_ids(c.id for c in self.timeline.all_clips())
        targets = self._split_targets(ids, frame)
        if not targets:
            return
        pairs = []

        def action():
            for cid in targets:
                new_id = self.timeline.split_clip(cid, frame)
                if new_id is not None:
                    pairs.append((cid, new_id))
            self._detach_split_links([new_id for _cid, new_id in pairs])

        self.stack.run("Split clips" if len(targets) > 1 else "Split clip", action)
        for pair in pairs:
            self.clip_split.emit(*pair)

    def split_clip_at(self, clip, frame):
        pairs = []
        targets = self._split_targets([clip.id], frame) or ([clip.id] if clip.timeline_start < frame < clip.end else [])

        def action():
            for cid in targets:
                new_id = self.timeline.split_clip(cid, frame)
                if new_id is not None:
                    pairs.append((cid, new_id))
            self._detach_split_links([new_id for _cid, new_id in pairs])

        self.stack.run("Split clips" if len(targets) > 1 else "Split clip", action)
        for pair in pairs:
            self.clip_split.emit(*pair)

    def linked_clip_ids(self, clip):
        """Clips of the other media type that mirror this one (a video/audio pair).

        The app has no explicit link model, so a pair is recognised by sharing the
        source file and the exact timeline start; that is what an imported video with
        its own audio produces.
        """
        if clip is None:
            return []
        return [c.id for c in self.timeline.all_clips()
                if c.id != clip.id and c.source_path == clip.source_path
                and c.timeline_start == clip.timeline_start and c.track.type != clip.track.type]

    def set_clip_fade(self, clip_id, edge, frames=None, curve=None, linked=False):
        """Undoable fade length and/or curve edit; Shift applies it to the linked clip too."""
        clip = self.timeline.find_clip(clip_id)
        if clip is None:
            return False
        ids = [clip.id] + (self.linked_clip_ids(clip) if linked else [])
        ids = [cid for cid in ids
               if (target := self.timeline.find_clip(cid)) is not None and not target.track.locked]
        if not ids:
            return False

        def action():
            for cid in ids:
                if frames is not None:
                    self.timeline.set_fade(cid, edge, frames, curve)
                elif curve is not None:
                    self.timeline.set_fade_curve(cid, edge, curve)

        before = self.timeline.snapshot()
        action()
        self.stack.commit("Fade in" if edge == "in" else "Fade out", before)
        return True

    def reset_clip_fades(self, clip_id, edge=None, linked=False):
        """Back to no fade: one edge, or both when edge is None."""
        clip = self.timeline.find_clip(clip_id)
        if clip is None:
            return False
        ids = [clip.id] + (self.linked_clip_ids(clip) if linked else [])
        ids = [cid for cid in ids
               if (target := self.timeline.find_clip(cid)) is not None and not target.track.locked]
        edges = ("in", "out") if edge is None else (edge,)

        def action():
            for cid in ids:
                for which in edges:
                    self.timeline.set_fade(cid, which, 0)

        before = self.timeline.snapshot()
        action()
        self.stack.commit("Clear fade", before)
        return True

    # ------------------------------------------------------------- keyframes
    def set_keyframe_time(self, clip_id, effect_id, name, old_time, new_time):
        """Move one keyframe in time without touching the undo stack (live drag).

        `effect_id`/`name` is the legacy addressing of an effect parameter; a channel id
        (see model.channels) also works and covers transform and puppet-pin keyframes.
        """
        channel = effect_id if "." in str(effect_id) and name is None else CH.effect_channel(effect_id, name)
        return self.set_channel_key_time(clip_id, channel, old_time, new_time)

    def set_channel_key_time(self, clip_id, channel, old_time, new_time):
        clip = self.timeline.find_clip(clip_id)
        if clip is None or clip.track.locked:
            return None
        target = max(0, min(max(0, clip.duration - 1), int(new_time)))
        return CH.set_key_time(clip, channel, old_time, target)

    def commit_keyframes(self, label, before):
        """Push one undo entry for a gesture that mutated keyframes live."""
        return self.stack.commit(label, before)

    def delete_keyframes(self, entries):
        """Delete [(clip_id, effect_id, parameter, time)] as one undoable edit."""
        entries = [entry for entry in entries if entry]
        if not entries:
            return 0
        removed = 0

        def action():
            nonlocal removed
            for entry in entries:
                clip_id, time = entry[0], entry[-1]
                channel = entry[1] if len(entry) == 3 else CH.effect_channel(entry[1], entry[2])
                clip = self.timeline.find_clip(clip_id)
                if clip is None or clip.track.locked:
                    continue
                if CH.remove_key(clip, channel, time):
                    removed += 1

        before = self.timeline.snapshot()
        action()
        if self.stack.commit("Delete keyframe" if len(entries) == 1 else "Delete keyframes", before):
            return removed
        return 0

    # ------------------------------------------- simple timeline keyframe editing
    def envelope_channel(self, clip):
        """The one universal channel a clip's timeline envelope edits.

        Visual clips use the Clip Opacity transform channel; audio clips use their own
        gain. There is no second opacity or gain model - the inspector, the graph editor
        and the exporter read exactly these channels.
        """
        if clip is not None and clip.track is not None and clip.track.type == "audio":
            return CH.gain_channel()
        return CH.transform_channel("opacity")

    def envelope_visible(self, clip):
        if clip is None:
            return False
        audio = clip.track is not None and clip.track.type == "audio"
        flag = self.show_gain_envelope if audio else self.show_opacity_envelope
        return bool(flag.get())

    def set_key_selection(self, entries, additive=False):
        """Select [(clip_id, channel, time)] keyframes in every view at once."""
        target = {(int(cid), str(channel), int(time)) for cid, channel, time in entries or ()}
        if additive:
            target |= self.key_selection
        if target != self.key_selection:
            self.key_selection = target
            self.key_selection_changed.emit()
            self.request_redraw()

    def toggle_key_selection(self, clip_id, channel, time):
        entry = (int(clip_id), str(channel), int(time))
        if entry in self.key_selection:
            self.set_key_selection(self.key_selection - {entry})
        else:
            self.set_key_selection({entry}, additive=True)

    def clear_key_selection(self):
        self.set_key_selection(set())

    def selected_key_entries(self):
        """The live part of the keyframe selection, sorted for stable iteration."""
        alive = set()
        for clip in self.timeline.all_clips():
            for channel, time in CH.keyframe_entries(clip):
                entry = (clip.id, channel, int(time))
                if entry in self.key_selection:
                    alive.add(entry)
        if alive != self.key_selection:
            # A keyframe that no longer exists cannot stay selected (undo, delete, split).
            self.key_selection = alive
            self.key_selection_changed.emit()
        return sorted(self.key_selection)

    def key_bases(self, entries):
        """Capture the keys a group drag will move: {(clip_id, channel): records + times}."""
        bases = {}
        for clip_id, channel, time in entries or ():
            clip = self.timeline.find_clip(clip_id)
            if clip is None or clip.track.locked:
                continue
            bucket = bases.setdefault((clip_id, channel),
                                      {"records": CH.key_records(clip, channel), "times": set()})
            bucket["times"].add(int(time))
        return bases

    def key_shift_limits(self, bases):
        """(low, high) frame delta a group move may use without leaving any clip."""
        low, high = -10 ** 9, 10 ** 9
        for (clip_id, _channel), bucket in (bases or {}).items():
            clip = self.timeline.find_clip(clip_id)
            if clip is None or not bucket["times"]:
                continue
            low = max(low, -min(bucket["times"]))
            high = min(high, max(0, clip.duration - 1) - max(bucket["times"]))
        if low > high:
            low = high = 0
        return low, high

    def shift_keyframes(self, bases, delta):
        """Move every captured keyframe by `delta` frames (live, no undo entry).

        Keys that were not captured keep their own time; where a moved key lands on a
        stationary one the moved key wins, which is what dragging in AE/Vegas does.
        """
        delta = int(delta)
        changed = False
        for (clip_id, channel), bucket in (bases or {}).items():
            clip = self.timeline.find_clip(clip_id)
            if clip is None or clip.track.locked:
                continue
            moved = {}
            for row in bucket["records"]:
                if int(row[0]) not in bucket["times"]:
                    continue
                new_time = max(0, min(max(0, clip.duration - 1), int(row[0]) + delta))
                moved[new_time] = (new_time, row[1], row[2], row[3], row[4])
            if not moved:
                continue
            kept = [row for row in CH.key_records(clip, channel)
                    if int(row[0]) not in bucket["times"] and int(row[0]) not in moved]
            CH.replace_keys(clip, channel, kept + list(moved.values()))
            changed = True
        if changed:
            self.changed.emit()
            self.request_redraw()
        return changed

    def add_keyframes_at_playhead(self, ids=None, channels=None):
        """Add a keyframe at the playhead on every animated channel of the clip(s).

        The simple "key" button: a clip with animation gets a key on each animated
        property (and only where one is missing); a clip with none gets its envelope
        channel keyed so the button always does something visible.
        """
        clips = [self.timeline.find_clip(cid) for cid in (ids if ids is not None else self.selected)]
        clips = [clip for clip in clips if clip is not None and not clip.track.locked]
        if not clips:
            self.status.emit("Select a clip to add keyframes")
            return 0
        targets = []
        for clip in clips:
            frame = max(0, int(self.playhead) - int(clip.timeline_start))
            names = list(channels or ())
            if not names:
                names = [row["id"] for row in CH.animated_channels(clip, self._effect_resolver())]
            if not names:
                names = [self.envelope_channel(clip)]
            for channel in names:
                targets.append((clip.id, channel, frame))
        holder = {"added": 0, "entries": []}

        def action():
            for clip_id, channel, frame in targets:
                clip = self.timeline.find_clip(clip_id)
                if clip is None or clip.track.locked:
                    continue
                if CH.ensure_key(clip, channel, frame):
                    holder["added"] += 1
                    holder["entries"].append((clip_id, channel, int(frame)))

        self.stack.run("Add keyframe" if len(targets) == 1 else "Add keyframes", action)
        self.status.emit(f"{holder['added']} keyframe(s) added" if holder["added"]
                         else "Keyframe already at the playhead")
        if holder["entries"]:
            # Show what the button just did: the new keys become the selection in every
            # view, so they can be dragged or eased straight away.
            self.set_key_selection(holder["entries"])
        return holder["added"]

    def add_keyframe(self, clip_id, channel, frame):
        """Add one keyframe at a clip-local frame (double-click on a property lane)."""
        clip = self.timeline.find_clip(clip_id)
        if clip is None or clip.track.locked:
            return False
        frame = max(0, min(max(0, clip.duration - 1), int(frame)))
        holder = {"added": False}

        def action():
            target = self.timeline.find_clip(clip_id)
            if target is None or target.track.locked:
                return
            holder["added"] = CH.ensure_key(target, channel, frame)

        self.stack.run("Add keyframe", action)
        if holder["added"]:
            self.set_key_selection({(clip_id, channel, frame)})
        return holder["added"]

    def set_envelope_value(self, clip_id, channel, value, frame, label="Envelope"):
        """Set an envelope value at a clip-local frame (live, part of a drag gesture)."""
        clip = self.timeline.find_clip(clip_id)
        if clip is None or clip.track.locked:
            return None
        frame = max(0, min(max(0, clip.duration - 1), int(frame)))
        CH.set_value(clip, channel, value, frame, CH.is_animated(clip, channel))
        return CH.value_at(clip, channel, frame)

    def add_envelope_key(self, clip_id, channel, frame):
        """Double-click on the envelope: key the value at that frame."""
        clip = self.timeline.find_clip(clip_id)
        if clip is None or clip.track.locked:
            return False
        frame = max(0, min(max(0, clip.duration - 1), int(frame)))
        holder = {"added": False}

        def action():
            target = self.timeline.find_clip(clip_id)
            if target is not None and not target.track.locked:
                holder["added"] = CH.ensure_key(target, channel, frame)

        self.stack.run("Add keyframe", action)
        if holder["added"]:
            self.set_key_selection({(clip_id, channel, frame)})
        return holder["added"]

    def reset_envelope(self, clip_id, channel=None):
        """Back to 100 % opacity / 0 dB, dropping the channel's keys."""
        clip = self.timeline.find_clip(clip_id)
        if clip is None or clip.track.locked:
            return False
        channel = channel or self.envelope_channel(clip)
        default = 0.0 if channel == CH.gain_channel() else 100.0

        def action():
            target = self.timeline.find_clip(clip_id)
            if target is None:
                return
            CH.set_static(target, channel, default)

        self.stack.run("Reset envelope", action)
        self.changed.emit()
        return True

    def set_clip_gain(self, clip_id, db, frame=None):
        """Inspector gain edit (dB), keyed at the playhead when the channel is animated."""
        clip = self.timeline.find_clip(clip_id)
        if clip is None or clip.track.locked:
            return False
        frame = (max(0, int(self.playhead) - int(clip.timeline_start))
                 if frame is None else int(frame))
        auto = CH.is_animated(clip, CH.gain_channel()) or bool(self.auto_keyframe.get())
        self.stack.run("Clip gain", lambda: CH.set_value(
            self.timeline.find_clip(clip_id), CH.gain_channel(), db, frame, auto))
        return True

    # ---------------------------------------------------- shared keyframe clipboard
    def copy_keyframes(self, entries):
        """Copy [(clip_id, channel, time)] so they can be pasted onto another property."""
        rows = []
        for clip_id, channel, time in entries or ():
            clip = self.timeline.find_clip(clip_id)
            if clip is None:
                continue
            record = next((row for row in CH.key_records(clip, channel)
                           if int(row[0]) == int(time)), None)
            if record is None:
                continue
            rows.append({"channel": channel, "time": int(record[0]), "value": record[1],
                         "interpolation": record[2],
                         "out_value_handle": record[4], "in_value_handle": record[3]})
        self._key_clipboard = rows
        return len(rows)

    def _paste_rows(self, clip, rows, base):
        """Merge clipboard rows onto their own channels, first key landing on `base`."""
        groups = {}
        for row in rows:
            groups.setdefault(row["channel"], []).append(row)
        for channel, items in groups.items():
            first = min(item["time"] for item in items)
            added = {}
            for item in items:
                time = base + (item["time"] - first)
                added[time] = (time, item["value"], item.get("interpolation", "linear"),
                               item.get("in_value_handle"), item.get("out_value_handle"))
            kept = [row for row in CH.key_records(clip, channel) if int(row[0]) not in added]
            CH.replace_keys(clip, channel, kept + list(added.values()))
        return len(rows)

    def paste_keyframes(self, clip_id, channel):
        """Paste the copied keys onto one channel, starting at the playhead."""
        rows = [row for row in self._key_clipboard if row["channel"] == channel]
        clip = self.timeline.find_clip(clip_id)
        if not rows or clip is None or clip.track.locked:
            return 0
        base = max(0, min(max(0, clip.duration - 1), self.playhead - clip.timeline_start))

        def action():
            target = self.timeline.find_clip(clip_id)
            if target is not None and not target.track.locked:
                self._paste_rows(target, rows, base)

        self.stack.run("Paste keyframes", action)
        self.changed.emit()
        return len(rows)

    def paste_keyframes_at_playhead(self, ids=None):
        """Paste the keyframe clipboard onto the selected clips' matching channels."""
        rows = list(self._key_clipboard)
        clips = [self.timeline.find_clip(cid) for cid in (ids if ids is not None else self.selected)]
        clips = [clip for clip in clips if clip is not None and not clip.track.locked]
        if not rows or not clips:
            self.status.emit("Copy keyframes first" if not rows else "Select a clip to paste onto")
            return 0
        pasted = {"count": 0}

        def action():
            for clip_id in [clip.id for clip in clips]:
                target = self.timeline.find_clip(clip_id)
                if target is None or target.track.locked:
                    continue
                base = max(0, min(max(0, target.duration - 1),
                                  self.playhead - target.timeline_start))
                pasted["count"] += self._paste_rows(target, rows, base)

        self.stack.run("Paste keyframes", action)
        self.status.emit(f"{pasted['count']} keyframe(s) pasted")
        self.changed.emit()
        return pasted["count"]

    def duplicate_keyframes(self, entries):
        """Copy the selection forward by its own span (one undo step)."""
        groups = {}
        for clip_id, channel, time in entries or ():
            clip = self.timeline.find_clip(clip_id)
            if clip is None or clip.track.locked:
                continue
            record = next((row for row in CH.key_records(clip, channel)
                           if int(row[0]) == int(time)), None)
            if record is not None:
                groups.setdefault(clip.id, []).append((channel, record))
        if not groups:
            return 0
        created = 0

        def action():
            nonlocal created
            for clip_id, rows in groups.items():
                clip = self.timeline.find_clip(clip_id)
                if clip is None:
                    continue
                shift = max(int(row[1][0]) for row in rows) - min(int(row[1][0]) for row in rows) + 1
                per_channel = {}
                for channel, record in rows:
                    time = min(max(0, clip.duration - 1), int(record[0]) + shift)
                    per_channel.setdefault(channel, {})[time] = (time, record[1], record[2],
                                                                 record[3], record[4])
                for channel, added in per_channel.items():
                    kept = [row for row in CH.key_records(clip, channel) if int(row[0]) not in added]
                    CH.replace_keys(clip, channel, kept + list(added.values()))
                    created += len(added)

        self.stack.run("Duplicate keyframes", action)
        self.status.emit(f"{created} keyframe(s) duplicated")
        self.changed.emit()
        return created

    def set_key_interpolation(self, entries, spec, handles=None, clear_handles=False):
        """Set the interpolation (and optional Bezier handles) of selected keyframes."""
        entries = list(entries or ())
        if not entries:
            return False

        def action():
            for clip_id, channel, time in entries:
                clip = self.timeline.find_clip(clip_id)
                if clip is None or clip.track.locked:
                    continue
                raw = CH.stored(clip, channel)
                if not isinstance(raw, AnimatedValue):
                    continue
                key = raw.key_at(int(time))
                if key is None:
                    continue
                key.interpolation = spec
                if clear_handles:
                    key.in_value_handle = key.out_value_handle = None
                elif handles:
                    key.out_value_handle = (float(handles[0]), float(handles[1]))
                    key.in_value_handle = (float(handles[2]), float(handles[3]))
                    following = raw.keyframes[raw.keyframes.index(key) + 1:]
                    if following:
                        following[0].in_value_handle = (float(handles[2]), float(handles[3]))

        self.stack.run("Set keyframe easing", action)
        self.changed.emit()
        return True

    def easing_entries(self, entries):
        """Only the keys that can carry a curve (held text words and locked clips cannot)."""
        out = []
        for clip_id, channel, time in entries or ():
            clip = self.timeline.find_clip(clip_id)
            if clip is None or clip.track.locked:
                continue
            if CH.parse(channel)[0] == "text" and CH.parse(channel)[2] == TXT.CONTENT:
                continue
            if isinstance(CH.stored(clip, channel), AnimatedValue):
                out.append((clip_id, channel, int(time)))
        return out

    # ------------------------------------------------------------ anchor presets
    def clip_image_size(self, clip):
        """(width, height) of the picture the anchor is measured against."""
        if clip is not None and clip.is_text:
            size = self.project_size
            return (int(size[0]), int(size[1])) if size else (1920, 1080)
        media_size = (self.media.source_size(clip.source_path) if (clip is not None
                      and clip.source_path and self.media is not None) else None)
        if media_size:
            return media_size
        size = self.project_size
        return (int(size[0]), int(size[1])) if size else (1920, 1080)

    def set_anchor(self, clip_id, anchor_x, anchor_y, keep_position=True, label="Move anchor point"):
        clip = self.timeline.find_clip(clip_id)
        if clip is None or clip.track.locked:
            return False
        frame = max(0, int(self.playhead) - int(clip.timeline_start))
        project = self.project_size or (1920, 1080)
        image = self.clip_image_size(clip)
        auto = bool(self.auto_keyframe.get())

        def action():
            target = self.timeline.find_clip(clip_id)
            if target is not None and not target.track.locked:
                T.set_anchor(target, anchor_x, anchor_y, frame, auto,
                             project_size=project, image_size=image,
                             keep_position=keep_position)

        self.stack.run(label, action)
        self.changed.emit()
        return True

    def set_anchor_preset(self, clip_id, preset, keep_position=True):
        clip = self.timeline.find_clip(clip_id)
        if clip is None or clip.track.locked:
            return False
        project = self.project_size or (1920, 1080)
        image = self.clip_image_size(clip)
        x, y = T.anchor_for(preset, project, image)
        label = "Center anchor point" if preset == "center" else \
            f"Anchor: {T.ANCHOR_LABELS.get(preset, preset)}"
        return self.set_anchor(clip_id, x, y, keep_position, label)

    def center_anchor(self, clip_id, keep_position=True):
        return self.set_anchor_preset(clip_id, "center", keep_position)

    # ---------------------------------------------------------- text animations
    def apply_text_animation(self, clip_id, slot, preset, duration=None,
                             easing=ANIM.DEFAULT_EASING, delay=0):
        clip = self.timeline.find_clip(clip_id)
        if clip is None or clip.track.locked:
            return False
        fps = self.timeline.fps
        context = {"project_size": self.project_size or (1920, 1080)}

        def action():
            target = self.timeline.find_clip(clip_id)
            if target is not None and not target.track.locked:
                ANIM.apply_text_animation(target, slot, preset, duration, easing, delay,
                                          fps=fps, context=context)

        label = ("Clear text animation" if not preset or preset == "None"
                 else f"Text animation: {preset}")
        self.stack.run(label, action)
        self.changed.emit()
        return True

    def _effect_resolver(self):
        """Effect runtime specs for channel metadata, when the session can see them."""
        return getattr(self, "effect_resolver", None)

    def set_double_click_opens_graph(self, on):
        self.double_click_opens_graph.set(bool(on))

    def set_clip_duration(self, clip_id, duration):
        """Set an exact clip length in frames, trimming the right edge.

        Stills and text simply change length; media clips are limited by the frames their
        source still has, exactly like dragging the right edge. A linked pair changes
        together, so its audio never drifts out of sync.
        """
        clip = self.timeline.find_clip(clip_id)
        if clip is None or clip.track.locked:
            return 0
        target = max(1, int(duration))
        delta = target - clip.duration
        if delta == 0:
            return clip.duration
        before = self.timeline.snapshot()
        also = self.link_partners(clip)
        self.timeline.apply_trim(before, clip_id, "right", delta, also=also)
        self.stack.commit("Set clip duration", before)
        self.changed.emit()
        result = self.timeline.find_clip(clip_id)
        return result.duration if result is not None else 0

    def delete_gap(self):
        """Close the empty space before each selected clip on its own track."""
        ids = self._editable_ids(self.selected)
        if not ids:
            return False
        timeline = self.timeline
        tracks = {clip.track for clip in (timeline.find_clip(i) for i in ids) if clip is not None}

        def action():
            for track in tracks:
                ordered = track.sorted_clips()
                for index, clip in enumerate(ordered):
                    if clip.id not in ids:
                        continue
                    previous_end = max((other.end for other in ordered
                                        if other is not clip and other.end <= clip.timeline_start),
                                       default=0)
                    gap = clip.timeline_start - previous_end
                    if gap <= 0:
                        continue
                    for other in ordered:
                        if other.timeline_start >= clip.timeline_start:
                            other.timeline_start = max(0, other.timeline_start - gap)
            return True

        return self.stack.run("Delete gap", action)

    def apply_clip_settings(self, clip_id, *, speed=None, reverse=None, loop=None, volume=None):
        clip = self.timeline.find_clip(clip_id)
        if clip is None or clip.track.locked:
            return

        def action():
            target = self.timeline.find_clip(clip_id)
            if speed is not None:
                target.speed = max(0.1, min(10.0, float(speed)))
                if not target.loop and not target.freeze:
                    remaining = max(1.0, (target.source_out - target.source_in) - target.source_offset)
                    target.duration = max(1, int(round(remaining * target.timeline_fps /
                                                       max(1e-9, target.source_fps * target.speed))))
                    target.clamp_fades()
            if reverse is not None:
                target.reverse = bool(reverse)
            if volume is not None:
                target.volume = max(0.0, min(2.0, float(volume)))
            if loop is not None:
                target.loop = bool(loop)
                if not target.loop and not target.freeze:
                    remaining = max(1.0, (target.source_out - target.source_in) - target.source_offset)
                    target.duration = max(1, int(round(remaining * target.timeline_fps /
                                                       max(1e-9, target.source_fps * target.speed))))
                    target.clamp_fades()

        self.stack.run("Change clip timing", action)

    def freeze_selected_clip(self, clip_id, frame=None):
        clip = self.timeline.find_clip(clip_id)
        if clip is None or clip.track.locked or not clip.source_path:
            return
        at = self.playhead if frame is None else int(frame)
        source_frame = clip.source_frame_at(at if clip.timeline_start <= at < clip.end else clip.timeline_start)

        def action():
            target = self.timeline.find_clip(clip_id)
            target.source_in = int(source_frame)
            target.source_out = int(source_frame) + 1
            target.source_offset = 0
            target.freeze = True
            target.loop = False

        self.stack.run("Freeze frame", action)

    def add_freeze_at_playhead(self, duration=None):
        frame = self.playhead
        duration = max(1, int(duration if duration is not None else self.timeline.fps * 2))
        candidates = [c for c in self.timeline.all_clips()
                      if c.track.type == "video" and not c.track.locked and c.source_path
                      and c.timeline_start <= frame < c.end]
        primary = next((c for c in candidates if c.id in self.selected), None)
        if primary is None and candidates:
            primary = candidates[0]
        if primary is None:
            self.status.emit("Place the playhead inside a video clip to add a freeze frame")
            return None
        targets = [primary]
        created = []

        def action():
            for source_clip in targets:
                mapped = source_clip.source_frame_at(frame)
                if frame == source_clip.timeline_start:
                    right = source_clip
                    right.timeline_start += duration
                    right.source_offset = source_clip.source_offset
                else:
                    right_id = self.timeline.split_clip(source_clip.id, frame)
                    if right_id is None:
                        continue
                    right = self.timeline.find_clip(right_id)
                    right.timeline_start += duration
                frozen = self.timeline.new_clip(source_clip.name + " (Freeze)", source_clip.source_path,
                                                source_clip.track, frame, duration, int(mapped),
                                                source_out=int(mapped) + 1, source_fps=source_clip.source_fps,
                                                speed=1.0, reverse=False, loop=False, freeze=True)
                frozen.effects = copy.deepcopy(source_clip.effects)
                created.append(frozen.id)
                self.timeline._push_neighbours(source_clip.track, {right.id, frozen.id})

        self.stack.run("Add freeze frame", action)
        if created:
            self.set_selection({created[0]})
            self.clip_split.emit(primary.id, created[0])
            return created[0]
        return None

    def delete_selected(self, ripple=False):
        # Selected markers are deleted first: they are the most specific selection.
        if self.marker_selected:
            self.delete_markers()
            return
        ids = self._editable_ids(self.with_links(self.selected))
        if ids:
            self.stack.run("Ripple delete" if ripple else "Delete", lambda: self.timeline.delete_clips(ids, ripple))

    # ---------------------------------------------------------------- clipboard
    def copy_selected(self):
        clips = self._selected_clips()
        if not clips:
            return False
        TimelineSession._clipboard = [self.timeline.clip_record(c) for c in clips]
        self.clip_copied.emit()
        self.status.emit(f"Copied {len(clips)} clip(s)")
        return True

    def cut_selected(self):
        if not self.copy_selected():
            return
        ids = self._editable_ids(self.selected)
        if ids:
            self.stack.run("Cut", lambda: self.timeline.delete_clips(ids))

    def paste(self):
        records = TimelineSession._clipboard
        if not records:
            return
        anchor = None
        selected = self._selected_clips()
        if selected:
            anchor = min(selected, key=lambda c: c.timeline_start).track
        frame, mode, tl = self.playhead, self.overlap_mode, self.timeline
        ids = self.stack.run("Paste", lambda: tl.paste_records(records, frame, anchor, mode))
        self.set_selection(set(ids))

    # ------------------------------------------------------ effects & attributes
    _attributes = None

    def copy_effects(self, ids=None):
        """Copy the effect stack (and animations) of the first selected clip."""
        clips = self._selected_clips() if ids is None else [
            c for c in (self.timeline.find_clip(i) for i in ids) if c is not None]
        if not clips:
            return False
        source = clips[0]
        TimelineSession._attributes = {
            "effects": clone_effects(source.effects),
            "presets": copy.deepcopy(getattr(source, "presets", []) or []),
            "transform": copy.deepcopy(source.transform),
            "transform_meta": copy.deepcopy(source.transform_meta),
            "audio": {"volume": source.volume, "gain": copy.deepcopy(source.gain),
                      "fade_in": source.fade_in, "fade_out": source.fade_out,
                      "fade_in_curve": source.fade_in_curve,
                      "fade_out_curve": source.fade_out_curve},
            "text": copy.deepcopy(source.text) if source.is_text else None,
        }
        self.status.emit(f"Copied effects from {source.name}")
        return True

    def copy_attributes(self, ids=None):
        """Copy everything Paste Attributes can transfer from the first selected clip."""
        return self.copy_effects(ids)

    @staticmethod
    def _compatible_effects(entries, clip, resolver):
        """Drop effects whose declaration does not cover this clip's media type."""
        if resolver is None:
            return clone_effects(entries)
        allowed = "audio" if (clip.track is not None and clip.track.type == "audio") else "video"
        kept = []
        for entry in entries or ():
            runtime = resolver.get(entry.get("effect_id"))
            if runtime is not None and hasattr(runtime, "supports_track_type") \
                    and not runtime.supports_track_type(allowed):
                continue
            kept.append(copy.deepcopy(entry))
        return kept

    def paste_effects(self, ids=None, resolver=None):
        """Replace the selected clips' effect stacks with the copied one."""
        source = TimelineSession._attributes
        if not source:
            self.status.emit("Copy effects first")
            return 0
        ids = self.with_links(ids) if ids is not None else self.selected
        clips = [clip for clip in (self.timeline.find_clip(i) for i in ids)
                 if clip is not None and not clip.track.locked]
        if not clips:
            return 0
        copied = source

        def action():
            for candidate in clips:
                target = self.timeline.find_clip(candidate.id)
                if target is None:
                    continue
                target.effects = self._compatible_effects(copied["effects"], target, resolver)
                target.presets = copy.deepcopy(copied["presets"])
            return len(clips)

        self.stack.run("Paste effects", action)
        self.changed.emit()
        self.status.emit(f"Effects pasted onto {len(clips)} clip(s)")
        return len(clips)

    def paste_attributes(self, parts, ids=None, resolver=None):
        """Apply chosen attribute groups from the copied clip; one undo step."""
        source = TimelineSession._attributes
        if not source:
            self.status.emit("Copy attributes first")
            return 0
        parts = {str(part) for part in (parts or ())}
        ids = self.with_links(ids) if ids is not None else self.selected
        clips = [clip for clip in (self.timeline.find_clip(i) for i in ids)
                 if clip is not None and not clip.track.locked]
        if not clips or not parts:
            return 0

        def action():
            for candidate in clips:
                target = self.timeline.find_clip(candidate.id)
                if target is None:
                    continue
                if "transform" in parts and isinstance(source.get("transform"), dict):
                    T.ensure_transform(target)
                    for name, value in copy.deepcopy(source["transform"]).items():
                        if name in ("flip_h", "flip_v"):
                            target.transform[name] = bool(value)
                        else:
                            target.transform[name] = value
                    target.transform_meta = copy.deepcopy(source.get("transform_meta") or {})
                if "effects" in parts:
                    target.effects = self._compatible_effects(source["effects"], target, resolver)
                if "animations" in parts:
                    target.presets = copy.deepcopy(source.get("presets") or [])
                if "audio" in parts:
                    audio = source.get("audio") or {}
                    for name in ("volume", "gain", "fade_in", "fade_out",
                                 "fade_in_curve", "fade_out_curve"):
                        if name in audio:
                            setattr(target, name, copy.deepcopy(audio[name]))
                    target.clamp_fades("in")
                if "text" in parts and target.is_text and isinstance(source.get("text"), dict):
                    for name, value in copy.deepcopy(source["text"]).items():
                        if name in ("text", "keys", "caption"):
                            continue
                        target.text[name] = value
            return len(clips)

        self.stack.run("Paste attributes", action)
        self.changed.emit()
        self.status.emit(f"Attributes pasted onto {len(clips)} clip(s)")
        return len(clips)

    def remove_all_effects(self, ids=None):
        """Clear the effects and animations of the selected clips (one undo step)."""
        ids = self.with_links(ids) if ids is not None else self.selected
        clips = [clip for clip in (self.timeline.find_clip(i) for i in ids)
                 if clip is not None and not clip.track.locked]
        clips = [clip for clip in clips if clip.effects or getattr(clip, "presets", None)]
        if not clips:
            return 0

        def action():
            for candidate in clips:
                target = self.timeline.find_clip(candidate.id)
                if target is not None:
                    target.effects = []
                    target.presets = []
            return len(clips)

        self.stack.run("Remove all effects", action)
        self.changed.emit()
        return len(clips)

    def duplicate_selected(self):
        clips = self._selected_clips()
        if not clips:
            return
        records = [self.timeline.clip_record(c) for c in clips]
        frame, mode, tl = max(c.end for c in clips), self.overlap_mode, self.timeline
        ids = self.stack.run("Duplicate", lambda: tl.paste_records(records, frame, None, mode))
        self.set_selection(set(ids))

    def select_all(self):
        self.set_selection({c.id for c in self.timeline.all_clips()})

    def handle_key(self, event):
        """Process a model-level shortcut. Returns True if the key was consumed."""
        keysym = event.keysym
        lower = keysym.lower()
        ctrl, shift = bool(event.state & 0x4), bool(event.state & 0x1)
        fps = self.timeline.fps
        limit = max(self.timeline.duration, self.timeline.min_duration)
        if ctrl:
            if lower == "z":
                self.redo() if shift else self.undo()
            elif lower == "y":
                self.redo()
            elif lower == "c":
                self.copy_selected()
            elif lower == "x":
                self.cut_selected()
            elif lower == "v":
                self.paste()
            elif lower == "d":
                self.duplicate_selected()
            elif lower == "a":
                self.select_all()
            else:
                return False
            return True
        if keysym == "space":
            self.play_toggle_requested.emit()
        elif keysym in ("Left", "Right"):
            step = fps if shift else 1
            self.set_playhead(min(limit, max(0, self.playhead + (step if keysym == "Right" else -step))))
        elif keysym == "Home":
            self.set_playhead(0)
        elif keysym == "End":
            self.set_playhead(self.timeline.duration)
        elif keysym in ("Delete", "BackSpace"):
            self.delete_selected(ripple=shift)
        elif lower in ("c", "s"):
            self.split_at_playhead()
        elif lower == "i":
            self.set_loop_start_at_playhead()
        elif lower == "o":
            self.set_loop_end_at_playhead()
        elif lower == "f" and shift:
            self.add_freeze_at_playhead()
        elif lower == "m":
            self.add_marker_at_playhead()
        elif keysym in ("bracketleft", "["):
            self.jump_to_marker(-1)
        elif keysym in ("bracketright", "]"):
            self.jump_to_marker(1)
        elif lower == "r":
            self.tool.set("select" if self.tool.get() == "razor" else "razor")
        elif keysym == "Escape":
            self.tool.set("select")
            self.set_selection(set())
        else:
            return False
        return True


    # ------------------------------------------------------- animation gestures
    def begin_gesture(self):
        """Snapshot once for a continuous drag, so it becomes one undo step."""
        if self._gesture_snapshot is None:
            self._gesture_snapshot = self.timeline.snapshot()
        return self._gesture_snapshot

    def commit_gesture(self, label):
        """Push the gesture's single undo entry (no-op when nothing changed)."""
        before = self._gesture_snapshot
        self._gesture_snapshot = None
        if before is None:
            return False
        return self.stack.commit(label, before)

    def cancel_gesture(self):
        before = self._gesture_snapshot
        self._gesture_snapshot = None
        if before is not None:
            self.timeline.restore(before)
            self.changed.emit()

    def edit_channel(self, clip, channel, value, frame=None, label="Edit property"):
        """One undoable property write at the playhead (or `frame`)."""
        frame = self.playhead - clip.timeline_start if frame is None else frame
        auto = bool(self.auto_keyframe.get())

        def action():
            target = self.timeline.find_clip(clip.id)
            if target is not None and not target.track.locked:
                CH.set_value(target, channel, value, frame, auto)

        return self.stack.run(label, action)

    def toggle_channel_key(self, clip, channel):
        frame = max(0, self.playhead - clip.timeline_start)

        def action():
            target = self.timeline.find_clip(clip.id)
            if target is not None and not target.track.locked:
                CH.toggle_key(target, channel, frame)

        self.stack.run("Toggle keyframe", action)
        return frame

    def toggle_channel_animation(self, clip, channel):
        animated = CH.is_animated(clip, channel)
        frame = max(0, self.playhead - clip.timeline_start)

        def action():
            target = self.timeline.find_clip(clip.id)
            if target is None or target.track.locked:
                return
            if animated:
                CH.unanimate(target, channel, frame)
            else:
                CH.animate(target, channel, frame)

        self.stack.run("Disable animation" if animated else "Enable animation", action)
        self.changed.emit()

    def jump_to_channel_key(self, clip, channel, direction=1):
        """Previous/next keyframe of one property, moving the playhead onto it."""
        frame = max(0, self.playhead - clip.timeline_start)
        times = CH.key_times(clip, channel)
        options = [time for time in times if (time < frame if direction < 0 else time > frame)]
        if not options:
            return None
        target = max(options) if direction < 0 else min(options)
        self.set_playhead(clip.timeline_start + target)
        return target

    # ------------------------------------------------------------ transform edits
    def transform_clips(self, ids, action, label):
        """Apply `action(clip)` to each id as one undoable transform edit."""
        def run():
            for cid in ids:
                clip = self.timeline.find_clip(cid)
                if clip is not None and not clip.track.locked:
                    action(clip)
        self.stack.run(label, run)
        self.changed.emit()

    def reset_transform(self, ids):
        self.transform_clips(ids, lambda clip: T.reset_transform(clip), "Reset transform")

    def center_in_frame(self, ids):
        self.transform_clips(ids, lambda clip: T.center_in_frame(clip), "Center in frame")

    def fit_frame(self, ids):
        self.transform_clips(ids, lambda clip: T.fit_frame(clip), "Fit to frame")

    def fill_frame(self, ids, size_for=None):
        """Scale the clips to cover the project frame (aspect kept, overflow cropped)."""
        size = self.project_size
        if not size or not size[0] or not size[1]:
            self.status.emit("Fill frame needs an open project with a resolution")
            return

        def action(clip):
            width, height = (size_for(clip) if size_for is not None else (0, 0))
            if not width or not height:
                # Text and other generated clips already are the project frame.
                width, height = size
            T.fill_frame(clip, size, (width, height))
        self.transform_clips(ids, action, "Fill frame")

    def flip_selected(self, ids, axis):
        def action(clip):
            T.ensure_transform(clip)
            key = "flip_h" if axis == "h" else "flip_v"
            clip.transform[key] = not bool(clip.transform.get(key))
        self.transform_clips(ids, action, "Flip horizontal" if axis == "h" else "Flip vertical")

    def scale_selected(self, ids, percent):
        def action(clip):
            T.scale_to(clip, percent)
        self.transform_clips(ids, action, "Scale clip")

    # ------------------------------------------------------------ easing presets
    def apply_easing(self, entries, name):
        """Apply an easing preset to [(clip_id, channel, time)] as one undo step."""
        from .easing import preset_spec
        payload = preset_spec(name)
        if payload is None or not entries:
            return False
        spec = payload["interpolation"]
        handles = payload["handles"]

        def action():
            for clip_id, channel, time in entries:
                clip = self.timeline.find_clip(clip_id)
                if clip is None or clip.track.locked:
                    continue
                value = CH.stored(clip, channel)
                if not isinstance(value, AnimatedValue):
                    continue
                key = value.key_at(time)
                if key is None:
                    continue
                key.interpolation = spec
                if handles:
                    key.out_value_handle = (float(handles[0]), float(handles[1]))
                    following = value.keyframes[value.keyframes.index(key) + 1:]
                    if following:
                        following[0].in_value_handle = (float(handles[2]), float(handles[3]))
                    key.in_value_handle = (float(handles[2]), float(handles[3]))
                else:
                    key.out_value_handle = None
        self.stack.run("Apply easing: " + name, action)
        self.changed.emit()
        return True

    def set_interpolation(self, entries, spec):
        """Set a raw interpolation spec (linear/hold/bezier/custom) on keyframes."""
        def action():
            for clip_id, channel, time in entries:
                clip = self.timeline.find_clip(clip_id)
                if clip is None or clip.track.locked:
                    continue
                value = CH.stored(clip, channel)
                if isinstance(value, AnimatedValue):
                    key = value.key_at(time)
                    if key is not None:
                        key.interpolation = spec
        self.stack.run("Set interpolation", action)
        self.changed.emit()

    # ---------------------------------------------------------- keyframe clipboard
    def has_key_clipboard(self):
        return bool(self._key_clipboard)

    # ------------------------------------------------------------- effects/drops
    def apply_to_clips(self, ids, label, fn):
        """Run fn(clip) for each id as a single undoable action."""
        ids = list(ids)

        def action():
            for cid in ids:
                clip = self.timeline.find_clip(cid)
                if clip is not None:
                    fn(clip)

        self.stack.run(label, action)

    # ---------------------------------------------------------------- presets
    def apply_preset(self, preset, unit=None, ids=None):
        """Add a library preset to every selected video clip as one undoable edit.

        In and Out presets replace their own category (one entrance and one exit per
        clip); Loops stack instead. Text clips take the requested unit, media clips
        ignore it.
        """
        from . import preset_library as LIB
        from . import presets as PM

        clips = [clip for clip in self._selected_clips()
                 if clip.track is not None and clip.track.type == "video"
                 and not clip.track.locked]
        if ids is not None:
            clips = [clip for clip in (self.timeline.find_clip(cid) for cid in ids)
                     if clip is not None and clip.track.type == "video"]
        if not clips or not preset:
            return 0
        category, program, args, knobs = LIB.preset_spec(preset, unit)
        name = preset.get("name") or preset.get("id", "Preset")
        fps = self.timeline.fps
        added = 0

        def action():
            nonlocal added
            for candidate in clips:
                target = self.timeline.find_clip(candidate.id)
                if target is None:
                    continue
                if preset.get("instances") and not preset.get("program"):
                    # Saved from a clip: replay its whole animation, not just one program.
                    PM.from_payload(preset, target)
                    added += 1
                    continue
                instance_knobs = dict(knobs)
                if not target.is_text:
                    # Media clips animate as a whole; the Unit knob never reaches them.
                    instance_knobs["unit"] = "whole"
                PM.add(target, preset["id"], category, program, args, instance_knobs,
                       duration=PM.default_duration(target, fps, category), name=name)
                added += 1

        self.stack.run(f"Apply {name}", action)
        if added:
            self.status.emit(f"{name} applied to {added} clip{'s' if added != 1 else ''}")
        return added

    def apply_animation(self, presets, ids=None, family_program=""):
        """Apply one animation family - one variant, or an In+Out pair - atomically.

        The family's previous instance is replaced in the same undo step, so switching
        Slide from In to Out edits that animation instead of stacking a second one.
        """
        from . import preset_library as LIB
        from . import presets as PM

        clips = [clip for clip in (self.timeline.find_clip(cid) for cid in (ids or []))
                 if clip is not None and clip.track is not None
                 and clip.track.type == "video" and not clip.track.locked]
        presets = [preset for preset in (presets or []) if preset]
        if not clips or not presets:
            return 0
        specs = [LIB.preset_spec(preset, None) for preset in presets]
        fps = self.timeline.fps
        added = 0

        def action():
            nonlocal added
            for candidate in clips:
                target = self.timeline.find_clip(candidate.id)
                if target is None:
                    continue
                if family_program:
                    PM.instances(target)[:] = [row for row in PM.instances(target)
                                               if row.get("program") != family_program]
                for preset, spec in zip(presets, specs):
                    category, program, args, knobs = spec
                    instance_knobs = dict(knobs)
                    if not target.is_text:
                        instance_knobs["unit"] = "whole"
                    PM.add(target, preset["id"], category, program, args, instance_knobs,
                           duration=PM.default_duration(target, fps, category),
                           name=preset.get("name") or preset.get("id", "Animation"))
                    added += 1

        name = presets[0].get("name") or "animation"
        self.stack.run(f"Apply {name}", action)
        if added:
            self.status.emit(f"{name} applied to {added} clip{'s' if added != 1 else ''}")
        return added

    def switch_preset_variant(self, clip_id, index, label, preset_id, category, program,
                              args=None, name=""):
        """Turn one instance into another variant of its animation, in place.

        Switching Slide from In to Out (or changing its direction) edits the instance the
        clip already has: its knobs, its window and its place in the stack survive, and
        no second animation is stacked on top.
        """
        from . import presets as PM

        clip = self.timeline.find_clip(clip_id)
        if clip is None or not 0 <= int(index) < len(PM.instances(clip)):
            return False
        if category not in PM.CATEGORIES:
            return False

        def action():
            target = self.timeline.find_clip(clip_id)
            if target is None or not 0 <= int(index) < len(PM.instances(target)):
                return
            instance = PM.instances(target)[int(index)]
            instance["preset"] = str(preset_id)
            instance["category"] = category
            instance["program"] = str(program)
            instance["args"] = dict(args or {})
            if name:
                instance["name"] = str(name)
            if category == "in":
                instance["start"] = 0
            PM.set_range(instance, None, None, target)

        self.stack.run(label, action)
        return True

    def remove_preset(self, clip_id, index):
        """Drop one instance from a clip (one undo step)."""
        from . import presets as PM

        clip = self.timeline.find_clip(clip_id)
        if clip is None or not 0 <= int(index) < len(PM.instances(clip)):
            return False
        name = PM.instances(clip)[int(index)].get("name") or "preset"

        def action():
            target = self.timeline.find_clip(clip_id)
            if target is not None and 0 <= int(index) < len(PM.instances(target)):
                PM.instances(target).pop(int(index))

        self.stack.run(f"Remove {name}", action)
        return True

    def update_preset(self, clip_id, index, label="Preset", knobs=None, start=None,
                      duration=None):
        """Change some knobs and/or the window of one instance as one undo step."""
        from . import presets as PM

        clip = self.timeline.find_clip(clip_id)
        if clip is None or not 0 <= int(index) < len(PM.instances(clip)):
            return False
        knobs = dict(knobs or {})
        if not knobs and start is None and duration is None:
            return False

        def action():
            target = self.timeline.find_clip(clip_id)
            if target is None or not 0 <= int(index) < len(PM.instances(target)):
                return
            instance = PM.instances(target)[int(index)]
            for key, value in knobs.items():
                PM.set_knob(instance, key, value)
            if start is not None or duration is not None:
                PM.set_range(instance, start, duration, target)

        self.stack.run(label, action)
        return True

    def bake_preset(self, clip_id, index):
        """Turn one instance into plain keyframes (one undo step)."""
        from . import presets as PM

        clip = self.timeline.find_clip(clip_id)
        if clip is None or not 0 <= int(index) < len(PM.instances(clip)):
            return False
        instance = PM.instances(clip)[int(index)]
        if not PM.bakeable(instance, clip):
            self.status.emit("Per-unit presets need Split text; bake works on whole-clip presets")
            return False
        name = instance.get("name") or "preset"
        size = self.project_size or (1920, 1080)

        def action():
            target = self.timeline.find_clip(clip_id)
            if target is not None and 0 <= int(index) < len(PM.instances(target)):
                PM.bake(target, PM.instances(target)[int(index)], project_size=size)

        self.stack.run(f"Bake {name}", action)
        self.status.emit(f"{name} baked to keyframes")
        return True

    def split_text_clip(self, clip_id, pieces, mode="letter"):
        """Split a text clip into one clip per unit, at the exact same positions.

        `pieces` is [(text, offset_x, offset_y)] in project pixels relative to the frame
        centre, produced by the text layout (the model stays free of font metrics). The
        pieces inherit the picture's transform, effects and fades, and a per-unit preset
        instance becomes a whole-clip instance on each piece - which is the point of
        splitting: every letter can then be edited and animated on its own.
        """
        from . import presets as PM
        from . import text as TXT

        clip = self.timeline.find_clip(clip_id)
        if clip is None or not clip.is_text or len(pieces or ()) < 2:
            return []
        if clip.track is None or clip.track.locked:
            return []
        source = {key: value for key, value in (clip.text or {}).items() if key != "keys"}
        created = []

        def action():
            nonlocal created
            created = []
            track = clip.track
            start, duration = clip.timeline_start, clip.duration
            for text_value, offset_x, offset_y in pieces:
                piece = self.timeline.new_clip(
                    name=str(text_value)[:40] or "Text", source_path="", track=track,
                    start=start, duration=duration, source_in=clip.source_in,
                    effects=clone_effects(clip.effects), source_out=clip.source_out,
                    source_fps=clip.source_fps, speed=clip.speed, reverse=clip.reverse,
                    loop=clip.loop, freeze=clip.freeze, source_offset=clip.source_offset,
                    source_min=clip.source_min, source_max=clip.source_max,
                    volume=clip.volume, fade_in=clip.fade_in, fade_out=clip.fade_out,
                    fade_in_curve=clip.fade_in_curve, fade_out_curve=clip.fade_out_curve,
                    stream=clip.stream, transform=copy.deepcopy(clip.transform),
                    transform_meta=copy.deepcopy(clip.transform_meta),
                    gain=copy.deepcopy(clip.gain))
                settings = copy.deepcopy(source)
                settings["text"] = str(text_value)
                settings["keys"] = {}
                piece.text = settings
                TXT.ensure_text(piece)
                piece.text["offset_x"] = float(TXT.DEFAULTS["offset_x"]) + float(offset_x)
                piece.text["offset_y"] = float(TXT.DEFAULTS["offset_y"]) + float(offset_y)
                piece.presets = copy.deepcopy(PM.instances(clip))
                for instance in PM.instances(piece):
                    instance.setdefault("knobs", {})["unit"] = "whole"
                created.append(piece.id)
            track.clips.remove(clip)

        self.stack.run(f"Split text into {mode}s", action)
        if created:
            self.set_selection(set(created), expand=False)
            self.status.emit(f"Split into {len(created)} {mode} clips")
        return created


    def apply_look(self, look, ids=None):
        """Insert a look's effect stack plus its Strength card (one undo step).

        The stack is a normal list of effect entries, so every effect inside a look can
        still be reordered, tweaked or removed; the group card only scales their Mix.
        """
        from . import looks as LOOKS
        look = LOOKS.get(look) if not isinstance(look, dict) else look
        if look is None:
            return []
        ids = list(ids if ids is not None else sorted(self.selected))
        targets = []
        for clip_id in ids:
            clip = self.timeline.find_clip(clip_id)
            if clip is None:
                continue
            track = getattr(clip, "track", None)
            if track is not None and track.type != "video":
                continue
            targets.append(clip_id)
        if not targets:
            self.status.emit("Looks need a video clip or an adjustment layer")
            return []
        group_counter = [0]

        def action():
            for clip_id in targets:
                clip = self.timeline.find_clip(clip_id)
                if clip is None:
                    continue
                group_counter[0] += 1
                group = f"{look['id']}#{group_counter[0]}"
                clip.effects.append({"effect_id": "look", "enabled": True,
                                     "look_id": group, "name": look["name"],
                                     "params": {"look": look["id"], "strength": 100}})
                for item in LOOKS.stack_for(look):
                    clip.effects.append({"effect_id": item["effect_id"],
                                         "enabled": item.get("enabled", True),
                                         "look_id": group,
                                         "params": copy.deepcopy(item.get("params") or {})})

        self.stack.run(f"Apply {look['name']} look", action)
        self.status.emit(f"{look['name']} applied")
        return targets

    def set_blend_mode(self, clip_id, mode):
        """Set how a clip is mixed with the layers below it (one undo step)."""
        from ..processing.blend import normalise_mode
        key = normalise_mode(mode)
        clip = self.timeline.find_clip(clip_id)
        if clip is None or clip.blend == key:
            return False
        self.apply_to_clips([clip_id], "Blend mode", lambda c: setattr(c, "blend", key))
        return True

    def add_adjustment_clip(self, start=None, duration=None, track=None):
        """Create an adjustment layer on a video track and select it.

        It has no picture: its effects run over everything already composited below it
        for as long as the clip plays, and it is editable like any other clip (trim,
        fades, opacity keyframes, blend mode, effects).
        """
        tl = self.timeline
        start = max(0, int(self.playhead if start is None else start))
        duration = max(1, int(duration if duration is not None else round(tl.fps * 5)))
        holder = {}

        def action():
            target = track
            if target is None or target.type != "video":
                # A new video track becomes the top layer, above every picture below it.
                target = tl.add_track("video")
            holder["clip"] = tl.new_clip(name="Adjustment", source_path="", track=target,
                                         start=start, duration=duration, adjustment=True)

        self.stack.run("Add adjustment layer", action)
        clip = holder.get("clip")
        if clip is None:
            return None
        self.set_selection({clip.id})
        self.reveal_requested.emit(clip.timeline_start)
        self.status.emit("Adjustment layer added")
        return clip.id

    # ---------------------------------------------------------------- text clips
    def add_text_clip(self, text="Text", start=None, duration=None, track=None):
        """Create a generated text clip on a video track and select it.

        The clip is an ordinary timeline clip (transform, effects, fades, keyframes), so
        everything the editor can do to a picture also works on it.
        """
        tl = self.timeline
        start = max(0, int(self.playhead if start is None else start))
        duration = max(1, int(duration if duration is not None else round(tl.fps * 5)))
        holder = {}

        def free_track():
            # Text belongs above the picture, so take the topmost video lane that is clear
            # at that time; otherwise add a new video track (which becomes the top layer).
            for lane in tl.tracks_of_type("video"):
                busy = any(not (clip.end <= start or clip.timeline_start >= start + duration)
                           for clip in lane.clips)
                if not busy and not lane.locked:
                    return lane
            return tl.add_track("video")

        def action():
            target = track
            if target is None or target.type != "video":
                target = free_track()
            first_line = (str(text).strip().splitlines() or ["Text"])[0].strip()
            clip = tl.new_clip(name=first_line[:40] or "Text",
                               source_path="", track=target, start=start, duration=duration,
                               text=TXT.blank())
            TXT.set_content(clip, text, 0, False)
            holder["clip"] = clip

        self.stack.run("Add text", action)
        clip = holder.get("clip")
        if clip is None:
            return None
        self.set_selection({clip.id})
        self.reveal_requested.emit(clip.timeline_start)
        self.status.emit("Text clip added")
        return clip.id

    def add_media(self, entries, start=None, track=None, find_free=False, reveal=True,
                  select=True, fit=False, label="Import media"):
        """Place media entries (dict name/path/duration/type) one after another.

        `fit` scales the newly created stills to the project frame, and it happens inside
        the same undo entry as the insert: one paste is one Undo, never two.
        """
        tl = self.timeline

        def action():
            ids = tl.place_clips(entries, start, track, find_free)
            if fit:
                for clip_id in ids:
                    clip = tl.find_clip(clip_id)
                    if clip is not None and clip.kind == "image":
                        T.fit_frame(clip)
            return ids

        ids = self.stack.run(label, action)
        if ids:
            if select:
                self.set_selection(set(ids))
            first = tl.find_clip(ids[0])
            if reveal and first is not None:
                self.reveal_requested.emit(first.timeline_start)
        return ids

    def update_captions(self, edits, label="Edit captions"):
        """Apply transcript/editor changes in one undo step.

        Each edit is {"id": clip_id, "text": str|None, "start": frame|None, "end": frame|None};
        missing keys leave that part of the caption alone.
        """
        from ..captions import model as caption_model

        edits = [dict(edit) for edit in edits or ()]
        if not edits:
            return False

        def action():
            for edit in edits:
                clip = self.timeline.find_clip(edit.get("id"))
                if clip is None or not caption_model.is_caption(clip):
                    continue
                if edit.get("start") is not None:
                    clip.timeline_start = max(0, int(edit["start"]))
                if edit.get("end") is not None:
                    clip.duration = max(2, int(edit["end"]) - clip.timeline_start)
                if edit.get("text") is not None:
                    TXT.set_content(clip, str(edit["text"]), 0)
                clip.clamp_fades("in")

        changed = self.stack.run(label, action)
        if changed:
            self.changed.emit()
        return changed

    # ------------------------------------------------------------------ normalize
    def normalize_audio(self, targets, label="Normalize audio"):
        """Set each (clip_id, gain_db) as one undo step; peak normalization, not loudness."""
        from .animation import clamp_gain_db

        targets = [(int(cid), float(db)) for cid, db in (targets or ())]
        if not targets:
            return False

        def action():
            for clip_id, gain_db in targets:
                clip = self.timeline.find_clip(clip_id)
                if clip is None or clip.track.locked:
                    continue
                clip.gain = clamp_gain_db(gain_db)

        changed = self.stack.run(label, action)
        if changed:
            self.changed.emit()
        return changed

    # -------------------------------------------------------------- remove silences
    def remove_silence(self, clip_id, cuts, *, members=None, crossfade_frames=0,
                       ripple=True, label="Remove dead silence"):
        """Cut silent ranges (seconds, relative to the analysed clip) from a group.

        `members` is the authoritative set of clips the dialog chose to include; when it
        is omitted the clip's link group is used. Every cut is converted to absolute
        timeline frames once, then all members are split at those exact frames. Only the
        span every member covers is edited, so a cut can never land where one member is
        missing and pull the group out of sync. Surviving pieces are re-linked per
        retained segment (not merely per start frame, so offset pairs keep working) with
        fresh ids. Ripple closes gaps on the included tracks only; other tracks and all
        source files are untouched. The whole edit is one undo step.
        """
        timeline = self.timeline
        clip = timeline.find_clip(clip_id)
        if clip is None:
            return 0
        fps = max(1, timeline.fps)
        if members is None:
            group = timeline.link_group(clip.link_id) if clip.link_id else [clip]
        else:
            group = [timeline.find_clip(cid) for cid in members]
            group = [member for member in group if member is not None]
            if not any(member.id == clip.id for member in group):
                group.insert(0, clip)
        unique = []
        for member in group:
            if all(member.id != other.id for other in unique):
                unique.append(member)
        locked = next((member for member in unique if member.track.locked), None)
        if locked is not None:
            self.status.emit(f"{locked.track.name} is locked — unlock it to remove dead silence")
            return 0
        window_start = max(member.timeline_start for member in unique)
        window_end = min(member.end for member in unique)
        if window_end - window_start < 2:
            self.status.emit("The included clips do not overlap, so there is nothing to cut together")
            return 0
        # The detected cuts are relative to the analysed clip, but every member is split
        # on the same absolute frame; one plan, one rounding rule.
        ranges = []
        for start_seconds, end_seconds in cuts or ():
            first = clip.timeline_start + int(round(float(start_seconds) * fps))
            last = clip.timeline_start + int(round(float(end_seconds) * fps))
            first = max(window_start, int(first))
            last = min(window_end, int(last))
            if last - first >= 1:
                ranges.append((first, last))
        if not ranges:
            return 0
        merged = []
        for first, last in sorted(ranges):
            if merged and first <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], last))
            else:
                merged.append((first, last))
        ranges = merged
        spans = [(member.id, member.track, member.timeline_start, member.end)
                 for member in unique]
        removed_frames = 0

        def action():
            nonlocal removed_frames
            boundaries = sorted({frame for pair in ranges for frame in pair})
            pieces = []
            piece_ids = set()
            for member_id, track, start, end in spans:
                current = timeline.find_clip(member_id)
                for boundary in boundaries:
                    if current is None:
                        break
                    if current.timeline_start < boundary < current.end:
                        new_id = timeline.split_clip(current.id, boundary)
                        current = timeline.find_clip(new_id) if new_id is not None else None
                for candidate in list(track.clips):
                    if (candidate.id not in piece_ids and candidate.timeline_start >= start
                            and candidate.end <= end):
                        pieces.append(candidate)
                        piece_ids.add(candidate.id)
            doomed = [piece for piece in pieces
                      if any(piece.timeline_start < last and piece.end > first
                             for first, last in ranges)]
            doomed_ids = {piece.id for piece in doomed}
            survivors = [piece for piece in pieces if piece.id not in doomed_ids]
            removed_frames = sum(piece.duration for piece in doomed)

            # A piece that ends where a cut begins gets a fade-out, and one that starts at
            # the cut's end gets a fade-in: a short crossfade removes the click a hard cut
            # would leave. Timings are the pre-ripple positions, so this runs before the
            # delete moves anything.
            if crossfade_frames > 0:
                cut_starts = {first for first, _last in ranges}
                cut_ends = {last for _first, last in ranges}
                for piece in survivors:
                    if piece.end in cut_starts:
                        piece.fade_out = min(int(crossfade_frames), piece.duration)
                        piece.clamp_fades("out")
                    if piece.timeline_start in cut_ends:
                        piece.fade_in = min(int(crossfade_frames), piece.duration)
                        piece.clamp_fades("in")

            # Re-link survivors per retained segment, before the ripple shifts them, so a
            # pair keeps one id per surviving piece even when the members started apart.
            retained = []
            cursor = window_start
            for first, last in ranges:
                if first > cursor:
                    retained.append((cursor, first))
                cursor = max(cursor, last)
            if cursor < window_end:
                retained.append((cursor, window_end))
            for piece in survivors:
                piece.link_id = 0
            for segment_start, segment_end in retained:
                shared = [piece for piece in survivors
                          if piece.timeline_start < segment_end and piece.end > segment_start]
                if len(shared) >= 2:
                    new_link = timeline.new_link_id()
                    for piece in shared:
                        piece.link_id = new_link
            timeline.delete_clips(doomed_ids, ripple=ripple)

        self.stack.run(label, action)
        if removed_frames:
            self.set_selection(set())
            self.status.emit(f"Removed {removed_frames / float(fps):.2f}s of silence")
        self.changed.emit()
        return len(ranges)

    # ------------------------------------------------------------------- captions
    def caption_source_range(self, source="timeline"):
        """(start_frame, end_frame, clip_ids or None) for one caption source choice.

        `clip_ids` limits transcription to those clips (a selected clip or a whole track);
        None means "every audible clip in the range", which is what the timeline uses.
        """
        timeline = self.timeline
        primary = self.primary_clip()
        if source == "clip":
            if primary is None:
                return None
            # A linked picture and sound are one editable subject. Analyse the audible
            # members of the clicked clip's group, not the picture: a video member is
            # silent (its linked audio carries the sound), and with Linked Selection on
            # the video often joins the selection first. Embedded-audio videos stay in
            # because carries_audio is true for them.
            group = (timeline.link_group(primary.link_id)
                     if primary.link_id else [primary])
            audible = [clip for clip in group
                       if clip.carries_audio and clip.source_path]
            members = audible or [primary]
            return (min(clip.timeline_start for clip in members),
                    max(clip.end for clip in members),
                    [clip.id for clip in members])
        if source == "track":
            track = primary.track if primary is not None else None
            if track is None or not track.clips:
                return None
            ids = [clip.id for clip in track.clips]
            return (min(clip.timeline_start for clip in track.clips),
                    max(clip.end for clip in track.clips), ids)
        if source == "range":
            region = timeline.loop_range()
            if region is None:
                return None
            return int(region[0]), int(region[1]), None
        return 0, max(1, int(timeline.duration)), None

    def generate_captions(self, cues, *, style_id="classic", position=None, animation="",
                          start_frame=0, width=1920, height=1080, replace_range=None,
                          label="Generate captions"):
        """Create caption clips from cues on the Captions track, as one undo step.

        `replace_range` (frames) removes the captions already inside that range first, so
        regenerating a range updates exactly that range and never touches the rest.
        """
        from ..captions import model as caption_model

        cues = list(cues or ())
        if not cues:
            return []

        def action():
            track = caption_model.caption_track(self.timeline, create=True)
            if replace_range is not None:
                caption_model.clear_range(self.timeline, replace_range[0], replace_range[1], track=track)
            created = caption_model.build_clips(
                self.timeline, track, cues, style_id=style_id, position=position,
                animation=animation, start_frame=start_frame, width=width, height=height,
                fps=self.timeline.fps)
            return [clip.id for clip in created]

        ids = self.stack.run(label, action)
        if ids:
            self.set_selection(set(ids))
            self.status.emit(f"{len(ids)} caption{'s' if len(ids) != 1 else ''} created")
        self.changed.emit()
        return ids

    def restyle_captions(self, ids=None, style_id="classic", position=None):
        """Apply one style to any number of captions, keeping their words and timings."""
        from ..captions import model as caption_model
        from ..captions import styles as caption_styles

        ids = self.with_links(ids) if ids is not None else ({c.id for c in caption_model.caption_clips(self.timeline)})
        clips = [self.timeline.find_clip(cid) for cid in ids]
        clips = [clip for clip in clips if clip is not None and caption_model.is_caption(clip)]
        if not clips:
            return 0
        width, height = self.project_size or (1920, 1080)
        info = caption_styles.style(style_id)

        def action():
            for clip in clips:
                target = self.timeline.find_clip(clip.id)
                if target is None:
                    continue
                current = target.text.get(caption_model.CAPTION_KEY, {})
                words = list(current.get("words") or ())
                animation = current.get("animation", info["animation"])
                target.text = caption_styles.caption_text(
                    style_id, TXT.content_at(target, 0), width, height, position, animation,
                    words=words, caption_id=current.get("id", ""),
                    highlight=current.get("highlight"))
                if position is not None:
                    target.text[caption_model.CAPTION_KEY]["position"] = position
            return len(clips)

        self.stack.run("Caption style: " + info["name"], action)
        self.changed.emit()
        return len(clips)

    # ------------------------------------------------------------------- markers
    def selected_markers(self):
        return [self.timeline.marker_by_id(mid) for mid in sorted(self.marker_selected)
                if self.timeline.marker_by_id(mid) is not None]

    def set_marker_selection(self, ids, additive=False):
        """Select markers by id; "additive" keeps the existing selection (Shift)."""
        ids = {int(i) for i in ids}
        target = (self.marker_selected | ids) if additive else ids
        if target != self.marker_selected:
            self.marker_selected = target
            self.marker_selection_changed.emit(sorted(target))
        self.request_redraw()

    def clear_marker_selection(self):
        self.set_marker_selection(set())

    def add_marker_at_playhead(self):
        """M: add at the playhead; an existing marker on that frame is selected instead."""
        return self.add_marker(self.playhead)

    # Kept for older call sites and menus that still use the old name.
    toggle_marker_at_playhead = add_marker_at_playhead

    def add_marker(self, frame, name="", color=None):
        frame = max(0, int(frame))
        existing = self.timeline.marker_at(frame)
        if existing is not None:
            self.set_marker_selection({existing.id})
            self.status.emit(f"Marker already on {self._marker_label(existing)}")
            return existing
        holder = {}

        def action():
            holder["marker"] = self.timeline.add_marker(frame, name, color)

        self.stack.run("Add marker", action)
        marker = holder.get("marker")
        if marker is not None:
            self.set_marker_selection({marker.id})
            self.status.emit(f"Marker added at {self._marker_label(marker)}")
        return marker

    def delete_markers(self, ids=None):
        ids = set(ids) if ids is not None else set(self.marker_selected)
        ids &= {m.id for m in self.timeline.markers}
        if not ids:
            return 0
        tl = self.timeline
        self.stack.run("Delete marker" if len(ids) == 1 else f"Delete {len(ids)} markers",
                       lambda: [tl.remove_marker(mid) for mid in list(ids)])
        self.set_marker_selection(set())
        self.status.emit("Marker deleted" if len(ids) == 1 else f"{len(ids)} markers deleted")
        return len(ids)

    def delete_all_markers(self):
        if not self.timeline.markers:
            return 0
        tl = self.timeline
        count = len(tl.markers)
        self.stack.run("Delete all markers", tl.clear_markers)
        self.set_marker_selection(set())
        self.status.emit(f"All {count} markers deleted")
        return count

    def rename_marker(self, marker_id, name):
        tl = self.timeline
        marker = tl.marker_by_id(marker_id)
        if marker is None or marker.name == (name or ""):
            return False
        self.stack.run("Rename marker", lambda: tl.set_marker_name(marker_id, name))
        self.status.emit(f"Marker renamed to \"{name}\"" if name else "Marker name cleared")
        return True

    def set_marker_color(self, color, ids=None):
        ids = set(ids) if ids is not None else set(self.marker_selected)
        ids &= {m.id for m in self.timeline.markers}
        if not ids:
            return False
        tl = self.timeline
        self.stack.run("Marker colour", lambda: [tl.set_marker_color(mid, color) for mid in list(ids)])
        return True

    def move_markers(self, ids, delta):
        """Nudge markers by frames as one undoable edit (used by the arrow keys)."""
        ids = set(ids) & {m.id for m in self.timeline.markers}
        if not ids or not delta:
            return False
        tl = self.timeline

        def action():
            for mid in sorted(ids):
                marker = tl.marker_by_id(mid)
                if marker is not None:
                    tl.set_marker_frame(mid, marker.frame + delta)

        self.stack.run("Move marker" if len(ids) == 1 else f"Move {len(ids)} markers", action)
        return True

    def go_to_marker(self, marker_id):
        marker = self.timeline.marker_by_id(marker_id)
        if marker is None:
            return False
        self.set_marker_selection({marker.id})
        self.set_playhead(marker.frame)
        self.reveal_requested.emit(marker.frame)
        return True

    def jump_to_marker(self, direction):
        """[ / ]: park the playhead on the previous or next marker."""
        frame = self.timeline.next_marker(self.playhead, direction)
        if frame is None:
            self.status.emit("No marker in that direction" if self.timeline.markers else "No markers yet")
            return False
        marker = self.timeline.marker_at(frame)
        if marker is not None:
            self.set_marker_selection({marker.id})
        self.set_playhead(frame)
        self.reveal_requested.emit(frame)
        self.status.emit(f"Marker {timecode_text(frame, self.timeline.fps)}"
                         + (f" \u2014 {marker.name}" if marker is not None and marker.name else ""))
        return True

    def set_markers(self, records):
        """Load markers (layout/preset restore); not undoable, like the dock layout."""
        markers = self.timeline.set_marker_records(records)
        self.set_marker_selection(set())
        self.request_redraw()
        self.changed.emit()
        return markers

    def _marker_label(self, marker):
        return timecode_text(marker.frame, self.timeline.fps)

    # -------------------------------------------------------------- loop region
    def set_loop_start_at_playhead(self):
        """I: the loop region starts at the playhead (creating it if needed)."""
        tl = self.timeline
        region = tl.loop_range()
        end = region[1] if region is not None and region[1] > self.playhead else self.playhead + 1
        self.set_loop_region(self.playhead, end)
        self.status.emit(f"Loop start at frame {self.playhead}")
        return True

    def set_loop_end_at_playhead(self):
        """O: the loop region ends after the playhead frame (creating it if needed)."""
        tl = self.timeline
        region = tl.loop_range()
        start = region[0] if region is not None and region[0] < self.playhead else max(0, self.playhead - 1)
        self.set_loop_region(start, self.playhead + 1)
        self.status.emit(f"Loop end at frame {self.playhead}")
        return True

    def set_loop_region(self, start, end, enable=True):
        """Set the loop region in frames as one undoable edit (dragging the loop bar)."""
        tl = self.timeline
        lo, hi = int(round(min(start, end))), int(round(max(start, end)))
        hi = max(hi, lo + 1)
        if tl.loop_range() == (lo, hi):
            if enable:
                self.set_loop_enabled(True)
            self.loop_changed.emit()
            self.request_redraw()
            return False

        def action():
            tl.loop_in, tl.loop_out = lo, hi

        self.stack.run("Loop region", action)
        if enable:
            self.set_loop_enabled(True)
        self.loop_changed.emit()
        self.request_redraw()
        return True

    def clear_loop_region(self):
        tl = self.timeline
        if tl.loop_range() is None:
            return False

        def action():
            tl.loop_in = tl.loop_out = None

        self.stack.run("Clear loop region", action)
        self.loop_changed.emit()
        self.request_redraw()
        return True

    def set_loop_to_selection(self):
        clips = self._selected_clips()
        if not clips:
            self.status.emit("Select clips first to set the loop region")
            return False
        return self.set_loop_region(min(c.timeline_start for c in clips), max(c.end for c in clips))

    def set_loop_to_timeline(self):
        end = max(1, self.timeline.duration)
        return self.set_loop_region(0, end)

    def set_loop_enabled(self, enabled):
        """Loop playback on/off; a playback mode, so it stays out of the undo stack."""
        enabled = bool(enabled)
        if self.timeline.loop_enabled == enabled:
            return
        self.timeline.loop_enabled = enabled
        self.loop_changed.emit()
        self.request_redraw()

    def toggle_loop(self):
        self.set_loop_enabled(not self.timeline.loop_enabled)
        if self.timeline.loop_range() is None and self.timeline.loop_enabled:
            self.status.emit("Loop playback on - drag in the loop strip to set a region")

    def undo(self):
        self.stack.undo()

    def redo(self):
        self.stack.redo()
