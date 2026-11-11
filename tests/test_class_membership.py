"""The backend routes every net in the class KiCad puts it in.

KiCad matches a `netclass_patterns` entry as an anchored regular expression and as a wildcard.
The builtin engine reads it as a glob. Found on a four-layer board whose project says `in\\d+`,
`.*out\\d+` and `uio\\d+`: 24 nets were routed as Default, 1895 of 4559 new tracks under their
class width, and the run's own report named 10 nets with a class where KiCad has 45.
"""
import json
import re
import shutil

import pytest

from helpers import FIXTURE, run
from kicad_route import class_membership
from kicad_route import cli as route

NETS = {"in3": {"netclass": "ioin"}, "out6": {"netclass": "ioout"}, "GND": {"netclass": "pwr"},
        "/QSPI_CLK": {"netclass": "Default"}, "": {"netclass": "Default"}}


def test_every_net_gets_kicads_class_and_default_nets_get_none():
    found, unknown = class_membership.assignments(NETS, ["Default", "ioin", "ioout", "pwr"])
    assert found == {"GND": "pwr", "in3": "ioin", "out6": "ioout"} and unknown == []


def test_a_net_in_several_classes_is_assigned_all_of_them():
    nets = {"clk": {"netclass": "fast,pwr"}, "x": {"netclass": "fast,Default"}, "odd": {"netclass": "fast,gone"}}
    found, unknown = class_membership.assignments(nets, ["Default", "fast", "pwr"])
    assert found == {"clk": ["fast", "pwr"], "x": ["fast"]} and unknown == ["odd"]


def test_only_a_project_with_patterns_is_rewritten_and_nothing_else_changes(tmp_path):
    project = tmp_path / "p.kicad_pro"
    body = {"net_settings": {"classes": [{"name": "Default"}, {"name": "ioin"}, {"name": "ioout"}, {"name": "pwr"}],
                             "netclass_patterns": [{"netclass": "ioin", "pattern": "in\\d+"}],
                             "netclass_assignments": None, "meta": {"version": 4}},
            "board": {"design_settings": {"rules": {"min_clearance": 0.1}}}}
    project.write_text(json.dumps(body))
    record = class_membership.pin(project, NETS)
    after = json.loads(project.read_text())
    assert record == {"written": True, "nets": 3, "by_class": {"ioin": 1, "ioout": 1, "pwr": 1}}
    assert after["net_settings"]["netclass_assignments"] == {"GND": "pwr", "in3": "ioin", "out6": "ioout"}
    assert after["net_settings"]["netclass_patterns"] == []
    assert after["board"] == body["board"] and after["net_settings"]["classes"] == body["net_settings"]["classes"]
    # no patterns: nothing a backend could misread, the file is left alone
    body["net_settings"]["netclass_patterns"] = []
    project.write_text(json.dumps(body))
    before = project.read_bytes()
    assert class_membership.pin(project, NETS) is None and project.read_bytes() == before
    assert class_membership.pin(tmp_path / "absent.kicad_pro", NETS) is None


def test_a_class_the_project_does_not_define_leaves_the_project_as_it_is(tmp_path):
    project = tmp_path / "p.kicad_pro"
    project.write_text(json.dumps({"net_settings": {"classes": [{"name": "Default"}],
                                                    "netclass_patterns": [{"netclass": "ioin", "pattern": "in*"}]}}))
    before = project.read_bytes()
    record = class_membership.pin(project, NETS)
    assert record["written"] is False and "in3" in record["nets_in_a_class_the_project_does_not_define"]
    assert project.read_bytes() == before


@pytest.fixture(scope="module")
def regex_dir(tmp_path_factory):
    """The fixture with its four signal nets in a class of 0.3 mm tracks, assigned by a pattern
    that is a regular expression and no glob."""
    d = tmp_path_factory.mktemp("regex") / "route_demo"
    shutil.copytree(FIXTURE, d, ignore=shutil.ignore_patterns("generate.py", "__pycache__"))
    project = d / "route_demo.kicad_pro"
    data = json.loads(project.read_text())
    settings = data["net_settings"]
    default = next(c for c in settings["classes"] if c["name"] == "Default")
    settings["classes"].append(dict(default, name="Wide", track_width=0.3))
    settings["netclass_patterns"].append({"netclass": "Wide", "pattern": "/SIG_\\w+"})
    project.write_text(json.dumps(data, indent=2) + "\n")
    return d


def test_the_builtin_router_routes_a_net_assigned_by_a_regular_expression_at_its_class(regex_dir):
    r, run_dir, result = run("run", "--backend", "builtin", "--budget", "180", "--dir", str(regex_dir),
                             "--name", "route_demo_regex", "--label", "smoke-class-membership")
    membership = result["process"]["class_membership_from_kicad"]
    assert membership == {"written": True, "nets": 6, "by_class": {"Power": 2, "Wide": 4}}, membership
    assert result["routing"]["status"] == route.COMPLETE, result["routing"]
    # routed as Default, the four nets come back at 0.2 mm and this check fails
    assert result["validation"]["preservation"]["new_copper_netclass_geometry"] == route.PASS, \
        result["validation"]["reasons"]
    assert result["validation"]["status"] == route.PASS, result["validation"]["reasons"]
    board = (run_dir / "candidate" / "route_demo" / "route_demo.kicad_pcb").read_text()
    widths = set()
    for block in re.findall(r"\n\t\(segment\b(.*?)\n\t\)", board, re.S):
        if re.search(r'\(net "?/SIG_', block):
            widths.add(float(re.search(r"\(width ([\d.]+)\)", block).group(1)))
    assert widths - {0.2} == {0.3}, widths     # 0.2 mm is the fixture's own stub, which stays
    # the candidate's project is the input's own: its patterns are as the designer wrote them
    cand = json.loads((run_dir / "candidate" / "route_demo" / "route_demo.kicad_pro").read_text())
    assert {"netclass": "Wide", "pattern": "/SIG_\\w+"} in cand["net_settings"]["netclass_patterns"]
    assert result["validation"]["preservation"]["project_files"] == route.PASS
