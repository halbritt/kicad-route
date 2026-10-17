"""Run hygiene: stall detection, the run ledger, the awkward-net-name canary and
the real-board corpus gate (docs/hygiene.md). Shared harness: helpers.py and
conftest.py; unit-level checks run in process, integration checks through the CLI.
"""
import json
import shutil
import sys

import pytest

from helpers import FIXTURE, OUT, RUNS, cli, record, run, summary
from kicad_route import canary, common, corpus, ledger, netnames, stall
from kicad_route import cli as route
from kicad_route.kicad import specctra


# ----------------------------------------------------------------- stall

KRT_LINES = """Loading /x/route_demo.kicad_pcb...
Using MPS ordering strategy...
Routing 5 single-ended net(s)...
============================================================
Building base obstacle map...
[1/5] Routing /SIG_A_OUT (id=3)
Probe: forward=5000, backward=5000 iters, trying forward with full iterations...
No route found after 90173 iterations (forward), trying backward...
  FAILED: Could not find route (0.15s)
[2/5 (1 failed)] Routing /SIG_A (id=5)
  SUCCESS: 6 segments, 0 vias, 203 iterations
Per-net fine-parameter rescue (#331/#371): 4 candidate net(s)
  Rescuing /VIN (partial, 2 disconnected parts)
Probe: forward=5000, backward=5000 iters, trying forward with full iterations...
No route found after 1000000 iterations (forward), trying backward...
""".splitlines()


def test_krt_progress_markers():
    p = stall.KrtProgress()
    progress = [line for line in KRT_LINES if p.feed(line)]
    assert p.phase == "net rescue" and p.metric == {"rescuing": "/VIN", "parts": 2}
    # search chatter is not progress: a search that never ends is what --stall catches
    assert not any(line.startswith(("Probe", "No route found")) for line in progress)
    assert "[2/5 (1 failed)] Routing /SIG_A (id=5)" in progress
    assert "stalled in net rescue at rescuing /VIN, parts 2 for 7 s" == stall.stall_reason(
        {"stall": {"phase": p.phase, "metric": p.metric, "no_progress_s": 7}})


def test_freerouting_progress_counts_only_improving_passes():
    p = stall.FreeroutingProgress()
    pass_line = ("INFO [x] Auto-routing pass #{} on board 'b' was completed in 0.4 seconds with score {} "
                 "({} unrouted and 0 violations), using 0.6 CPU seconds")
    assert p.feed("INFO [x] Auto-routing stage started on board 'b' with baseline score 148.15 for 7 unrouted items.")
    assert p.feed(pass_line.format(1, 407.41, 4))
    assert not p.feed(pass_line.format(2, 407.41, 4))        # same best: not progress
    assert not p.feed(pass_line.format(3, 296.30, 5))        # worse: not progress
    assert p.feed(pass_line.format(4, 851.85, 1))
    assert p.phase == "auto-routing started" and p.metric["pass"] == 4 and p.metric["unrouted"] == 1


FAKE_BACKEND = r'''
import os, sys, time
from pathlib import Path
stop = Path(os.environ["KICAD_STOP_FILE"])
print("Loading /x/board.kicad_pcb...")
print("Routing 3 single-ended net(s)...")
print("[1/3] Routing /A (id=1)")
print("  SUCCESS: 2 segments")
print("[2/3] Routing /B (id=2)")
for i in range(3000):
    print(f"No route found after {i * 1000} iterations (forward), trying backward...")
    if stop.exists():
        print("CHECKPOINT STOP (KICAD_STOP_FILE): stopping before the next net")
        sys.exit(0)
    time.sleep(0.1)
'''


@pytest.mark.parametrize("stall_s", [2, None])
def test_monitor_stall_and_budget(tmp_path, stall_s):
    """A backend that keeps printing search chatter but never advances: with a
    stall limit it is stopped as stalled (with its phase); without one the
    budget path is unchanged."""
    script = tmp_path / "fake_backend.py"
    script.write_text(FAKE_BACKEND)
    stop, log = tmp_path / "STOP", tmp_path / "krt.log"
    rec = route.run_budgeted([sys.executable, script], tmp_path, log, 4 if stall_s is None else 60,
                             {"KICAD_STOP_FILE": str(stop)}, stop_file=stop,
                             monitor=stall.Monitor("krt", log, stall_s))
    beats = [json.loads(line) for line in (tmp_path / "heartbeat.jsonl").read_text().splitlines()]
    record(f"hygiene_monitor_{'stall' if stall_s else 'budget'}",
           {k: rec.get(k) for k in ("stalled", "budget_reached", "stop_requested", "killed", "last_phase",
                                    "stall", "duration_s")} | {"heartbeats": beats})
    assert rec["stop_requested"] and not rec.get("killed") and rec["exit_code"] == 0
    assert rec["heartbeats"]["count"] == len(beats) >= 4
    assert not any("No route found" in b["line"] for b in beats)
    if stall_s:
        assert rec["stalled"] and not rec.get("budget_reached")
        assert rec["stall"]["phase"] == "routing single-ended net(s)"
        assert stall.stall_reason(rec).startswith("stalled in routing single-ended net(s) at net 2/3")
        assert rec["duration_s"] < 30
    else:
        assert rec["budget_reached"] and not rec["stalled"] and "stall" not in rec
    assert rec["last_phase"] == "checkpoint stop"


def test_stall_on_blocked_fixture(blocked_dir):
    """KRT with net rescue on keeps searching the walled-off nets: a short --stall
    stops it and says where it stalled, instead of a bare budget kill."""
    r, run_dir, result = run("run", "--backend", "krt", "--budget", "240", "--stall", "6", "--dir", str(blocked_dir),
                             "--label", "hygiene-stall-krt")
    record("hygiene_stall_krt", summary(result) | {"last_phase": result["process"].get("last_phase"),
                                                   "heartbeats": result["process"].get("heartbeats")})
    reasons = " ".join(result["process"]["reasons"])
    assert "stalled in" in reasons and "for " in reasons, reasons
    assert "budget" not in reasons
    assert result["settings"]["stall_s"] == 6
    assert result["process"]["stalled"] is True
    assert result["process"]["last_phase"]
    assert result["process"]["commands"][0]["stall"]["phase"] in reasons
    assert result["process"]["heartbeats"]["count"] > 0
    assert (run_dir / "work" / "heartbeat.jsonl").read_text().strip()
    assert result["runtime_s"] < 200


# ---------------------------------------------------------------- ledger

def _result(outcome_exit, version="1.0", settings=None, run_dir="r1"):
    st = {"process": common.PASS if outcome_exit != 1 else common.FAIL,
          "validation": common.FAIL if outcome_exit == 3 else common.PASS,
          "routing": "INCOMPLETE" if outcome_exit == 2 else "COMPLETE"}
    return {"backend": {"name": "krt", "version": version, "commit": "c" + version},
            "input": {"fingerprint": "f" * 64, "project": "p"},
            "settings": settings or {"backend": "krt", "budget_s": 60, "mode": "all", "max_iterations": None},
            "process": {"status": st["process"]}, "validation": {"status": st["validation"]},
            "routing": {"status": st["routing"]}, "exit_code": outcome_exit, "run_dir": run_dir}


def test_ledger_gate_unit(tmp_path, monkeypatch):
    monkeypatch.delenv("KICAD_ROUTE_LEDGER", raising=False)
    path = tmp_path / "ledger.jsonl"
    settings = {"backend": "krt", "budget_s": 60, "mode": "all", "max_iterations": None}
    v1 = {"version": "1.0", "commit": "c1.0"}
    for exit_code, run_dir in ((0, "ok-run"), (2, "incomplete-run")):
        r = _result(exit_code, run_dir=str(tmp_path / run_dir))
        g = ledger.check(path, r["input"]["fingerprint"], "krt", v1, settings)
        ledger.append(path, g, r)
    gate = ledger.check(path, "f" * 64, "krt", v1, dict(settings, max_iterations=None))
    assert gate["refuse"] and "incomplete-run" in gate["message"] and "incomplete" in gate["message"]
    assert gate["short"] == ledger.from_result(_result(2))[0][:12]
    forced = ledger.check(path, "f" * 64, "krt", v1, settings, force=True)
    assert not forced["refuse"] and forced["forced"] and forced["forced_over"].endswith("incomplete-run")
    # same attempt, new upstream build: allowed, noted
    newer = ledger.check(path, "f" * 64, "krt", {"version": "1.1", "commit": "c1.1"}, settings)
    assert not newer["refuse"] and newer["upstream_changed_since"].endswith("incomplete-run")
    assert any("upstream changed since" in n for n in newer["notes"])
    # different settings: a different attempt
    assert not ledger.check(path, "f" * 64, "krt", v1, dict(settings, budget_s=120))["refuse"]
    # net-pattern order does not make a new attempt
    a = ledger.attempt_key("f", "krt", {"mode": "selected", "net_patterns": ["/A", "/B"]})
    assert a == ledger.attempt_key("f", "krt", {"mode": "selected", "net_patterns": ["/B", "/A"]})


def test_ledger_refuses_repeat_of_failed_attempt(monkeypatch):
    """A process failure (an option KRT rejects) is recorded; the identical attempt
    is refused with exit 4 naming the earlier run; --force reruns it, recorded."""
    monkeypatch.delenv("KICAD_ROUTE_LEDGER", raising=False)
    args = ("run", "--backend", "krt", "--budget", "60", "--backend-arg=--no-such-krt-option",
            "--label", "hygiene-ledger")
    r1, run1, res1 = run(*args)
    assert r1.returncode == 1 and res1["process"]["status"] == route.FAIL
    assert res1["ledger"]["short"] == ledger.from_result(res1)[0][:12]

    def stamps():
        return {p.name for p in (RUNS / "hygiene-ledger").iterdir() if not p.is_symlink()}
    before = stamps()
    r2 = cli(*args, "--dir", str(FIXTURE))
    assert r2.returncode == ledger.REFUSED_EXIT, r2.stdout
    assert "refused" in r2.stdout and run1.name in r2.stdout and "--force" in r2.stdout
    r3, run3, res3 = run(*args, "--force")
    assert r3.returncode == 1 and res3["ledger"]["forced"] is True
    assert res3["ledger"]["forced_over"].endswith(run1.name)
    assert stamps() == before | {run3.name}                          # the refused attempt left no run dir
    assert (RUNS / "hygiene-ledger" / "latest").resolve() == run3.resolve()
    lines = [json.loads(line) for line in (OUT / "ledger.jsonl").read_text().splitlines()]
    mine = [e for e in lines if e["run_dir"] in (str(run1.resolve()), str(run3.resolve()))]
    assert [e["forced"] for e in mine] == [False, True] and {e["outcome"] for e in mine} == {"process_fail"}
    c = cli("compare", str(run1), str(run3))
    record("hygiene_ledger", {"refusal": r2.stdout[-1500:], "compare": c.stdout, "ledger": mine})
    assert "| fp |" in c.stdout and c.stdout.count(res1["ledger"]["short"]) == 2


def test_run_canary_refusal(monkeypatch, tmp_path):
    """run --canary refuses (exit 4) before the baseline when the canary fails."""
    monkeypatch.setattr(common, "_out", common._out)
    monkeypatch.setattr(canary, "run_canary", lambda backend, **kw: {"status": common.FAIL,
                                                                    "reasons": ["bridge: simulated"]})
    rc = route.main(["--out", str(tmp_path), "run", "--backend", "freerouting", "--dir", str(FIXTURE),
                     "--canary", "--label", "canary-refusal"])
    assert rc == ledger.REFUSED_EXIT
    assert not list((tmp_path / "runs").rglob("result.json"))
    assert not (tmp_path / ".cache" / "baseline").exists()     # refused before the baseline DRC


# ---------------------------------------------------------------- canary

def test_canary_split_unit():
    assert canary.check_split_unit()["status"] == common.PASS


def test_canary_split_fails_without_quote_aware_find_open(monkeypatch):
    """The parenthesised-name bug, reverted: the canary must catch it."""
    monkeypatch.setattr(specctra, "find_open", lambda text, start: text.find("(", start))
    res = canary.check_split_unit()
    assert res["status"] == common.FAIL
    assert any("Net-(BT1-+)" in p for p in res["problems"]), res["problems"]


def test_split_classes_moves_quote_named_net_in_definition_form():
    """KiCad writes the net "q" as `"q"` in class lists but `''q''` in its definition."""
    dsn = ("(pcb x (network\n    (net ''q''\n      (pins A-1 B-1))\n"
           '    (class kicad_default "q" A\n      (circuit (use_via V))\n      (rule (width 200) (clearance 200)))))')
    out, ignored = specctra.split_classes(dsn, {'"q"'})
    assert ignored == ["kicad_default__skip"]
    assert '(class "kicad_default__skip" \'\'q\'\'\n' in out and "(class kicad_default A\n" in out


def test_krt_net_patterns_are_exact():
    pats = netnames.krt_patterns(["-12V", "GND", "/D[0]", "/SIG_A"], ["-12V", "GND", "/x/GND", "/D[0]", "/SIG_A"])
    assert pats == ["[-]12V", "/D[[]0]", "/SIG_A", "GND", "!/x/GND"]
    assert netnames.krt_patterns(["/SIG_A", "GND"], ["/SIG_A", "GND", "/SIG_B"]) == ["/SIG_A", "GND"]


def test_canary_quick_bridge():
    """What doctor runs: the class split and KiCad's DSN export of awkward names (no routing)."""
    res = canary.run_canary(quick=True, dest=OUT / "canary-quick")
    record("hygiene_canary_quick", res)
    assert res["status"] == common.PASS, res["reasons"]
    assert [c["check"] for c in res["checks"]] == ["split", "bridge"]


def test_canary_backends():
    r = cli("canary", timeout=900)
    res = json.loads(sorted((OUT / "canary").glob("2*/canary.json"))[-1].read_text())
    record("hygiene_canary", res)
    assert r.returncode == 0, r.stdout[-3000:]
    assert {c["check"]: c["status"] for c in res["checks"]} == {
        "split": "PASS", "bridge": "PASS", "freerouting": "PASS", "krt": "PASS"}
    for c in res["checks"][2:]:
        assert set(c["needed_copper"]) <= set(c["new_copper_nets"])
        assert not set(c["new_copper_nets"]) & set(canary.SCOPED_OUT)


# ---------------------------------------------------------------- corpus

def test_corpus_add_check_round_trip(monkeypatch, blocked_dir):
    root = OUT / "corpus-test"
    shutil.rmtree(root, ignore_errors=True)
    add = ("corpus", "add", "--dir", str(FIXTURE), "--name", "fixture-sig-a", "--backend", "krt",
           "--nets", "/SIG_A*", "--default-clearance", "0.4", "--budget", "120",
           "--backend-env", "KICAD_NET_RESCUE=0", "--root", str(root))
    r = cli(*add)
    assert r.returncode == 0, r.stdout[-2000:]
    d = root / "fixture-sig-a"
    entry = json.loads((d / "entry.json").read_text())
    assert entry["baseline"]["exit_code"] == 0 and entry["baseline"]["unconnected_in_scope"] == 0
    assert (d / entry["reference_dir"] / "route_demo.kicad_pcb").read_bytes() == \
        (FIXTURE / "route_demo.kicad_pcb").read_bytes()
    assert cli(*add).returncode != 0                                # an existing entry is not overwritten
    r = cli("corpus", "check", "--root", str(root))
    assert r.returncode == 0, r.stdout[-2000:]
    assert "PASS fixture-sig-a" in r.stdout
    # A regression: the same attempt now leaves nets unconnected (the walled board
    # stands in for a backend that got worse). update records it, never rolls back.
    shutil.copyfile(blocked_dir / "route_demo.kicad_pcb", d / entry["reference_dir"] / "route_demo.kicad_pcb")
    monkeypatch.setattr(common, "_out", OUT)
    update_json = OUT / "corpus-update" / "update.json"
    common.write_json(update_json, {"errors": []})
    rc = corpus.after_update(update_json, 0, root=root)
    rec = json.loads(update_json.read_text())
    record("hygiene_corpus", {"entry": entry, "update": rec})
    assert rc == 1 and rec["corpus"]["status"] == common.FAIL
    assert any("unconnected in scope 0 ->" in x for x in rec["corpus"]["regressions"]["fixture-sig-a"])
    assert len(list((d / "checks").glob("*.json"))) == 2
    assert corpus.after_update(update_json, 0, root=OUT / "corpus-empty") == 0   # empty corpus: smoke's code
