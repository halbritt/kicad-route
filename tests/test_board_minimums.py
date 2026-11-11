"""The backend routes at the sizes KiCad's DRC enforces: a net class is never below the board's minimums.

Found on a two-layer board whose Board Setup asks 0.3 mm clearance and track width and a
1.5 mm via while its Default class says 0.2 mm, 0.2 mm and 0.6 mm: the backend routed at the
class values and the candidate had 57 new DRC errors under the board's own rules.
"""
import json
import shutil

import pytest

from helpers import FIXTURE, run
from kicad_route import board_minimums
from kicad_route import cli as route

BOARD = "route_demo.kicad_pcb"
RULES = {"min_clearance": 0.3, "min_track_width": 0.3, "min_via_diameter": 0.9, "min_via_annular_width": 0.2,
         "min_through_hole_diameter": 0.35}
DEFAULT = {"name": "Default", "clearance": 0.2, "track_width": 0.2, "via_diameter": 0.6, "via_drill": 0.3,
           "diff_pair_width": 0.2, "microvia_diameter": 0.3, "microvia_drill": 0.1}
POWER = {"name": "Power", "clearance": 0.4, "track_width": 0.5, "via_diameter": 1.2, "via_drill": 0.4}


def test_each_field_is_raised_to_its_own_minimum_and_nothing_is_lowered():
    raised, changes = board_minimums.effective([DEFAULT, POWER], RULES)
    default, power = raised
    assert (default["clearance"], default["track_width"], default["diff_pair_width"]) == (0.3, 0.3, 0.3)
    assert (default["via_drill"], default["via_diameter"]) == (0.35, 0.9)
    # a class above the minimums is left as it is
    assert power == POWER
    assert {(c["netclass"], c["field"], c["minimum"]) for c in changes} == {
        ("Default", "clearance", "min_clearance"), ("Default", "track_width", "min_track_width"),
        ("Default", "diff_pair_width", "min_track_width"), ("Default", "via_drill", "min_through_hole_diameter"),
        ("Default", "via_diameter", "min_via_diameter")}
    assert DEFAULT["clearance"] == 0.2, "the input classes are not modified in place"


def test_a_via_keeps_the_boards_annular_width_around_its_raised_hole():
    rules = {"min_through_hole_diameter": 0.5, "min_via_annular_width": 0.25, "min_via_diameter": 0.4}
    (cls,), changes = board_minimums.effective([{"name": "Default", "via_diameter": 0.6, "via_drill": 0.3}], rules)
    assert (cls["via_drill"], cls["via_diameter"]) == (0.5, 1.0)       # 0.5 + 2 * 0.25
    assert [c["minimum"] for c in changes] == ["min_through_hole_diameter", "min_via_annular_width"]


def test_unset_fields_and_unset_minimums_are_left_alone():
    classes = [{"name": "Default", "clearance": 0.2}, {"name": "Bare"}]
    assert board_minimums.effective(classes, {}) == (classes, [])
    assert board_minimums.effective(classes, {"min_clearance": 0, "min_track_width": None}) == (classes, [])
    raised, changes = board_minimums.effective(classes, {"min_clearance": 0.25, "min_track_width": 0.3})
    assert raised == [{"name": "Default", "clearance": 0.25}, {"name": "Bare"}] and len(changes) == 1


def test_only_a_project_that_needs_it_is_rewritten(tmp_path):
    project = tmp_path / "p.kicad_pro"
    body = {"board": {"design_settings": {"rules": {"min_clearance": 0.15}}},
            "net_settings": {"classes": [dict(DEFAULT)]}, "other": {"kept": True}}
    project.write_text(json.dumps(body))
    before = project.read_bytes()
    assert board_minimums.raise_classes(project) == [] and project.read_bytes() == before
    body["board"]["design_settings"]["rules"]["min_clearance"] = 0.3
    project.write_text(json.dumps(body))
    changes = board_minimums.raise_classes(project)
    after = json.loads(project.read_text())
    assert [(c["field"], c["from"], c["to"]) for c in changes] == [("clearance", 0.2, 0.3)]
    assert after["net_settings"]["classes"][0]["clearance"] == 0.3 and after["other"] == {"kept": True}
    assert board_minimums.describe(changes) == ["Default: clearance 0.2 -> 0.3 mm (min_clearance)"]
    assert board_minimums.raise_classes(tmp_path / "absent.kicad_pro") == []


@pytest.fixture(scope="module")
def strict_dir(tmp_path_factory):
    """The fixture with Board Setup minimums above its Default class, and without its custom rule
    file: that rule (0.4 mm between Power and other nets) is one the builtin router cannot read."""
    d = tmp_path_factory.mktemp("strict") / "route_demo"
    shutil.copytree(FIXTURE, d, ignore=shutil.ignore_patterns("generate.py", "__pycache__", "*.kicad_dru"))
    project = d / "route_demo.kicad_pro"
    data = json.loads(project.read_text())
    # 0.4 mm: at its class clearance the router passes a ground pad at 0.37 mm on this board
    data["board"]["design_settings"]["rules"].update(min_clearance=0.4, min_track_width=0.3, min_via_diameter=0.8,
                                                     min_via_annular_width=0.2)
    project.write_text(json.dumps(data, indent=2) + "\n")
    return d


def test_the_builtin_router_obeys_minimums_above_the_default_class(strict_dir):
    r, run_dir, result = run("run", "--backend", "builtin", "--budget", "180", "--dir", str(strict_dir),
                             "--name", "route_demo_strict", "--label", "smoke-board-minimums")
    process, validation = result["process"], result["validation"]
    raised = {(c["netclass"], c["field"]): (c["from"], c["to"]) for c in process["classes_raised_to_board_minimums"]}
    assert raised == {("Default", "clearance"): (0.2, 0.4), ("Default", "track_width"): (0.2, 0.3),
                      ("Default", "diff_pair_width"): (0.2, 0.3), ("Default", "via_diameter"): (0.6, 0.8),
                      ("Power", "clearance"): (0.3, 0.4), ("Power", "diff_pair_width"): (0.2, 0.3)}, raised
    assert process["status"] == route.PASS
    # nothing the backend drew breaks a Board Setup minimum (routed at the class values, this
    # variant has two new clearance errors)
    new = validation["drc"]["new_violations"]
    assert new["count"] == 0, new
    # the candidate's project is the input's own, not the working copy the backend read
    cand = json.loads((run_dir / "candidate" / "route_demo" / "route_demo.kicad_pro").read_text())
    assert cand["net_settings"]["classes"][0]["clearance"] == 0.2
    assert validation["preservation"]["project_files"] == route.PASS
    assert result["routing"]["status"] == route.COMPLETE, result["routing"]
