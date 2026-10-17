"""kicad-route integration checks (`kicad-route smoke`; `kicad-route update` runs them too).

They prove, on the route_demo fixture, that each backend completes the KiCad
round trip, that the candidate is graded by KiCad against the unchanged input
rules, and that routing and validation failures reach the caller. They are not
a routing-quality benchmark. Shared harness: helpers.py and conftest.py.

Every test is self-sufficient: a routing attempt that more than one test reads
(the round trips, the /SIG_A* subset, the blocked runs) comes from the module
fixture `attempt`, which runs it the first time any test asks and hands the same
run to every later test. Running one test alone (-k) routes only what it needs;
the whole module routes each attempt once, as before.
"""
import json
import os
import shutil
import subprocess

import pytest

from helpers import CLI, FIXTURE, OUT, PROTECTED, ROOT, cli, record, run, summary, tree_hash  # noqa: F401
from kicad_route import cli as route
from kicad_route.kicad import specctra


# Attempts read by more than one test: label -> run arguments. "{blocked}" is the
# conftest blocked_dir fixture (the keepout wall around J2).
FREEROUTING_CLEAN = ("--clearance-margin", "0.01", "--passes", "100", "--backend-arg=--router.fanout.enabled=true")
ATTEMPTS = {
    # the retry the custom-rule failure suggests: Default class at 0.4 mm (tighter, never looser)
    "smoke-krt": ("--backend", "krt", "--budget", "180", "--default-clearance", "0.4"),
    # fanout (off by default: it can neck traces down) is re-enabled as the retry
    # playbook says; new_copper_netclass_geometry still grades every new conductor
    "smoke-freerouting": ("--backend", "freerouting", "--budget", "180", *FREEROUTING_CLEAN),
    "smoke-krt-nets": ("--backend", "krt", "--budget", "180", "--nets", "/SIG_A*", "--default-clearance", "0.4"),
    # KRT's net rescue retries unroutable nets at length; switched off so the attempt
    # returns a candidate. --default-clearance 0.4 as in smoke-krt: without it KRT's
    # copper also fails the Power-to-signal rule it cannot read (exit 3, covered by
    # test_krt_custom_rule_failure_is_reported), which would hide whether an
    # incomplete-but-valid candidate reaches the caller as exit 2.
    "smoke-blocked-krt": ("--backend", "krt", "--budget", "180", "--dir", "{blocked}",
                          "--backend-env", "KICAD_NET_RESCUE=0", "--default-clearance", "0.4"),
    "smoke-blocked-freerouting": ("--backend", "freerouting", "--budget", "180", "--dir", "{blocked}"),
}


@pytest.fixture(scope="module")
def attempt(request):
    """attempt(label) -> (process, run_dir, result) of ATTEMPTS[label], run once per module."""
    done = {}

    def get(label):
        if label not in done:
            args = ATTEMPTS[label]
            if "{blocked}" in args:
                blocked = str(request.getfixturevalue("blocked_dir"))
                args = tuple(blocked if a == "{blocked}" else a for a in args)
            done[label] = run("run", *args, "--label", label)
        return done[label]
    return get


def assert_clean_candidate(result, run_dir):
    assert result["process"]["status"] == route.PASS, result["process"]
    assert result["routing"]["status"] == route.COMPLETE, result["routing"]
    assert result["validation"]["status"] == route.PASS, result["validation"]["reasons"]
    assert all(v == route.PASS for v in result["validation"]["preservation"].values())
    assert result["validation"]["parity_performed"] is True
    assert result["validation"]["candidate_board_sha256"] == result["validation"]["validated_board_sha256"]
    assert result["exit_code"] == 0
    board = OUT / result["candidate"]["board"]
    assert board.exists()
    # the candidate carries the input's own rules and schematic, byte for byte
    for name in ("route_demo.kicad_pro", "route_demo.kicad_dru", "route_demo.kicad_sch"):
        assert (board.parent / name).read_bytes() == (FIXTURE / name).read_bytes(), name
    checks = json.loads((run_dir / "validate" / "preservation.json").read_text())
    prot = next(c for c in checks if c["check"] == "protected_copper")
    assert f"{PROTECTED['tracks']} tracks and {PROTECTED['vias']} vias designated" in prot["detail"]


def test_route_doctor():
    r = cli("doctor", timeout=600)
    doc = json.loads((OUT / "doctor" / "doctor.json").read_text())
    record("doctor", {"exit_code": r.returncode, "status": doc["status"], "versions": doc["versions"],
                      "checks": doc["checks"]})
    assert r.returncode == 0, r.stdout


def test_krt_custom_rule_failure_is_reported():
    """KRT cannot read the fixture's Power-to-signal .kicad_dru rule. Its candidate is
    complete, but KiCad grades it against that rule and the failure reaches the caller."""
    r, run_dir, result = run("run", "--backend", "krt", "--budget", "180", "--label", "smoke-krt-default")
    record("krt_default", summary(result))
    assert r.returncode == 3 and result["exit_code"] == 3
    assert result["routing"]["status"] == route.COMPLETE
    assert result["validation"]["status"] == route.FAIL
    new = json.loads((run_dir / "validate" / "new-findings.json").read_text())["violations"]
    assert new and all("power_to_signal_clearance" in v["description"] for v in new)
    assert any("power_to_signal_clearance" in u and "skipped" in u for u in result["unsupported"])


def test_krt_round_trip(attempt):
    """The retry the failure above suggests: Default class at 0.4 mm (tighter, never looser)."""
    r, run_dir, result = attempt("smoke-krt")
    record("krt_round_trip", summary(result))
    assert_clean_candidate(result, run_dir)
    rel = result["process"]["backend_report"]["relaxations"]
    assert rel["escalation_policy"] == "off" and rel["fab_tier"] == "standard" and not rel["narrowed"]
    argv = result["process"]["commands"][0]["argv"]
    assert "--no-fix-drc-settings" in argv and "--escalation" in argv


def test_freerouting_round_trip(attempt):
    r, run_dir, result = attempt("smoke-freerouting")
    record("freerouting_round_trip", summary(result))
    assert_clean_candidate(result, run_dir)
    bridge = result["process"]["bridge"]
    assert bridge["import"]["placement_dropped"] is True
    argv = result["process"]["commands"][1]["argv"]
    assert "--router.automatic_neckdown=false" in argv and "-da" in argv
    assert [t["rule"] for t in bridge["export"]["dru_translated"]] == ["power_to_signal_clearance"]
    assert "Power" in (run_dir / "work" / "board.dsn").read_text().split("(class_class", 1)[1][:80]


def test_net_name_selection(attempt):
    r, run_dir, result = attempt("smoke-krt-nets")
    record("krt_net_selection", summary(result))
    assert result["scope"]["nets_list"] == ["/SIG_A", "/SIG_A_OUT"]
    assert result["routing"]["status"] == route.COMPLETE          # in scope
    assert result["routing"]["unconnected_total"] > 0             # other nets left alone
    assert result["validation"]["preservation"]["routing_scope"] == route.PASS
    assert result["exit_code"] == 0


def test_chained_attempts_graded_against_origin(attempt):
    """Continue from a partial candidate with --dir, then grade the chain's end
    against the first attempt's input with validate --against."""
    first = attempt("smoke-krt-nets")[1]
    cand = first / json.loads((first / "attempt.json").read_text())["candidate_dir"]
    r, run_dir, result = run("run", "--backend", "krt", "--budget", "180", "--dir", str(cand),
                             "--board", "route_demo.kicad_pcb", "--nets", "!/SIG_A*",
                             "--default-clearance", "0.4", "--label", "smoke-krt-chain")
    assert result["process"]["status"] == route.PASS
    v = cli("validate", str(run_dir), "--against", str(first.resolve()), timeout=600)
    res = json.loads((sorted((run_dir / "revalidate-origin").iterdir())[-1] / "result.json").read_text())
    record("chain", {"step": summary(result), "against_origin": res})
    assert result["routing"]["status"] == route.COMPLETE     # this step's own scope
    assert res["routing"]["status"] == route.COMPLETE        # origin scope /SIG_A* still connected
    assert res["validation"]["status"] == route.PASS, res["validation"]["reasons"]
    assert res["validation"]["preservation"]["protected_copper"] == route.PASS
    assert v.returncode == 0


def test_bridge_scope_split_handles_parenthesised_net_names():
    """KiCad names unlabeled nets like "Net-(BT1-+)"; the scope split must not stop
    at a parenthesis inside quotes (it produced an unparsable DSN on a real board)."""
    dsn = ('(pcb x (network\n    (class kicad_default A "Net-(BT1-+)" B\n'
           '      (circuit (use_via V))\n      (rule (width 254) (clearance 200)))))')
    out, ignored = specctra.split_classes(dsn, {"Net-(BT1-+)"})
    assert ignored == ["kicad_default__skip"]
    assert '(class kicad_default A B\n' in out
    assert '(class "kicad_default__skip" "Net-(BT1-+)"\n' in out
    assert specctra.block_span(out, 0) == len(out)


def test_net_class_selection():
    r, run_dir, result = run("run", "--backend", "freerouting", "--budget", "180", "--netclass", "Power",
                             "--clearance-margin", "0.01", "--backend-arg=--router.fanout.enabled=true",
                             "--label", "smoke-freerouting-netclass")
    record("freerouting_netclass_selection", summary(result))
    assert result["scope"]["nets_list"] == ["/VIN", "GND"]
    assert "kicad_default__skip" in result["process"]["bridge"]["export"]["ignored_classes"]
    assert result["validation"]["preservation"]["routing_scope"] == route.PASS
    assert result["routing"]["status"] == route.COMPLETE
    assert result["routing"]["unconnected_total"] > 0


@pytest.mark.parametrize("backend", ["krt", "freerouting"])
def test_unroutable_board_fails(backend, blocked_dir, attempt):
    """A keepout wall makes J2 unreachable: the candidate must come back incomplete
    and otherwise valid -- exit 2, not 3. (KRT's net rescue is switched off so the
    attempt returns a candidate -- the budget test below covers the other path.)"""
    before = tree_hash(blocked_dir)
    r, run_dir, result = attempt(f"smoke-blocked-{backend}")
    record(f"blocked_{backend}", summary(result))
    assert tree_hash(blocked_dir) == before
    assert result["validation"]["status"] == route.PASS, result["validation"]["reasons"]
    assert r.returncode == 2 and result["exit_code"] == 2
    assert result["routing"]["status"] == route.INCOMPLETE
    assert result["routing"]["unconnected_in_scope"] > 0
    assert result["validation"]["preservation"]["keepouts"] == route.PASS


@pytest.mark.parametrize("backend", ["krt", "freerouting"])
def test_unlocked_existing_copper_survives(backend, unlocked_dir):
    """--protect existing (default): unlocked input copper comes back bit-identical and
    still unlocked; the backends only see it locked for the duration of the run."""
    extra = (["--default-clearance", "0.4"] if backend == "krt" else
             ["--clearance-margin", "0.01", "--passes", "100", "--backend-arg=--router.fanout.enabled=true"])
    r, run_dir, result = run("run", "--backend", backend, "--budget", "180", "--dir", str(unlocked_dir),
                             *extra, "--label", f"smoke-unlocked-{backend}")
    record(f"unlocked_{backend}", summary(result))
    assert result["validation"]["preservation"]["protected_copper"] == route.PASS
    facts = json.loads((run_dir / "validate" / "candidate-facts.json").read_text())
    sig_b = [t for t in facts["tracks"] + facts["vias"] if t["net"] == "/SIG_B"]
    assert len(sig_b) == PROTECTED["tracks"] + PROTECTED["vias"]
    assert not any(t["locked"] for t in sig_b)
    detail = next(c for c in json.loads((run_dir / "validate" / "preservation.json").read_text())
                  if c["check"] == "protected_copper")["detail"]
    assert "max deviation 0 nm" in detail


def test_budget_overrun_is_a_process_failure(blocked_dir):
    """With rescue on, KRT keeps searching the unroutable nets past the budget:
    the wrapper stops it and reports a process failure, not a candidate.
    The budget must outlast KRT's start-up: on a loaded host a 15 s budget expired
    before KRT's first net, KRT honoured the stop file at that checkpoint and
    wrote the board unrouted (process PASS, exit 2). At 40 s the stop lands in a
    search on the walled nets, which does not check the stop file."""
    r, run_dir, result = run("run", "--backend", "krt", "--budget", "40", "--dir", str(blocked_dir),
                             "--label", "smoke-budget-krt")
    record("budget_krt", summary(result))
    assert r.returncode == 1 and result["process"]["status"] == route.FAIL
    assert "budget" in " ".join(result["process"]["reasons"])
    assert result["routing"]["status"] == route.NOT_RUN and result["candidate"] is None


def test_validation_detects_tampering(tmp_path, attempt):
    """Edit a passing candidate the ways a router must not, then re-validate it."""
    import pcbnew
    src = attempt("smoke-krt")[1]
    run_dir = tmp_path / "run"
    shutil.copytree(src.resolve(), run_dir, symlinks=True)
    shutil.rmtree(run_dir / "validate")
    # rewrite paths in attempt.json relative to the copy (they are relative already)
    cand = run_dir / json.loads((run_dir / "attempt.json").read_text())["candidate_dir"]
    board_path = cand / "route_demo.kicad_pcb"
    board = pcbnew.LoadBoard(str(board_path))
    board.FindFootprintByReference("R1").Move(pcbnew.VECTOR2I(pcbnew.FromMM(0.5), 0))
    locked_via = next(t for t in board.GetTracks() if isinstance(t, pcbnew.PCB_VIA) and t.IsLocked())
    board.Remove(locked_via)
    vin = next(t for t in board.GetTracks() if not isinstance(t, pcbnew.PCB_VIA) and t.GetNetname() == "/VIN")
    vin.SetWidth(pcbnew.FromMM(0.3))
    board.Save(str(board_path))
    pro = cand / "route_demo.kicad_pro"
    data = json.loads(pro.read_text())
    data["net_settings"]["classes"][0]["clearance"] = 0.1   # a router lowering a rule
    pro.write_text(json.dumps(data, indent=2))
    # the baseline report path is relative to the original run directory
    att = json.loads((run_dir / "attempt.json").read_text())
    att["baseline"]["report"] = str((src.resolve() / att["baseline"]["report"]).resolve())
    (run_dir / "attempt.json").write_text(json.dumps(att))
    r = cli("validate", str(run_dir), timeout=600)
    out = sorted((run_dir / "revalidate").iterdir())[-1]
    res = json.loads((out / "result.json").read_text())
    record("tampering", {"exit_code": r.returncode, "validation": res["validation"], "routing": res["routing"]})
    assert r.returncode == 3
    p = res["validation"]["preservation"]
    for check in ("footprints", "protected_copper", "new_copper_netclass_geometry", "project_files"):
        assert p[check] == route.FAIL, check


def test_compare_same_and_different_inputs(attempt):
    same = [str(attempt("smoke-krt")[1]), str(attempt("smoke-freerouting")[1])]
    r1 = cli("compare", *same)
    blocked = attempt("smoke-blocked-krt")[1]
    r2 = cli("compare", same[0], str(blocked))
    record("compare", {"same_input": {"exit_code": r1.returncode, "table": r1.stdout},
                       "different_input": {"exit_code": r2.returncode, "table": r2.stdout}})
    assert r1.returncode == 0 and "| krt" in r1.stdout and "| freerouting" in r1.stdout
    assert r2.returncode == 1 and "different inputs" in r2.stdout


def test_audit_reports_rule_changes_and_design_edits(tmp_path):
    """A commit that lowers a rule and moves a part: audit grades it against its
    parent, flags the rule change and what it hides, and lists the design edits."""
    import pcbnew
    repo = tmp_path / "audit-repo"
    d = repo / "route_demo"
    shutil.copytree(FIXTURE, d, ignore=shutil.ignore_patterns("generate.py", "__pycache__"))

    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                       env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                                GIT_COMMITTER_EMAIL="t@t"))
    git("init", "-q")
    git("add", ".")
    git("commit", "-q", "-m", "parent")
    board = pcbnew.LoadBoard(str(d / "route_demo.kicad_pcb"))
    board.FindFootprintByReference("R1").Move(pcbnew.VECTOR2I(pcbnew.FromMM(0.5), 0))
    board.Save(str(d / "route_demo.kicad_pcb"))
    pro = d / "route_demo.kicad_pro"
    data = json.loads(pro.read_text())
    data["board"]["design_settings"]["rules"]["min_track_width"] = 0.1   # a lowered rule
    pro.write_text(json.dumps(data, indent=2))
    git("commit", "-q", "-am", "change")
    r = cli("audit", "--dir", str(d), "--old", "HEAD~1", "--new", "HEAD", timeout=900)
    res = json.loads(next((OUT / "audit" / "route_demo").glob("HEAD~1..HEAD/audit.json")).read_text())
    record("audit", {"exit_code": r.returncode, **{k: res[k] for k in (
        "old", "new", "new_under_old_rules", "rule_changes", "new_violations", "findings")},
        "changes": [{k: c[k] for k in ("check", "status")} for c in res["changes"]]})
    assert r.returncode == 3, r.stdout
    assert [c["setting"] for c in res["rule_changes"] if c["kind"] == "changed"] == ["rules.min_track_width"]
    assert any("min_track_width" in f for f in res["findings"])
    assert res["new_under_old_rules"] is not None
    assert {c["check"]: c["status"] for c in res["changes"]}["footprints"] == route.FAIL
