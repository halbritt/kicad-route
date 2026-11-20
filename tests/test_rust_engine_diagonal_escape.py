"""An exact SMD diagonal can fit where a four-direction grid staircase cannot."""
import json

import pcbnew
import pytest

from tests.e2e_rust_router.kicad_verify import BoardBuilder
from test_rust_engine_native import _built_rust_engine, bridge, guarded_cli as guarded_cli


def corridor(directory, offset, shift, layer):
    board = BoardBuilder(seed=127)
    side = 12_000_000
    board.set_rect_outline(0, 0, side, side)
    opposite = pcbnew.B_Cu if layer == pcbnew.F_Cu else pcbnew.F_Cu

    def contact(ref, net, xy, face):
        fp = board.add_footprint(ref, xy, side="F" if face == pcbnew.F_Cu else "B")
        fp.Reference().SetVisible(False)
        fp.Value().SetVisible(False)
        board.add_pad(fp, "1", net, xy, size_nm=(500_000, 500_000), layers=[face])

    a = (2_000_000, 2_000_000)
    b = (10_000_000, 10_000_000 + shift)
    contact("J1", "DATA", a, layer)
    contact("J2", "DATA", b, layer)
    p, q = (3_000_000, 3_000_000), (4_000_000, 4_000_000)
    contact("P1", "FIXED", p, opposite)
    contact("P2", "FIXED", q, opposite)
    locked = board.add_track("FIXED", p, q, 200_000, layer=opposite, locked=True)
    if offset is not None:
        # Block only the signal face so the locked control stays legal in all variants.
        # Absence of vias is part of the accepted positive result. Forbid vias on
        # both faces in the same two polygons so they cannot escape the channel.
        walls = [[(0, offset), (side-offset, side), (0, side)],
                 [(offset, 0), (side, side-offset), (side, 0)]]
        for ring in walls:
            board.add_keepout([layer], ring, no_tracks=True, no_vias=True)
            board.add_keepout([opposite], ring, no_tracks=False, no_vias=True)
    source = board.save(directory / "input").pcb_path
    locked_id = locked.m_Uuid.AsString()
    if shift:
        board.add_track("DATA", a, (b[0], a[1] + b[0] - a[0]), 200_000, layer=layer)
        board.add_track("DATA", (b[0], a[1] + b[0] - a[0]), b, 200_000, layer=layer)
    else:
        board.add_track("DATA", a, b, 200_000, layer=layer)
    witness = board.save(directory / "drawn").pcb_path
    return source, witness, locked_id, {a, b}


@pytest.mark.parametrize("offset,shift,layer,accept", [
    (160_000, 0, pcbnew.F_Cu, True), (145_000, 7_000, pcbnew.B_Cu, False),
    (160_000, 7_000, pcbnew.B_Cu, True), (145_000, 0, pcbnew.F_Cu, True),
    (141_421, 0, pcbnew.F_Cu, False), (126_000, 0, pcbnew.B_Cu, False),
    (None, 0, pcbnew.F_Cu, True),
])
def test_saved_octilinear_escape(tmp_path, guarded_cli, offset, shift, layer, accept):
    source, drawn, locked_id, centers = corridor(tmp_path, offset, shift, layer)
    original = source.read_bytes()
    initial = bridge.native_violations(source, tmp_path / "initial.json")
    witness = bridge.native_violations(drawn, tmp_path / "witness.json")
    assert len(initial["unconnected_items"]) == 1 and not initial["violations"]
    assert not witness["unconnected_items"]
    assert {v["type"] for v in witness["violations"]} == (set() if accept else {"items_not_allowed"})
    run, _ = guarded_cli
    engine = _built_rust_engine(run)

    def route(stem, *limits):
        result = tmp_path / (stem + ".json")
        output = tmp_path / (stem + ".kicad_pcb")
        proc = run([str(engine), "route-native", str(source), str(output),
                    "--operation-result", str(result), *limits], check=False)
        assert source.read_bytes() == original
        return proc.returncode, json.loads(result.read_bytes()), output

    code, op, output = route("saved")
    if not accept:
        assert (code, op["status"]) == (3, "unroutable"), op.get("reason")
        assert not output.exists() and op["routing"]["added_track_count"] == 0
        return
    assert (code, op["status"]) == (0, "complete"), op.get("reason")
    saved = pcbnew.LoadBoard(str(output))
    fixed = [t for t in saved.GetTracks() if t.GetNetname() == "FIXED"]
    assert len(fixed) == 1 and fixed[0].IsLocked() and fixed[0].m_Uuid.AsString() == locked_id
    drc = bridge.native_violations(output, tmp_path / "drc.json")
    assert not drc["unconnected_items"] and not drc["violations"]
    tracks = [t for t in saved.GetTracks() if t.GetNetname() == "DATA"]
    assert all(not isinstance(t, pcbnew.PCB_VIA) and t.GetLayer() == layer and t.GetWidth() == 200_000 for t in tracks)
    ends = {(tuple(t.GetStart()), tuple(t.GetEnd())) for t in tracks}
    diagonal = [(a, b) for a, b in ends if a[0] != b[0] and a[1] != b[1]]
    if offset is None:
        assert not diagonal   # originally routable Manhattan path is retained
    else:
        assert len(diagonal) == 1 and len(tracks) == 1 + bool(shift)
        assert all(abs(a[0]-b[0]) == abs(a[1]-b[1]) for a,b in diagonal)
        assert centers <= {point for segment in ends for point in segment}
    if offset == 160_000 and not shift:
        for counter, flag, status, code in [("expanded_states", "--max-expansions", "budget_exhausted", 4),
                                             ("work_used", "--limits", "work_exhausted", 5)]:
            limit = op["routing"][counter] - 1
            value = str(limit)
            if flag == "--limits":
                file = tmp_path / "limit.json"
                file.write_text(json.dumps({"max_work": limit}))
                value = str(file)
            got, stopped, none = route(status, flag, value)
            assert (got, stopped["status"]) == (code, status)
            assert stopped["routing"][counter] == limit and not none.exists()
            assert stopped["routing"]["added_track_count"] == 0
