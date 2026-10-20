"""Original four-layer fixture, exact-plan native evidence and refusal boundaries."""
import copy
import json
from pathlib import Path
import subprocess
import sys

import pytest
import jsonschema
import pcbnew as k

from helpers import ROOT, tree_hash
from kicad_route import plane_plan, planes


def plan():
    settings = dict(clearance_mm=.25, min_width_mm=.2, priority=0, island_removal='always',
                    thermal=dict(connection='thermal', gap_mm=.3, spoke_width_mm=.3))
    return dict(schema=plane_plan.SCHEMA, zones=[
        dict(settings, net='GND', layer='In1.Cu', outline='board'),
        dict(settings, net='+3V3', layer='In2.Cu',
             outline={'polygon_mm': [[10, 10], [30, 10], [30, 40], [10, 40]]})])


@pytest.fixture
def project(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    board_path = source / 'probe.kicad_pcb'
    board = k.BOARD()
    board.SetCopperLayerCount(4)
    edge = k.PCB_SHAPE(board)
    edge.SetShape(k.SHAPE_T_RECT)
    edge.SetLayer(k.Edge_Cuts)
    edge.SetStart(k.VECTOR2I(k.FromMM(10), k.FromMM(10)))
    edge.SetEnd(k.VECTOR2I(k.FromMM(40), k.FromMM(40)))
    edge.SetWidth(k.FromMM(.1))
    board.Add(edge)
    for i, name in enumerate(('GND', '+3V3')):
        net = k.NETINFO_ITEM(board, name)
        board.Add(net)
        fp = k.FOOTPRINT(board)
        fp.SetReference(f'J{i+1}')
        fp.SetValue('Owned plane probe')
        fp.SetFPIDAsString('Owned:Probe')
        fp.SetPosition(k.VECTOR2I(k.FromMM(15), k.FromMM(15 + i*15)))
        board.Add(fp)
        for field in fp.GetFields():
            field.SetVisible(False)
        pad = k.PAD(fp)
        pad.SetNumber('1')
        pad.SetNet(net)
        pad.SetAttribute(k.PAD_ATTRIB_PTH)
        pad.SetShape(k.PAD_SHAPE_CIRCLE)
        pad.SetSize(k.VECTOR2I(k.FromMM(2), k.FromMM(2)))
        pad.SetDrillSize(k.VECTOR2I(k.FromMM(.8), k.FromMM(.8)))
        pad.SetLayerSet(k.LSET.AllCuMask())
        pad.SetPosition(fp.GetPosition())
        fp.Add(pad)
    track = k.PCB_TRACK(board)
    track.SetNet(board.FindNet('GND'))
    track.SetLayer(k.F_Cu)
    track.SetStart(k.VECTOR2I(k.FromMM(15), k.FromMM(15)))
    track.SetEnd(k.VECTOR2I(k.FromMM(20), k.FromMM(15)))
    track.SetWidth(k.FromMM(.3))
    track.SetLocked(True)
    board.Add(track)
    via = k.PCB_VIA(board)
    via.SetNet(board.FindNet('GND'))
    via.SetPosition(track.GetEnd())
    via.SetWidth(k.FromMM(.7))
    via.SetDrill(k.FromMM(.3))
    via.SetViaType(k.VIATYPE_THROUGH)
    via.SetLayerPair(k.F_Cu, k.B_Cu)
    via.SetLocked(True)
    board.Add(via)
    keepout = k.ZONE(board)
    keepout.SetIsRuleArea(True)
    keepout.SetZoneName('Owned plane keepout')
    layers = k.LSET()
    layers.AddLayer(k.In1_Cu)
    layers.AddLayer(k.In2_Cu)
    keepout.SetLayerSet(layers)
    keepout.SetDoNotAllowZoneFills(True)
    keepout.Outline().NewOutline()
    for x, y in [(26, 20), (28, 20), (28, 23), (26, 23)]:
        keepout.Outline().Append(k.FromMM(x), k.FromMM(y))
    board.Add(keepout)
    assert k.SaveBoard(str(board_path), board)
    board_path.with_suffix('.kicad_pro').write_text('{}\n')
    board_path.with_suffix('.kicad_prl').write_text('{"sentinel": true}\n')
    plan_path = source / 'plan.json'
    plan_path.write_text(json.dumps(plan()))
    return board_path, plan_path


def invoke(tmp_path, project, name='run', env=None):
    board, plan_path = project
    run_dir = tmp_path / name
    before = tree_hash(board.parent)
    proc = subprocess.run([sys.executable, str(ROOT / 'bin/kicad-route'), '--out', str(tmp_path / 'out'),
                           'planes', '--board', str(board), '--plan', str(plan_path),
                           '--run-dir', str(run_dir), '--json'], capture_output=True, text=True, env=env)
    assert tree_hash(board.parent) == before
    report = json.loads(proc.stdout)
    assert proc.returncode == report['exit_code'], proc.stderr
    if run_dir.exists():
        assert json.loads((run_dir / 'result.json').read_text()) == report
        schema = json.loads((ROOT / 'schemas/planes-1.json').read_text())
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.validate(report, schema)
    return proc, report, run_dir


def assert_created(report, run_dir):
    assert report['exit_code'] == 0, (report, (run_dir / 'sandbox.log').read_text())
    assert report['schema'] == 'kicad-route/planes/1'
    assert report['validation']['status'] == 'PASS'
    assert {(z['net'], z['layer']) for z in report['planes']['zones']} == {('GND', 'In1.Cu'), ('+3V3', 'In2.Cu')}
    assert all(z['filled_area_mm2'] > 100 for z in report['planes']['zones'])
    integrity = report['planes']['pour_integrity']
    assert integrity['check']['check'] == 'pour_integrity'
    assert integrity['check']['status'] == 'PASS'
    assert integrity['before']['zones'] == []
    assert len(integrity['after']['zones']) == 2
    assert len(integrity['delta']['layers']) == 2
    # Independently reload the saved artifact, rather than trusting its report.
    board = k.LoadBoard(report['candidate'])
    actual = {z.m_Uuid.AsString(): z for z in board.Zones() if not z.GetIsRuleArea()}
    assert sum(z.GetIsRuleArea() for z in board.Zones()) == 1
    assert len(list(board.GetTracks())) == 2
    assert all(t.IsLocked() for t in board.GetTracks())
    inventory = next(c for c in report['validation']['checks'] if c['check'] == 'plane_copper_inventory')
    assert inventory['status'] == 'PASS'
    assert set(actual) == set(plane_plan.zone_ids(plan()))
    for uid, spec in zip(plane_plan.zone_ids(plan()), plan()['zones']):
        zone = actual[uid]
        assert zone.GetNetname() == spec['net']
        assert board.GetLayerName(zone.GetLayer()) == spec['layer']
        assert zone.GetLocalClearance() == k.FromMM(spec['clearance_mm'])
        assert zone.GetMinThickness() == k.FromMM(spec['min_width_mm'])
        assert zone.GetPadConnection() == k.ZONE_CONNECTION_THERMAL
        assert zone.GetThermalReliefGap() == k.FromMM(.3)
        assert zone.GetThermalReliefSpokeWidth() == k.FromMM(.3)
        assert zone.GetIslandRemovalMode() == k.ISLAND_REMOVAL_MODE_ALWAYS
        assert zone.GetFilledPolysList(zone.GetLayer()).Area() > 0
    assert (Path(report['candidate']).with_suffix('.kicad_prl')).read_text() == '{"sentinel": true}\n'


def test_four_layer_exact_plan_and_pour_integrity(tmp_path, project):
    _, report, run_dir = invoke(tmp_path, project)
    assert_created(report, run_dir)
    assert report['planes']['zones_created'] == 2
    assert report['input']['files']['probe.kicad_pcb']
    assert '--unshare-all' in report['sandbox']['argv']
    assert report['sandbox']['exit_code'] == 0


def test_same_plan_repairs_without_duplicates(tmp_path, project):
    _, first, run_dir = invoke(tmp_path, project)
    assert_created(first, run_dir)
    _, second, second_dir = invoke(tmp_path, (Path(first['candidate']), project[1]), 'second')
    assert second['exit_code'] == 0, (second, (second_dir / 'sandbox.log').read_text())
    assert second['planes']['zones_created'] == 0
    assert len(second['planes']['zones']) == 2
    assert second['planes']['pour_integrity']['check']['status'] == 'PASS'


@pytest.mark.parametrize('bad', ['zone', 'zone-net', 'footprint-zone', 'missing-net', 'disabled-layer', 'crossed', 'empty-fill'])
def test_native_refuses_unplanned_or_unfillable(tmp_path, project, bad):
    board_path, plan_path = project
    board = k.LoadBoard(str(board_path))
    spec = plan()
    if bad in ('zone', 'zone-net', 'footprint-zone'):
        owner = next(iter(board.GetFootprints())) if bad == 'footprint-zone' else board
        zone = k.ZONE(owner)
        zone.SetNet(board.FindNet('GND'))
        zone.SetLayer(k.In1_Cu)
        if bad == 'zone-net':
            uid = plane_plan.zone_ids(spec)[0]
            zone.SetUuidDirect(k.KIID(uid))
            zone.SetZoneName('agent-ee-plane/' + uid)
            zone.SetNet(board.FindNet('+3V3'))
        zone.Outline().NewOutline()
        for x, y in [(11, 11), (20, 11), (20, 20), (11, 20)]:
            zone.Outline().Append(k.FromMM(x), k.FromMM(y))
        owner.Add(zone)
        assert k.SaveBoard(str(board_path), board)
    elif bad == 'missing-net':
        spec['zones'][0]['net'] = 'UNDECLARED'
    elif bad == 'disabled-layer':
        spec['zones'][0]['layer'] = 'In3.Cu'
    elif bad == 'crossed':
        spec['zones'][0]['outline'] = {'polygon_mm': [[10, 10], [40, 40], [40, 10], [10, 40]]}
    else:
        spec['zones'][0]['outline'] = {'polygon_mm': [[100, 100], [110, 100], [110, 110], [100, 110]]}
    plan_path.write_text(json.dumps(spec))
    _, report, run_dir = invoke(tmp_path, project)
    assert report['exit_code'] == 1
    assert report['process']['status'] == 'FAIL'
    assert report['validation']['status'] == 'NOT_RUN'
    assert report['planes']['pour_integrity'] is None
    assert report['candidate'] is None
    assert not (run_dir / 'candidate').exists()
    assert report['sandbox']['exit_code'] != 0


@pytest.mark.parametrize('change', [lambda p: p.update(stitching_vias=[]),
    lambda p: p['zones'][0].update(net=''), lambda p: p['zones'][0].update(clearance_mm=True),
    lambda p: p['zones'][0].update(priority=1.2), lambda p: p['zones'][0].update(island_removal='auto'),
    lambda p: p['zones'][0]['thermal'].update(connection='auto'),
    lambda p: p['zones'][0].update(min_width_mm=float('nan')),
    lambda p: p['zones'][0].update(clearance_mm=10**1000),
    lambda p: p['zones'][0].update(outline={'polygon_mm': [[1, 1], [2, 2], [1, 1]]}),
    lambda p: p.update(zones=[]), lambda p: p.update(schema='plane-plan/2')])
def test_strict_plan_boundaries(change):
    spec = copy.deepcopy(plan())
    change(spec)
    with pytest.raises(ValueError):
        plane_plan.validate_plan(spec)


def test_duplicate_plan_keys_refused(tmp_path, project):
    project[1].write_text('{"schema":"bad", "schema":"agent-ee.plane-plan/1", "zones":[]}')
    proc, report, run_dir = invoke(tmp_path, project)
    assert proc.returncode == 4
    assert report['kind'] == 'refusal'
    assert not run_dir.exists()


def test_missing_native_tool_is_failure(tmp_path, project):
    import os
    env = dict(os.environ, KICAD_ROUTE_PCBNEW_PYTHON='/no-such-native-python')
    _, report, run_dir = invoke(tmp_path, project, env=env)
    assert report['exit_code'] == 1
    assert report['process']['status'] == 'FAIL'
    assert report['planes']['pour_integrity'] is None
    assert report['candidate'] is None


def test_sandbox_denies_network_and_source_writes(tmp_path):
    from kicad_route import common
    work = tmp_path / 'work'
    work.mkdir()
    sentinel = tmp_path / 'sentinel'
    sentinel.write_text('immutable')
    code = '''import socket, pathlib, sys
assert str(pathlib.Path('/proc/self/ns/net').readlink()) != sys.argv[2]
try:
    pathlib.Path(sys.argv[1]).write_text('wrong')
except OSError:
    pass
else:
    raise AssertionError('source writable')
s = socket.socket()
s.settimeout(.1)
try:
    s.connect(('192.0.2.1', 443))
except OSError:
    pass
else:
    raise AssertionError('network reachable')
'''
    command = planes.sandbox_command(work, ['/usr/bin/python3', '-c', code, str(sentinel),
                                          str(Path('/proc/self/ns/net').readlink())])
    rec = common.run_cmd(command, work, work / 'sandbox.log', timeout=10)
    assert rec['exit_code'] == 0, rec
    assert sentinel.read_text() == 'immutable'


@pytest.mark.parametrize('case', ['missing-project', 'existing-output', 'inside-source'])
def test_input_and_state_refusals(tmp_path, project, case):
    board, plan_path = project
    run_dir = tmp_path / 'refused'
    if case == 'missing-project':
        board.with_suffix('.kicad_pro').unlink()
    elif case == 'existing-output':
        run_dir.mkdir()
    else:
        run_dir = board.parent / 'output'
    proc = subprocess.run([sys.executable, str(ROOT / 'bin/kicad-route'), '--out', str(tmp_path / 'out'),
                           'planes', '--board', str(board), '--plan', str(plan_path), '--run-dir', str(run_dir),
                           '--json'], capture_output=True, text=True)
    report = json.loads(proc.stdout)
    assert proc.returncode == 4
    assert report['reason_class'] == ('state' if case == 'existing-output' else 'input')
    assert not (run_dir / 'result.json').exists()


def test_inspector_rejects_saved_setting_tamper(tmp_path, project):
    _, report, run_dir = invoke(tmp_path, project)
    assert_created(report, run_dir)
    candidate = Path(report['candidate'])
    board = k.LoadBoard(str(candidate))
    next(z for z in board.Zones() if not z.GetIsRuleArea()).SetThermalReliefGap(k.FromMM(.8))
    assert k.SaveBoard(str(candidate), board)
    work = run_dir / 'work'
    rec = subprocess.run(planes.sandbox_command(work, [sys.executable, planes.NATIVE, 'inspect', candidate,
                         run_dir / 'plan.json', work / 'tampered.json']), capture_output=True, text=True)
    assert rec.returncode == 1
    assert 'saved plane settings or outline differ from plan' in rec.stderr
    assert not (work / 'tampered.json').exists()


def test_native_defect_fails_validation(tmp_path, project):
    spec = plan()
    spec['zones'][0]['outline'] = {'polygon_mm': [[10, 10], [28, 10], [28, 40], [10, 40]]}
    spec['zones'][1]['layer'] = 'In1.Cu'
    spec['zones'][1]['outline'] = {'polygon_mm': [[22, 10], [40, 10], [40, 40], [22, 40]]}
    project[1].write_text(json.dumps(spec))
    _, report, run_dir = invoke(tmp_path, project)
    assert report['exit_code'] == 3
    assert report['process']['status'] == 'PASS'
    assert report['validation']['status'] == 'FAIL'
    assert Path(report['candidate']).is_file()
    check = next(c for c in report['validation']['checks'] if c['check'] == 'native_drc')
    assert check['status'] == 'FAIL'
    assert any(v['type'] == 'isolated_copper' for v in check['differences'])
