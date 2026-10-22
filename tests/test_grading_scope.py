"""Grading-scope regressions: what validate() counts as in scope, which rules it
grades with, and what it records and leaves behind.

Most tests drive preservation() and validate() directly on a synthetic run
directory, with KiCad's DRC and the board-facts extraction stubbed out, so they run
in milliseconds. The rest call kicad-cli once (a DRC of the fixture with renamed
nets) or stop at the pre-routing refusal, so they take seconds.

    KICADROUTE-10  a bus net (`/BUS[0]`) in a DRC item is read whole, so it is in scope
    KICADROUTE-13  new copper with no net fails routing_scope
    KICADROUTE-14  a board without a .kicad_pro of its own name is refused / fails
    KICADROUTE-31  the delivered (pre-refill) board hash is taken before the refill
    KICADROUTE-32  repeated validate(run_dir[, origin=...]) calls do not collide
    KICADROUTE-33  the candidate's own project files are kept before they are restored
    KICADROUTE-35  `validate` of a run without a candidate board is a process failure (1)
"""
import argparse
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from helpers import FIXTURE, OUT, cli
from kicad_route import cli as route
from kicad_route import common, netnames
from component_fixtures import component_receipt

BOARD = "route_demo.kicad_pcb"


# ------------------------------------------------------------ the net parser

@pytest.mark.parametrize("text,known,nets", [
    ("Pad 1 [/BUS[0]] of C1 on F.Cu", None, ["/BUS[0]"]),
    ("Track [/D[7..0]] on F.Cu, length 1.0000 mm", None, ["/D[7..0]"]),
    ("PTH pad 3 [/X]y] of J1", None, ["/X]y"]),          # unbalanced: ends where a separator follows
    ("Pad 2 [/Y[z] of R2 on F.Cu", None, ["/Y[z"]),
    ("Zone 'pour' [GND] on F.Cu", None, ["GND"]),
    ("Pad 1 [<no net>] of R1 on F.Cu", None, [netnames.NO_NET]),
    ("PTH pad 3 [/X] y] of J1", {"/X", "/X] y"}, ["/X] y"]),   # known names win, brackets and all
    ("Via [/A] on F.Cu - B.Cu; Pad 1 [/BUS[1]] of U1", {"/A", "/BUS[1]"}, ["/A", "/BUS[1]"]),
])
def test_nets_in_description(text, known, nets):
    assert netnames.nets_in_description(text, known) == nets


def test_item_nets_keeps_bus_nets_whole():
    """KICADROUTE-10: the old regex read `/BUS[0` and the scope intersection came out empty."""
    u = {"items": [{"description": "Pad 1 [/BUS[0]] of C1 on F.Cu"}, {"description": "PTH pad 3 [/BUS[0]] of J2"}]}
    assert route.item_nets(u) == {"/BUS[0]"}
    assert route.item_nets(u) & {"/BUS[0]"}


def test_kicad_drc_reports_bracketed_nets_the_parser_reads(tmp_path):
    """The description format the parser relies on, from KiCad itself: the fixture with
    /SIG_A_OUT renamed to a bus net and two nets with unbalanced brackets."""
    proj = tmp_path / "route_demo"
    shutil.copytree(FIXTURE, proj, ignore=shutil.ignore_patterns("generate.py", "__pycache__"))
    names = {"/SIG_A_OUT": "/BUS[0]", "/SIG_A": "/X]y", "/SIG_B_OUT": "/Y[z"}
    (tmp_path / "map.json").write_text(json.dumps(names))
    subprocess.run([common.PCBNEW_PYTHON, route.KICAD_SCRIPTS / "rename_nets.py", proj / BOARD,
                    tmp_path / "map.json"], check=True, capture_output=True)
    out = tmp_path / "drc.json"
    subprocess.run(["kicad-cli", "pcb", "drc", "--format", "json", "--severity-all", "-o", out, proj / BOARD],
                   check=True, capture_output=True)
    unconnected = json.loads(out.read_text())["unconnected_items"]
    found = set().union(*(route.item_nets(u) for u in unconnected))
    assert set(names.values()) <= found, [[i["description"] for i in u["items"]] for u in unconnected]


# ------------------------------------------------------------ synthetic facts

def facts(nets, tracks=(), vias=()):
    return {"outline": [], "copper_layer_count": 2, "enabled_layers": ["F.Cu", "B.Cu"],
            "copper_layers": ["F.Cu", "B.Cu"], "stackup": None, "setup": {}, "board": BOARD,
            "nets": {n: {"netclass": "Default", "track_width": 200_000, "via_diameter": 600_000,
                         "via_drill": 300_000, "clearance": 200_000} for n in nets},
            "footprints": {}, "zones": [], "rule_areas": [], "copper_graphics": [],
            "tracks": list(tracks), "vias": list(vias)}


def track(net, uuid="t1", y=0):
    return {"kind": "segment", "layer": "F.Cu", "net": net, "width": 250_000, "start": [0, y],
            "end": [5_000_000, y], "locked": False, "uuid": uuid}


def via(net, uuid="v1"):
    return {"net": net, "layers": ["F.Cu", "B.Cu"], "via_type": "through", "diameter": 600_000,
            "drill": 300_000, "position": [1_000_000, 1_000_000], "locked": False, "uuid": uuid}


def checks_of(ref, cand, scope, tmp_path):
    (tmp_path / "r").mkdir(exist_ok=True)
    (tmp_path / "c").mkdir(exist_ok=True)
    checks, _ = route.preservation(ref, cand, tmp_path / "r", tmp_path / "c", BOARD, "existing", scope)
    return {c["check"]: c for c in checks}


def test_new_copper_without_a_net_fails_routing_scope(tmp_path):
    """KICADROUTE-13: `if t["net"] and ...` let net-less copper through both checks."""
    ref = facts(["/A"])
    cand = facts(["/A"], tracks=[track("")], vias=[via("")])
    c = checks_of(ref, cand, {"/A"}, tmp_path)
    assert c["routing_scope"]["status"] == route.FAIL
    assert netnames.NO_NET in c["routing_scope"]["detail"]
    assert {d["item"] for d in c["routing_scope"]["differences"]} == {"track", "via"}
    assert all(d["net"] == netnames.NO_NET for d in c["routing_scope"]["differences"])


def test_new_copper_on_scope_nets_passes_routing_scope(tmp_path):
    c = checks_of(facts(["/A", "/B"]), facts(["/A", "/B"], tracks=[track("/A")], vias=[via("/A")]), {"/A"},
                  tmp_path)
    assert c["routing_scope"]["status"] == route.PASS
    c = checks_of(facts(["/A", "/B"]), facts(["/A", "/B"], tracks=[track("/B")]), {"/A"}, tmp_path)
    assert c["routing_scope"]["status"] == route.FAIL


# ------------------------------------------------------- a synthetic run dir

EMPTY_DRC = {"violations": [], "unconnected_items": [], "schematic_parity": []}


def make_run(root, nets=("/A", "/BUS[0]"), scope=("/BUS[0]",), board=BOARD, pro=True, source_dir=None):
    """A run directory validate() can grade: reference/ and candidate/ projects, the
    reference facts, an empty baseline and attempt.json."""
    root.mkdir(parents=True)
    for side in ("reference", "candidate"):
        d = root / side / "p"
        d.mkdir(parents=True)
        (d / board).write_text("(kicad_pcb)\n")
        if pro:
            (d / board).with_suffix(".kicad_pro").write_text('{"rules": "input"}\n')
        (d / board).with_suffix(".kicad_dru").write_text("(version 1)\n")
    (root / "facts").mkdir()
    (root / "facts" / "reference-facts.json").write_text(json.dumps(facts(nets)))
    (root / "baseline.json").write_text(json.dumps(EMPTY_DRC))
    attempt = {"board": board, "schematic": None, "candidate_dir": "candidate/p", "reference_dir": "reference/p",
               "reference_facts": "facts/reference-facts.json", "baseline": {"report": "baseline.json"},
               "scope": {"nets": list(scope)}, "settings": {"protect": "existing"}, "floor": None,
               "source": {"source_dir": str(source_dir) if source_dir else None}, "input_fingerprint": "test-input"}
    (root / "attempt.json").write_text(json.dumps(attempt))
    return root


@pytest.fixture
def stub_kicad(monkeypatch):
    """kicad_drc, board_facts and pour_check without KiCad. The DRC stub refills by
    appending to the board, then grades; `state` sets what it reports."""
    state = {"unconnected": [], "drc_edits_after_grading": False, "drc_calls": 0,
             "cand_facts": lambda board: facts(["/A", "/BUS[0]"])}

    def kicad_drc(board, out_dir, label, refill_and_save):
        state["drc_calls"] += 1
        out_dir.mkdir(parents=True, exist_ok=True)
        before = common.sha256(board)
        with open(board, "a") as f:
            f.write("(zone_fill)\n")
        info = {"commands": [], "reasons": [], "parity_performed": True,
                "refill": {"board_sha256_before": before, "board_sha256_after": common.sha256(board)},
                "board_sha256_graded": common.sha256(board)}
        if state["drc_edits_after_grading"]:
            with open(board, "a") as f:
                f.write("(edited)\n")
        info["board_sha256_checked"] = common.sha256(board)
        return {"violations": [], "unconnected_items": state["unconnected"], "schematic_parity": []}, info

    monkeypatch.setattr(route, "kicad_drc", kicad_drc)
    monkeypatch.setattr(route, "board_facts", lambda board, out: (state["cand_facts"](board), {"log": str(out)}))
    monkeypatch.setattr(route, "pour_check", lambda *a, **k: {"check": "pour_integrity", "status": route.PASS,
                                                             "detail": "stub", "differences": []})
    monkeypatch.setattr(route.blocked, "write_report", lambda *a, **k: None)
    state["reference_components"] = component_receipt({"/A": [["a1", "a2"]], "/BUS[0]": [["b1", "b2"]]})
    state["candidate_components"] = state["reference_components"]
    monkeypatch.setattr(route.baselinecache, "identity", lambda *a, **k: {"test": True})
    monkeypatch.setattr(route.baselinecache, "terminal_facts", lambda *a, **k: state["reference_components"])
    monkeypatch.setattr(route.native_components, "extract", lambda *a, **k: (state["candidate_components"], {}))
    return state


def unconnected(net):
    return {"type": "unconnected_items", "severity": "error",
            "items": [{"description": f"Pad 1 [{net}] of C1 on F.Cu", "pos": {"x": 1, "y": 1}},
                      {"description": f"PTH pad 3 [{net}] of J2", "pos": {"x": 2, "y": 2}}]}


def test_unrouted_bus_net_is_incomplete(tmp_path, stub_kicad):
    """KICADROUTE-10: an open connection on /BUS[0] (in scope) is INCOMPLETE, exit 2."""
    run_dir = make_run(tmp_path / "run")
    open_bus = [unconnected("/BUS[0]")]
    (run_dir / "baseline.json").write_text(json.dumps({**EMPTY_DRC, "unconnected_items": open_bus}))
    stub_kicad["unconnected"] = open_bus  # still open: not routed, and not lost either
    v, routing = route.validate(run_dir)
    assert v["status"] == route.PASS, v["reasons"]
    assert routing["status"] == route.INCOMPLETE and routing["unrouted_nets"] == ["/BUS[0]"]
    assert route.overall_exit({"status": route.PASS}, routing, v) == 2


def test_bus_net_losing_its_connection_fails_net_completion(tmp_path, stub_kicad):
    """KICADROUTE-10, the ratchet: /BUS[0] was connected in the input and is open now."""
    run_dir = make_run(tmp_path / "run")
    stub_kicad["unconnected"] = [unconnected("/BUS[0]")]
    stub_kicad["candidate_components"] = component_receipt({"/A": [["a1", "a2"]], "/BUS[0]": [["b1"], ["b2"]]})
    v, routing = route.validate(run_dir)
    assert v["preservation"]["net_completion"] == route.FAIL and v["net_completion"]["nets_lost"] == ["/BUS[0]"]


def test_open_connection_outside_scope_is_complete(tmp_path, stub_kicad):
    run_dir = make_run(tmp_path / "run")
    stub_kicad["unconnected"] = [unconnected("/A")]
    v, routing = route.validate(run_dir)
    assert routing["status"] == route.COMPLETE and routing["unconnected_total"] == 1


def test_unidentified_open_connection_counts_in_scope(tmp_path, stub_kicad):
    """Fail closed: a connection whose net the parser cannot match to a board net is
    never silently treated as out of scope."""
    run_dir = make_run(tmp_path / "run")
    stub_kicad["unconnected"] = [unconnected("/NOT_ON_THE_BOARD")]
    v, routing = route.validate(run_dir)
    assert routing["status"] == route.INCOMPLETE and routing["unconnected_unidentified"]


def test_hashes_bracket_the_refill(tmp_path, stub_kicad):
    """KICADROUTE-31: delivered is the board before the refill, validated the board DRC
    read; candidate (what candidate/ holds afterwards) must equal validated."""
    run_dir = make_run(tmp_path / "run")
    board = run_dir / "candidate" / "p" / BOARD
    delivered = common.sha256(board)
    v, _ = route.validate(run_dir)
    assert v["delivered_board_sha256"] == delivered
    assert v["validated_board_sha256"] == common.sha256(board) == v["candidate_board_sha256"]
    assert v["refill_modified_board"] is True
    assert v["status"] == route.PASS, v["reasons"]


def test_board_changed_after_grading_fails(tmp_path, stub_kicad):
    run_dir = make_run(tmp_path / "run")
    stub_kicad["drc_edits_after_grading"] = True
    v, _ = route.validate(run_dir)
    assert v["status"] == route.FAIL
    assert any("changed after DRC graded it" in r for r in v["reasons"])


def test_repeat_validate_gets_fresh_directories(tmp_path, stub_kicad):
    """KICADROUTE-32: validate(run_dir) and validate(run_dir, origin=...) twice each,
    without out_dir: no FileExistsError, and every grading lands under run_dir."""
    origin = make_run(tmp_path / "origin")
    step = make_run(tmp_path / "step", source_dir=origin / "candidate" / "p")
    dirs = [route.validate(step)[0]["dir"], route.validate(step)[0]["dir"],
            route.validate(step, origin=origin)[0]["dir"], route.validate(step, origin=origin)[0]["dir"]]
    assert len(set(dirs)) == 4
    assert all(Path(d).resolve().is_relative_to(step.resolve()) for d in dirs), dirs
    assert not (origin / "validate").exists() and not (origin / "revalidate-origin").exists()


def test_restored_project_files_are_kept(tmp_path, stub_kicad):
    """KICADROUTE-33: what the backend wrote to the project files is graded away (the
    input's rules) but kept under the validation directory and recorded."""
    run_dir = make_run(tmp_path / "run")
    cand = run_dir / "candidate" / "p"
    (cand / "route_demo.kicad_pro").write_text('{"rules": "relaxed by the backend"}\n')
    (cand / "extra.kicad_dru").write_text("(version 1) (rule relaxed)\n")
    v, _ = route.validate(run_dir)
    assert v["status"] == route.FAIL and v["preservation"]["project_files"] == route.FAIL
    assert sorted(v["restored_project_files"]) == ["extra.kicad_dru", "route_demo.kicad_pro"]
    # graded with the input's rules ...
    assert (cand / "route_demo.kicad_pro").read_text() == '{"rules": "input"}\n'
    assert not (cand / "extra.kicad_dru").exists()
    # ... and the delivered versions survive as evidence
    kept = {e["path"]: e for e in v["delivered_project_files"]}
    assert kept["route_demo.kicad_pro"]["action"] == "restored"
    assert kept["extra.kicad_dru"]["action"] == "removed"
    vdir = run_dir / "validate" / "delivered-project-files"
    assert (vdir / "route_demo.kicad_pro").read_text() == '{"rules": "relaxed by the backend"}\n'
    assert (vdir / "extra.kicad_dru").read_text() == "(version 1) (rule relaxed)\n"
    assert kept["route_demo.kicad_pro"]["delivered_sha256"] == common.sha256(vdir / "route_demo.kicad_pro")


def test_validate_without_project_file_fails(tmp_path, stub_kicad):
    """KICADROUTE-14: no <board>.kicad_pro beside the board means KiCad grades with its
    defaults, so validation fails instead of grading."""
    run_dir = make_run(tmp_path / "run", board="other.kicad_pcb", pro=False)
    for side in ("reference", "candidate"):  # the project exists, under another name
        (run_dir / side / "p" / "route_demo.kicad_pro").write_text("{}\n")
    v, routing = route.validate(run_dir)
    assert v["status"] == route.FAIL and routing["status"] == route.NOT_RUN
    assert any("other.kicad_pro" in r and "route_demo.kicad_pro" in r for r in v["reasons"])
    assert stub_kicad["drc_calls"] == 0
    assert route.overall_exit({"status": route.PASS}, routing, v) == 3


def test_rules_files(tmp_path):
    (tmp_path / "other.kicad_pcb").write_text("")
    (tmp_path / "proj.kicad_pro").write_text("{}")
    (tmp_path / "proj.kicad_dru").write_text("")
    r = route.rules_files(tmp_path, "other.kicad_pcb")
    assert r["refusal"] and r["not_loaded"] == ["proj.kicad_dru", "proj.kicad_pro"]
    (tmp_path / "other.kicad_pro").write_text("{}")
    r = route.rules_files(tmp_path, "other.kicad_pcb")
    assert r["refusal"] is None and r["custom_rules"] is None and r["project"] == "other.kicad_pro"
    (tmp_path / "other.kicad_dru").write_text("")
    assert route.rules_files(tmp_path, "other.kicad_pcb")["custom_rules"] == "other.kicad_dru"


def test_cmd_validate_without_candidate_board_exits_1(tmp_path, stub_kicad):
    """KICADROUTE-35: nothing to grade is a process failure, not a validation failure."""
    run_dir = make_run(tmp_path / "run")
    (run_dir / "candidate" / "p" / BOARD).unlink()
    args = argparse.Namespace(run_dir=str(run_dir), against=None, pour_area_loss=None, pour_min_spokes=None)
    assert route.cmd_validate(args) == 1
    rec = json.loads(next((run_dir / "revalidate").glob("*/result.json")).read_text())
    assert rec["process"]["status"] == route.FAIL and rec["exit_code"] == 1


# --------------------------------------------- refusal before routing / audit

def renamed_board_project(tmp_path):
    """The fixture with its board saved as other.kicad_pcb: the project (and rules) keep
    the route_demo name, so KiCad would load neither for the board."""
    d = tmp_path / "proj"
    d.mkdir()
    for f in FIXTURE.iterdir():
        if f.is_file() and f.name != "generate.py":
            shutil.copy2(f, d / (("other" + f.suffix) if f.name == BOARD else f.name))
    return d


def test_run_refuses_board_without_its_project(tmp_path):
    """KICADROUTE-14: refused (exit 4) before routing, and the run directory is discarded."""
    d = renamed_board_project(tmp_path)
    runs = OUT / "runs" / "proj" / "no-project"
    before = set(runs.glob("*/attempt.json"))
    r = cli("run", "--backend", "krt", "--dir", str(d), "--board", "other.kicad_pcb", "--label", "no-project",
            timeout=300)
    assert r.returncode == 4, r.stdout + r.stderr
    assert "refused" in r.stdout and "other.kicad_pro" in r.stdout
    assert set(runs.glob("*/attempt.json")) == before


def test_audit_refuses_board_without_its_project(tmp_path):
    d = renamed_board_project(tmp_path)
    git = ["git", "-C", str(d), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "one"], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "two"], check=True)
    r = cli("audit", "--dir", str(d), "--board", "other.kicad_pcb", "--old", "HEAD~1", "--new", "HEAD", timeout=300)
    assert r.returncode == 4, r.stdout + r.stderr
    assert "refused" in r.stdout and "other.kicad_pro" in r.stdout


def test_scoped_native_deficit_cannot_hide_behind_other_drc_findings(tmp_path, stub_kicad):
    run_dir = make_run(tmp_path / "run")
    terminals = component_receipt({"/A": [["a1", "a2"]], "/BUS[0]": [["b1"], ["b2"], ["b3"]]})
    stub_kicad["reference_components"] = stub_kicad["candidate_components"] = terminals
    stub_kicad["unconnected"] = [unconnected("/BUS[0]"), unconnected("/A"), unconnected("/A")]
    v, routing = route.validate(run_dir)
    assert routing["unconnected_total"] == 3 and routing["unconnected_in_scope"] == 1
    assert routing["status"] == route.NOT_RUN
    assert v["net_completion"]["status"] == route.PASS and v["status"] == route.FAIL
    assert any("do not cover" in reason for reason in v["reasons"])


@pytest.mark.parametrize("damage", ["missing", "stale"])
def test_validate_rejects_missing_or_stale_native_coverage(tmp_path, stub_kicad, damage):
    run_dir = make_run(tmp_path / "run")
    if damage == "missing":
        stub_kicad["reference_components"] = None
    else:
        candidate = component_receipt({"/A": [["a1", "a2"]], "/BUS[0]": [["b1", "b2"]]})
        candidate["identity"]["board_sha256"] = "f" * 64
        stub_kicad["candidate_components"] = candidate
    v, routing = route.validate(run_dir)
    assert v["net_completion"]["status"] == route.NOT_RUN
    assert v["status"] == route.FAIL
    assert route.overall_exit({"status": route.PASS}, routing, v) == 3


def test_validate_rejects_partial_terminal_split_despite_equal_native_open_counts(tmp_path, stub_kicad):
    run_dir = make_run(tmp_path / "run")
    stub_kicad["reference_components"] = component_receipt(
        {"/A": [["a1", "a2"]], "/BUS[0]": [["b1", "b2"], ["b3"]]})
    stub_kicad["candidate_components"] = component_receipt(
        {"/A": [["a1", "a2"]], "/BUS[0]": [["b1"], ["b2", "b3"]]})
    stub_kicad["unconnected"] = [unconnected("/BUS[0]")]
    common.write_json(run_dir / "baseline.json", {**EMPTY_DRC, "unconnected_items": [unconnected("/BUS[0]")]})
    v, routing = route.validate(run_dir)
    assert routing["unconnected_total"] == routing["baseline_unconnected_total"] == 1
    assert v["status"] == v["net_completion"]["status"] == route.FAIL
    assert v["net_completion"]["nets_lost"] == []
    assert v["net_completion"]["split_components"][0]["original_terminals"] == ["b1", "b2"]


@pytest.mark.parametrize("remove_input", [False, True])
def test_explicit_candidate_context_grades_real_saved_board(tmp_path, monkeypatch, remove_input):
    """An independent candidate is graded against the supplied original reference."""
    import pcbnew
    from helpers import tree_hash
    from kicad_route import evidence, native_components

    monkeypatch.setattr(common, "_out", tmp_path / "out")
    common.ensure_kicad_config()
    run = tmp_path / "original"
    ref = run / "reference" / "route_demo"
    ref.parent.mkdir(parents=True)
    route.copy_project(FIXTURE, ref)
    candidate = tmp_path / "worker" / "candidate"
    candidate.parent.mkdir()
    route.copy_project(ref, candidate)
    if remove_input:
        board = pcbnew.LoadBoard(str(candidate / BOARD))
        board.Remove(next(iter(board.GetTracks())))
        pcbnew.SaveBoard(str(candidate / BOARD), board, aSkipSettings=True)
    before_reference = tree_hash(ref)
    before_candidate = common.sha256(candidate / BOARD)
    ref_facts, _ = route.board_facts(ref / BOARD, run / "reference-facts.json")
    assert ref_facts is not None
    baseline, info = route.baseline_drc(ref, BOARD, "explicit-context",
                                       pcbnew_build=ref_facts["kicad_version"])
    assert baseline is not None, info
    attempt = {"board": BOARD, "schematic": "route_demo.kicad_sch",
               "reference_dir": "reference/route_demo", "reference_facts": "reference-facts.json",
               "candidate_dir": "candidate/absent", "input_fingerprint": "explicit-context",
               "baseline": {"report": info["report"]}, "settings": {"protect": "existing"},
               "floor": None}
    scope = set(ref_facts["nets"])
    out = tmp_path / "worker" / "grade"
    validation, routing = route.validate_candidate(run, attempt, candidate, scope, out)
    assert validation["status"] == (route.FAIL if remove_input else route.PASS), validation["reasons"]
    assert validation["preservation"]["protected_copper"] == (route.FAIL if remove_input else route.PASS)
    assert validation["delivered_board_sha256"] == before_candidate
    assert validation["validated_board_sha256"] == validation["candidate_board_sha256"] == common.sha256(candidate / BOARD)
    assert tree_hash(ref) == before_reference
    assert not (run / "candidate").exists() and not (run / "attempt.json").exists()
    assert "chain" not in validation
    assert validation["parity_performed"] is True
    assert native_components.coverage(common.load_json(out / "reference-terminal-components.json"))
    final_terms = common.load_json(out / "candidate-terminal-components.json")
    assert native_components.coverage(final_terms)
    assert final_terms["identity"]["board_sha256"] == common.sha256(candidate / BOARD)
    bound = evidence.block(run, attempt, out, validation)
    assert bound["input_board_sha256"] == common.sha256(ref / BOARD)
    assert bound["candidate_board_sha256"] == common.sha256(candidate / BOARD)
    assert "protected_copper" in validation["preservation"]
    assert routing["status"] in (route.COMPLETE, route.INCOMPLETE)
