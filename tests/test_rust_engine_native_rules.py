"""Public custom-rule acceptance under the installed native KiCad rule compiler."""
import json

import pcbnew
import pytest

from tests.test_rust_engine_native import _built_rust_engine, bridge, guarded_cli, native_board


@pytest.fixture
def unrouted_board(native_board):
    board = pcbnew.LoadBoard(str(native_board))
    for track in list(board.GetTracks()):
        board.Remove(track)
    assert pcbnew.SaveBoard(str(native_board), board, True)
    return native_board


@pytest.mark.parametrize(('condition', 'width'), [
    ("A.Type == 'tRaCk'", 350000),
    ("A.Type == 'Tr*' && A.NetName == 'sIg??l'", 350000),
    ("'Tr*' == A.Type", 200000),
    ("A.Layer == 'F.*'", 350000),
    ("'F.*' == A.Layer", 200000),
])
def test_native_condition_patterns_route_at_the_applicable_width(
        unrouted_board, tmp_path, guarded_cli, condition, width):
    run, _ = guarded_cli
    rules = unrouted_board.with_suffix('.kicad_dru')
    text = f'(version 1)\n(rule wide (condition "{condition}") (constraint track_width (min 0.35mm)))\n'
    rules.write_text(text)
    output = tmp_path / 'routed.kicad_pcb'
    proc = run([str(_built_rust_engine(run)), 'route-native', str(unrouted_board), str(output)], check=False)
    operation = json.loads(proc.stdout)
    assert operation['status'] == 'complete', operation.get('reason')
    assert proc.returncode == 0
    assert rules.read_text() == text
    board = pcbnew.LoadBoard(str(output))
    board.BuildConnectivity()
    assert board.GetConnectivity().GetUnconnectedCount(False) == 0
    added_widths = {item.GetWidth() for item in board.GetTracks()
                    if item.GetNetname() == 'SIGNAL' and not isinstance(item, pcbnew.PCB_VIA)}
    assert added_widths == {width}


def test_native_hole_size_sets_the_drill_on_each_generated_via(unrouted_board, tmp_path, guarded_cli):
    board = pcbnew.LoadBoard(str(unrouted_board))
    back = pcbnew.LSET()
    back.AddLayer(pcbnew.B_Cu)
    next(pad for pad in board.GetPads() if pad.GetParentFootprint().GetReference() == 'J2').SetLayerSet(back)
    assert pcbnew.SaveBoard(str(unrouted_board), board, True)
    rules = unrouted_board.with_suffix('.kicad_dru')
    rules.write_text("""(version 1)
(rule drill (condition "A.Type == 'via'") (constraint hole_size (min 0.45mm)))
""")
    output = tmp_path / 'routed.kicad_pcb'
    run, _ = guarded_cli
    proc = run([str(_built_rust_engine(run)), 'route-native', str(unrouted_board), str(output)], check=False)
    operation = json.loads(proc.stdout)
    assert proc.returncode == 0 and operation['status'] == 'complete', operation.get('reason')
    saved = pcbnew.LoadBoard(str(output))
    assert {v.GetDrillValue() for v in saved.GetTracks() if isinstance(v, pcbnew.PCB_VIA)} == {450000}


@pytest.mark.parametrize(('extra', 'reason'), [
    (b'(rule broken (constraint via_drill (min 0.1mm)))', 'via_drill'),
    (b'(rule broken (layer "No.Such.Layer") (constraint track_width (min 0.9mm)))',
     'KiCad did not load the supplied custom design rules'),
])
def test_native_refuses_file_with_valid_width_rule_and_invalid_rule(
        unrouted_board, tmp_path, guarded_cli, extra, reason):
    run, _ = guarded_cli
    rules = unrouted_board.with_suffix('.kicad_dru')
    text = (b"(version 1)\n(rule wide (condition \"A.Type == 'track'\") "
            b"(constraint track_width (min 0.35mm)))\n"
            + extra + b"\n")
    rules.write_bytes(text)
    source = unrouted_board.read_bytes()
    output = tmp_path / 'routed.kicad_pcb'
    proc = run([str(_built_rust_engine(run)), 'route-native', str(unrouted_board), str(output)], check=False)
    operation = json.loads(proc.stdout)
    assert proc.returncode != 0
    assert reason in operation['reason']
    assert not operation['publication']['published']
    assert not output.exists()
    assert unrouted_board.read_bytes() == source and rules.read_bytes() == text


def test_native_custom_rule_check_detects_a_discarded_file_on_an_unrouted_board(unrouted_board):
    original = unrouted_board.read_bytes()
    pro = unrouted_board.with_suffix('.kicad_pro')
    project = pro.read_bytes() if pro.is_file() else None
    valid = b'(version 1)\n(rule drill (constraint hole_size (min 0.3mm)))\n'
    bridge.verify_native_rule_loading(unrouted_board, project, valid, 30)
    with pytest.raises(bridge.NativeValidationError, match='custom design rules|rule_compile_error|console error'):
        bridge.verify_native_rule_loading(
            unrouted_board, project, valid + b'(rule bad (constraint no_such_constraint (min 0.1mm)))\n', 30)
    assert unrouted_board.read_bytes() == original
    assert not unrouted_board.with_suffix('.kicad_dru').exists()
