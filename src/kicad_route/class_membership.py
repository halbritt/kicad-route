"""Each net's class as KiCad resolves it, stated to the backend by exact name.

A project assigns nets to classes with `net_settings.netclass_patterns`. KiCad matches a
pattern as an anchored regular expression and as a wildcard, whichever fits. The builtin
engine reads the patterns as globs. A board that says `in\\d+`, `.*out\\d+` and `uio\\d+`
therefore had those nets routed as Default: 24 nets, 1895 of 4559 new tracks under their
class width and their vias at the Default size, on a four-layer board of the lab corpus.
The engine reported 10 nets with a class of their own where KiCad has 45.

The native facts hold KiCad's own answer for every net (pcbnew's effective net class).
pin() writes that answer into a WORKING copy of the project, the one the backend reads,
as exact `netclass_assignments`, and removes the patterns, so no second reading of them
can disagree. The input project and the candidate's project are never touched.
"""
import json
from pathlib import Path

DEFAULT = "Default"


def assignments(nets, class_names):
    """{net name: class or [classes]} for every net KiCad puts in a class of the project
    other than Default, and the nets whose class the project does not define.

    KiCad 9 and later give a net several classes and name the effective one by joining
    them with commas; such a net is assigned the list when every part is a project class."""
    known = set(class_names)
    found, unknown = {}, []
    for name in sorted(nets):
        cls = (nets[name] or {}).get("netclass")
        if not name or not cls or cls == DEFAULT:
            continue
        if cls in known:
            found[name] = cls
            continue
        parts = [p.strip() for p in cls.split(",")]
        if len(parts) > 1 and all(p in known for p in parts):
            found[name] = [p for p in parts if p != DEFAULT] or DEFAULT
        else:
            unknown.append(name)
    return {k: v for k, v in found.items() if v != DEFAULT}, unknown


def pin(project_path, nets):
    """Rewrite a working project's class membership to KiCad's own -> a record of what was
    written, or None when the project has no patterns to misread or cannot be read.

    When KiCad names a class the project does not define for some net, the project is left
    as it is: the backend's own reading is then the best statement available."""
    path = Path(project_path)
    if not path.is_file() or not isinstance(nets, dict):
        return None
    data = json.loads(path.read_text())
    settings = data.get("net_settings")
    if not isinstance(settings, dict) or not settings.get("netclass_patterns"):
        return None
    names = [c.get("name") for c in settings.get("classes") or [] if isinstance(c, dict) and c.get("name")]
    found, unknown = assignments(nets, names)
    if unknown:
        return {"written": False, "nets_in_a_class_the_project_does_not_define": unknown[:20]}
    by_class = {}
    for cls in found.values():
        for part in (cls if isinstance(cls, list) else [cls]):
            by_class[part] = by_class.get(part, 0) + 1
    settings["netclass_assignments"] = found
    settings["netclass_patterns"] = []
    path.write_text(json.dumps(data, indent=2) + "\n")
    return {"written": True, "nets": len(found), "by_class": dict(sorted(by_class.items()))}
