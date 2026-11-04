import os, sys, tempfile, time, zipfile, tracemalloc
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meenifx.model.timeline import Timeline
from meenifx.model.keyframes import AnimatedValue, Keyframe
from meenifx.project import ProjectSettings, MediaLibrary
from meenifx.project import container as C
from meenifx.project.serialize import timeline_to_sections, sections_to_timeline
from meenifx.project.convert import convert_timeline_fps


def build(n_clips=3, kfs=2):
    tl = Timeline(30)
    v = tl.add_track("video"); a = tl.add_track("audio")
    for i in range(n_clips):
        track = v if i % 2 == 0 else a
        eff = [{"effect_id": "blur", "enabled": i % 3 != 0, "params": {
            "radius": AnimatedValue([Keyframe(j * 5, float(j), "bezier", 0.2, 0.4, 0.1, None) for j in range(kfs)], 1.5),
            "mode": "soft", "on": True}}]
        tl.new_clip(f"c{i}", f"C:\\media\\f{i % 50}.mp4", track, i * 40, 30, effects=eff, fade_in=5, fade_out=7,
                    speed=1.5, reverse=bool(i % 2), loop=True, fade_in_curve=0.5)
    tl.add_marker(10, "m", "#ff0000"); tl.loop_in, tl.loop_out, tl.loop_enabled = 5, 99, True
    return tl


def roundtrip(tl, path, lib=None):
    lib = lib or MediaLibrary()
    s = ProjectSettings.from_preset("HD 1080p 60", "T")
    d = C.ProjectData()
    d["settings"] = s.to_dict()
    for k, v in timeline_to_sections(tl, lambda p: lib.add(p).id).items():
        d[k] = v
    d["media"] = lib.to_dict()
    d["meta"] = C.make_meta("T", s, tl.duration, 0)
    d["ui"] = {"x": 1}; d.window_state = b"\x00" * 100; d.thumbnails[C.PROJECT_THUMB] = b"\xff\xd8jpg"
    C.write_container(path, d)
    d2 = C.read_container(path)
    lib2 = MediaLibrary.from_dict(d2["media"])
    return d2, sections_to_timeline(d2.sections, 30, lambda mid: lib2.items[mid].path)


def dump(tl):
    out = []
    for t in tl.tracks:
        for c in t.clips:
            effs = [(e["effect_id"], e["enabled"], {k: ([(x.time, x.value, x.interpolation, x.in_influence, x.out_influence) for x in v.keyframes], v.base_value) if isinstance(v, AnimatedValue) else v for k, v in e["params"].items()}) for e in c.effects]
            out.append((t.id, c.state()[:16], c.volume, c.fade_in, c.fade_out, c.fade_in_curve, effs))
    return out, [m.state() for m in tl.markers], tl.loop_in, tl.loop_out, tl.loop_enabled


d = tempfile.mkdtemp()
p = os.path.join(d, "a.mfxpf")
tl = build()
d2, tl2 = roundtrip(tl, p)
a, b = dump(tl), dump(tl2)
a[0].sort(key=lambda x: x[1][0]); b[0].sort(key=lambda x: x[1][0])
assert a == b, (a, b)
assert d2.window_state == b"\x00" * 100
meta, thumb = C.read_summary(p)
assert thumb == b"\xff\xd8jpg" and meta["fps"] == 60.0
with zipfile.ZipFile(p) as z:
    info = {i.filename: i.compress_type for i in z.infolist()}
assert info["thumbs/project.jpg"] == zipfile.ZIP_STORED and info["timeline/clips.json"] == zipfile.ZIP_DEFLATED
print("roundtrip ok; entries:", sorted(info))

bad = os.path.join(d, "bad.mfxpf"); open(bad, "wb").write(b"nope")
try: C.read_container(bad); raise SystemExit("no error")
except C.ProjectFormatError as e: print("bad ->", e)
newer = ('"version":%d' % C.FORMAT_VERSION).encode()
with zipfile.ZipFile(p) as zin, zipfile.ZipFile(os.path.join(d, "new.mfxpf"), "w") as zo:
    for n in zin.namelist():
        zo.writestr(n, zin.read(n).replace(newer, b'"version":99') if n == "manifest.json" else zin.read(n))
try: C.read_container(os.path.join(d, "new.mfxpf")); raise SystemExit("no error")
except C.ProjectFormatError as e: print("newer ->", e)

tl3 = build(4, 2); convert_timeline_fps(tl3, 60)
c0 = tl3.tracks[0].clips[0]
assert (c0.timeline_start, c0.duration, c0.fade_in, c0.fade_out) == (0, 60, 10, 14), c0.state()
assert tl3.loop_in == 10 and tl3.loop_out == 198 and tl3.markers[0].frame == 20 and tl3.fps == 60
assert [k.time for k in c0.effects[0]["params"]["radius"].keyframes] == [0, 10]
print("convert ok")

N = 5000
big = build(N, 6)
tracemalloc.start()
t0 = time.time(); d3, tl4 = roundtrip(big, os.path.join(d, "big.mfxpf")); dt = time.time() - t0
cur, peak = tracemalloc.get_traced_memory(); tracemalloc.stop()
assert sum(len(t.clips) for t in tl4.tracks) == N
print(f"large: {N} clips, {N*6} keyframes, save+load {dt:.2f}s, peak {peak/1e6:.0f} MB, file {os.path.getsize(os.path.join(d,'big.mfxpf'))/1e3:.0f} KB")
print("ALL OK")
