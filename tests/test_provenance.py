"""Copper provenance (`--protect human`) and the completion ratchet (`net_completion`).

The chain starts from the fixture with /SIG_B UNLOCKED (conftest `unlocked_dir`),
so the design's own copper is ordinary unlocked copper that only provenance can
protect. Step 1 routes /SIG_A* with --protect human on that (non-candidate) input,
which must behave exactly like --protect existing. Step 2 routes the rest from
step 1's candidate with --protect human: only /SIG_B (the origin's copper) is
locked, step 1's /SIG_A* copper is left to the backend. See docs/provenance.md.
"""
import json
import shutil

import pytest

from helpers import OUT, RUNS, cli, record, run, summary
from kicad_route import cli as route

SIG_B = {"tracks": 5, "vias": 2}   # the fixture's pre-routed /SIG_B (unlocked in this variant)
KRT = ["--backend", "krt", "--budget", "180", "--default-clearance", "0.4"]


def candidate_of(run_dir):
    return run_dir / json.loads((run_dir / "attempt.json").read_text())["candidate_dir"]


def checks(run_dir, sub="validate"):
    return {c["check"]: c for c in json.loads((run_dir / sub / "preservation.json").read_text())}


def krt_route_cmd(result):
    return next(c for c in result["process"]["commands"] if any(a.endswith("route.py") for a in c["argv"]))


def copy_run(src, dst):
    """A copy of a run that `validate` can grade: the baseline path is relative to the original."""
    shutil.copytree(src.resolve(), dst, symlinks=True)
    attempt = json.loads((dst / "attempt.json").read_text())
    attempt["baseline"]["report"] = str((src.resolve() / attempt["baseline"]["report"]).resolve())
    (dst / "attempt.json").write_text(json.dumps(attempt))
    return dst


@pytest.fixture(scope="module")
def chain(unlocked_dir):
    _, step1, r1 = run("run", *KRT, "--nets", "/SIG_A*", "--protect", "human", "--dir", str(unlocked_dir),
                       "--label", "prov-step1")
    _, step2, r2 = run("run", *KRT, "--nets", "!/SIG_A*", "--protect", "human",
                       "--dir", str(candidate_of(step1)), "--board", "route_demo.kicad_pcb", "--label", "prov-step2")
    return {"step1": step1, "r1": r1, "step2": step2, "r2": r2}


def test_human_on_non_candidate_input_is_existing(chain, unlocked_dir):
    """The input is not a kicad-route candidate: every item is the design's, and the
    attempt is the same as --protect existing (backend flags, locks, designation)."""
    step1, r1 = chain["step1"], chain["r1"]
    _, ex_dir, ex = run("run", *KRT, "--nets", "/SIG_A*", "--protect", "existing", "--dir", str(unlocked_dir),
                        "--label", "prov-step1-existing")
    prov = json.loads((step1 / "provenance.json").read_text())
    record("provenance_non_candidate", {"provenance": prov, "human": summary(r1), "existing": summary(ex)})
    assert prov["input_is_candidate"] is False and prov["effective_protect"] == "existing"
    assert r1["settings"]["protect"] == "existing" and r1["settings"]["protect_requested"] == "human"
    assert {k: v for k, v in r1["settings"].items() if k != "protect_requested"} == ex["settings"]

    def normalised(cmd, run_dir):
        def n(a):
            return str(a).replace(str(run_dir.resolve()), "RUN")
        return [n(a) for a in cmd["argv"]], {k: n(v) for k, v in cmd["env"].items()}
    assert normalised(krt_route_cmd(r1), step1) == normalised(krt_route_cmd(ex), ex_dir)
    assert "--keep-input-copper" in krt_route_cmd(r1)["argv"]
    assert r1["process"]["existing_copper_locks"]["lock"] == ex["process"]["existing_copper_locks"]["lock"] \
        == {"locked": SIG_B["tracks"] + SIG_B["vias"]}
    assert checks(step1)["protected_copper"]["detail"] == checks(ex_dir)["protected_copper"]["detail"]
    assert r1["validation"]["preservation"] == ex["validation"]["preservation"]
    assert r1["exit_code"] == ex["exit_code"] == 0


def test_human_chain_locks_only_the_origin_copper(chain):
    """Step 2 may rework step 1's copper but never the origin's: provenance names
    /SIG_B human and step 1's /SIG_A* copper agent; only the human items are locked,
    KRT runs without its keep-input-copper switches, and grading designates exactly
    the human items."""
    step1, r1, step2, r2 = chain["step1"], chain["r1"], chain["step2"], chain["r2"]
    prov = json.loads((step2 / "provenance.json").read_text())
    record("provenance_chain", {"provenance": {k: v for k, v in prov.items() if k not in ("human", "agent")}
                                | {"human": {k: v for k, v in prov["human"].items() if k != "keys"},
                                   "agent": {k: v for k, v in prov["agent"].items() if k != "keys"}},
                                "step1": summary(r1), "step2": summary(r2)})
    assert prov["input_is_candidate"] is True and prov["effective_protect"] == "human"
    assert (OUT / prov["origin_run"]).resolve() == step1.resolve()   # paths are relative to the CLI cwd
    assert {k: prov["human"][k] for k in ("tracks", "vias")} == SIG_B and prov["human"]["nets"] == ["/SIG_B"]
    s1 = r1["routing"]["stats"]
    assert (prov["agent"]["tracks"], prov["agent"]["vias"]) == (s1["new_tracks"], s1["new_vias"])
    assert prov["agent"]["nets"] == ["/SIG_A", "/SIG_A_OUT"]
    assert prov["unattributed"]["tracks"] == prov["unattributed"]["vias"] == 0
    assert r2["settings"]["protect"] == "human" and r2["settings"]["protect_only"] == "facts/human-keys.json"

    # backend: only the human copper is locked, and KRT may rip the rest
    locks = r2["process"]["existing_copper_locks"]
    assert locks["lock"] == {"locked": 7, "allowlist": 7, "allowlist_not_found": 0}
    assert locks["restore"] == {"restored": 7, "not_found": 0}
    locked = json.loads((step2 / "work" / "locks.json").read_text())
    assert {k[1] for k in locked} == {"/SIG_B"}
    cmd = krt_route_cmd(r2)
    assert not set(route.KRT_PROTECT_EXISTING_FLAGS) & set(cmd["argv"])
    assert not set(route.KRT_PROTECT_EXISTING_ENV) & set(cmd["env"])

    # grading: the human items are the protected copper, and they came back untouched and unlocked
    c = checks(step2)
    assert f"{SIG_B['tracks']} tracks and {SIG_B['vias']} vias designated (locked items and human copper" \
        in c["protected_copper"]["detail"]
    assert c["protected_copper"]["status"] == route.PASS and "max deviation 0 nm" in c["protected_copper"]["detail"]
    assert f"of {prov['agent']['tracks']} agent (earlier-step) tracks" in c["existing_unlocked_copper"]["detail"]
    facts = json.loads((step2 / "validate" / "candidate-facts.json").read_text())
    sig_b = [t for t in facts["tracks"] + facts["vias"] if t["net"] == "/SIG_B"]
    assert len(sig_b) == SIG_B["tracks"] + SIG_B["vias"] and not any(t["locked"] for t in sig_b)
    assert r2["validation"]["net_completion"] == {"status": route.PASS, "nets_connected_in_input": 3,
                                                  "nets_lost": []}
    assert r2["validation"]["status"] == route.PASS, r2["validation"]["reasons"]

    # the end of the chain against the origin: protection is the origin's (all of its copper)
    v = cli("validate", str(step2), "--against", str(step1.resolve()), timeout=600)
    res = json.loads((sorted((step2 / "revalidate-origin").iterdir())[-1] / "result.json").read_text())
    record("provenance_chain_against_origin", res)
    assert res["validation"]["preservation"]["protected_copper"] == route.PASS
    assert res["validation"]["preservation"]["net_completion"] == route.PASS
    assert v.returncode in (0, 2), res["validation"]["reasons"]


def test_freerouting_bridge_exports_agent_copper_unlocked(chain):
    """Freerouting's bridge locks only the human items for the round trip, so agent
    copper exports as (type route); the human items come back with their flags."""
    _, step, r = run("run", "--backend", "freerouting", "--budget", "180", "--clearance-margin", "0.01",
                     "--nets", "!/SIG_A*", "--protect", "human", "--dir", str(candidate_of(chain["step1"])),
                     "--board", "route_demo.kicad_pcb", "--label", "prov-step2-freerouting")
    record("provenance_freerouting", summary(r))
    assert r["process"]["status"] == route.PASS, r["process"]
    agent = json.loads((step / "provenance.json").read_text())["agent"]
    bridge = r["process"]["bridge"]
    assert bridge["export"]["existing_copper_locked_for_export"] == 7
    assert bridge["export"]["existing_copper_left_unlocked"] == agent["tracks"] + agent["vias"]
    assert bridge["import"]["existing_copper_locked"] == bridge["import"]["lock_flags_restored"] == 7
    wiring = (step / "work" / "board.dsn").read_text().split("(wiring", 1)[1]
    assert "(type route)" in wiring and "(type fix)" in wiring
    assert any("--protect human" in u for u in r["unsupported"])
    p = r["validation"]["preservation"]
    assert p["protected_copper"] == p["net_completion"] == route.PASS


def test_copper_edited_between_steps_counts_as_human(chain, tmp_path):
    """Fail closed: a step-1 trace the designer changed before step 2 is not copper
    any step delivered, so it is locked like the origin's."""
    import pcbnew
    step1 = copy_run(chain["step1"], tmp_path / "step1")
    board_path = candidate_of(step1) / "route_demo.kicad_pcb"
    board = pcbnew.LoadBoard(str(board_path))
    edited = next(t for t in board.GetTracks() if not isinstance(t, pcbnew.PCB_VIA) and t.GetNetname() == "/SIG_A")
    edited.SetWidth(edited.GetWidth() + pcbnew.FromMM(0.05))   # wider: a design edit, still within the rules
    board.Save(str(board_path))
    _, step2, r2 = run("run", *KRT, "--nets", "/SIG_B_OUT", "--protect", "human", "--dir", str(candidate_of(step1)),
                       "--board", "route_demo.kicad_pcb", "--label", "prov-edited")
    prov = json.loads((step2 / "provenance.json").read_text())
    record("provenance_edited", {k: v for k, v in prov.items() if k not in ("human", "agent", "unattributed")}
           | {"unattributed": prov["unattributed"]})
    s1 = chain["r1"]["routing"]["stats"]
    assert (prov["unattributed"]["tracks"], prov["unattributed"]["vias"]) == (1, 0)
    assert prov["unattributed"]["nets"] == ["/SIG_A"]
    assert (prov["human"]["tracks"], prov["human"]["vias"]) == (SIG_B["tracks"] + 1, SIG_B["vias"])
    assert prov["agent"]["tracks"] == s1["new_tracks"] - 1
    assert r2["process"]["existing_copper_locks"]["lock"]["locked"] == 8
    assert "6 tracks and 2 vias designated" in checks(step2)["protected_copper"]["detail"]


def test_ratchet_fails_a_cut_net(chain, tmp_path):
    """/SIG_A was connected in step 2's input (step 1 routed it) and is outside step
    2's scope. Cutting it in the candidate breaks no protection under --protect human
    (it is agent copper) and leaves routing COMPLETE, but the ratchet fails it."""
    import pcbnew
    run_dir = copy_run(chain["step2"], tmp_path / "step2")
    shutil.rmtree(run_dir / "validate")
    board_path = candidate_of(run_dir) / "route_demo.kicad_pcb"
    board = pcbnew.LoadBoard(str(board_path))
    board.Remove(next(t for t in board.GetTracks()
                      if not isinstance(t, pcbnew.PCB_VIA) and t.GetNetname() == "/SIG_A"))
    board.Save(str(board_path))
    r = cli("validate", str(run_dir), timeout=600)
    res = json.loads((sorted((run_dir / "revalidate").iterdir())[-1] / "result.json").read_text())
    record("ratchet", {"exit_code": r.returncode, "validation": res["validation"], "routing": res["routing"]})
    assert r.returncode == 3
    v = res["validation"]
    assert v["net_completion"]["status"] == route.FAIL and v["net_completion"]["nets_lost"] == ["/SIG_A"]
    assert v["preservation"]["net_completion"] == route.FAIL
    assert v["preservation"]["protected_copper"] == route.PASS
    assert res["routing"]["status"] == route.COMPLETE
    assert any("net_completion" in reason and "/SIG_A" in reason for reason in v["reasons"])


def test_ratchet_unit():
    """net_completion compares the nets named by unconnected items before and after."""
    from kicad_route import provenance
    item = lambda *nets: {"items": [{"description": f"Pad [{n}]"} for n in nets]}  # noqa: E731
    facts = {"nets": {"A": {}, "B": {}, "C": {}}}
    base = {"unconnected_items": [item("C")]}
    ok = provenance.net_completion(base, {"unconnected_items": [item("C")]}, facts, route.item_nets)
    assert ok["status"] == route.PASS and ok["nets_connected_in_input"] == 2
    bad = provenance.net_completion(base, {"unconnected_items": [item("B"), item("C")]}, facts, route.item_nets)
    assert bad["status"] == route.FAIL and bad["nets_lost"] == ["B"]
    assert provenance.net_completion(None, base, facts, route.item_nets)["status"] == route.NOT_RUN
