"""Net classes as KiCad's DRC enforces them: never below the board's own minimums.

Board Setup has minimums for clearance, track width, via diameter, hole size and
annular width (`board.design_settings.rules.min_*` in the project file). KiCad's DRC
applies them above every net class: a class with a 0.2 mm clearance on a board whose
minimum clearance is 0.3 mm is checked at 0.3 mm. A backend that reads the classes
alone routes that board at 0.2 mm, and every such track is a DRC error under the
board's own rules.

raise_classes() rewrites the classes of a WORKING copy of the project, the one the
backend reads, to the values DRC enforces. The input project and the candidate's
project are never touched: grading runs against the rules as the designer wrote
them. Raising only tightens what the backend may do.
"""
import json
from pathlib import Path

# class field -> the board minimum that bounds it from below
FLOORS = (("clearance", "min_clearance"), ("track_width", "min_track_width"),
          ("diff_pair_width", "min_track_width"), ("via_drill", "min_through_hole_diameter"),
          ("via_diameter", "min_via_diameter"), ("microvia_drill", "min_microvia_drill"),
          ("microvia_diameter", "min_microvia_diameter"))


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else None


def effective(classes, rules):
    """(the classes with every field raised to its board minimum, the changes made).

    A via must also keep the board's annular width around its own hole, so its diameter
    is raised to drill + 2 * min_via_annular_width after the drill has its minimum."""
    changes, out = [], []
    annular = _number(rules.get("min_via_annular_width")) or 0
    for cls in classes:
        cls = dict(cls)
        for field, floor_key in FLOORS:
            value, floor = _number(cls.get(field)), _number(rules.get(floor_key))
            if value is not None and floor is not None and value < floor - 1e-9:
                changes.append({"netclass": cls.get("name"), "field": field, "from": value, "to": floor,
                                "minimum": floor_key})
                cls[field] = floor
        d, h = _number(cls.get("via_diameter")), _number(cls.get("via_drill"))
        if d is not None and h is not None and annular and d < h + 2 * annular - 1e-9:
            need = round(h + 2 * annular, 6)
            changes.append({"netclass": cls.get("name"), "field": "via_diameter", "from": d, "to": need,
                            "minimum": "min_via_annular_width"})
            cls["via_diameter"] = need
        out.append(cls)
    return out, changes


def raise_classes(project_path):
    """Rewrite a working project's net classes to what DRC enforces -> the changes (may be empty)."""
    path = Path(project_path)
    if not path.is_file():
        return []
    data = json.loads(path.read_text())
    rules = ((data.get("board") or {}).get("design_settings") or {}).get("rules") or {}
    settings = data.get("net_settings") or {}
    classes = settings.get("classes")
    if not isinstance(classes, list) or not rules:
        return []
    raised, changes = effective(classes, rules)
    if changes:
        settings["classes"] = raised
        path.write_text(json.dumps(data, indent=2) + "\n")
    return changes


def describe(changes):
    """One line per class for the run log."""
    by_class = {}
    for c in changes:
        by_class.setdefault(c["netclass"], []).append(f"{c['field']} {c['from']:g} -> {c['to']:g} mm ({c['minimum']})")
    return [f"{name}: " + ", ".join(parts) for name, parts in by_class.items()]
