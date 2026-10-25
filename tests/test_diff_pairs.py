"""Native pair routing and failure evidence; default KRT remains on route.py."""
import json
import shutil

import jsonschema
import pytest

from helpers import FIXTURE, cli, record, run, tree_hash
from kicad_route import cli as route
from kicad_route import diff_pairs
from kicad_route import ledger
from test_interface_contract import BASE_ATTEMPT_KEYS, BASE_RESULT_KEYS, PAIR_RESULT_SCHEMA


def pair_project(directory, blocked=False, single=False, width=0.25, gap=0.35):
    import pcbnew

    directory.mkdir(parents=True)
    # Keep the demo's stock stackup/outline, with only our own two-terminal nets.
    board = pcbnew.LoadBoard(str(FIXTURE / "route_demo.kicad_pcb"))
    for item in [*board.GetTracks(), *board.GetFootprints(), *board.Zones()]:
        board.Delete(item)
    for net in list(board.GetNetsByName().values()):
        if net.GetNetCode():
            board.Delete(net)
    nets = {}
    for name in ["/CAN+", "/CAN-"] + (["/AUX"] if single else []):
        net = pcbnew.NETINFO_ITEM(board, name)
        board.Add(net)
        nets[name] = net
    for ref, x in [("J1", 110), ("J2", 140)]:
        fp = pcbnew.FOOTPRINT(board)
        fp.SetReference(ref)
        fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(112)))
        board.Add(fp)
        for i, (name, net) in enumerate(nets.items()):
            pad = pcbnew.PAD(fp)
            pad.SetNumber(str(i + 1))
            pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
            pad.SetShape(pcbnew.PAD_SHAPE_RECT)
            pad.SetSize(pcbnew.VECTOR2I(pcbnew.FromMM(width), pcbnew.FromMM(width)))
            layers = pcbnew.LSET()
            layers.AddLayer(pcbnew.F_Cu)
            pad.SetLayerSet(layers)
            y = 112 + (i * (width + gap) if i < 2 else 5)
            pad.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y)))
            pad.SetNet(net)
            fp.Add(pad)
    if blocked:
        wall = pcbnew.ZONE(board)
        wall.SetIsRuleArea(True)
        layers = pcbnew.LSET()
        layers.AddLayer(pcbnew.F_Cu)
        layers.AddLayer(pcbnew.B_Cu)
        wall.SetLayerSet(layers)
        wall.SetDoNotAllowTracks(True)
        wall.SetDoNotAllowVias(True)
        wall.SetDoNotAllowZoneFills(True)
        wall.SetZoneName("pair-wall")
        outline = wall.Outline()
        outline.NewOutline()
        for x, y in [(124, 99), (126, 99), (126, 131), (124, 131)]:
            outline.Append(pcbnew.FromMM(x), pcbnew.FromMM(y))
        board.Add(wall)
    board.Save(str(directory / "pair.kicad_pcb"))
    project = json.loads((FIXTURE / "route_demo.kicad_pro").read_text())
    default = project["net_settings"]["classes"][0]
    pair_class = {**default, "name": "CAN", "track_width": 0.3,
                  "diff_pair_width": width, "diff_pair_gap": gap}
    project["net_settings"]["classes"] = [default, pair_class]
    project["net_settings"]["netclass_patterns"] = [{"netclass": "CAN", "pattern": "/CAN*"}]
    (directory / "pair.kicad_pro").write_text(json.dumps(project, indent=2) + "\n")
    (directory / "pair.kicad_dru").write_text(
        '(version 1)\n(rule "CAN pair gap" '
        f'(constraint diff_pair_gap (min {gap - .01:g}mm) (max {gap + .01:g}mm)))\n')
    return directory


@pytest.fixture(scope="module")
def pair_attempt(tmp_path_factory):
    directory = pair_project(tmp_path_factory.mktemp("diff-pair") / "pair", single=True)
    before = tree_hash(directory)
    attempt = run("run", "--backend", "krt", "--diff-pairs", "auto", "--dir", str(directory),
                  "--budget", "120", "--label", "diff-pair", "--grid-step", "0.025")
    assert tree_hash(directory) == before
    return attempt


def test_native_pair_routing_and_evidence(pair_attempt):
    process, directory, result = pair_attempt
    record("diff_pair", result)
    assert process.returncode == 0, result
    assert result["schema"] == "kicad-route/route/2"
    assert set(result) == BASE_RESULT_KEYS | {"pairs", "copper_ownership"}
    attempt = json.loads((directory / "attempt.json").read_text())
    assert attempt["schema"] == "kicad-route/attempt/2"
    assert set(attempt) == BASE_ATTEMPT_KEYS | {"schema", "pairs", "pair_routing"}
    assert result["settings"]["diff_pairs"] == "auto"
    assert result["settings"]["grid_step_mm"] == 0.025
    assert result["settings"]["pairs"]
    assert result["validation"]["status"] == "PASS", result["validation"]
    assert all(value == "PASS" for value in result["validation"]["preservation"].values())
    pair, = result["pairs"]
    assert (pair["p"], pair["n"], pair["width_mm"], pair["gap_mm"]) == ("/CAN+", "/CAN-", .25, .35)
    assert pair["routed"] is True and pair["status"] == "ROUTED"
    assert pair["backend_status"] == "COUPLED"
    assert pair["length_p"] > 29 and pair["length_n"] > 29
    assert pair["skew"] == round(abs(pair["length_p"] - pair["length_n"]), 6)
    assert pair["gap_violations"] == 0
    assert ledger.from_result(result)[0] == result["ledger"]["fingerprint"]
    drc = json.loads((directory / "validate" / "candidate-drc.json").read_text())
    assert not drc["violations"] and not drc["unconnected_items"]
    commands = result["process"]["commands"]
    diff = next(c["argv"] for c in commands if any(str(a).endswith("route_diff.py") for a in c["argv"]))
    single = next(c["argv"] for c in commands if any(str(a).endswith("/route.py") for a in c["argv"]))
    assert diff[diff.index("--diff-pair-gap") + 1] == "0.35"
    assert diff[diff.index("--grid-step") + 1] == "0.025"
    assert single[single.index("--grid-step") + 1] == "0.025"
    assert single[single.index("--nets") + 1:] == ["/AUX"]
    # The separate remaining-net pass neither moves nor unlocks the pair permanently.
    facts = json.loads((directory / "validate" / "candidate-facts.json").read_text())
    assert not any(t["locked"] for t in facts["tracks"])
    jsonschema.validate(result, PAIR_RESULT_SCHEMA)


def test_short_native_pair_keeps_explicit_coupling_policy(tmp_path):
    import pcbnew
    directory = pair_project(tmp_path / 'short-pair')
    path = directory / 'pair.kicad_pcb'
    board = pcbnew.LoadBoard(str(path))
    next(fp for fp in board.GetFootprints() if fp.GetReference() == 'J2').SetPosition(
        pcbnew.VECTOR2I(pcbnew.FromMM(112), pcbnew.FromMM(112)))
    project = path.with_suffix('.kicad_pro').read_bytes()
    pcbnew.SaveBoard(str(path), board)
    path.with_suffix('.kicad_pro').write_bytes(project)
    before = tree_hash(directory)
    process, _, result = run('run', '--backend', 'builtin', '--diff-pairs', 'auto',
                             '--dir', str(directory), '--budget', '60', '--label', 'short-pair')
    assert process.returncode == 0, result
    assert tree_hash(directory) == before
    pair, = result['pairs']
    assert pair['routed'] and pair['backend_status'] == 'COUPLED'
    assert result['routing']['status'] == 'COMPLETE'
    assert result['validation']['status'] == 'PASS'


def test_pair_revalidation_measures_current_candidate(pair_attempt, tmp_path):
    _, directory, original = pair_attempt
    copied = tmp_path / "edited"
    shutil.copytree(directory.resolve(), copied, symlinks=True)
    attempt = json.loads((copied / "attempt.json").read_text())
    attempt["baseline"]["report"] = str((directory / attempt["baseline"]["report"]).resolve())
    (copied / "attempt.json").write_text(json.dumps(attempt) + "\n")
    import pcbnew
    board_path = copied / "candidate" / "pair" / "pair.kicad_pcb"
    board = pcbnew.LoadBoard(str(board_path))
    for track in list(board.GetTracks()):
        if track.GetNetname() == "/CAN+":
            board.Delete(track)
    project = board_path.with_suffix(".kicad_pro").read_bytes()
    board.Save(str(board_path))
    board_path.with_suffix(".kicad_pro").write_bytes(project)
    process = cli("validate", str(copied), "--json")
    result = json.loads(process.stdout)
    assert process.returncode == 2, result
    assert result["schema"] == "kicad-route/route-validate/2"
    jsonschema.validate(result, PAIR_RESULT_SCHEMA)
    pair, = result["pairs"]
    assert pair["routed"] is False and pair["status"] == "INCOMPLETE"
    assert pair["length_p"] == 0
    assert pair["length_n"] == original["pairs"][0]["length_n"]
    assert pair["skew"] == pair["length_n"]


@pytest.mark.parametrize("field", ["schema", "pairs", "settings.diff_pairs", "settings.pairs"])
def test_pair_schema_requires_version_and_pair_fields(pair_attempt, field):
    result = json.loads(json.dumps(pair_attempt[2]))
    if field.startswith("settings."):
        del result["settings"][field.split(".")[1]]
    elif field == "schema":
        result[field] = "kicad-route/route/1"
    else:
        del result[field]
    assert list(jsonschema.Draft202012Validator(PAIR_RESULT_SCHEMA).iter_errors(result))


def test_empty_pair_plan_still_uses_version_2(tmp_path):
    process, directory, result = run("run", "--backend", "krt", "--diff-pairs", "auto",
                                   "--dir", str(FIXTURE), "--nets", "/SIG_A*", "--budget", "120",
                                   "--default-clearance", "0.4", "--label", "diff-pair-empty")
    assert process.returncode == 0, result
    assert result["schema"] == "kicad-route/route/2" and result["pairs"] == []
    attempt = json.loads((directory / "attempt.json").read_text())
    assert attempt["schema"] == "kicad-route/attempt/2" and attempt["pairs"] == []
    assert attempt["pair_routing"] == {}
    jsonschema.validate(result, PAIR_RESULT_SCHEMA)
    process = cli("validate", str(directory), "--json")
    revalidated = json.loads(process.stdout)
    assert process.returncode == 0, revalidated
    assert revalidated["schema"] == "kicad-route/route-validate/2" and revalidated["pairs"] == []
    jsonschema.validate(revalidated, PAIR_RESULT_SCHEMA)


def test_unroutable_pair_is_incomplete(tmp_path):
    source = pair_project(tmp_path / "pair", blocked=True)
    process, directory, result = run("run", "--backend", "krt", "--diff-pairs", "auto", "--dir", str(source),
                                   "--budget", "90", "--max-iterations", "1000", "--label", "diff-pair-blocked")
    record("diff_pair_blocked", result)
    assert process.returncode == 2, result
    assert result["validation"]["status"] == "PASS", result["validation"]
    pair, = result["pairs"]
    assert pair["routed"] is False and pair["status"] == "INCOMPLETE"
    assert pair["length_p"] == pair["length_n"] == 0
    assert result["routing"]["unrouted_nets"] == ["/CAN+", "/CAN-"]
    assert not any(any(str(a).endswith("/route.py") for a in c["argv"])
                   for c in result["process"]["commands"])


def native_nets():
    return {name: {"netclass": "CAN", "width": 250000, "gap": 250000, "clearance": 200000,
                   "pad_count": 2, "length": 30000000} for name in ("/CAN+", "/CAN-")}


@pytest.mark.parametrize("case", ["unpaired", "class", "scope", "width", "gap", "clearance"])
def test_detection_refuses_ambiguous_or_incompatible_pairs(case):
    nets = native_nets()
    scope = set(nets)
    if case == "unpaired":
        del nets["/CAN-"]
        scope.remove("/CAN-")
    elif case == "scope":
        scope.remove("/CAN-")
    elif case == "class":
        nets["/CAN-"]["netclass"] = "Other"
    elif case == "clearance":
        nets["/CAN-"]["clearance"] = 300000
    else:
        nets["/CAN-"][case] = None
    with pytest.raises(ValueError):
        diff_pairs.detect(nets, scope)


def test_pair_detection_conventions_are_exact_and_sorted():
    nets = {name: next(iter(native_nets().values())).copy() for name in
            ("/b/D_P", "/b/D_N", "/a/D+", "/a/D-", "/c/TXP", "/c/TXN", "VIN", "GND")}
    assert [(p["p"], p["n"]) for p in diff_pairs.detect(nets, set(nets))] == [
        ("/a/D+", "/a/D-"), ("/b/D_P", "/b/D_N"), ("/c/TXP", "/c/TXN")]


@pytest.mark.parametrize("outcome", ["REJECTED", "NOT_RUN", "PROCESS_FAILED"])
def test_connected_members_cannot_hide_failed_pair_routing(outcome):
    nets = native_nets()
    pairs = diff_pairs.detect(nets, set(nets))
    records = diff_pairs.grade(pairs, {"/CAN+": {"status": outcome}}, nets,
                               {"unconnected_items": [], "violations": []})
    assert records[0]["routed"] is False
    routing = {"status": "COMPLETE", "unrouted_nets": []}
    diff_pairs.apply_routing(routing, records)
    assert routing["status"] == "INCOMPLETE"


def test_native_gap_violation_cannot_be_hidden():
    nets = native_nets()
    pairs = diff_pairs.detect(nets, set(nets))
    drc = {"unconnected_items": [], "violations": [
        {"type": "diff_pair_gap_out_of_range", "items": [{"description": "Track [/CAN+] on F.Cu"}]}]}
    pair, = diff_pairs.grade(pairs, {"/CAN+": {"status": "COUPLED"}}, nets, drc)
    assert pair["gap_violations"] == 1 and pair["routed"] is False


def test_unidentified_native_connection_cannot_establish_pair_completion():
    nets = native_nets()
    drc = {"unconnected_items": [{"items": [{"description": "Unknown [<no net>]"}]}], "violations": []}
    pair, = diff_pairs.grade(diff_pairs.detect(nets, set(nets)), {"/CAN+": {"status": "COUPLED"}}, nets, drc)
    assert pair["routed"] is False


@pytest.mark.parametrize("outcome,extra,fallback", [
    ("deferred", {}, []), ("partial", {}, []), ("coupled", {"escape": "hybrid"}, []),
    ("coupled", {}, ["/CAN+"]), (None, {}, []),
])
def test_backend_fallback_is_discarded_and_missing_coverage_fails(tmp_path, monkeypatch, outcome, extra, fallback):
    # Stub only the external KRT executable, so deliberate fallback output is
    # distinguishable from the input even when it would claim full connectivity.
    engine = tmp_path / "engine"
    (engine / "py_router").mkdir(parents=True)
    member = {"p_net": "/CAN+", "n_net": "/CAN-", "outcome": outcome, **extra}
    report = {"pair_reports": [member], "single_ended_fallback": {"routed": fallback}} if outcome else None
    (engine / "py_router" / "route_diff.py").write_text(
        'import json, sys\nfrom pathlib import Path\n'
        'Path(sys.argv[sys.argv.index("--output") + 1]).write_text("SINGLE-ENDED FALLBACK")\n'
        f'print("JSON_SUMMARY: " + json.dumps({report!r}))\n')
    monkeypatch.setattr(route, "KRT_DIR", engine)
    source = tmp_path / "source"
    source.mkdir()
    board = source / "pair.kicad_pcb"
    board.write_text("ORIGINAL BOARD")
    nets = native_nets()
    settings = {"protect": "locked", "pairs": diff_pairs.detect(nets, set(nets)),
                "budget_s": 30, "backend_env": {}}
    work = tmp_path / "work"
    work.mkdir()
    proc = diff_pairs.route(work, board, sorted(nets), {"nets": nets}, settings, route)
    if outcome is None:
        assert proc["status"] == "FAIL" and proc["output_board"] is None
        assert "no usable pair report" in proc["reasons"][0]
    else:
        assert proc["status"] == "PASS"
        assert proc["pair_routing"]["/CAN+"]["status"] == "REJECTED"
        assert proc["output_board"].read_text() == "ORIGINAL BOARD"


@pytest.mark.parametrize("args", [("--backend", "freerouting"),
                                  ("--backend", "krt", "--backend-arg=--nets=*")])
def test_incompatible_pair_options_are_usage_refusals(tmp_path, args):
    process = cli("run", *args, "--diff-pairs", "auto", "--dir", str(FIXTURE), "--json",
                  "--run-dir", str(tmp_path / "must-not-exist"))
    assert process.returncode == 4
    assert json.loads(process.stdout)["reason_class"] == "usage"
    assert not (tmp_path / "must-not-exist").exists()


@pytest.mark.parametrize('args', [
    ['--grid-step', '0'], ['--grid-step', '-0.1'], ['--grid-step', 'nan'],
    ['--grid-step', 'inf'], ['--grid-step', '0.0000001'],
    ['--backend', 'freerouting', '--grid-step', '0.025'],
    ['--grid-step', '0.025', '--backend-arg=--grid-step=0.1'],
])
def test_grid_resolution_refuses_invalid_or_ambiguous_controls(args):
    process = cli('run', '--backend', 'builtin', '--dir', str(FIXTURE), '--json', *args)
    assert process.returncode == 4, process.stdout + process.stderr
    assert '--grid-step' in process.stdout + process.stderr
    assert json.loads(process.stdout)["reason_class"] == "usage"


@pytest.mark.parametrize("amplitude,accepted", [(1, False), (2, True)])
def test_asymmetric_intra_pair_matching_uses_saved_copper(tmp_path, amplitude, accepted):
    from pathlib import Path
    source = Path(__file__).resolve().parents[1] / "fixtures/pair-skew"
    directory = tmp_path / "skew"
    shutil.copytree(source, directory)
    before = tree_hash(directory)
    process, run_dir, result = run("run", "--backend", "builtin", "--diff-pairs", "auto",
        "--diff-pair-intra-match", "--length-match-tolerance", "0.1",
        "--meander-amplitude", str(amplitude), "--grid-step", "0.025",
        "--dir", str(directory), "--budget", "180", "--label", f"match-{amplitude}")
    assert tree_hash(directory) == before
    assert result["settings"]["intra_pair_match"] == {"tolerance_mm": .1, "amplitude_mm": amplitude}
    pair, = result["pairs"]
    assert pair["matching"]["status"] == ("PASS" if accepted else "FAIL")
    assert pair["routed"] is True  # Connectivity is distinct from requested matching.
    drc = json.loads((run_dir / "validate/candidate-drc.json").read_text())
    assert not drc["unconnected_items"]
    assert bool(drc["violations"]) is not accepted
    if accepted:
        assert process.returncode == 0, result
        assert pair["skew"] < .1
        assert result["validation"]["status"] == "PASS"
    else:
        assert process.returncode != 0
        assert pair["skew"] > .15
        assert {v["type"] for v in drc["violations"]} == {"skew_out_of_range"}
    rechecked = cli("validate", str(run_dir), "--json")
    assert json.loads(rechecked.stdout)["pairs"][0]["matching"] == pair["matching"]
    assert (rechecked.returncode == 0) is accepted
    jsonschema.validate(result, PAIR_RESULT_SCHEMA)
    if accepted:
        # A tighter requested target must fail independently of native DRC and
        # the historical backend success report. No copper or rules are edited.
        attempt_path = run_dir / "attempt.json"
        attempt = json.loads(attempt_path.read_text())
        attempt["settings"]["intra_pair_match"]["tolerance_mm"] = .000001
        attempt_path.write_text(json.dumps(attempt))
        tighter = cli("validate", str(run_dir), "--json")
        tightened = json.loads(tighter.stdout)
        assert tighter.returncode == 3, tightened
        assert tightened["pairs"][0]["matching"]["status"] == "FAIL"
        assert tightened["routing"]["status"] == "COMPLETE"
        assert any("intra-pair matching failed" in reason for reason in tightened["validation"]["reasons"])
        jsonschema.validate(tightened, PAIR_RESULT_SCHEMA)


@pytest.mark.parametrize("extra", [
    ["--meander-amplitude", "2"], ["--length-match-tolerance", "0.1"],
    ["--diff-pair-intra-match"],
    ["--diff-pairs", "auto", "--diff-pair-intra-match", "--meander-amplitude", "nan"],
    ["--diff-pairs", "auto", "--diff-pair-intra-match", "--length-match-tolerance", "0"],
])
def test_invalid_matching_controls_refused(extra, tmp_path):
    process = cli("run", "--backend", "builtin", "--dir", str(FIXTURE),
                  "--run-dir", str(tmp_path / "refused"), "--json", *extra)
    assert process.returncode == 4, process.stdout + process.stderr
    assert not (tmp_path / "refused").exists()


def test_saved_length_over_target_overrides_backend_match():
    pair = {"p": "D+", "n": "D-"}
    outcomes = {"D+": {"status": "COUPLED", "report": {"intra_pair_matching": [{"status": "matched"}]}}}
    nets = {"D+": {"length": 10000000}, "D-": {"length": 10120000}}
    records = diff_pairs.grade([pair], outcomes, nets,
                              {"violations": [], "unconnected_items": []}, {"tolerance_mm": .1})
    assert records[0]["matching"]["status"] == "FAIL"
    assert records[0]["routed"] is True
