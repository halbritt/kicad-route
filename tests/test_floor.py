"""Rule floor: the ordering table, the history high-water mark, the declared floor,
and their use in `audit` and the `run` pre-flight (docs/floor.md).

The integration tests build git repositories from copies of the fixture in tmp
directories; the fixture itself is never edited.
"""
import json
import os
import shutil
import subprocess

import pytest

from helpers import FIXTURE, OUT, cli, record, run, summary
from kicad_route import cli as route
from kicad_route import floor

BOARD = "route_demo.kicad_pcb"
FLOOR_FILE = "route_demo" + floor.FLOOR_SUFFIX


# ------------------------------------------------------------------ ordering

@pytest.mark.parametrize("setting, stricter, looser", [
    ("rules.min_track_width", 0.2, 0.15),
    ("rules.min_clearance", 0.2, 0.1),
    ("rules.min_resolved_spokes", 2, 1),
    ("rules.min_hole_clearance", 0.25, 0.2),
    ("rules.min_copper_edge_clearance", 0.5, 0.3),
    ("rules.min_text_height", 1.0, 0.8),
    ("rules.min_microvia_drill", 0.1, 0.075),
    ("rules.solder_mask_to_copper_clearance", 0.05, 0.0),
    ("rules.max_via_count", 2, 5),                       # any rules.max_* except max_error
    ("rules.allow_microvias", False, True),
    ("rule_severities.starved_thermal", "error", "warning"),
    ("rule_severities.clearance", "warning", "ignore"),
    ("net_settings.classes.Power.clearance", 0.3, 0.2),
    ("net_settings.classes.Power.track_width", 0.5, 0.3),  # router default: ordered, not ratcheted by default
    ("dru.power_to_signal_clearance.clearance.min", 0.4, 0.3),
    ("dru.length_match.length.max", 50.0, 60.0),
    ("dru.thermals.min_resolved_spokes.value", 3, 2),
    ("dru.power_to_signal_clearance.severity", "error", "warning"),
])
def test_rule_tightness_orders_each_family(setting, stricter, looser):
    assert floor.rule_tightness(setting, stricter) > floor.rule_tightness(setting, looser)
    assert floor.compare(setting, stricter, looser) == 1
    assert floor.compare(setting, looser, stricter) == -1
    assert floor.compare(setting, stricter, stricter) == 0
    assert floor.stricter_of(setting, looser, stricter) == stricter
    assert floor.stricter_of(setting, None, looser) == looser
    assert floor.at_least(setting, looser, stricter) is False


@pytest.mark.parametrize("setting", [
    "rules.max_error", "rules.use_height_for_length_calcs", "net_settings.classes.Power.bus_width",
    "net_settings.classes.Power.pcb_color", "net_settings.netclass_patterns", "route_demo.kicad_dru",
    "dru.r.track_width.opt", "teardrop_options",
])
def test_unordered_settings_are_never_ordered(setting):
    assert floor.order_of(setting) is None
    assert floor.rule_tightness(setting, 0.1) is None
    assert floor.compare(setting, 0.1, 0.2) is None
    assert floor.stricter_of(setting, 0.1, 0.2) is None


def test_rule_tightness_rejects_values_of_the_wrong_kind():
    assert floor.rule_tightness("rules.min_clearance", True) is None
    assert floor.rule_tightness("rules.min_clearance", "0.2") is None
    assert floor.rule_tightness("rule_severities.clearance", "fatal") is None
    assert floor.compare("rules.min_clearance", 0.2, None) is None
    assert floor.compare("rules.min_clearance", 0.2, 0.2 + 1e-12) == 0   # float noise is equal


def test_drc_exclusions_fewer_is_stricter():
    s = "drc_exclusions"
    assert floor.order_of(s) == "subset"
    assert floor.compare(s, ["a"], ["a", "b"]) == 1
    assert floor.compare(s, ["a", "b"], ["a"]) == -1
    assert floor.compare(s, ["a"], ["b"]) is None                    # neither contains the other
    assert floor.at_least(s, ["a"], ["b"]) is False                  # a waiver outside the floor's
    assert floor.at_least(s, [], ["b"]) is True
    assert floor.stricter_of(s, ["a", "b"], ["b", "c"]) == ["b"]     # the waivers both allow
    # KiCad 9+ stores [description, comment] pairs; the comment does not matter
    assert floor.compare(s, [["a", "why"]], ["a"]) == 0


def test_custom_rule_values_are_parsed_and_rewritten(tmp_path):
    text = ('(version 1)\n# comment (with parens\n'
            '(rule "pwr" (constraint clearance (min 16mil)) (condition "A.NetClass == \'Power\'"))\n'
            '(rule "w" (constraint track_width (min 0.2mm) (opt 0.25mm) (max 2mm)) (severity warning))\n'
            '(rule "spokes" (constraint min_resolved_spokes 2))\n')
    values, scopes = floor.dru_rules(text)
    got = {k: v for k, (v, _) in values.items()}
    assert got.pop("dru.w.severity") == "warning"
    assert got == pytest.approx({"dru.pwr.clearance.min": 0.4064, "dru.w.track_width.min": 0.2,
                                 "dru.w.track_width.max": 2.0, "dru.spokes.min_resolved_spokes.value": 2.0})
    assert scopes["pwr"] == "condition A.NetClass == 'Power'"
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    (src / "route_demo.kicad_dru").write_text(text)
    (src / "route_demo.kicad_pro").write_text(json.dumps({"board": {"design_settings": {"rules": {}}}}))
    current, _ = floor.dir_settings(src, BOARD)
    applied, unapplied = floor.write_floor_project(src, dst, BOARD, {
        "dru.pwr.clearance.min": 0.5, "dru.w.severity": "error", "dru.spokes.min_resolved_spokes.value": 3,
        "dru.gone.clearance.min": 0.3, "rules.min_resolved_spokes": 2}, current)
    assert {a["setting"] for a in applied} == {"dru.pwr.clearance.min", "dru.w.severity",
                                               "dru.spokes.min_resolved_spokes.value", "rules.min_resolved_spokes"}
    assert [u["setting"] for u in unapplied] == ["dru.gone.clearance.min"]
    out = (dst / "route_demo.kicad_dru").read_text()
    assert "(min 0.5mm)" in out and "(severity error)" in out and "min_resolved_spokes 3)" in out
    assert "(opt 0.25mm)" in out and "# comment (with parens" in out
    assert json.loads((dst / "route_demo.kicad_pro").read_text())["board"]["design_settings"]["rules"] == \
        {"min_resolved_spokes": 2}
    assert (src / "route_demo.kicad_dru").read_text() == text   # the source is untouched


def test_high_water_keeps_the_strictest_value_and_its_commit():
    revs = [{"commit": c, "date": "", "subject": c, "scopes": {}, "values": v} for c, v in [
        ("c1", {"rules.min_track_width": 0.15, "rules.min_resolved_spokes": 2, "drc_exclusions": ["x"],
                "net_settings.classes.Power.track_width": 0.5}),
        ("c2", {"rules.min_track_width": 0.2, "rules.min_resolved_spokes": 1, "drc_exclusions": ["x", "y"]}),
        ("c3", {"rules.min_track_width": 0.2, "rules.min_resolved_spokes": 1, "drc_exclusions": []}),
    ]]
    marks = floor.high_water(revs)
    assert (marks["rules.min_track_width"]["value"], marks["rules.min_track_width"]["commit"]) == (0.2, "c2")
    assert (marks["rules.min_resolved_spokes"]["value"], marks["rules.min_resolved_spokes"]["commit"]) == (2, "c1")
    assert (marks["drc_exclusions"]["value"], marks["drc_exclusions"]["commit"]) == ([], "c3")
    assert "net_settings.classes.Power.track_width" not in marks          # router default: opt-in
    assert floor.high_water(revs, netclass_sizes=True)["net_settings.classes.Power.track_width"]["value"] == 0.5
    rows = floor.below({"rules.min_track_width": 0.2, "rules.min_resolved_spokes": 1}, marks)
    assert [(r["setting"], r["status"], r["commit"]) for r in rows] == [
        ("drc_exclusions", "absent", "c3"), ("rules.min_resolved_spokes", "looser", "c1")]


def test_declared_floor_parsing_combining_and_lowering():
    old, bad = floor.parse_declared({"settings": {
        "rules.min_resolved_spokes": {"value": 2, "reason": "thermals", "owner": "hw"},
        "rules.min_clearance": {"value": 0.15, "owner": "hw"},
        "rules.max_error": {"value": 0.005},                        # not a constraint
        "rule_severities.clearance": {"value": "fatal"}}}, "old")    # not a severity
    assert set(old) == {"rules.min_resolved_spokes", "rules.min_clearance"}
    assert [b["setting"] for b in bad] == ["rules.max_error", "rule_severities.clearance"]
    new, _ = floor.parse_declared({"rules.min_resolved_spokes": {"value": 1, "owner": "someone"},
                                   "rules.min_track_width": 0.2}, "new")    # bare mapping form
    changes = {c["setting"]: c["kind"] for c in floor.floor_changes(old, new)}
    assert changes == {"rules.min_resolved_spokes": "lowered", "rules.min_clearance": "removed",
                       "rules.min_track_width": "added"}
    both = floor.combine(old, new)                 # fail closed: the stricter of the two holds
    assert both["rules.min_resolved_spokes"]["value"] == 2 and both["rules.min_track_width"]["value"] == 0.2


def test_declaring_a_value_below_the_high_water_mark_is_a_lowering():
    """A person may accept a value below the history mark by declaring it; the
    commit that does so lowers the effective floor and is reported as such."""
    marks = {"rules.min_resolved_spokes": {"value": 2, "commit": "c" * 40, "subject": "s"},
             "rules.min_clearance": {"value": 0.15, "commit": "c" * 40, "subject": "s"}}
    new, _ = floor.parse_declared({"rules.min_resolved_spokes": {"value": 1, "owner": "hw"},
                                   "rules.min_clearance": {"value": 0.2, "owner": "hw"}}, "new")
    changes = {c["setting"]: (c["kind"], c["old"], c["new"]) for c in floor.floor_changes({}, new, marks)}
    assert changes == {"rules.min_resolved_spokes": ("added_below_high_water", 2, 1),
                       "rules.min_clearance": ("added", 0.15, 0.2)}
    assert {k for k, (kind, *_) in changes.items() if kind in floor.LOWERING} == {"rules.min_resolved_spokes"}
    # once declared, the declared value is the floor for that setting; the mark fills the rest
    eff = floor.effective_floor(new, {**marks, "rules.min_track_width": {"value": 0.15}})
    assert eff == {"rules.min_resolved_spokes": 1, "rules.min_clearance": 0.2, "rules.min_track_width": 0.15}


def test_draft_declared_never_goes_below_the_declared_floor():
    declared, _ = floor.parse_declared({"rules.min_clearance": {"value": 0.3, "reason": "fab", "owner": "hw"}}, "f")
    marks = {"rules.min_clearance": {"value": 0.2, "commit": "a" * 40, "subject": "s"},
             "rules.min_track_width": {"value": 0.15, "commit": "b" * 40, "subject": "init"}}
    draft = floor.draft_declared(declared, marks, BOARD)["settings"]
    assert draft["rules.min_clearance"] == {"value": 0.3, "reason": "fab", "owner": "hw"}
    assert draft["rules.min_track_width"]["value"] == 0.15 and "bbbbbbbbbbbb" in draft["rules.min_track_width"]["reason"]


# ------------------------------------------------------------- git fixtures

def make_repo(tmp_path):
    repo = tmp_path / "repo"
    d = repo / "route_demo"
    shutil.copytree(FIXTURE, d, ignore=shutil.ignore_patterns("generate.py", "__pycache__"))

    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                              env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                                       GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")).stdout.strip()
    git("init", "-q")
    git("add", ".")
    git("commit", "-q", "-m", "initial rules")
    return d, git


def set_rule(d, key, value):
    pro = d / "route_demo.kicad_pro"
    data = json.loads(pro.read_text())
    data["board"]["design_settings"]["rules"][key] = value
    pro.write_text(json.dumps(data, indent=2))


def write_declared(d, settings):
    (d / FLOOR_FILE).write_text(json.dumps({"version": 1, "settings": settings}, indent=2))


def test_declared_floor_for_a_working_tree_input_holds_the_committed_value(tmp_path):
    """Lowering the declared floor in the working tree does not lower it for a run:
    the committed floor still applies until the lowering is committed (and audited)."""
    d, git = make_repo(tmp_path)
    write_declared(d, {"rules.min_clearance": {"value": 0.2, "reason": "fab", "owner": "hw"}})
    git("add", ".")
    git("commit", "-q", "-m", "declare floor")
    write_declared(d, {"rules.min_clearance": {"value": 0.1, "reason": "", "owner": "nobody"}})
    cfg = {"name": "route_demo", "path": d, "pcb": BOARD}
    rec = floor.preflight(cfg, d, BOARD)
    assert rec["declared"]["rules.min_clearance"]["value"] == 0.2
    assert [(r["setting"], r["current"], r["floor"]) for r in rec["floor_violated"]] == \
        [("rules.min_clearance", 0.15, 0.2)]
    assert floor.preflight({"name": "x", "path": tmp_path, "pcb": BOARD}, d, BOARD) is None   # no floor: no check


# ------------------------------------------------------- audit integration

def test_floor_catches_a_rule_loosened_one_commit_before_it_is_used(tmp_path):
    """Commit 2 lowers min_track_width; commit 3 narrows a track below the old rule
    and moves a part. Auditing commit 3 against commit 2 sees no rule change; the
    floor names the commit that set the mark and grades the board under it."""
    import pcbnew
    d, git = make_repo(tmp_path)
    first = git("rev-parse", "HEAD")
    set_rule(d, "min_track_width", 0.1)
    git("commit", "-q", "-am", "loosen min_track_width")
    board = pcbnew.LoadBoard(str(d / BOARD))
    sig_b = next(t for t in board.GetTracks() if not isinstance(t, pcbnew.PCB_VIA) and t.GetNetname() == "/SIG_B")
    sig_b.SetWidth(pcbnew.FromMM(0.12))
    board.FindFootprintByReference("R1").Move(pcbnew.VECTOR2I(pcbnew.FromMM(0.5), 0))
    board.Save(str(d / BOARD))
    git("commit", "-q", "-am", "route")

    f = cli("floor", "--dir", str(d), "--write-declared", timeout=300)
    rec = json.loads((OUT / "floor" / "route_demo" / "floor.json").read_text())
    assert f.returncode == 0, f.stdout                                   # no declared floor: notes only
    assert rec["high_water"]["rules.min_track_width"]["commit"] == first
    assert [(r["setting"], r["current"], r["floor"], r["commit"]) for r in rec["below_high_water"]] == \
        [("rules.min_track_width", 0.1, 0.15, first)]
    assert "rules.min_track_width" in f.stdout
    draft = json.loads((OUT / "floor" / "route_demo" / FLOOR_FILE).read_text())
    assert draft["settings"]["rules.min_track_width"]["value"] == 0.15
    assert not (d / FLOOR_FILE).exists()                                 # the draft is never put in the project

    r = cli("audit", "--dir", str(d), "--old", "HEAD~1", "--new", "HEAD", timeout=900)
    res = json.loads((OUT / "audit" / "route_demo" / "HEAD~1..HEAD" / "audit.json").read_text())
    fl = res["floor"]
    record("floor_audit_two_step", {"exit_code": r.returncode, "findings": res["findings"],
                                    "rule_changes": [c for c in res["rule_changes"] if c["kind"] != "added"],
                                    **{k: fl[k] for k in ("below_high_water", "below_declared", "notes",
                                                          "floor_project", "hidden_by_floor")}})
    # pairwise: no rule value changed (KiCad's save only wrote its defaults out, kind "added")
    assert [c for c in res["rule_changes"] if c["kind"] != "added"] == []
    assert res["hidden_by_rule_changes"]["count"] == 0
    assert [(n["setting"], n["commit"]) for n in fl["below_high_water"]] == [("rules.min_track_width", first)]
    assert any(first[:12] in n for n in fl["notes"])
    assert fl["floor_project"]["applied"] == [{"setting": "rules.min_track_width", "project": 0.1, "floor": 0.15}]
    assert fl["hidden_by_floor"]["by_type"] == {"track_width": 1}
    assert any("only under the rule floor" in x for x in res["findings"])
    assert r.returncode == 3


def test_audit_reports_a_lowered_declared_floor(tmp_path):
    d, git = make_repo(tmp_path)
    write_declared(d, {"rules.min_track_width": {"value": 0.15, "reason": "fab minimum", "owner": "hw-lead"}})
    git("add", ".")
    git("commit", "-q", "-m", "declare floor")
    write_declared(d, {"rules.min_track_width": {"value": 0.1, "reason": "make it route", "owner": "agent"}})
    set_rule(d, "min_track_width", 0.1)
    git("commit", "-q", "-am", "lower floor and rule")
    r = cli("audit", "--dir", str(d), "--old", "HEAD~1", "--new", "HEAD", timeout=900)
    res = json.loads((OUT / "audit" / "route_demo" / "HEAD~1..HEAD" / "audit.json").read_text())
    fl = res["floor"]
    record("floor_audit_lowered", {"exit_code": r.returncode, "findings": res["findings"],
                                   "floor_lowered": fl["floor_lowered"], "below_declared": fl["below_declared"]})
    assert [(c["setting"], c["kind"], c["old"], c["new"], c["new_owner"]) for c in fl["floor_lowered"]] == \
        [("rules.min_track_width", "lowered", 0.15, 0.1, "agent")]
    assert [(b["setting"], b["floor"], b["owner"]) for b in fl["below_declared"]] == \
        [("rules.min_track_width", 0.15, "hw-lead")]                     # the parent's floor still holds
    assert any("declared rule floor lowered" in x for x in res["findings"])
    assert any("below the declared floor" in x for x in res["findings"])
    assert r.returncode == 3


# ------------------------------------------------------- run pre-flight

def test_run_preflight_fails_validation_below_the_declared_floor(tmp_path):
    """The input routes as usual and is graded under its own rules, but a rule below
    its declared floor fails validation and is recorded in result.json."""
    d = tmp_path / "route_demo"
    shutil.copytree(FIXTURE, d, ignore=shutil.ignore_patterns("generate.py", "__pycache__"))
    write_declared(d, {"rules.min_clearance": {"value": 0.2, "reason": "fab", "owner": "hw"}})
    r, run_dir, result = run("run", "--backend", "krt", "--budget", "180", "--dir", str(d), "--nets", "/SIG_A*",
                             "--default-clearance", "0.4", "--label", "floor-preflight")
    record("floor_run_preflight", summary(result) | {"floor": result["floor"]})
    assert result["process"]["status"] == route.PASS
    assert result["validation"]["status"] == route.FAIL
    assert [(v["setting"], v["current"], v["floor"]) for v in result["validation"]["floor_violated"]] == \
        [("rules.min_clearance", 0.15, 0.2)]
    assert result["floor"]["floor_violated"] == result["validation"]["floor_violated"]
    assert any("declared rule floor" in x for x in result["validation"]["reasons"])
    # graded under the input's own rules, not the floor's
    cand = OUT / result["candidate"]["board"]
    assert (cand.parent / "route_demo.kicad_pro").read_bytes() == (FIXTURE / "route_demo.kicad_pro").read_bytes()
    assert r.returncode == 3 and result["exit_code"] == 3
