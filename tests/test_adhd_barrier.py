"""Collision barriers (`run --barrier-from RUN_DIR`, `kicad-route barrier RUN_DIR`), KICADROUTE-53.

A /SIG_A track routed 0.3748 mm from C1.2 (GND) breaks the fixture's 0.4 mm
power_to_signal rule; KRT's unscoped route does exactly that. A synthetic run
carrying a hand-placed copy of that track drives the planning, injection and strip
tests without routing; one short KRT pair (the colliding run, then a rewind with a
barrier) checks that the backend routes around the barrier and that the stripped
candidate passes every preservation check, keepouts included.
"""
import importlib.util
import json
import shutil
import subprocess

import pytest

from helpers import FIXTURE, OUT, ROOT, cli, record, run, summary
from kicad_route import barrier, common, ledger
from kicad_route import cli as route

SCRIPT = ROOT / "src" / "kicad_route" / "kicad" / "barrier.py"
BOARD = "route_demo.kicad_pcb"
MM = 1_000_000
C1_2 = "ed710432-edbd-4efe-9cf2-6ec252719503"  # Pad 2 [GND] of C1, F.Cu
# The /SIG_A diagonal KRT routed on the fixture (0.2 mm, F.Cu), 0.3748 mm from C1.2.
TRACK = ((113.7, 103.0), (105.6, 111.1))

spec = importlib.util.spec_from_file_location("barrier_script", SCRIPT)
geo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(geo)


def script(*args):
    r = subprocess.run([str(common.PCBNEW_PYTHON), str(SCRIPT), *map(str, args)], capture_output=True, text=True,
                       env=common.tool_env(), timeout=600)
    tag = next((json.loads(l.split(":", 1)[1]) for l in r.stdout.splitlines() if l.startswith("BARRIER_JSON:")),
               None)
    return r, tag


def facts(board, out):
    f, rec = route.board_facts(board, out)
    assert f is not None, rec
    return f


@pytest.fixture(scope="module")
def collided_run(tmp_path_factory):
    """A graded-run directory whose candidate has the colliding /SIG_A track and whose
    new-findings.json carries the clearance finding KiCad reports for it."""
    import pcbnew
    rd = tmp_path_factory.mktemp("barrier") / "collided"
    shutil.copytree(FIXTURE, rd / "reference" / "route_demo", ignore=shutil.ignore_patterns("generate.py", "__pycache__"))
    cand = rd / "candidate" / "route_demo"
    shutil.copytree(rd / "reference" / "route_demo", cand)
    board = pcbnew.LoadBoard(str(cand / BOARD))
    t = pcbnew.PCB_TRACK(board)
    t.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(TRACK[0][0]), pcbnew.FromMM(TRACK[0][1])))
    t.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(TRACK[1][0]), pcbnew.FromMM(TRACK[1][1])))
    t.SetWidth(pcbnew.FromMM(0.2))
    t.SetLayer(pcbnew.F_Cu)
    t.SetNet(board.FindNet("/SIG_A"))
    board.Add(t)
    uuid = t.m_Uuid.AsString()
    board.Save(str(cand / BOARD))
    (rd / "facts").mkdir()
    facts(rd / "reference" / "route_demo" / BOARD, rd / "facts" / "reference-facts.json")
    (rd / "validate").mkdir()
    facts(cand / BOARD, rd / "validate" / "candidate-facts.json")
    finding = {"type": "clearance", "severity": "error",
               "description": "Clearance violation (rule 'power_to_signal_clearance' clearance 0.4000 mm; "
                              "actual 0.3748 mm)",
               "items": [{"description": "Track [/SIG_A] on F.Cu, length 11.4551 mm", "pos": {"x": 113.7, "y": 103.0},
                          "uuid": uuid},
                         {"description": "Pad 2 [GND] of C1 on F.Cu", "pos": {"x": 110.95, "y": 104.0},
                          "uuid": C1_2}]}
    unrelated = {"type": "silk_overlap", "severity": "warning", "description": "silk",
                 "items": [{"uuid": uuid}, {"uuid": C1_2}]}
    common.write_json(rd / "validate" / "new-findings.json",
                      {"violations": [finding, unrelated], "schematic_parity": [], "resolved_violations": []})
    common.write_json(rd / "attempt.json", {
        "board": BOARD, "reference_dir": "reference/route_demo", "candidate_dir": "candidate/route_demo",
        "reference_facts": "facts/reference-facts.json", "input_fingerprint": "synthetic"})
    return rd, uuid


# ------------------------------------------------------------- pure helpers

def test_geometry_helpers():
    sq = [(0, 0), (10, 0), (10, 10), (0, 10)]
    d, p = geo.closest_on_path([(-5, 12), (15, 12)], [sq])
    assert d == pytest.approx(2) and p[1] == 12 and 0 <= p[0] <= 10
    d, p = geo.closest_on_path([(-5, 5), (15, 5)], [sq])            # crosses: the middle of the overlap
    assert d == 0 and p == pytest.approx((5, 5))
    d, p = geo.closest_on_path([(-5, 5), (5, 5)], [sq])             # ends inside: between entry and end
    assert d == 0 and p[1] == 5 and 0 <= p[0] <= 5
    d, p = geo.closest_on_path([(2, 5), (8, 5)], [sq])              # wholly inside
    assert d == 0 and p == pytest.approx((5, 5))
    d, p = geo.closest_on_path([(20, 20)], [sq])                    # a via: its centre
    assert p == (20, 20) and d == pytest.approx(10 * 2 ** 0.5)
    assert geo.polys_touch(geo.square((5, 11), 2), sq)              # edge contact counts
    assert not geo.polys_touch(geo.square((5, 12.5), 2), sq)
    assert geo.barrier_name(111712500, 104987500) == "kicad-route:barrier:111.7125:104.9875"
    assert geo.barrier_name(100_000_000, -2_500_000) == "kicad-route:barrier:100:-2.5"


def test_select_keeps_collisions_made_by_new_copper():
    def v(kind, *uuids):
        return {"type": kind, "items": [{"uuid": u} for u in uuids]}
    reqs, skipped = barrier.select([v("clearance", "new1", "pad"), v("clearance", "old", "pad"),
                                    v("starved_thermal", "pad"), v("shorting_items", "pad", "new2"),
                                    v("tracks_crossing", "new1")], {"new1", "new2"})
    assert [(r["index"], r["new_uuid"], r["other_uuids"]) for r in reqs] == [(0, "new1", ["pad"]),
                                                                             (3, "new2", ["pad"])]
    assert {s["index"]: s["reason"].split(" ")[0] for s in skipped} == {1: "no", 2: "not", 4: "a"}


def test_ledger_sees_the_barriers():
    rec = {"from": "x", "size_mm": 0.05, "input_fingerprint": "fp",
           "barriers": [{"x": 2, "y": 3, "layer": "F.Cu"}, {"x": 1, "y": 1, "layer": "B.Cu"}]}
    s = barrier.settings_of(rec)
    assert s == {"source_input": "fp", "size_mm": 0.05, "points": [[1, 1, "B.Cu"], [2, 3, "F.Cu"]]}
    assert barrier.settings_of(None) is None
    plain = {"backend": "krt", "budget_s": 180, "barrier": None}
    assert ledger.attempt_key("i", "krt", plain) == ledger.attempt_key("i", "krt", {"backend": "krt", "budget_s": 180})
    assert ledger.attempt_key("i", "krt", plain) != ledger.attempt_key("i", "krt", dict(plain, barrier=s))


# ------------------------------------------------------------- pcbnew: plan, inject, strip

def test_plan_locates_the_collision(collided_run, tmp_path):
    rd, uuid = collided_run
    rec = barrier.plan(rd, tmp_path / "plan")
    assert not rec["reasons"], rec
    assert [s["type"] for s in rec["skipped"]] == ["silk_overlap"]
    (b,) = rec["barriers"]
    assert b["new_item"] == {"uuid": uuid, "kind": "PCB_TRACK", "net": "/SIG_A"} and b["other_uuids"] == [C1_2]
    assert b["layer"] == "F.Cu" and b["type"] == "clearance"
    # on the track's centreline, at its closest approach to the pad: 0.3748 mm + half the 0.2 mm width
    assert b["distance_to_other_copper"] == pytest.approx(474_784, abs=200)
    assert (b["x"], b["y"]) == (pytest.approx(111_712_500, abs=2000), pytest.approx(104_987_500, abs=2000))
    assert b["name"] == geo.barrier_name(b["x"], b["y"])
    assert (tmp_path / "plan" / "barrier-plan.json").exists()


def test_plan_without_a_collision_gives_reasons(collided_run, tmp_path):
    rd, _ = collided_run
    other = tmp_path / "quiet"
    shutil.copytree(rd, other)
    common.write_json(other / "validate" / "new-findings.json", {"violations": [], "schematic_parity": []})
    rec = barrier.plan(other, tmp_path / "p")
    assert rec["barriers"] == [] and "no copper collision" in rec["reasons"][0]
    assert "not a routing run" in barrier.plan(tmp_path, tmp_path / "q")["reasons"][0]


def test_inject_strip_round_trip(collided_run, tmp_path):
    rd, _ = collided_run
    rec = barrier.plan(rd, tmp_path / "plan")
    board = tmp_path / "r" / BOARD
    shutil.copytree(rd / "reference" / "route_demo", board.parent)
    original = board.read_bytes()
    before = facts(board, tmp_path / "before.json")
    r, tag = script("inject", board, tmp_path / "plan" / "barrier-plan.json")
    assert r.returncode == 0 and tag["injected"] == 1 and tag["skipped"] == [], r.stdout + r.stderr
    injected = facts(board, tmp_path / "injected.json")
    ours = [a for a in injected["rule_areas"] if a["name"].startswith("kicad-route:barrier:")]
    assert [a["name"] for a in ours] == [rec["barriers"][0]["name"]]
    (a,) = ours
    assert a["layers"] == ["F.Cu"] and a["no_tracks"] and a["no_vias"]
    assert not (a["no_pads"] or a["no_footprints"] or a["no_zone_fills"])
    assert len(injected["rule_areas"]) == len(before["rule_areas"]) + 1
    assert injected["zones"] == before["zones"] and injected["tracks"] == before["tracks"]
    # Freerouting sees it: KiCad exports it as one more single-layer Specctra keepout on F.Cu
    dsn = tmp_path / "r.dsn"
    e = subprocess.run([str(common.PCBNEW_PYTHON), str(route.KICAD_SCRIPTS / "specctra.py"), "export", board, dsn],
                       capture_output=True, text=True, env=common.tool_env(), timeout=600)
    assert e.returncode == 0, e.stdout + e.stderr
    ref_dsn = tmp_path / "ref.dsn"
    e = subprocess.run([str(common.PCBNEW_PYTHON), str(route.KICAD_SCRIPTS / "specctra.py"), "export",
                        rd / "reference" / "route_demo" / BOARD, ref_dsn],
                       capture_output=True, text=True, env=common.tool_env(), timeout=600)
    assert e.returncode == 0, e.stdout + e.stderr
    key = '(keepout "" (polygon F.Cu'
    assert dsn.read_text().count(key) == ref_dsn.read_text().count(key) + 1
    again, _ = script("inject", board, tmp_path / "plan" / "barrier-plan.json")
    assert again.returncode != 0 and "strip them first" in again.stderr
    r, tag = script("strip", board)
    assert r.returncode == 0 and tag["stripped"] == 1 and tag["remaining"] == 0
    assert facts(board, tmp_path / "after.json")["rule_areas"] == before["rule_areas"]
    assert board.read_bytes() == original
    r, tag = script("strip", board)                               # nothing to strip: file untouched
    assert tag["stripped"] == 0 and board.read_bytes() == original


def test_inject_skips_a_barrier_on_existing_copper(collided_run, tmp_path):
    """A keepout over existing copper would block that copper's own net: skipped, board untouched."""
    rd, _ = collided_run
    board = tmp_path / "r" / BOARD
    shutil.copytree(rd / "reference" / "route_demo", board.parent)
    original = board.read_bytes()
    plan = {"size": 50_000, "barriers": [{"x": 110_950_000, "y": 104_000_000, "layer": "F.Cu",   # C1.2 centre
                                          "name": geo.barrier_name(110_950_000, 104_000_000)}]}
    common.write_json(tmp_path / "plan.json", plan)
    r, tag = script("inject", board, tmp_path / "plan.json")
    assert r.returncode == 0 and tag["injected"] == 0, r.stdout + r.stderr
    assert "overlaps existing copper (PAD GND)" in tag["skipped"][0]["reason"]
    assert board.read_bytes() == original


def test_barrier_preview_command(collided_run):
    rd, _ = collided_run
    ref_before = (rd / "reference" / "route_demo" / BOARD).read_bytes()
    r = cli("barrier", str(rd), timeout=600)
    assert r.returncode == 0, r.stdout + r.stderr
    rec = json.loads((rd / "barrier-preview" / "barrier-plan.json").read_text())
    assert rec["injected"] == 1
    text = (rd / "barrier-preview" / "route_demo" / BOARD).read_text()
    assert text.count('(name "kicad-route:barrier:') == 1
    assert (rd / "reference" / "route_demo" / BOARD).read_bytes() == ref_before  # the copy, never the reference


def test_run_refuses_without_a_collision(collided_run, tmp_path):
    """--barrier-from a run with nothing to barrier: a process failure before routing (exit 1)."""
    rd, _ = collided_run
    quiet = tmp_path / "quiet"
    shutil.copytree(rd, quiet)
    common.write_json(quiet / "validate" / "new-findings.json", {"violations": [], "schematic_parity": []})
    r, run_dir, result = run("run", "--backend", "krt", "--budget", "60", "--nets", "/SIG_A",
                             "--barrier-from", str(quiet), "--label", "smoke-barrier-none")
    assert r.returncode == 1 and result["process"]["status"] == route.FAIL
    assert any("no copper collision" in x for x in result["process"]["reasons"])
    assert not (run_dir / "work" / "krt.log").exists()          # the backend never ran


# ------------------------------------------------------------- one rewind on KRT

def test_krt_rewind_routes_around_the_barrier():
    """Route /SIG_A with KRT (it lands 0.3748 mm from C1.2), then rewind: the same input
    with a barrier where that track ran. The second route must avoid the barrier, the
    collision must not recur, and the stripped candidate must pass every check."""
    r1, run1, res1 = run("run", "--backend", "krt", "--budget", "180", "--nets", "/SIG_A",
                         "--label", "smoke-barrier-collide")
    new = json.loads((run1 / "validate" / "new-findings.json").read_text())["violations"]
    if not any(v["type"] == "clearance" for v in new):
        pytest.skip(f"this KRT build no longer collides on /SIG_A (exit {res1['exit_code']}): nothing to rewind")
    r2, run2, res2 = run("run", "--backend", "krt", "--budget", "180", "--nets", "/SIG_A",
                         "--barrier-from", str(run1), "--label", "smoke-barrier-rewind")
    b = res2["process"]["barrier"]
    record("barrier_rewind_krt", summary(res2) | {"collided": summary(res1), "barrier":
                                                  {k: b.get(k) for k in ("barriers", "injected", "stripped",
                                                                         "remaining", "outcome", "skipped")}})
    assert res2["settings"]["barrier"]["points"] and b["injected"] >= 1
    assert b["stripped"] == b["injected"] and b["remaining"] == 0 and not b["reasons"]
    assert res2["validation"]["preservation"]["keepouts"] == route.PASS
    cand = (OUT / res2["candidate"]["board"]).read_text()
    assert "kicad-route:barrier:" not in cand
    assert "kicad-route:barrier:" in (run2 / "work" / "input" / "route_demo" / BOARD).read_text()
    assert "kicad-route:barrier:" not in (run2 / "reference" / "route_demo" / BOARD).read_text()
    assert b["outcome"]["entered"] == 0 and b["outcome"]["recurred"] == 0, b["outcome"]
    assert all(v == route.PASS for v in res2["validation"]["preservation"].values()), res2["validation"]
    assert res2["exit_code"] == 0, res2["validation"]
