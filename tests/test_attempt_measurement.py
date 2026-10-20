"""Native candidates keep their grade when wall-clock evidence disqualifies timing."""
import json
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest

from helpers import FIXTURE, ROOT, tree_hash
from kicad_route import cli, common, determinism, statemanifest
from test_diff_pairs import pair_project


@pytest.mark.parametrize("pair_mode", [False, True], ids=["single-budget", "pair-stall-before-tail"])
def test_native_attempt_stop_disqualifies_measurement_and_survives_revalidation(
        tmp_path, monkeypatch, pair_mode):
    out = tmp_path / "out"
    monkeypatch.setattr(common, "_out", out)
    monkeypatch.setenv("KICAD_ROUTE_LEDGER", "off")
    monkeypatch.setattr(statemanifest, "reproducibility", lambda backend, **kwargs: {
        "reproducible": True, "measurement": True, "basis": "installed build repeat passed"})
    project = pair_project(tmp_path / "pair", single=True) if pair_mode else FIXTURE
    before = tree_hash(project)
    runner = cli.run_budgeted
    flag = "stalled" if pair_mode else "budget_reached"
    script = "route_diff.py" if pair_mode else "route.py"
    injected = []
    completed = []

    def completed_then_stopped(argv, *args, **kwargs):
        # Run real routing and retain its native board. Only its timing metadata
        # is changed after the process has finished and has been reaped.
        rec = runner(argv, *args, **kwargs)
        completed.append(rec)
        assert rec["exit_code"] == 0, rec
        assert not determinism.wall_clock_stops({"commands": [rec]}), rec
        if any(Path(str(arg)).name == script for arg in argv):
            rec[flag] = True
            rec["stop_requested"] = True
            injected.append(rec)
        return rec

    monkeypatch.setattr(cli, "run_budgeted", completed_then_stopped)
    run = tmp_path / "run"
    options = {"diff_pairs": "auto"} if pair_mode else {"nets": ["/SIG_A*"]}
    result = cli.run_attempt(cli.run_namespace(
        "krt", dir=str(project), run_dir=str(run), budget=240,
        default_clearance=None if pair_mode else 0.4, **options))
    assert len(injected) == 1
    assert result["backend"]["reproducible"] is True
    assert result["measurement"] is False
    qualification = result["process"]["measurement_qualification"]
    assert qualification["measurement"] is False and qualification["build_reproducible"] is True
    assert qualification["reasons"] == [f"wall-clock stop: {flag}"]
    assert result["process"]["status"] == "PASS"
    assert result["routing"]["status"] == "COMPLETE"
    assert result["validation"]["status"] == "PASS"
    assert result["exit_code"] == 0
    assert all(value == "PASS" for value in result["validation"]["preservation"].values())
    board = out / result["candidate"]["board"]
    assert board.is_file()
    assert common.sha256(board) == result["validation"]["candidate_board_sha256"]
    assert common.load_json(run / "result.json") == result
    schema = json.loads((ROOT / "schemas" / f"result-{2 if pair_mode else 1}.json").read_text())
    jsonschema.validate(result, schema)
    if pair_mode:
        assert len(completed) == 2, "one coupled pair pass and a separate single-ended tail must run"
        assert completed[0] is injected[0]
        assert not determinism.wall_clock_stops({"commands": completed[1:]})
        commands = result["process"]["commands"]
        pair_index = next(i for i, rec in enumerate(commands) if any(
            Path(str(arg)).name == "route_diff.py" for arg in rec["argv"]))
        tail_index = next(i for i, rec in enumerate(commands) if any(
            Path(str(arg)).name == "route.py" for arg in rec["argv"]))
        assert pair_index < tail_index and commands[pair_index][flag] is True
        assert not determinism.wall_clock_stops({"commands": [commands[tail_index]]})
        assert all(pair["routed"] and pair["status"] == "ROUTED" for pair in result["pairs"])

    regraded = cli.validate_record(SimpleNamespace(
        run_dir=str(run), against=None, pour_area_loss=None, pour_min_spokes=None))
    assert regraded["process"]["status"] == "PASS"
    assert regraded["routing"]["status"] == "COMPLETE"
    assert regraded["validation"]["status"] == "PASS"
    assert regraded["exit_code"] == 0
    assert regraded["process"]["measurement_qualification"]["measurement"] is False
    assert f"wall-clock stop: {flag}" in regraded["process"]["measurement_qualification"]["reasons"]
    assert common.sha256(board) == regraded["evidence"]["candidate_board_sha256"]
    jsonschema.validate(regraded, schema)
    assert tree_hash(project) == before
