"""KICADROUTE-58: `decoupling` and `barrier` refuse every failure before their analysis
(exit 4, a schema-valid refusal record in OUT/refusals/ and, with --json, on stdout),
and exit 1 only when the analysis ran and failed, with a record of its own.

No backend runs. Most cases need no KiCad at all: a fake run directory (attempt.json,
placeholder boards, empty facts) and monkeypatched pcbnew script calls stand in for a
graded run. The unloadable-board cases run the pcbnew scripts on a garbage file.
"""
import json
import os
import subprocess
import sys

import jsonschema
import pytest

from helpers import ROOT
from kicad_route import barrier, common, contract, decoupling, spokes
from kicad_route import cli as route

CLI = [sys.executable, str(ROOT / "bin" / "kicad-route")]
REFUSALS = jsonschema.Draft202012Validator(json.loads((ROOT / "schemas" / "refusal-1.json").read_text()))
BOARD = "p.kicad_pcb"


@pytest.fixture(autouse=True)
def _keep_out(monkeypatch):
    monkeypatch.setattr(common, "_out", common._out)  # route.main(--out) sets the global OUT


def kr(out, *args, cwd):
    return subprocess.run([*CLI, "--out", str(out), *map(str, args)], capture_output=True, text=True, cwd=cwd,
                          timeout=300)


def one_json(stdout):
    lines = [l for l in stdout.splitlines() if l.strip()]
    assert len(lines) == 1, f"expected one JSON line on stdout, got {len(lines)}:\n{stdout[-2000:]}"
    return json.loads(lines[0])


def refusal(out, reason_class, command):
    latest = out / "refusals" / "refusal-latest.json"
    assert latest.is_symlink(), f"no refusal record under {out}/refusals"
    rec = json.loads(latest.read_text())
    REFUSALS.validate(rec)
    assert (rec["reason_class"], rec["command"], rec["exit_code"]) == (reason_class, command, contract.REFUSED_EXIT)
    return rec


def assert_refused(r, out, reason_class, command, json_mode):
    assert r.returncode == contract.REFUSED_EXIT, f"exit {r.returncode}\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}"
    rec = refusal(out, reason_class, command)
    if json_mode:
        assert one_json(r.stdout) == rec and "refused" in r.stderr
    else:
        assert f"refused ({reason_class})" in r.stdout and not r.stdout.lstrip().startswith("{")
    return rec


def fake_run(root, violations=()):
    """A run directory plan() and run_command() accept, with placeholder boards."""
    rd = root / "run"
    for role in ("reference", "candidate"):
        (rd / role / "p").mkdir(parents=True)
        (rd / role / "p" / BOARD).write_text("(kicad_pcb)\n")
    empty = {"tracks": [], "vias": [], "copper_layers": [{"name": "F.Cu"}]}
    common.write_json(rd / "facts" / "reference-facts.json", empty)
    common.write_json(rd / "validate" / "candidate-facts.json", empty)
    common.write_json(rd / "validate" / "new-findings.json", {"violations": list(violations)})
    common.write_json(rd / "attempt.json", {"board": BOARD, "reference_dir": "reference/p",
                                            "candidate_dir": "candidate/p",
                                            "reference_facts": "facts/reference-facts.json"})
    return rd


COLLISION = {"type": "clearance", "description": "Clearance violation",
             "items": [{"uuid": "NEW"}, {"uuid": "OTHER"}]}


def fake_script(monkeypatch, **results):
    """barrier._script stand-in: results[verb] = (exit code, output, tag, data written to argv[-1])."""
    calls = []

    def script(args, cwd, log_path, timeout=900):
        verb = args[0]
        calls.append(verb)
        code, output, tag, data = results[verb]
        if data is not None:
            common.write_json(args[-1], data)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(output)
        return {"argv": [str(a) for a in args], "log": common.rel(log_path), "exit_code": code,
                "output": output}, tag
    monkeypatch.setattr(barrier, "_script", script)
    monkeypatch.setattr(spokes, "new_copper", lambda ref, cand: [{"uuid": "NEW"}])
    return calls


LOCATED = {"barriers": [{"index": 0, "name": "kicad-route:barrier:1:2", "x": 1_000_000, "y": 2_000_000,
                         "layer": "F.Cu", "type": "clearance", "other_uuids": ["OTHER"],
                         "new_item": {"uuid": "NEW", "kind": "PCB_TRACK", "net": "/SIG_A"}}],
           "skipped": []}


def main_json(argv, capfd):
    rc = route.main(["--json", *map(str, argv)])
    return rc, one_json(capfd.readouterr().out)


# ------------------------------------------------------------- decoupling: refusals

@pytest.mark.parametrize("json_mode", [False, True], ids=["human", "json"])
def test_decoupling_on_a_non_run_directory_is_refused(tmp_path, json_mode):
    out = tmp_path / "out"
    r = kr(out, "decoupling", tmp_path, *(["--json"] if json_mode else []), cwd=tmp_path)
    rec = assert_refused(r, out, "input", "decoupling", json_mode)
    assert "not a routing run" in rec["message"]


@pytest.mark.parametrize("json_mode", [False, True], ids=["human", "json"])
def test_decoupling_board_missing_is_refused(tmp_path, json_mode):
    out = tmp_path / "out"
    r = kr(out, "decoupling", "--board", tmp_path / "missing.kicad_pcb", *(["--json"] if json_mode else []),
           cwd=tmp_path)
    rec = assert_refused(r, out, "input", "decoupling", json_mode)
    assert "no such board" in rec["message"]
    assert not (out / "decoupling").exists()


def test_decoupling_board_kicad_cannot_load_is_refused(tmp_path):
    out = tmp_path / "out"
    bad = tmp_path / "bad.kicad_pcb"
    bad.write_text("(kicad_pcb (version 1) garbage")
    r = kr(out, "decoupling", "--board", bad, "--json", cwd=tmp_path)
    rec = assert_refused(r, out, "input", "decoupling", True)
    assert "could not load" in rec["message"]
    assert not (out / "decoupling" / "bad" / "decoupling.json").exists()


def test_decoupling_run_without_a_candidate_is_refused(tmp_path, capfd):
    rd = fake_run(tmp_path)
    (rd / "candidate" / "p" / BOARD).unlink()  # a run whose backend failed has no candidate
    out = tmp_path / "out"
    rc, rec = main_json(["--out", out, "decoupling", rd], capfd)
    assert rc == contract.REFUSED_EXIT and rec == refusal(out, "input", "decoupling")
    assert "no candidate board" in rec["message"]
    assert not (rd / "decoupling.json").exists()


# ------------------------------------------------------------- decoupling: the analysis ran and failed

def test_decoupling_analysis_failure_exits_1_with_a_record(tmp_path, monkeypatch, capfd):
    board = tmp_path / "b.kicad_pcb"
    board.write_text("(kicad_pcb)\n")
    out = tmp_path / "out"
    stale = out / "decoupling" / "b" / "decoupling.md"
    stale.parent.mkdir(parents=True)
    stale.write_text("an earlier report\n")
    monkeypatch.setattr(common, "run_cmd", lambda argv, cwd, log_path, timeout=0: {
        "argv": [str(a) for a in argv], "log": common.rel(log_path), "exit_code": 1, "output": "Traceback"})
    rc, rec = main_json(["--out", out, "decoupling", "--board", board], capfd)
    assert rc == 1 and rec["exit_code"] == 1
    assert rec["schema"] == contract.schema_id("decoupling") and rec["kind"] == "decoupling"
    assert "analysis failed" in rec["error"]
    assert json.loads((out / "decoupling" / "b" / "decoupling.json").read_text()) == rec
    assert not stale.exists()                                   # no report contradicts the record
    assert not (out / "refusals").exists()                      # a failure, not a refusal


def test_decoupling_board_changed_during_analysis_exits_1(tmp_path, monkeypatch):
    rd = fake_run(tmp_path)

    def writes_the_board(argv, cwd, log_path, timeout=0):
        common.write_json(argv[3], {"kind": "decoupling", "cells": []})
        if "candidate" in str(argv[2]):
            argv[2].write_text("(kicad_pcb changed)\n")
        return {"argv": [str(a) for a in argv], "log": common.rel(log_path), "exit_code": 0, "output": ""}
    monkeypatch.setattr(common, "run_cmd", writes_the_board)
    rc, rec = decoupling.run_command(rd, {"radius_mm": 5.0})
    assert rc == 1 and rec["kind"] == "decoupling-run" and rec["schema"] == contract.schema_id("decoupling-run")
    assert rec["error"].startswith("candidate: ") and "changed during analysis" in rec["error"]
    assert "reference" in rec                                     # what did run is kept
    assert json.loads((rd / "decoupling.json").read_text()) == rec
    assert not (rd / "decoupling.md").exists()


# ------------------------------------------------------------- barrier: refusals

@pytest.mark.parametrize("json_mode", [False, True], ids=["human", "json"])
def test_barrier_on_a_non_run_directory_is_refused(tmp_path, json_mode):
    out = tmp_path / "out"
    r = kr(out, "barrier", tmp_path, *(["--json"] if json_mode else []), cwd=tmp_path)
    assert "not a routing run" in assert_refused(r, out, "input", "barrier", json_mode)["message"]


@pytest.mark.parametrize("json_mode", [False, True], ids=["human", "json"])
def test_barrier_with_no_collision_to_barrier_is_refused(tmp_path, json_mode):
    """Plan reasons (no copper collision made by new copper): nothing to preview."""
    rd = fake_run(tmp_path)
    old = rd / "barrier-preview" / "p" / BOARD   # an earlier preview's copy must not survive the refusal
    old.parent.mkdir(parents=True)
    old.write_text("(kicad_pcb)\n")
    out = tmp_path / "out"
    r = kr(out, "barrier", rd, *(["--json"] if json_mode else []), cwd=tmp_path)
    rec = assert_refused(r, out, "input", "barrier", json_mode)
    assert "nothing to barrier" in rec["message"] and "no copper collision" in rec["message"]
    assert not old.exists() and not (rd / "barrier-preview" / "barrier-plan.json").exists()


def test_barrier_candidate_kicad_cannot_load_is_refused(tmp_path, monkeypatch, capfd):
    rd = fake_run(tmp_path, [COLLISION])
    fake_script(monkeypatch, locate=(3, 'BARRIER_LOAD_ERROR: "x: KiCad could not load the board"', None, None))
    out = tmp_path / "out"
    rc, rec = main_json(["--out", out, "barrier", rd], capfd)
    assert rc == contract.REFUSED_EXIT and rec == refusal(out, "input", "barrier")
    assert "could not load the candidate board" in rec["message"]


def test_barrier_candidate_kicad_cannot_load_real_script(tmp_path, monkeypatch):
    """The real kicad/barrier.py signals an unloadable board the way the host expects."""
    rd = fake_run(tmp_path, [COLLISION])
    (rd / "candidate" / "p" / BOARD).write_text("not a KiCad board\n")
    monkeypatch.setattr(spokes, "new_copper", lambda ref, cand: [{"uuid": "NEW"}])
    rec = barrier.plan(rd, tmp_path / "plan")
    assert not rec.get("locate_failed") and "could not load the candidate board" in rec["reasons"][0], rec


def test_barrier_inject_failure_is_a_state_refusal(tmp_path, monkeypatch, capfd):
    rd = fake_run(tmp_path, [COLLISION])
    calls = fake_script(monkeypatch, locate=(0, "BARRIER_JSON: {}", {"barriers": 1}, LOCATED),
                        inject=(1, "Traceback (most recent call last)", None, None))
    out = tmp_path / "out"
    rc, rec = main_json(["--out", out, "barrier", rd], capfd)
    assert calls == ["locate", "inject"]
    assert rc == contract.REFUSED_EXIT and rec == refusal(out, "state", "barrier")
    assert "could not inject" in rec["message"]
    assert not (rd / "barrier-preview" / "p").exists()             # no half-made preview board
    assert (rd / "reference" / "p" / BOARD).read_text() == "(kicad_pcb)\n"


# ------------------------------------------------------------- barrier: the analysis ran

def test_barrier_locate_failure_exits_1_with_a_record(tmp_path, monkeypatch, capfd):
    rd = fake_run(tmp_path, [COLLISION])
    fake_script(monkeypatch, locate=(1, "Traceback (most recent call last)", None, None))
    out = tmp_path / "out"
    rc, rec = main_json(["--out", out, "barrier", rd], capfd)
    assert rc == 1 and rec["exit_code"] == 1 and rec["locate_failed"] is True
    assert rec["schema"] == contract.schema_id("barrier-preview") and rec["kind"] == "barrier-preview"
    assert "could not locate" in rec["reasons"][0]
    assert json.loads((rd / "barrier-preview" / "barrier-plan.json").read_text()) == rec
    assert not (out / "refusals").exists()


def test_barrier_nothing_injected_exits_1_with_a_record(tmp_path, monkeypatch):
    rd = fake_run(tmp_path, [COLLISION])
    fake_script(monkeypatch, locate=(0, "", {"barriers": 1}, LOCATED),
                inject=(0, "", {"injected": 0, "barriers": [],
                                "skipped": [{"name": "kicad-route:barrier:1:2", "reason": "overlaps"}]}, None))
    rc, rec = barrier.preview(rd)
    assert rc == 1 and rec["exit_code"] == 1 and rec["injected"] == 0
    assert "nothing was injected" in rec["reasons"][-1]
    assert json.loads((rd / "barrier-preview" / "barrier-plan.json").read_text()) == rec


def test_barrier_preview_success_record(tmp_path, monkeypatch, capfd):
    rd = fake_run(tmp_path, [COLLISION])
    fake_script(monkeypatch, locate=(0, "", {"barriers": 1}, LOCATED),
                inject=(0, "", {"injected": 1, "barriers": [{"name": "kicad-route:barrier:1:2"}],
                                "skipped": []}, None))
    out = tmp_path / "out"
    rc, rec = main_json(["--out", out, "barrier", rd], capfd)
    assert rc == 0 and rec["exit_code"] == 0 and rec["schema"] == contract.schema_id("barrier-preview")
    assert rec["injected_names"] == ["kicad-route:barrier:1:2"] and (rd / "barrier-preview" / "p" / BOARD).exists()


def test_both_commands_have_machine_mode():
    assert {"decoupling", "barrier"} <= set(route.JSON_COMMANDS)
    text = (ROOT / "docs" / "interface.md").read_text()
    refusals = text[text.index("## Refusals (exit 4)"):text.index("The record (`schemas/refusal-1.json`)")]
    for cmd in ("decoupling", "barrier"):
        assert f"`{cmd}`" in text.split("## Refusals (exit 4)")[0].split("Other commands:")[1], cmd
        assert cmd in refusals, cmd
