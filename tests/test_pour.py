"""pour_integrity checks (docs/pour.md) on the route_demo fixture.

A KRT candidate routed without GND (`--nets '!GND'`) leaves J1.2 and J2.2 joined to
GND only through the B.Cu pour, each with four thermal spokes. The tests edit copies
of that clean candidate the ways new routing can damage a pour (a track slicing the
pour, foreign copper crowding a thermal pad, a track cutting off part of the pour),
re-validate them, and check that pour_integrity reports the damage. The fixture
round trip itself must pass.
"""
import json
import math
import shutil

import pytest

from helpers import FIXTURE, OUT, RUNS, cli, record, run, summary  # noqa: F401
from kicad_route import cli as route

LABEL = "smoke-pour-krt"


@pytest.fixture(scope="module")
def pour_run():
    """Clean KRT candidate with GND left to the pour (round trip, graded like any attempt)."""
    r, run_dir, result = run("run", "--backend", "krt", "--budget", "180", "--default-clearance", "0.4",
                             "--nets", "!GND", "--label", LABEL)
    return r, run_dir, result


def pour_check(run_or_out):
    checks = json.loads((run_or_out / "preservation.json").read_text())
    return next(c for c in checks if c["check"] == "pour_integrity")


def test_pour_round_trip(pour_run):
    """The routed candidate keeps both GND pads on the main pour with all their spokes."""
    r, run_dir, result = pour_run
    check = pour_check(run_dir / "validate")
    delta = json.loads((run_dir / "validate" / "pour-integrity.json").read_text())
    record("pour_round_trip", {**summary(result), "pour_integrity": check})
    assert result["exit_code"] == 0, result["validation"]["reasons"]
    assert result["validation"]["preservation"]["pour_integrity"] == route.PASS
    assert check["status"] == route.PASS and not delta["failures"]
    pads = {p["pad"]: p for p in delta["pads"]}
    assert set(pads) == {"J1.2@B.Cu", "J2.2@B.Cu"}
    for p in pads.values():
        assert p["touches_fill"] == [True, True] and p["in_main"] == [True, True]
        assert p["spokes"][0] == 4 and p["spokes"][1] >= 2
    assert delta["min_spokes"] == 2 and delta["project_min_resolved_spokes"] == 2
    # the baseline's pour facts are cached with its DRC report, from the same refill
    base = run_dir / json.loads((run_dir / "attempt.json").read_text())["baseline"]["report"]
    assert (base.parent / "baseline-pour.json").exists()


def add_track(board, net, layer, a, b, width=0.2):
    import pcbnew
    t = pcbnew.PCB_TRACK(board)
    t.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(a[0]), pcbnew.FromMM(a[1])))
    t.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(b[0]), pcbnew.FromMM(b[1])))
    t.SetWidth(pcbnew.FromMM(width))
    t.SetLayer(layer)
    t.SetNet(board.FindNet(net))
    board.Add(t)


def crowd(board, ref, number, diagonals):
    """Foreign (/SIG_A_OUT) stubs across the given 45-degree spoke paths of a pad on B.Cu,
    just outside its thermal gap, so the refill cannot land those spokes."""
    import pcbnew
    pos = board.FindFootprintByReference(ref).FindPadByNumber(number).GetPosition()
    cx, cy = pcbnew.ToMM(pos.x), pcbnew.ToMM(pos.y)
    for sx, sy in diagonals:
        r = 1.75  # pad radius 0.85 + gap 0.5 + track half-width and a little
        mx, my = cx + sx * r / math.sqrt(2), cy + sy * r / math.sqrt(2)
        px, py = -sy * 0.45 / math.sqrt(2), sx * 0.45 / math.sqrt(2)  # perpendicular, 0.9 mm long
        add_track(board, "/SIG_A_OUT", pcbnew.B_Cu, (mx - px, my - py), (mx + px, my + py))


def revalidate(tmp_path, src, edit, *args):
    """Copy a passing run, edit its candidate board with pcbnew, validate it again."""
    import pcbnew
    run_dir = tmp_path / "run"
    shutil.copytree(src.resolve(), run_dir, symlinks=True)
    shutil.rmtree(run_dir / "validate")
    attempt = json.loads((run_dir / "attempt.json").read_text())
    board_path = run_dir / attempt["candidate_dir"] / attempt["board"]
    board = pcbnew.LoadBoard(str(board_path))
    edit(board)
    board.Save(str(board_path))
    attempt["baseline"]["report"] = str((src.resolve() / attempt["baseline"]["report"]).resolve())
    (run_dir / "attempt.json").write_text(json.dumps(attempt))
    r = cli("validate", str(run_dir), *args, timeout=600)
    out = sorted((run_dir / "revalidate").iterdir())[-1]
    res = json.loads((out / "result.json").read_text())
    delta = json.loads((out / "pour-integrity.json").read_text())
    return r, res, pour_check(out), delta


def failures(delta, kind):
    return {f.get("pad") or f.get("net_layer"): f for f in delta["failures"] if f["kind"] == kind}


def test_pour_slice_isolates_pad(tmp_path, pour_run):
    """A signal track across the whole pour cuts J2's side off: J2.2 ends up on an
    island with no via or other path to the main GND pour."""
    import pcbnew

    def edit(board):
        add_track(board, "/SIG_A_OUT", pcbnew.B_Cu, (142, 100.4), (142, 129.6))
    r, res, check, delta = revalidate(tmp_path, pour_run[1], edit)
    record("pour_slice", {"exit_code": r.returncode, "pour_integrity": check})
    assert r.returncode == 3 and res["validation"]["preservation"]["pour_integrity"] == route.FAIL
    iso = failures(delta, "isolated_island")
    assert set(iso) == {"J2.2@B.Cu"}, delta["failures"]
    f = iso["J2.2@B.Cu"]
    assert f["in_main"] == [True, False] and f["touches_fill"] == [True, True] and not f["other_path"]
    assert [i["pads"] for i in f["island"]] == [["J2.2"]] and all(i["vias"] == 0 for i in f["island"])
    layer = next(x for x in delta["layers"] if x["net_layer"] == "GND@B.Cu")
    assert layer["polygons"] == [1, 2] and layer["islands_off_main"] == [0, 1]
    assert any("pour_integrity" in reason and "isolated_island" in reason for reason in res["validation"]["reasons"])


@pytest.mark.parametrize("sides,kind", [(3, "spokes_below_minimum"), (4, "lost_fill_contact")])
def test_pour_thermal_crowding(tmp_path, pour_run, sides, kind):
    """Foreign copper beside J1.2 leaves it one spoke (below min_resolved_spokes 2), or none."""
    diagonals = [(1, -1), (-1, -1), (-1, 1), (1, 1)][:sides]
    r, res, check, delta = revalidate(tmp_path, pour_run[1], lambda b: crowd(b, "J1", "2", diagonals))
    record(f"pour_crowd_{sides}", {"exit_code": r.returncode, "pour_integrity": check})
    assert res["validation"]["preservation"]["pour_integrity"] == route.FAIL
    hit = failures(delta, kind)
    assert set(hit) == {"J1.2@B.Cu"}, delta["failures"]
    f = hit["J1.2@B.Cu"]
    if kind == "spokes_below_minimum":
        assert f["spokes"] == [4, 1] and f["minimum"] == 2 and f["touches_fill"] == [True, True]
    else:
        assert f["touches_fill"] == [True, False] and f["spokes"][1] == 0
    assert not failures(delta, "isolated_island")


def test_pour_outline_loses_pad(tmp_path, pour_run):
    """A GND zone whose outline no longer covers J2.2 does not reach that pad at all, so the
    candidate's pour facts have no record for it. That is a lost pour (lost_pad_reach), not
    the informational pad_reach_changed; J1.2 is still compared as before."""
    import pcbnew

    def edit(board):
        z = next(z for z in board.Zones() if z.GetNetname() == "GND")
        outline = z.Outline()
        outline.RemoveAllContours()
        outline.NewOutline()
        for x, y in ((100.5, 100.5), (140, 100.5), (140, 129.5), (100.5, 129.5)):
            outline.Append(pcbnew.FromMM(x), pcbnew.FromMM(y))
    r, res, check, delta = revalidate(tmp_path, pour_run[1], edit)
    record("pour_lost_reach", {"exit_code": r.returncode, "pour_integrity": check})
    assert r.returncode == 3 and res["validation"]["preservation"]["pour_integrity"] == route.FAIL
    lost = failures(delta, "lost_pad_reach")
    assert set(lost) == {"J2.2@B.Cu"}, delta["failures"]
    f = lost["J2.2@B.Cu"]
    assert f["connection"] == "thermal" and f["touches_fill"] == [True, False] and f["spokes"] == [4, 0]
    assert not [i for i in delta["informational"] if i["kind"] == "pad_reach_changed"]
    assert {p["pad"] for p in delta["pads"]} == {"J1.2@B.Cu"}
    assert not failures(delta, "lost_fill_contact") and not failures(delta, "isolated_island")
    assert any("pour_integrity" in reason and "lost_pad_reach" in reason for reason in res["validation"]["reasons"])


def test_pour_area_loss_threshold(tmp_path, pour_run):
    """A track near the bottom edge cuts off a strip of pour with no pads (KiCad removes
    it as an island). Area loss is informational unless --pour-area-loss is given."""
    import pcbnew

    def edit(board):
        add_track(board, "/SIG_A_OUT", pcbnew.B_Cu, (100.4, 128.2), (149.6, 128.2))
    r, res, check, delta = revalidate(tmp_path, pour_run[1], edit)
    layer = next(x for x in delta["layers"] if x["net_layer"] == "GND@B.Cu")
    assert layer["area_delta_pct"] < -1
    assert check["status"] == route.PASS, check   # reported, not failed
    r2, res2, check2, delta2 = revalidate(tmp_path / "limit", pour_run[1], edit, "--pour-area-loss", "1")
    record("pour_area_loss", {"default": check, "with_limit": check2})
    assert check2["status"] == route.FAIL
    assert set(failures(delta2, "area_loss")) == {"GND@B.Cu"} and delta2["area_loss_limit_pct"] == 1.0
