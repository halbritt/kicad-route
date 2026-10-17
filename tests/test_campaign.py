"""kicad-route campaign: hard-nets-first chains on the route_demo fixture.

The blocked variant (keepout wall around J2) leaves the same nets open on every
attempt, so a backlog exists deterministically; KRT's net rescue is switched off so
each attempt returns a candidate. Shared harness: helpers.py and conftest.py.
"""
import fnmatch
import json
from pathlib import Path

from helpers import FIXTURE, OUT, cli, record
from kicad_route import campaign
from kicad_route import cli as route

KRT = ["--backend", "krt", "--budget", "120", "--default-clearance", "0.4"]


def run_campaign(*args, timeout=1800):
    r = cli("campaign", *args, timeout=timeout)
    line = next((l for l in r.stdout.splitlines() if l.startswith("[campaign] result: ")), None)
    assert line, f"campaign printed no result (exit {r.returncode}):\n{r.stdout[-3000:]}\n{r.stderr[-2000:]}"
    path = OUT / line.split()[2]
    return r, path.parent, json.loads(path.read_text())


def attempt(run_dir):
    return json.loads((OUT / run_dir / "attempt.json").read_text())


def test_net_pattern_matches_exactly_one_name():
    names = ["a[0]", "a0", "a*b", "axb", "a?", "ab", "-5V", "!x", "x", "Net-(BT1-+)"]
    facts = {"nets": {n: {"netclass": "Default"} for n in names}}
    for n in names:
        assert fnmatch.fnmatchcase(n, campaign.net_pattern(n))
        assert route.select_nets(facts, [campaign.net_pattern(n)], None)[0] == [n], n
    # the rest of a scope: the campaign's own patterns, then exclusions
    args = type("A", (), {"nets": None, "netclass": None})()
    rest = campaign.scope_args(args, "rest", ["a[0]", "!x"])[1:]
    assert route.select_nets(facts, rest, None)[0] == sorted(set(names) - {"a[0]", "!x"})
    args.netclass = ["Default"]
    rest = campaign.scope_args(args, "rest", ["ab"])
    assert rest[:3] == ["--nets", "!ab", "--netclass"]


def test_rank_prefers_complete_valid_then_fewer_unconnected():
    def chain(cid, exit_code, unconnected, new_v=0, pres=(), runtime=10.0):
        return {"id": cid, "runtime_s": runtime, "grade": {
            "exit_code": exit_code, "routing": {"unconnected_in_scope": unconnected},
            "validation": {"new_violations": new_v, "new_parity": 0, "preservation_failures": list(pres)}}}
    chains = [chain("fail", 1, None) | {"grade": None}, chain("pres", 3, 0, pres=["footprints"]),
              chain("more", 2, 5), chain("fewer", 3, 2, new_v=1), chain("fewer-clean", 2, 2, runtime=99),
              chain("done", 0, 0, runtime=500)]
    assert [c["id"] for c in sorted(chains, key=campaign.rank_key)] == [
        "done", "fewer-clean", "fewer", "more", "pres", "fail"]


def test_campaign_complete_in_round_0_stops():
    r, cdir, rec = run_campaign(*KRT, "--dir", str(FIXTURE), "--nets", "/SIG_A*", "--label", "campaign-trivial")
    record("campaign_trivial", {k: rec[k] for k in ("rounds", "leaderboard", "stop_reason", "best", "exit_code")})
    assert r.returncode == 0 and rec["exit_code"] == 0
    assert [x["round"] for x in rec["rounds"]] == [0] and len(rec["chains"]) == 1
    assert rec["stop_reason"] == "complete in round 0"
    assert rec["backlog_evolution"] == [{"round": 0, "size": 0, "nets": []}]
    assert rec["best"]["chain"] == "r0" and (OUT / rec["best"]["candidate"]).exists()
    assert (cdir / "CAMPAIGN.md").exists()


def test_campaign_routes_backlog_first_on_original_input(blocked_dir):
    r, cdir, rec = run_campaign(*KRT, "--dir", str(blocked_dir), "--backend-env", "KICAD_NET_RESCUE=0",
                                "--rounds", "2", "--label", "campaign-blocked")
    record("campaign_blocked", {k: rec[k] for k in ("rounds", "backlog_evolution", "leaderboard", "stop_reason",
                                                    "best", "exit_code")})
    chains = {c["id"]: c for c in rec["chains"]}
    r0 = chains["r0"]["steps"][0]
    assert r0["routing"] == route.INCOMPLETE
    backlog = rec["rounds"][0]["backlog_next"]
    assert backlog == sorted(r0["unrouted_nets"]) and backlog

    # round 1: the backlog alone, on the ORIGINAL input ...
    first, rest = chains["r1"]["steps"]
    assert first["role"] == "backlog" and first["nets"] == backlog
    a1 = attempt(first["run_dir"])
    assert Path(a1["source"]["source_dir"]).resolve() == blocked_dir.resolve()
    assert sorted(a1["scope"]["nets"]) == backlog
    assert a1["input_fingerprint"] == attempt(r0["run_dir"])["input_fingerprint"] == rec["input"]["fingerprint"]
    # ... then everything else from that candidate
    a2 = attempt(rest["run_dir"])
    assert Path(a2["source"]["source_dir"]).resolve() == (OUT / first["candidate_dir"]).resolve()
    assert set(a2["scope"]["nets"]) == set(attempt(r0["run_dir"])["scope"]["nets"]) - set(backlog)
    for name in ("route_demo.kicad_pro", "route_demo.kicad_dru"):   # rules are the input's, unchanged
        assert (OUT / rest["candidate_dir"] / name).read_bytes() == (blocked_dir / name).read_bytes()

    # the chain end is graded against the original input, with the whole scope
    g = chains["r1"]["grade"]
    assert g["by"] == "validate --against" and g["against"] == first["run_dir"]
    res = json.loads((OUT / g["result"]).read_text())
    assert res["against"] == first["run_dir"] and res["validation"]["chain"] == [rest["run_dir"], first["run_dir"]]
    assert set(g["routing"]["unrouted_nets"]) <= set(backlog) | set(a2["scope"]["nets"])

    # the wall keeps J2's nets open, so the backlog stops growing after round 1
    assert [x["round"] for x in rec["rounds"]] == [0, 1]
    assert rec["stop_reason"].startswith("backlog did not grow")
    best = rec["best"]
    winner = chains[best["chain"]]
    assert best["graded_against"] == winner["steps"][0]["run_dir"]
    assert attempt(best["graded_against"])["source"]["source_dir"] == str(blocked_dir.resolve())
    assert best["run_dir"] == winner["steps"][-1]["run_dir"]
    assert rec["leaderboard"][0]["chain"] == best["chain"]
    assert r.returncode == rec["exit_code"] == best["exit_code"] == winner["grade"]["exit_code"]
    assert r.returncode in (2, 3)


def test_campaign_race_lane_is_a_two_pass_chain():
    r, cdir, rec = run_campaign(*KRT, "--dir", str(FIXTURE), "--nets", "/SIG_A*", "--race", "1",
                                "--label", "campaign-race")
    record("campaign_race", {k: rec[k] for k in ("rounds", "leaderboard", "stop_reason", "best", "exit_code")})
    assert rec["rounds"][0]["chains"] == ["r0", "r0-pads"]
    lane = next(c for c in rec["chains"] if c["id"] == "r0-pads")
    first, rest = lane["steps"]
    assert len(first["nets"]) == 1 and first["on"] == "input"
    assert Path(attempt(rest["run_dir"])["source"]["source_dir"]).resolve() == \
        (OUT / first["candidate_dir"]).resolve()
    assert lane["grade"]["by"] == "validate --against" and lane["grade"]["against"] == first["run_dir"]
    assert sorted(lane["grade"]["chain"]) == sorted([rest["run_dir"], first["run_dir"]])
    assert rec["stop_reason"] == "complete in round 0" and r.returncode == rec["exit_code"]


def test_campaign_total_budget_stops_new_runs(blocked_dir):
    r, cdir, rec = run_campaign(*KRT, "--dir", str(blocked_dir), "--backend-env", "KICAD_NET_RESCUE=0",
                                "--total-budget", "1", "--label", "campaign-total-budget")
    record("campaign_total_budget", {k: rec[k] for k in ("rounds", "leaderboard", "stop_reason", "exit_code")})
    assert rec["stop_reason"] == "total budget reached"
    assert [c["id"] for c in rec["chains"]] == ["r0"]       # round 1 never started
    assert rec["rounds"][0]["backlog_next"]                  # although a backlog was left
    assert r.returncode == rec["exit_code"] == rec["chains"][0]["grade"]["exit_code"]
