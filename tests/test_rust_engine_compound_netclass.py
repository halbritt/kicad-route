"""KiCad's loaded project is the oracle for the router's compound netclass sizes."""
import hashlib
import json
import shutil
import uuid

import pcbnew
import pytest

from tests.test_rust_engine_native import (
    ROOT, _built_rust_engine, bridge, connection_tracks, guarded_cli, native_board,
)


@pytest.mark.parametrize('version,reverse,expected', [
    (3, False, ('Power,HV', 500000, 800000, 400000)),
    (3, True, ('HV,Power', 250000, 700000, 350000)),
    (5, False, ('HV,Power', 250000, 700000, 350000)),
])
def test_published_vin_uses_loaded_compound_class(tmp_path, guarded_cli, version, reverse, expected):
    source = tmp_path / 'source'
    shutil.copytree(ROOT / 'fixtures/route_demo', source)
    pro = source / 'route_demo.kicad_pro'
    project = json.loads(pro.read_text())
    ns = project['net_settings']
    ns['meta'] = {'version': version}
    default, power = ns['classes']
    hv = dict(power, name='HV', track_width=0.25, via_diameter=0.7, via_drill=0.35, priority=0)
    power['priority'] = 1
    ns['classes'] = [default, hv, power] if reverse else [default, power, hv]
    ns['netclass_patterns'].append({'pattern': '/VIN', 'netclass': 'HV'})
    pro.write_text(json.dumps(project))
    output = tmp_path / 'route_demo.kicad_pcb'
    shutil.copy(pro, output.with_suffix('.kicad_pro'))
    shutil.copy(source / 'route_demo.kicad_dru', output.with_suffix('.kicad_dru'))
    input_board = pcbnew.LoadBoard(str(source / output.name))
    preexisting = {bridge.item_id(item) for item in input_board.GetTracks()}
    run, _ = guarded_cli
    run([str(_built_rust_engine(run)), 'route-native', str(source / output.name), str(output)])
    saved = pcbnew.LoadBoard(str(output))
    vin = saved.GetDesignSettings().m_NetSettings.GetEffectiveNetClass('/VIN')
    assert (vin.GetName(), vin.GetTrackWidth(), vin.GetViaDiameter(), vin.GetViaDrill()) == expected
    new = [item for item in saved.GetTracks() if item.GetNetname() == '/VIN'
           and bridge.item_id(item) not in preexisting]
    assert new
    for item in new:
        if isinstance(item, pcbnew.PCB_VIA):
            assert (item.GetWidth(), item.GetDrillValue()) == expected[2:]
        else:
            assert item.GetWidth() == expected[1]


@pytest.mark.parametrize('field,width,diameter,drill', [
    ('track width', 350000, 850000, 400000),
    ('via diameter', 500000, 700000, 400000),
    ('via drill', 500000, 850000, 300000),
])
def test_native_publication_rejects_each_undersized_compound_dimension(
        native_board, tmp_path, field, width, diameter, drill):
    classes = [{'name': 'Width', 'priority': 0, 'track_width': 0.5},
               {'name': 'Diameter', 'priority': 1, 'track_width': 0.25, 'via_diameter': 0.85},
               {'name': 'Default', 'clearance': 0.2, 'track_width': 0.25,
                'via_diameter': 0.6, 'via_drill': 0.4}]
    native_board.with_suffix('.kicad_pro').write_text(json.dumps({
        'meta': {'version': 1}, 'net_settings': {'meta': {'version': 5}, 'classes': classes,
        'netclass_assignments': {'SIGNAL': ['Width', 'Diameter']}}}))
    # Load under a fresh project path. KiCad cached the fixture before this sidecar existed.
    with bridge.source_snapshot(native_board) as snapshot:
        board = pcbnew.LoadBoard(str(snapshot))
        native = board.GetDesignSettings().m_NetSettings.GetEffectiveNetClass('SIGNAL')
        assert (native.GetTrackWidth(), native.GetViaDiameter(), native.GetViaDrill()) == (500000, 850000, 400000)
    request = tmp_path / 'req.json'
    bridge.write_request(bridge.export_board(native_board), request)
    added = connection_tracks()
    for item in added:
        item['width_nm'] = width
    via = dict(id=str(uuid.uuid4()), net=1, at=added[0]['end'], diameter_nm=diameter,
               drill_nm=drill, start_layer='F.Cu', end_layer='B.Cu', kind='through', locked=False)
    req = request.read_bytes()
    plan = dict(version=3, status='complete', source_id=hashlib.sha256(native_board.read_bytes()).hexdigest(),
                request_sha256=hashlib.sha256(req).hexdigest(), expanded_states=0, work_used=0,
                added_tracks=added, added_vias=[via], removed_track_ids=[])
    if field == 'track width':
        # An unsolicited pair declaration cannot lower the net's ordinary width.
        plan['diff_pairs'] = [dict(pos_net=1, neg_net=2, pos_name='SIGNAL', neg_name='OBSTACLE')]
    destination = tmp_path / 'rejected.kicad_pcb'
    with pytest.raises(bridge.NativeValidationError, match=field + '.*native netclass'):
        bridge.apply_plan(native_board, plan, destination, expected_request=req)
    assert not destination.exists()


def test_sparse_compound_dimensions_and_constituent_dru_apply_to_routed_via(
        native_board, tmp_path, guarded_cli):
    board = pcbnew.LoadBoard(str(native_board))
    board.SetCopperLayerCount(4)
    back = pcbnew.LSET()
    back.AddLayer(pcbnew.B_Cu)
    next(iter(next(iter(board.GetFootprints())).Pads())).SetLayerSet(back)
    assert pcbnew.SaveBoard(str(native_board), board)
    source_pro = native_board.with_suffix('.kicad_pro')
    source_pro.write_text(json.dumps({
        'meta': {'version': 1}, 'net_settings': {'meta': {'version': 5}, 'classes': [
            {'name': 'Power', 'priority': 0, 'clearance': 0.2, 'track_width': 0.5},
            {'name': 'Drilled', 'priority': 1, 'track_width': 0.25, 'via_diameter': 0.85},
            {'name': 'Default', 'track_width': 0.25, 'clearance': 0.2,
             'via_diameter': 0.6, 'via_drill': 0.4}],
        'netclass_assignments': {'SIGNAL': ['Drilled', 'Power']}}}))
    source_dru = native_board.with_suffix('.kicad_dru')
    source_dru.write_text('''(version 1)
(rule "supply width" (condition "A.NetClass == 'Power'") (constraint track_width (min 0.55mm)))
(rule "supply hole" (condition "A.NetClass == 'Drilled'") (constraint via_diameter (min 0.95mm)))
''')
    output = tmp_path / 'verified.kicad_pcb'
    shutil.copy(source_pro, output.with_suffix('.kicad_pro'))
    shutil.copy(source_dru, output.with_suffix('.kicad_dru'))
    run, _ = guarded_cli
    run([str(_built_rust_engine(run)), 'route-native', str(native_board), str(output)])
    saved = pcbnew.LoadBoard(str(output))
    tracks = [item for item in saved.GetTracks() if item.GetNetname() == 'SIGNAL']
    vias = [item for item in tracks if isinstance(item, pcbnew.PCB_VIA)]
    assert vias and len(tracks) > len(vias)
    assert {(v.GetWidth(), v.GetDrillValue()) for v in vias} == {(950000, 400000)}
    assert {t.GetWidth() for t in tracks if not isinstance(t, pcbnew.PCB_VIA)} == {550000}


def test_saved_differential_width_and_microvia_use_their_own_class_dimensions(tmp_path):
    source = tmp_path / 'pair.kicad_pcb'
    board = pcbnew.BOARD()
    board.SetCopperLayerCount(4)
    for code, name in [(1, 'USB_P'), (2, 'USB_N')]:
        board.Add(pcbnew.NETINFO_ITEM(board, name, code))
        track = pcbnew.PCB_TRACK(board)
        track.SetNetCode(code)
        track.SetLayer(pcbnew.F_Cu)
        track.SetStart(pcbnew.VECTOR2I(1000000, code * 1000000))
        track.SetEnd(pcbnew.VECTOR2I(5000000, code * 1000000))
        track.SetWidth(180000)
        board.Add(track)
    via = pcbnew.PCB_VIA(board)
    via.SetNetCode(1)
    via.SetViaType(pcbnew.VIATYPE_MICROVIA)
    via.SetLayerPair(pcbnew.F_Cu, pcbnew.In1_Cu)
    via.SetPosition(pcbnew.VECTOR2I(5000000, 1000000))
    via.SetWidth(300000)
    via.SetDrill(100000)
    board.Add(via)
    assert pcbnew.SaveBoard(str(source), board)
    source.with_suffix('.kicad_pro').write_text(json.dumps({
        'meta': {'version': 1}, 'net_settings': {'meta': {'version': 5}, 'classes': [{
            'name': 'Default', 'track_width': 0.6, 'diff_pair_width': 0.18, 'clearance': 0.16,
            'via_diameter': 0.8, 'via_drill': 0.35, 'microvia_diameter': 0.3, 'microvia_drill': 0.1}]}}))
    with bridge.source_snapshot(source) as snapshot:
        saved = pcbnew.LoadBoard(str(snapshot))
    pos, neg = (saved.FindNet(name).GetNetCode() for name in ('USB_P', 'USB_N'))
    pairs = [dict(pos_net=pos, neg_net=neg, pos_name='USB_P', neg_name='USB_N')]
    ids = {bridge.item_id(t) for t in saved.GetTracks()}
    assert len(ids) == 3
    for options in ({'diff_pairs_mode': 'auto'}, {'explicit_pairs': [['USB_P', 'USB_N']]}):
        bridge._verify_added_copper_sizes(saved, ids, pairs, options)
    with pytest.raises(bridge.NativeValidationError, match='track width.*600000'):
        bridge._verify_added_copper_sizes(saved, ids, pairs, {})
    micro = next(t for t in saved.GetTracks() if isinstance(t, pcbnew.PCB_VIA))
    micro.SetDrill(90000)
    with pytest.raises(bridge.NativeValidationError, match='via drill.*100000'):
        bridge._verify_added_copper_sizes(saved, {bridge.item_id(micro)}, [], {})
