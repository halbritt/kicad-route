"""Which nets get engine passes of their own, and what each pass may spend (plane_nets.py)."""
import shutil

import pytest

from helpers import FIXTURE, run
from kicad_route import cli as route
from kicad_route import plane_nets

LAYERS = [{"name": "F.Cu", "type": 0}, {"name": "GND", "type": 0}, {"name": "PWR", "type": 0}, {"name": "B.Cu", "type": 0}]
FACTS = {"copper_layers": LAYERS,
         "zones": [{"net": "GND", "layers": ["GND"]}, {"net": "GND", "layers": ["B.Cu"]},
                   {"net": "+3V3", "layers": ["PWR"]}, {"net": "/SW", "layers": ["F.Cu"]},
                   {"net": "", "layers": ["GND"]}]}
SCOPE = ["/A", "/B", "/SW", "GND", "+3V3", "+5V"]
FIRST = {"plane_nets": "first"}


def test_the_nets_with_a_zone_on_an_inner_layer_own_a_plane():
    assert plane_nets.owners(FACTS) == {"GND", "+3V3"}      # /SW pours an outer layer only
    assert plane_nets.nets(SCOPE, FACTS, FIRST) == ["+3V3", "GND"]


def test_a_two_layer_board_has_no_plane_however_many_pours():
    two = {"copper_layers": [LAYERS[0], LAYERS[3]], "zones": [{"net": "GND", "layers": ["F.Cu", "B.Cu"]}]}
    assert plane_nets.owners(two) == set() and plane_nets.nets(["/A", "GND"], two, FIRST) == []


def test_one_pass_unless_asked_and_when_passes_of_their_own_would_change_nothing():
    assert plane_nets.nets(SCOPE, FACTS, {}) == [] and plane_nets.nets(SCOPE, FACTS, {"plane_nets": "together"}) == []
    assert plane_nets.nets(["/A", "/B"], FACTS, FIRST) == []           # no net of the scope owns a plane
    assert plane_nets.nets(["GND", "+3V3"], FACTS, FIRST) == []        # nothing else to route
    assert plane_nets.nets(SCOPE, {"zones": []}, FIRST) == [] and plane_nets.nets(SCOPE, {}, FIRST) == []


def test_bus_and_length_matched_runs_keep_one_pass():
    assert plane_nets.nets(SCOPE, FACTS, {**FIRST, "bus": {}}) == []
    assert plane_nets.nets(SCOPE, FACTS, {**FIRST, "length_match": {"groups": []}}) == []


def test_the_extra_passes_take_no_time_from_the_runs_own_pass_and_the_budget_is_never_exceeded():
    assert plane_nets.first_budget(300) == 100 and plane_nets.first_budget(1) == 1
    assert plane_nets.main_budget(300, 21.4) == 278 and plane_nets.main_budget(300, None) == 300
    assert plane_nets.main_budget(30, 40) == 1
    # the run's own pass is asked to stop before its budget ends; what it then leaves is the last pass's
    assert plane_nets.last_budget(300, 21.4, 219.0) == 59 and plane_nets.last_reserve(59) == pytest.approx(5.9)
    assert plane_nets.last_budget(300, 21.4, 271.0) is None        # under ten seconds: not started


# ------------------------------------------------------------- in a real run

@pytest.fixture(scope="module")
def plane_dir(tmp_path_factory):
    """The fixture as a four-layer board whose ground pour is a plane on the first inner layer."""
    import pcbnew
    d = tmp_path_factory.mktemp("plane") / "route_demo"
    shutil.copytree(FIXTURE, d, ignore=shutil.ignore_patterns("generate.py", "__pycache__"))
    board = pcbnew.LoadBoard(str(d / "route_demo.kicad_pcb"))
    board.SetCopperLayerCount(4)
    for zone in board.Zones():
        if zone.GetNetname() == "GND" and not zone.GetIsRuleArea():
            zone.SetLayer(pcbnew.In1_Cu)
    board.Save(str(d / "route_demo.kicad_pcb"))
    return d


def engine_passes(result):
    return [c for c in result["process"]["commands"] if any(str(a).endswith("route.py") for a in c.get("argv", []))]


def nets_of(command):
    argv = command["argv"]
    start = argv.index("--nets") + 1
    end = next((k for k in range(start, len(argv)) if argv[k].startswith("--")), len(argv))
    return argv[start:end]


def test_a_run_routes_the_plane_net_first_in_its_own_pass_and_then_the_whole_scope(plane_dir):
    proc, run_dir, result = run("run", "--budget", "180", "--plane-nets", "first", "--label", "plane-nets-first",
                                "--dir", str(plane_dir))
    assert result["process"]["status"] == "PASS", result["process"]["reasons"]
    planes = result["process"]["plane_nets"]
    assert planes["nets"] == ["GND"] and planes["first"]["status"] == "routed" and planes["first"]["budget_s"] == 60
    passes = engine_passes(result)
    assert [nets_of(c) for c in passes] == [["GND"], ["*"]]
    # the run's own pass got all the first left, ended on its own, and so needed no last pass
    assert passes[1]["budget_s"] == int(180 - planes["first"]["used_s"])
    assert not passes[1].get("stop_requested") and planes["last"] is None
    assert (run_dir / "work" / "krt-first.log").is_file()
    assert result["settings"]["plane_nets"] == "first"
    assert result["routing"]["status"] == "COMPLETE" and result["validation"]["status"] == "PASS", result["validation"]["reasons"]
    assert result["routing"]["plane_nets_open"] == {"connections": 0, "nets": []} and route.plane_nets_hint(result) is None
    assert proc.returncode == 0


def test_without_the_option_a_board_with_a_plane_is_routed_in_one_pass_and_the_result_counts_its_open_plane_pads(plane_dir):
    proc, run_dir, result = run("run", "--budget", "180", "--label", "plane-nets-together", "--dir", str(plane_dir))
    assert "plane_nets" not in result["settings"]         # the default is not recorded
    assert "plane_nets" not in result["process"] and len(engine_passes(result)) == 1
    assert result["routing"]["plane_nets_open"] == {"connections": 0, "nets": []}


def test_the_two_layer_fixture_keeps_its_one_pass_when_asked_for_plane_nets_first():
    proc, run_dir, result = run("run", "--budget", "180", "--plane-nets", "first", "--label", "pour-not-plane")
    assert "plane_nets" not in result["process"] and len(engine_passes(result)) == 1
    assert "plane_nets_open" not in result["routing"]     # a board without a plane has nothing to say


def test_a_result_with_open_plane_pads_says_what_to_ask_for_unless_it_was_asked():
    result = {"routing": {"unconnected_in_scope": 56, "plane_nets_open": {"connections": 50, "nets": ["+3.3V", "GND"]}},
              "process": {}}
    hint = route.plane_nets_hint(result)
    assert hint.startswith("50 of the 56 open connections are on nets that own a plane (+3.3V, GND)")
    assert "--plane-nets first" in hint
    assert route.plane_nets_hint({**result, "process": {"plane_nets": {"nets": ["GND"]}}}) is None
    assert route.plane_nets_hint({"routing": {"plane_nets_open": {"connections": 0, "nets": []}}, "process": {}}) is None
    assert route.plane_nets_hint({"routing": {}, "process": {}}) is None


def stub_engine(monkeypatch, writes):
    seen = []

    def engine(argv, cwd, log_path, budget, env, **kwargs):
        argv = [str(a) for a in argv]
        seen.append({"argv": argv, "budget": budget, "env": dict(env)})
        if writes:
            out = argv[argv.index("--output") + 1]
            shutil.copyfile(argv[2], out)
            with open(out, "a") as fh:
                fh.write("\n; routed by the stub\n")
        return {"exit_code": 0 if writes else 1, "log": "krt-x.log", "cpu_s": 2.0, "duration_s": 3.0}
    monkeypatch.setattr(route, "run_budgeted", engine)
    return seen


def pass_inputs(tmp_path):
    board = tmp_path / "input" / "route_demo.kicad_pcb"
    board.parent.mkdir()
    shutil.copyfile(FIXTURE / "route_demo.kicad_pcb", board)
    argv = ["python", "route.py", "SOURCE", "--output", "OUT", "--json-out", "SUMMARY", "--nets", "*", "--via-cost", "600"]
    env = {"KICAD_STOP_FILE": "main-stop", "KICAD_EFFECTIVE_CONFIG_REQUEST": "request.json", "KICAD_CLASS_VIA_ONLY": "1"}
    return board, argv, env, {"nets": {"GND": {}, "/SIG_A": {}}}


def test_a_pass_is_the_runs_own_command_with_its_own_board_scope_stop_file_and_no_configuration_record(tmp_path, monkeypatch):
    board, argv, env, facts = pass_inputs(tmp_path)
    seen = stub_engine(monkeypatch, writes=True)
    record = route.krt_plane_pass("last", tmp_path, board, argv, env, ["GND"], facts,
                                  {"budget_s": 90, "budget_clock": "cpu"}, 41)
    assert record["status"] == "routed" and record["used_s"] == 2.0 and record["budget_s"] == 41
    assert record["output"] == tmp_path / "krt-last" / "route_demo.kicad_pcb" and record["output"].is_file()
    call = seen[0]
    assert call["budget"] == 41 and call["argv"][2] == str(board)
    assert call["argv"][call["argv"].index("--nets") + 1:] == ["GND", "--via-cost", "600"]
    assert call["env"]["KICAD_STOP_FILE"].endswith("STOP-last") and "KICAD_EFFECTIVE_CONFIG_REQUEST" not in call["env"]
    assert call["env"]["KICAD_CLASS_VIA_ONLY"] == "1"
    assert argv[2] == "SOURCE" and argv[argv.index("--nets") + 1] == "*"     # the run's own command is untouched


def test_a_pass_that_fails_says_why_and_leaves_the_work_board_alone(tmp_path, monkeypatch):
    board, argv, env, facts = pass_inputs(tmp_path)
    before = board.read_bytes()
    stub_engine(monkeypatch, writes=False)
    record = route.krt_plane_pass("first", tmp_path, board, argv, env, ["GND"], facts, {"budget_s": 90}, 45)
    assert record["status"] == "failed" and "exited 1" in record["reason"] and "output" not in record
    assert record["used_s"] == 3.0                         # wall seconds without --budget-clock cpu
    assert board.read_bytes() == before


def test_when_the_budget_ends_the_routing_the_plane_nets_get_a_last_pass_on_the_routed_board(plane_dir, tmp_path, monkeypatch):
    """The three passes in order, with a stub in the engine's place: the stub's second pass says the
    budget stopped it, as the engine does when it skips its closing repair of the planes."""
    work = tmp_path / "work"
    shutil.copytree(plane_dir, work / "input" / "route_demo")
    board = work / "input" / "route_demo" / "route_demo.kicad_pcb"
    facts, _ = route.board_facts(board, tmp_path / "facts.json")
    calls = []

    def engine(argv, cwd, log_path, budget, env, **kwargs):
        argv = [str(a) for a in argv]
        shutil.copyfile(argv[2], argv[argv.index("--output") + 1])
        calls.append({"nets": nets_of({"argv": argv}), "budget": budget, "log": log_path.name,
                      "quick": argv[-2:] == ["--quick", "1"], "reserve": kwargs.get("reserve")})
        log_path.write_text("")
        return {"argv": argv, "exit_code": 0, "log": log_path.name, "cpu_s": 10.0, "duration_s": 12.0,
                "stop_requested": len(calls) == 2}
    monkeypatch.setattr(route, "run_budgeted", engine)
    settings = {"protect": "existing", "budget_s": 100, "budget_clock": "cpu", "mode": "all",
                "backend_args": [], "backend_env": {}, "plane_nets": "first"}
    proc = route.krt_route(work, board, sorted(n for n in facts["nets"] if n), facts, settings)
    assert proc["status"] == "PASS", proc["reasons"]
    assert [(c["nets"], c["budget"], c["log"]) for c in calls] == [
        (["GND"], 33, "krt-first.log"), (["*"], 90, "krt.log"), (["GND"], 80, "krt-last.log")]
    assert calls[2]["reserve"] == pytest.approx(8.0) and calls[1]["reserve"] is None
    assert not any(c["quick"] for c in calls)              # every pass is the engine's whole procedure
    planes = proc["plane_nets"]
    assert planes["first"]["status"] == planes["last"]["status"] == "routed" and "output" not in planes["last"]
    assert [c["log"] for c in proc["commands"] if c.get("log", "").startswith("krt")] == [
        "krt-first.log", "krt.log", "krt-last.log"]
    # the run delivers the last pass's board; its own pass's board stays where its configuration record says
    assert proc["output_board"] == work / "krt-last" / "route_demo.kicad_pcb" and proc["output_board"].is_file()
    assert (work / "krt-out" / "route_demo.kicad_pcb").is_file()
    assert proc["effective_configuration"]["wrapper_output_board_sha256"] == route.common.sha256(proc["output_board"])


def test_a_routing_that_ends_on_its_own_gets_no_last_pass(plane_dir, tmp_path, monkeypatch):
    work = tmp_path / "work"
    shutil.copytree(plane_dir, work / "input" / "route_demo")
    board = work / "input" / "route_demo" / "route_demo.kicad_pcb"
    facts, _ = route.board_facts(board, tmp_path / "facts.json")
    calls = []

    def engine(argv, cwd, log_path, budget, env, **kwargs):
        argv = [str(a) for a in argv]
        shutil.copyfile(argv[2], argv[argv.index("--output") + 1])
        calls.append(log_path.name)
        log_path.write_text("")
        return {"argv": argv, "exit_code": 0, "log": log_path.name, "cpu_s": 10.0, "duration_s": 12.0}
    monkeypatch.setattr(route, "run_budgeted", engine)
    settings = {"protect": "existing", "budget_s": 100, "mode": "all", "backend_args": [], "backend_env": {}, "plane_nets": "first"}
    proc = route.krt_route(work, board, sorted(n for n in facts["nets"] if n), facts, settings)
    assert calls == ["krt-first.log", "krt.log"] and proc["plane_nets"]["last"] is None
    assert proc["output_board"] == work / "krt-out" / "route_demo.kicad_pcb"


def test_no_last_pass_when_the_engine_made_its_own_closing_repair_before_the_budget_stopped_it(plane_dir, tmp_path, monkeypatch):
    work = tmp_path / "work"
    shutil.copytree(plane_dir, work / "input" / "route_demo")
    board = work / "input" / "route_demo" / "route_demo.kicad_pcb"
    facts, _ = route.board_facts(board, tmp_path / "facts.json")
    calls = []

    def engine(argv, cwd, log_path, budget, env, **kwargs):
        argv = [str(a) for a in argv]
        shutil.copyfile(argv[2], argv[argv.index("--output") + 1])
        calls.append(log_path.name)
        log_path.write_text('JSON_ORACLE: {"available": true, "remaining": 0}\n' if log_path.name == "krt.log" else "")
        return {"argv": argv, "exit_code": 0, "log": log_path.name, "cpu_s": 10.0, "duration_s": 12.0,
                "stop_requested": True}
    monkeypatch.setattr(route, "run_budgeted", engine)
    settings = {"protect": "existing", "budget_s": 100, "mode": "all", "backend_args": [], "backend_env": {}, "plane_nets": "first"}
    proc = route.krt_route(work, board, sorted(n for n in facts["nets"] if n), facts, settings)
    assert calls == ["krt-first.log", "krt.log"] and proc["plane_nets"]["last"] is None


def test_no_last_pass_when_the_runs_own_pass_left_under_ten_seconds(plane_dir, tmp_path, monkeypatch):
    work = tmp_path / "work"
    shutil.copytree(plane_dir, work / "input" / "route_demo")
    board = work / "input" / "route_demo" / "route_demo.kicad_pcb"
    facts, _ = route.board_facts(board, tmp_path / "facts.json")
    calls = []

    def engine(argv, cwd, log_path, budget, env, **kwargs):
        argv = [str(a) for a in argv]
        shutil.copyfile(argv[2], argv[argv.index("--output") + 1])
        calls.append(log_path.name)
        log_path.write_text("")
        return {"argv": argv, "exit_code": 0, "log": log_path.name, "cpu_s": 47.0, "duration_s": 47.0,
                "stop_requested": True}
    monkeypatch.setattr(route, "run_budgeted", engine)
    settings = {"protect": "existing", "budget_s": 100, "mode": "all", "backend_args": [], "backend_env": {}, "plane_nets": "first"}
    proc = route.krt_route(work, board, sorted(n for n in facts["nets"] if n), facts, settings)
    assert calls == ["krt-first.log", "krt.log"] and proc["plane_nets"]["last"]["status"] == "skipped"
    assert proc["status"] == "PASS" and proc["output_board"] == work / "krt-out" / "route_demo.kicad_pcb"
