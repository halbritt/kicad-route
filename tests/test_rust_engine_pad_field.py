"""Native routing of off-grid via escapes between unfilled SMD pad rows."""
import json

import pcbnew
import pytest

from tests.e2e_rust_router.kicad_verify import BoardBuilder, run_native_drc
from test_rust_engine_native import bridge, guarded_cli as guarded_cli, _built_rust_engine


GRID = 250_000
RULES = dict(width_nm=200_000, clearance_nm=200_000, edge_clearance_nm=500_000, grid_nm=GRID)


def field_board(directory, family, sealed):
    front, back = pcbnew.F_Cu, pcbnew.B_Cu
    board = BoardBuilder(copper_layers=2, seed=2011 if family == "array" else 8273)
    board.set_rect_outline(0, 0, 22_000_000, 17_000_000)

    def connector(reference, name, x, y, layer=front):
        fp = board.add_footprint(reference, (x, y), side="F" if layer == front else "B")
        board.add_pad(fp, "1", name, (x, y), size_nm=(550_000, 550_000), layers=[layer])

    def keepout(x1, y1, x2, y2, *, tracks):
        board.add_keepout([front], [(x1, y1), (x2, y1), (x2, y2), (x1, y2)],
                          no_tracks=tracks, no_vias=True, no_zones=False)

    if family == "array":
        fp = board.add_footprint("U1", (7_000_000, 8_000_000))
        for row in range(5):
            for col in range(5):
                name = "AUX" if row == 2 and col in (2, 3) else f"PIN_{row}_{col}"
                at = (5_000_000 + col * 1_000_000, 6_000_000 + row * 1_000_000)
                board.add_pad(fp, str(1 + 5 * row + col), name, at, shape=pcbnew.PAD_SHAPE_RECT,
                              size_nm=(450_000, 450_000), layers=[front])
        connector("J1", "DATA", 7_500_000, 4_000_000)
        connector("J2", "DATA", 7_500_000, 12_000_000)
        connector("J3", "AUX", 17_000_000, 8_000_000)
        if sealed:
            keepout(4_500_000, 5_500_000, 9_500_000, 10_500_000, tracks=False)
    else:
        # An unequal-pitch module has to leave a shielded front-side region for a rear contact.
        fp = board.add_footprint("CN1", (8_100_000, 7_400_000))
        for col in range(4):
            for row in range(6):
                name = "DRAIN" if col == 1 and row in (2, 3) else f"CONTACT_{col}_{row}"
                at = (6_800_000 + col * 1_300_000, 5_600_000 + row * 900_000)
                board.add_pad(fp, str(1 + col * 6 + row), name, at, shape=pcbnew.PAD_SHAPE_RECT,
                              size_nm=(640_000, 360_000), layers=[front])
        connector("TP1", "DRAIN", 16_125_000, 4_075_000, back)
        for frame in [(5_750_000, 4_450_000, 11_600_000, 4_850_000),
                      (5_750_000, 10_850_000, 11_600_000, 11_250_000),
                      (5_750_000, 4_450_000, 6_150_000, 11_250_000),
                      (11_200_000, 4_450_000, 11_600_000, 11_250_000)]:
            keepout(*frame, tracks=True)
        if sealed:
            keepout(5_900_000, 4_600_000, 11_450_000, 11_100_000, tracks=False)
    return board.save(directory).pcb_path


@pytest.mark.parametrize("family,sealed", [("array", False), ("array", True),
                                            ("socket", False), ("socket", True)])
def test_surface_pad_field_escape(tmp_path, guarded_cli, family, sealed):
    source = field_board(tmp_path / "src", family, sealed)
    before = source.read_bytes()
    source_drc = bridge.native_violations(source, tmp_path / "before.json")
    assert source_drc["unconnected_items"] and not source_drc["violations"]
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps(RULES))
    run, _ = guarded_cli
    engine = _built_rust_engine(run)
    output = tmp_path / "saved.kicad_pcb"
    result = tmp_path / "op.json"
    done = run([str(engine), "route-native", str(source), str(output), "--rules", str(rules),
                "--operation-result", str(result)], check=False)
    op = json.loads(result.read_bytes())
    assert source.read_bytes() == before
    if sealed:
        assert (done.returncode, op["status"]) == (3, "unroutable"), op.get("reason")
        assert not output.exists() and op["routing"]["added_track_count"] == 0
        return
    assert (done.returncode, op["status"]) == (0, "complete"), op.get("reason")
    drc = run_native_drc(output, sidecar_source=source).raw
    assert not drc["unconnected_items"] and not drc["violations"]
    saved = pcbnew.LoadBoard(str(output))
    pads = list(saved.GetPads())
    vias = [t for t in saved.GetTracks() if isinstance(t, pcbnew.PCB_VIA)]
    assert len(vias) == (2 if family == "array" else 1)
    # The inside-package via requires a phase absent from the centers and the nominal grid.
    phases = {(0, 0)} | {(p.GetPosition().x % GRID, p.GetPosition().y % GRID) for p in pads}
    assert any((v.GetPosition().x % GRID, v.GetPosition().y % GRID) not in phases for v in vias)
    assert all(not pad.HitTest(v.GetPosition(), v.GetDrillValue() // 2, pad.GetLayer())
               for v in vias for pad in pads)
    if family == "socket":
        # A fail one step before completion must retain the consumed shared budget and publish nothing.
        for flag, counter, status, code in [
            ("--max-expansions", "expanded_states", "budget_exhausted", 4),
            ("--limits", "work_used", "work_exhausted", 5),
        ]:
            limit = op["routing"][counter] - 1
            setting = str(limit)
            if flag == "--limits":
                limits = tmp_path / "limits.json"
                limits.write_text(json.dumps({"max_work": limit}))
                setting = str(limits)
            failed = tmp_path / (status + ".json")
            unbuilt = tmp_path / (status + ".kicad_pcb")
            proc = run([str(engine), "route-native", str(source), str(unbuilt), "--rules", str(rules),
                        "--operation-result", str(failed), flag, setting], check=False)
            observed = json.loads(failed.read_bytes())
            assert (proc.returncode, observed["status"]) == (code, status), observed
            assert observed["routing"][counter] == limit
            assert not unbuilt.exists() and source.read_bytes() == before


def test_single_long_surface_lead_allows_a_two_via_escape(tmp_path, guarded_cli):
    board = BoardBuilder(copper_layers=4, seed=813)
    board.set_rect_outline(0, 0, 36_000_000, 36_000_000)

    def foot(ref, xy, side="F"):
        fp = board.add_footprint(ref, xy, side=side)
        fp.Reference().SetVisible(False)
        fp.Value().SetVisible(False)
        return fp

    source_xy = (7_150_000, 15_100_000)
    connector = foot("J1", source_xy)
    for number, net, xy, size in [
        ("1", "LEFT", (5_880_000, 14_850_000), (320_000, 22_000_000)),
        ("2", "SENSE", source_xy, (300_000, 1_050_000)),
        ("3", "RIGHT", (8_420_000, 15_250_000), (320_000, 22_000_000)),
    ]:
        board.add_pad(connector, number, net, xy, size_nm=size,
                      shape=pcbnew.PAD_SHAPE_RECT, layers=[pcbnew.F_Cu])
    for ref, net, xy, layer in [
        ("R1", "SENSE", (18_250_000, 15_350_000), pcbnew.F_Cu),
        ("T1", "AUX", (3_000_000, 32_000_000), pcbnew.B_Cu),
        ("T2", "AUX", (17_000_000, 32_000_000), pcbnew.B_Cu),
    ]:
        fp = foot(ref, xy, "F" if layer == pcbnew.F_Cu else "B")
        board.add_pad(fp, "1", net, xy, size_nm=(880_000, 880_000), layers=[layer])
    source = board.save(tmp_path / "input").pcb_path
    before = run_native_drc(source).raw
    assert len(before["unconnected_items"]) == 2 and not before["violations"]
    out, operation = tmp_path / "saved.kicad_pcb", tmp_path / "operation.json"
    run, _ = guarded_cli
    engine = _built_rust_engine(run)
    # The initial forward phase can connect both terminals beneath the lead.
    run([str(engine), "route-native", str(source), str(out),
         "--operation-result", str(operation), "--max-expansions", "256"])
    routed = json.loads(operation.read_bytes())["routing"]
    assert routed["status"] == "complete" and routed["added_via_count"] == 2
    drc = run_native_drc(out, sidecar_source=source).raw
    assert not drc["violations"] and not drc["unconnected_items"]
    saved = pcbnew.LoadBoard(str(out))
    vias = [v for v in saved.GetTracks() if isinstance(v, pcbnew.PCB_VIA)]
    assert [v.GetNetname() for v in vias] == ["SENSE", "SENSE"]
