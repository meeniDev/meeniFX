"""Reusable compositions (pre-comps) and the library that owns them.

A composition is an ordinary :class:`~meenifx.model.timeline.Timeline` plus its own
name, resolution, frame rate and background. A clip references one by stable id; the
rendered media is never copied into the project, so editing a composition updates every
instance of it.
"""

from __future__ import annotations

import copy
import itertools
import uuid

from .timeline import Clip, Timeline, Track, clone_effects


class Composition:
    """One reusable composition definition."""

    def __init__(self, comp_id=None, name="Composition", width=1920, height=1080,
                 fps=30, background=(0, 0, 0), declared_duration=0, bin_folder="",
                 transparent=False):
        self.id = str(comp_id or f"comp-{uuid.uuid4().hex}")
        self.name = str(name or "Composition")
        self.width = max(1, int(width or 1920))
        self.height = max(1, int(height or 1080))
        self.fps = max(1, int(round(float(fps or 30))))
        self.background = tuple(int(value) for value in (background or (0, 0, 0)))[:3]
        if len(self.background) < 3:
            self.background = (0, 0, 0)
        # A pre-composition covers the range it was created from even when its contents
        # end before that range (a trailing gap is still part of the composition).
        self.declared_duration = max(0, int(declared_duration or 0))
        # Compositions live in the same virtual project folders as media, so the Project
        # panel can organise both without a second folder model.
        self.bin_folder = str(bin_folder or "")
        # Legacy files have no flag and keep their historical opaque background. New
        # pre-compositions default to transparent so alpha from Smart Key/Roto Brush,
        # shapes and text can reach the parent timeline.
        self.transparent = bool(transparent)
        self.timeline = Timeline(self.fps)

    @property
    def duration(self):
        return max(self.declared_duration, self.timeline.duration)

    def snapshot(self):
        return (self.id, self.name, self.width, self.height, self.fps, self.background,
                self.declared_duration, self.timeline.snapshot(), self.bin_folder,
                self.transparent)

    def restore(self, snap):
        (self.id, self.name, self.width, self.height, self.fps, self.background,
         self.declared_duration, timeline_snap) = snap[:8]
        self.bin_folder = str(snap[8]) if len(snap) > 8 else ""
        self.transparent = bool(snap[9]) if len(snap) > 9 else False
        self.timeline.restore(timeline_snap)

    def __repr__(self):
        return f"Composition({self.id!r}, {self.name!r}, {self.width}x{self.height})"


class CompositionLibrary:
    """Stable-id registry of compositions used by one project."""

    def __init__(self):
        self.items = {}
        self._by_id = self.items

    def __len__(self):
        return len(self.items)

    def __iter__(self):
        return iter(self.items.values())

    def __contains__(self, comp_id):
        return str(comp_id) in self.items

    def get(self, comp_id):
        return self.items.get(str(comp_id or ""))

    def all(self):
        return list(self.items.values())

    def add(self, comp):
        if not isinstance(comp, Composition):
            raise TypeError("CompositionLibrary.add expects a Composition")
        self.items[comp.id] = comp
        return comp

    def new(self, name="Composition", width=1920, height=1080, fps=30,
            background=(0, 0, 0), declared_duration=0, bin_folder="", transparent=False):
        return self.add(Composition(name=name, width=width, height=height, fps=fps,
                                    background=background, declared_duration=declared_duration,
                                    bin_folder=bin_folder, transparent=transparent))

    def remove(self, comp_id):
        return self.items.pop(str(comp_id or ""), None)

    def rename(self, comp_id, name):
        comp = self.get(comp_id)
        if comp is None:
            return None
        comp.name = str(name or comp.name)
        return comp

    def duplicate(self, comp_id):
        source = self.get(comp_id)
        if source is None:
            return None
        # Snapshot/restore copies the timeline without tripping over its itertools
        # counters (deepcopy cannot pickle those) and keeps ids unique to the clone.
        clone = Composition(None, f"{source.name} copy", source.width, source.height,
                            source.fps, source.background, source.declared_duration,
                            source.bin_folder, source.transparent)
        clone.timeline.restore(source.timeline.snapshot())
        return self.add(clone)

    def reference_count(self, comp_id, root_timeline=None):
        """How many clips point at `comp_id`, in the root and in every composition."""
        comp_id = str(comp_id or "")
        timelines = [root_timeline] if root_timeline is not None else []
        timelines += [comp.timeline for comp in self.items.values()]
        count = 0
        for timeline in timelines:
            for clip in timeline.all_clips():
                if getattr(clip, "is_composition", False) \
                        and str(getattr(clip, "composition_id", "")) == comp_id:
                    count += 1
        return count

    def referrers(self, comp_id, root_timeline=None):
        """Human names of the places that use `comp_id` (for the delete warning)."""
        comp_id = str(comp_id or "")
        names = []
        root = root_timeline
        if root is not None and any(
                getattr(clip, "is_composition", False)
                and str(getattr(clip, "composition_id", "")) == comp_id
                for clip in root.all_clips()):
            names.append("the main timeline")
        for comp in self.items.values():
            if comp.id == comp_id:
                continue
            if any(getattr(clip, "is_composition", False)
                   and str(getattr(clip, "composition_id", "")) == comp_id
                   for clip in comp.timeline.all_clips()):
                names.append(comp.name)
        return names

    # --------------------------------------------------------------- nesting
    def references(self, comp_id):
        """Composition ids directly referenced by clips inside `comp_id`."""
        comp = self.get(comp_id)
        if comp is None:
            return set()
        return {str(clip.composition_id) for clip in comp.timeline.all_clips()
                if getattr(clip, "is_composition", False)}

    def would_cycle(self, parent_id, child_id):
        """True when nesting `child_id` inside `parent_id` would form a cycle."""
        parent_id, child_id = str(parent_id), str(child_id)
        if parent_id == child_id:
            return True
        seen, pending = set(), [child_id]
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            if current == parent_id:
                return True
            pending.extend(self.references(current))
        return False

    def usable_for(self, parent_id, child_id):
        return self.get(child_id) is not None and not self.would_cycle(parent_id, child_id)

    # ------------------------------------------------------------- pre-compose
    def precompose(self, parent_timeline, clip_ids, name, range_start, range_end,
                   width, height, background=(0, 0, 0), transparent=True):
        """Build a composition from selected clips, preserving tracks and timing.

        The caller removes the selected clips from the parent and inserts the nested
        clip. This method only creates the definition, so it is safe to use inside one
        undoable action.
        """
        clip_ids = {int(cid) for cid in clip_ids}
        selected = {clip.id: clip for clip in parent_timeline.all_clips()
                    if clip.id in clip_ids}
        if not selected:
            raise ValueError("No clips selected")
        range_start = int(range_start)
        range_end = max(range_start + 1, int(range_end))
        comp = self.new(name, width, height, parent_timeline.fps, background,
                        range_end - range_start, transparent=transparent)
        links = {}
        for parent_track in parent_timeline.tracks:
            chosen = [clip for clip in parent_track.clips if clip.id in selected]
            if not chosen:
                continue
            track = Track(len(comp.timeline.tracks) + 1, parent_track.type,
                          parent_track.name, parent_track.height)
            track.muted, track.solo = parent_track.muted, parent_track.solo
            track.locked, track.visible = parent_track.locked, parent_track.visible
            track.volume = parent_track.volume
            comp.timeline.tracks.append(track)
            for source in chosen:
                copied = copy.copy(source)
                copied.id = self._next_clip_id(comp.timeline)
                copied.track = track
                copied.timeline_start = source.timeline_start - range_start
                copied.effects = clone_effects(source.effects)
                copied.transform = copy.deepcopy(source.transform)
                copied.transform_meta = copy.deepcopy(source.transform_meta)
                copied.text = copy.deepcopy(source.text)
                copied.shape = copy.deepcopy(source.shape)
                copied.presets = copy.deepcopy(source.presets)
                copied.gain = copy.deepcopy(source.gain)
                # A new comp must not share a link id with clips still in the parent.
                if source.link_id:
                    copied.link_id = links.setdefault(source.link_id,
                                                      comp.timeline.new_link_id())
                else:
                    copied.link_id = 0
                track.clips.append(copied)
        # Keep the counters above every copied id so later edits cannot collide.
        max_clip = max((clip.id for clip in comp.timeline.all_clips()), default=0)
        comp.timeline._clip_ids = itertools.count(max_clip + 1)
        comp.timeline._track_ids = itertools.count(
            max((track.id for track in comp.timeline.tracks), default=0) + 1)
        comp.timeline._counters = {
            "video": sum(1 for track in comp.timeline.tracks if track.type == "video"),
            "audio": sum(1 for track in comp.timeline.tracks if track.type == "audio"),
        }
        return comp

    @staticmethod
    def _next_clip_id(timeline):
        used = {clip.id for clip in timeline.all_clips()}
        candidate = 1
        while candidate in used:
            candidate += 1
        return candidate

    # ------------------------------------------------------------ undo snapshot
    def snapshot(self):
        return tuple(comp.snapshot() for comp in self.items.values())

    def restore(self, snap):
        """Restore definitions in place, keeping existing Composition objects alive."""
        old = self.items
        self.items = {}
        for state in snap or ():
            comp = old.get(state[0])
            if comp is None:
                comp = Composition(state[0])
            comp.restore(state)
            self.items[comp.id] = comp
        self._by_id = self.items
