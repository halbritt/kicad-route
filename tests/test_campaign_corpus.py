"""Campaign and corpus input/time handling (KICADROUTE-20, -21, -26): --library-dir
reaches every chained step and every corpus check, a mapped library stays a link in
a chained snapshot, and a campaign's steps are bounded by --total-budget. Unit-level:
no backend runs. Shared harness: helpers.py and conftest.py.
"""
import json
import shutil
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import jsonschema

from helpers import FIXTURE
from kicad_route import campaign, common, corpus
from kicad_route import cli as route


def campaign_args(**over):
    a = route.build_parser().parse_args(["campaign", "--backend", "krt", "--dir", str(FIXTURE)])
    for k, v in over.items():
        setattr(a, k, v)
    return a


@pytest.fixture
def out(tmp_path, monkeypatch):
    """A private OUT, restored afterwards (common.out_dir is process-global)."""
    monkeypatch.setattr(common, "_out", None)
    common.set_out(tmp_path / "out")
    yield tmp_path / "out"


def make_campaign(out, **over):
    return campaign.Campaign(campaign_args(**over), route)


@pytest.fixture
def mapped_project(tmp_path):
    """The fixture with its footprints taken from an EMPTY libs/ directory that
    --library-dir maps to an external library (an uninitialised submodule)."""
    proj = tmp_path / "proj"
    proj.mkdir()
    for f in FIXTURE.glob("route_demo.*"):
        shutil.copy2(f, proj / f.name)
    (proj / "libs").mkdir()
    lib = tmp_path / "extlib"
    (lib / "My.pretty").mkdir(parents=True)
    (lib / "My.pretty" / "X.kicad_mod").write_text('(footprint "X")\n')
    (proj / "fp-lib-table").write_text('(fp_lib_table\n  (version 7)\n'
                                       '  (lib (name "My")(type "KiCad")(uri "${KIPRJMOD}/libs/My.pretty")'
                                       '(options "")(descr ""))\n)\n')
    return proj, f"libs={lib}"


# ------------------------------------------------- KICADROUTE-20: chained steps

def test_library_dir_reaches_every_step(out):
    c = make_campaign(out, library_dir=["fome-common=/opt/fome-common", "libs=/opt/libs"])
    first = c.input_argv()
    prev = {"candidate_dir": "runs/x/candidate/route_demo", "board": "route_demo.kicad_pcb",
            "schematic": "route_demo.kicad_sch"}
    chained = c.input_argv(prev)
    want = ["--library-dir", "fome-common=/opt/fome-common", "--library-dir", "libs=/opt/libs"]
    for argv in (first, chained):
        i = argv.index("--library-dir")
        assert argv[i:i + 4] == want
    assert chained[chained.index("--dir") + 1] == str(c.path(prev["candidate_dir"]))
    # the step's whole argv parses as `run` with the mappings intact
    argv = ["run", "--backend", "krt", *chained, *c.common_argv(), "--label", "x"]
    assert route.build_parser().parse_args(argv).library_dir == want[1::2]


def test_chained_step_snapshot_keeps_the_library_mapping(tmp_path, mapped_project):
    """A candidate keeps the mapped directory as a link to the library; with the
    mapping forwarded, the next step's snapshot links it again (as step 1 did) instead
    of copying the library into the input and its fingerprint."""
    proj, mapping = mapped_project

    def snap(d, libs, name):
        a = SimpleNamespace(dir=str(d), board="route_demo.kicad_pcb", schematic=None, name="p",
                            library_dir=libs, rev=None)
        run_dir = tmp_path / name
        run_dir.mkdir()
        return route.snapshot_input(route.resolve_input(a), run_dir)

    ref1, man1 = snap(proj, [mapping], "step1")
    assert (ref1 / "libs").is_symlink() and [l["path"] for l in man1["library_links"]] == ["libs"]
    cand = tmp_path / "candidate" / proj.name
    route.copy_project(ref1, cand)                       # what run does for its candidate
    ref2, man2 = snap(cand, [mapping], "step2")
    assert (ref2 / "libs").is_symlink()
    assert (ref2 / "libs").resolve() == Path(mapping.split("=", 1)[1]).resolve()
    assert [l["path"] for l in man2["library_links"]] == ["libs"]
    assert not any(f.startswith("libs/") for f in man2["files"])
    assert route.input_fingerprint(man2) == route.input_fingerprint(man1)
    # a populated directory that is not that link is still copied (never re-pointed)
    other, _ = snap(cand, [f"libs={tmp_path / 'elsewhere'}"], "step3")
    assert not (other / "libs").is_symlink() and (other / "libs" / "My.pretty" / "X.kicad_mod").exists()


# ------------------------------------------------- KICADROUTE-21: corpus entries

def test_corpus_entry_keeps_library_dir(out, tmp_path, monkeypatch, mapped_project):
    proj, mapping = mapped_project
    seen = []

    def fake_run_attempt(ns):
        seen.append(ns)
        return {"refused": "test stub", "exit_code": 4}

    monkeypatch.setattr(route, "run_attempt", fake_run_attempt)
    args = route.build_parser().parse_args(["corpus", "add", "--dir", str(proj), "--name", "mapped",
                                            "--library-dir", mapping, "--root", str(tmp_path / "corpus")])
    assert corpus.cmd_corpus(args) == 0
    entry = json.loads((tmp_path / "corpus" / "mapped" / "entry.json").read_text())
    assert entry["attempt"]["library_dir"] == [mapping]
    assert seen[0].library_dir == [mapping]              # the baseline run had it
    # ... and so does every later check of the entry
    e = corpus.entries(tmp_path / "corpus")[0]
    assert corpus.attempt_args(e).library_dir == [mapping]
    corpus.check(root=tmp_path / "corpus")
    assert seen[-1].library_dir == [mapping] and len(seen) == 2


def test_corpus_entry_without_library_dir_is_unchanged(tmp_path):
    e = {"name": "n", "board": "b.kicad_pcb", "reference_dir": "reference/p", "_dir": tmp_path,
         "attempt": {"backend": "krt"}}
    assert corpus.attempt_args(e).library_dir is None
    assert "library_dir" in corpus.RUN_KEYS


# --------------------------------------------- KICADROUTE-26: time and timeouts

def test_step_limits_without_total_budget(out):
    c = make_campaign(out, budget=300)
    budget, timeout = c.step_limits()
    assert budget == 300
    assert timeout == 300 + campaign.stop_grace(300) + campaign.HANG_ALLOWANCE_S
    assert c.validate_timeout() == campaign.HANG_ALLOWANCE_S


def test_step_limits_cut_to_total_budget(out):
    c = make_campaign(out, budget=300, total_budget=7200)
    assert c.step_limits()[0] == 300                     # plenty left: the campaign's own budget
    c.deadline = time.monotonic() + 100.5
    budget, timeout = c.step_limits()
    assert budget == 100                                 # cut to the time left
    # stopped at the deadline plus the run's own grace, never later
    assert 100 + campaign.stop_grace(100) + campaign.GRADE_ALLOWANCE_S <= timeout
    assert timeout <= 100.5 + campaign.stop_grace(100) + campaign.GRADE_ALLOWANCE_S
    c.deadline = time.monotonic() + 0.2
    assert c.step_limits()[0] == 1                       # never a zero budget
    c.deadline = time.monotonic() - 50
    assert c.validate_timeout() == campaign.GRADE_ALLOWANCE_S


def fake_invoker(c, results):
    """Replace Campaign.invoke: record (argv, timeout), return the next canned result."""
    calls = []

    def invoke(argv, log_name, timeout=None):
        calls.append((argv, timeout))
        rec = {"argv": argv, "exit_code": -15, "log": f"logs/{log_name}.log", "wall_s": 1.0,
               "timeout_s": timeout, "timed_out": False}
        rec.update(results.pop(0) if results else {})
        if rec["timed_out"]:
            c.timeouts.append({"log": rec["log"], "timeout_s": timeout, "wall_s": 1.0, "argv": argv})
        return rec, ""
    c.invoke = invoke
    return calls


def test_step_passes_cut_budget_and_timeout(out):
    c = make_campaign(out, budget=600, total_budget=3600)
    c.deadline = time.monotonic() + 42.9
    calls = fake_invoker(c, [])
    rec = c.step("r1", "backlog", "only", ["/A"])
    argv, timeout = calls[0]
    assert argv[argv.index("--budget") + 1] == "42"
    assert timeout == pytest.approx(42.9 + campaign.stop_grace(42) + campaign.GRADE_ALLOWANCE_S, abs=0.5)
    assert rec["budget_s"] == 42 and rec["status"] == "error"


def test_timed_out_round0_is_recorded(out):
    c = make_campaign(out, budget=60, total_budget=3600)
    fake_invoker(c, [{"timed_out": True}])
    rec = c.execute()
    step = rec["chains"][0]["steps"][0]
    assert step["timed_out"] and step["status"] == "error" and "timeout" in step["error"]
    assert rec["chains"][0]["status"] == "error" and rec["exit_code"] == 1
    saved = json.loads((c.dir / "campaign.json").read_text())
    assert saved["timeouts"] and saved["timeouts"][0]["timeout_s"] == step["timeout_s"]
    assert saved["settings"]["timeouts"]["grade_allowance_s"] == campaign.GRADE_ALLOWANCE_S
    assert "Stopped at a timeout" in (c.dir / "CAMPAIGN.md").read_text()


def test_stop_at_total_budget_after_timed_out_step(out):
    """A round whose step was stopped at the deadline ends the campaign: no new run."""
    c = make_campaign(out, budget=60, total_budget=3600)
    c.scope = ["/A", "/B"]
    calls = fake_invoker(c, [{"timed_out": True}])
    c.deadline = time.monotonic() + 0.05
    chain = c.run_chain("r1", 1, "hardfirst", [("backlog", "only", ["/A"], ["/A"]), ("rest", "rest", ["/A"], ["/B"])])
    time.sleep(0.1)
    assert chain["status"] == "error" and len(calls) == 1 and c.out_of_time()
    assert c.run_chain("r2", 2, "hardfirst", [("backlog", "only", ["/A"], ["/A"])])["status"] == "stopped"
    assert len(calls) == 1


HANG = """import os, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
open(os.environ["HANG_PID_FILE"], "w").write(str(child.pid))
time.sleep(120)
"""


def test_invoke_timeout_stops_the_whole_step(out, tmp_path, monkeypatch):
    """A hung step and the processes it started (a backend, kicad-cli) are stopped."""
    script = tmp_path / "hang.py"
    script.write_text(HANG)
    pid_file = tmp_path / "child.pid"
    monkeypatch.setenv("HANG_PID_FILE", str(pid_file))
    monkeypatch.setattr(campaign, "CLI", [sys.executable, str(script)])
    c = make_campaign(out)
    t0 = time.monotonic()
    rec, _ = c.invoke(["run"], "hang", timeout=2)
    assert rec["timed_out"] and rec["timeout_s"] == 2 and rec["exit_code"] != 0
    assert time.monotonic() - t0 < 30
    assert c.timeouts and c.timeouts[0]["log"] == rec["log"] and not c.procs
    child = int(pid_file.read_text())
    for _ in range(50):
        if not Path(f"/proc/{child}").exists() or "Z" in Path(f"/proc/{child}/stat").read_text().split()[2]:
            break
        time.sleep(0.1)
    else:
        pytest.fail(f"the step's child {child} survived the timeout")


def test_invoke_without_timeout_runs_to_the_end(out, tmp_path, monkeypatch):
    script = tmp_path / "ok.py"
    script.write_text("print('[route] hello')\n")
    monkeypatch.setattr(campaign, "CLI", [sys.executable, str(script)])
    c = make_campaign(out)
    rec, text = c.invoke(["run"], "ok")
    assert rec["exit_code"] == 0 and not rec["timed_out"] and text.strip() == "[route] hello"
    assert not c.timeouts


@pytest.mark.parametrize("defect", [None, "refill", "shorting_items", "clearance", "net_completion",
                                   "missing_preservation", "missing_delta"])
def test_native_grade_evidence_controls_campaign_eligibility(out, defect):
    c = make_campaign(out)
    run_dir = out / "run"
    run_dir.mkdir()
    preservation = dict.fromkeys([
        "project_files", "board_outline", "layers", "stackup", "board_setup", "nets_and_netclasses",
        "pad_connectivity", "footprints", "zones", "keepouts", "copper_graphics", "protected_copper",
        "new_copper_netclass_geometry", "routing_scope", "net_completion", "pour_integrity"], "PASS")
    v = {"status": "PASS", "reasons": [], "preservation": preservation,
         "validated_board_sha256": "a" * 64, "candidate_board_sha256": "a" * 64,
         "drc_commands": [{"argv": ["kicad-cli", "pcb", "drc", "--refill-zones", "--save-board"], "exit_code": 0},
                          {"argv": ["kicad-cli", "pcb", "drc", "--schematic-parity", "--severity-all"], "exit_code": 0}],
         "drc": {"new_violations": {"count": 0, "by_type": {}}, "new_parity": {"count": 0}}}
    if defect == "refill":
        v["drc_commands"][0]["exit_code"] = 1
    elif defect in ("shorting_items", "clearance"):
        v["drc"]["new_violations"] = {"count": 1, "by_type": {defect: 1}}
    elif defect == "net_completion":
        v["preservation"]["net_completion"] = "FAIL"
    elif defect == "missing_preservation":
        del v["preservation"]["protected_copper"]
    elif defect == "missing_delta":
        del v["drc"]["new_violations"]
    # Leave exit/status nominal to prove selection uses the underlying required evidence.
    common.write_json(run_dir / "result.json", {
        "exit_code": 0, "candidate": {"board": str(run_dir / "candidate.kicad_pcb")},
        "validation": v, "routing": {"status": "COMPLETE", "unconnected_in_scope": 0,
                                       "unconnected_total": 0, "unrouted_nets": []}})
    chain = {"id": "r0", "steps": [{"run_dir": str(run_dir)}]}
    c.grade(chain)
    assert (campaign.eligibility(chain)["status"] == "eligible") == (defect is None)
    if defect in ("shorting_items", "clearance"):
        assert chain["grade"]["validation"]["new_violation_types"] == {defect: 1}


def test_invalid_complete_campaign_retains_captured_original(out, tmp_path, monkeypatch):
    c = make_campaign(out, rounds=0)
    source = FIXTURE / "route_demo.kicad_pcb"
    original = source.read_bytes()
    # Simulate a native child process at the subprocess boundary. The campaign still
    # reads its actual machine record, attempt, and grade from disk.
    script = tmp_path / "invalid_child.py"
    script.write_text('''import json
from pathlib import Path
from kicad_route import cli
run_dir = Path(''' + repr(str(out / "invalid-run")) + ''')
run_dir.mkdir()
candidate_dir = run_dir / "candidate"
candidate_dir.mkdir()
(candidate_dir / "route_demo.kicad_pcb").write_text("invalid candidate")
manifest = json.loads(Path(''' + repr(str(c.dir / "input" / "snapshot.json")) + ''').read_text())
attempt = {"source": {}, "input_fingerprint": cli.input_fingerprint(manifest),
           "board": "route_demo.kicad_pcb", "scope": {"nets": ["/A"]}}
res = {"kind": "route", "exit_code": 3, "run_dir": str(run_dir),
       "process": {"status": "PASS"}, "runtime_s": 1,
       "candidate": {"dir": str(candidate_dir), "board": str(candidate_dir / "route_demo.kicad_pcb")},
       "routing": {"status": "COMPLETE", "unconnected_in_scope": 0, "unrouted_nets": []},
       "validation": {"status": "FAIL", "reasons": ["new clearance finding"],
                      "drc": {"new_violations": {"count": 1}, "new_parity": {"count": 0}}}}
(run_dir / "attempt.json").write_text(json.dumps(attempt))
(run_dir / "result.json").write_text(json.dumps(res))
print(json.dumps(res))
''')
    monkeypatch.setattr(campaign, "CLI", [sys.executable, str(script)])
    rec = c.execute()
    assert rec["best"] is None and rec["exit_code"] == 1
    assert rec["selection"]["status"] == "no_eligible_candidate"
    assert rec["stop_reason"] != "complete in round 0"
    assert len(rec["leaderboard"]) == 1 and rec["leaderboard"][0]["eligibility"]["status"] == "ineligible"
    baseline = Path(rec["selection"]["baseline"]["board"])
    assert baseline.read_bytes() == original == source.read_bytes()
    assert rec["schema"] == "kicad-route/route-campaign/2"
    schema = json.loads((Path(__file__).parents[1] / "schemas" / "result-campaign-2.json").read_text())
    jsonschema.validate(rec, schema)
