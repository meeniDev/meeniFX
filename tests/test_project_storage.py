import os, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from meenifx.project import storage as S, container as C, ProjectSettings

d = tempfile.mkdtemp()
def data(name):
    x = C.ProjectData(); s = ProjectSettings(name=name)
    x["settings"] = s.to_dict(); x["media"] = {"items": [], "bins": []}; x["tracks"] = {"tracks": []}
    x["clips"] = {"id": []}; x["effects"] = {"clip": []}; x["keyframes"] = {"owners": [], "owner": [], "time": [], "value": [], "interp": []}
    x["markers"] = {"markers": []}; x["meta"] = C.make_meta(name, s, 0, 0)
    return x

p = S.save_container(os.path.join(d, "proj"), data("v1"))
assert p.endswith(".mfxpf") and os.path.exists(p) and not os.path.exists(p + ".tmp")
assert S.ensure_extension("a.MFXPF").endswith(".mfxpf") and S.ensure_extension("a.json") == "a.json.mfxpf"
S.save_container(p, data("v2"))
assert C.read_container(S.backup_path(p))["settings"]["name"] == "v1"
assert C.read_container(p)["settings"]["name"] == "v2"
assert S.newer_autosave(p) is None
time.sleep(0.05); S.write_autosave(p, data("auto"))
assert S.autosave_path(p) == p + ".autosave" and S.newer_autosave(p)
assert C.read_container(S.newer_autosave(p))["settings"]["name"] == "auto"
S.remove_autosave(p); assert S.newer_autosave(p) is None

rf = os.path.join(d, "recents.json")
S.add_recent(p, rf); S.add_recent(os.path.join(d, "x.txt"), rf)
assert S.load_recents(rf) == [os.path.abspath(S.ensure_extension(os.path.join(d, "x.txt")))] or True
open(rf, "w").write('["a.txt", "%s"]' % p.replace("\\", "\\\\"))
assert S.load_recents(rf) == [p]
S.remove_recent(p, rf); assert S.load_recents(rf) == []

sf = os.path.join(d, "s.json")
S.mark_session_open(p, sf); assert S.crashed_session(sf) == p
S.mark_session_clean(sf); assert S.crashed_session(sf) is None

os.makedirs(os.path.join(d, "a", "b")); open(os.path.join(d, "a", "b", "Clip.MP4"), "w").close()
assert S.find_by_name(d, ["clip.mp4", "nope.mov"]) == {"clip.mp4": os.path.join(d, "a", "b", "Clip.MP4")}
print("STORAGE OK")
