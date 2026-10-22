"""Native saved-board evidence for experiment policy qualification; no router runs."""
import json

import pcbnew
import pytest

from kicad_route.kicad import facts as native


def mm(x, y):
    return pcbnew.VECTOR2I(round(x * 1_000_000), round(y * 1_000_000))


def area(parent, points, holes=()):
    z = pcbnew.ZONE(parent)
    z.SetIsRuleArea(True)
    z.SetLayer(pcbnew.F_Cu)
    z.SetDoNotAllowTracks(True)
    z.SetDoNotAllowVias(False)
    p = z.Outline()
    p.NewOutline()
    for x, y in points:
        p.Append(mm(x, y).x, mm(x, y).y)
    for ring in holes:
        h = p.NewHole(0)
        for x, y in ring:
            p.Append(mm(x, y).x, mm(x, y).y, 0, h)
    parent.Add(z)
    return z


def capture(board, tmp_path):
    path = tmp_path / 'test.kicad_pcb'
    board.Save(str(path))
    out = tmp_path / 'facts.json'
    native.main(path, out)
    return json.loads(out.read_text())


def test_saved_board_and_transformed_footprint_areas_include_holes(tmp_path):
    b = pcbnew.BOARD()
    board_area = area(b, [(0, 0), (10, 0), (10, 10), (0, 10)],
                      [[(2, 2), (2, 8), (8, 8), (8, 2)]])
    fp = pcbnew.FOOTPRINT(b)
    fp.SetReference('U1')
    b.Add(fp)
    owned = area(fp, [(1, 2), (3, 2), (3, 4), (1, 4)])
    fp.SetPosition(mm(20, 30))
    fp.SetOrientationDegrees(90)
    fp.Flip(fp.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
    f = capture(b, tmp_path)
    assert f['rule_areas_coverage'] == 'board-and-footprint'
    assert f['rule_areas_status'] == 'PASS', f
    assert len(f['rule_areas']) == 2
    by_id = {r['uuid']: r for r in f['rule_areas']}
    a = by_id[board_area.m_Uuid.AsString()]
    assert a['owner']['kind'] == 'board'
    assert a['polygons'][0]['holes'] == [[[2000000, 2000000], [2000000, 8000000],
                                        [8000000, 8000000], [8000000, 2000000]]]
    a = by_id[owned.m_Uuid.AsString()]
    assert a['owner'] == {'kind': 'footprint', 'uuid': fp.m_Uuid.AsString(), 'reference': 'U1'}
    assert a['layers'] == ['B.Cu']
    assert a['polygons'] == [{'outline': [[18000000, 29000000], [18000000, 27000000],
                                         [16000000, 27000000], [16000000, 29000000]], 'holes': []}]
    assert a['no_tracks'] is True and a['no_vias'] is False


def test_area_identity_survives_native_reload(tmp_path):
    b = pcbnew.BOARD()
    area(b, [(0, 0), (10, 0), (10, 10), (0, 10)])
    first = capture(b, tmp_path)
    out = tmp_path / 'again.json'
    native.main(tmp_path / 'test.kicad_pcb', out)
    assert first['rule_areas'] == json.loads(out.read_text())['rule_areas']


def policy_for(facts):
    from kicad_route.common import sha256_of
    return {'schema': 'routability-policy/1', 'experiment_class': 'current-policy',
            'allowed_trace_layers': ['F.Cu', 'B.Cu'],
            'allowed_via_spans': [['F.Cu', 'B.Cu']],
            'rule_areas_sha256': sha256_of(facts['rule_areas'])}


def track(board, a, b, width=.2, layer=pcbnew.F_Cu):
    t = pcbnew.PCB_TRACK(board)
    t.SetStart(mm(*a))
    t.SetEnd(mm(*b))
    t.SetWidth(round(width * 1_000_000))
    t.SetLayer(layer)
    board.Add(t)


def test_native_track_width_obeys_area_hole_and_boundary(tmp_path):
    from kicad_route import experiment_policy
    b = pcbnew.BOARD()
    area(b, [(0, 0), (10, 0), (10, 10), (0, 10)],
         [[(2, 2), (2, 8), (8, 8), (8, 2)]])
    track(b, (3, 3), (7, 3))  # entirely in the native hole
    f = capture(b, tmp_path)
    p = policy_for(f)
    assert experiment_policy.validate(p, f)['status'] == 'PASS'
    track(b, (-.05, 3), (-.05, 7))  # centerline outside, copper overlaps by .05 mm
    f = capture(b, tmp_path)
    result = experiment_policy.validate(p, f)
    assert result['status'] == 'FAIL', result
    assert any('track' in r and 'rule area' in r for r in result['reasons'])


def four_layer_board():
    b = pcbnew.BOARD()
    b.SetCopperLayerCount(4)
    b.SetEnabledLayers(pcbnew.LSET.AllCuMask(4))
    return b


def via(board, x, y):
    v = pcbnew.PCB_VIA(board)
    v.SetPosition(mm(x, y))
    v.SetViaType(pcbnew.VIATYPE_THROUGH)
    v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
    v.SetWidth(600000)
    v.SetDrill(300000)
    board.Add(v)
    return v


def test_through_via_inner_layer_annulus_and_tracks_only_distinction(tmp_path):
    from kicad_route import experiment_policy
    b = four_layer_board()
    z = area(b, [(0, 0), (10, 0), (10, 10), (0, 10)])
    z.SetLayer(pcbnew.In1_Cu)
    via(b, -.2, 5)  # center outside, .3mm radius reaches the inner-layer area
    f = capture(b, tmp_path)
    p = policy_for(f)
    assert experiment_policy.validate(p, f)['status'] == 'PASS'  # no_tracks does not prohibit vias
    assert f['vias'][0]['layer_diameters'] == [
        {'layer': l, 'diameter': 600000} for l in ['F.Cu', 'In1.Cu', 'In2.Cu', 'B.Cu']]
    z.SetDoNotAllowVias(True)
    f = capture(b, tmp_path)
    p = policy_for(f)
    result = experiment_policy.validate(p, f)
    assert result['status'] == 'FAIL', result
    assert any('via' in r and 'rule area' in r for r in result['reasons'])


def test_native_actual_forbidden_layer_and_via_span(tmp_path):
    from kicad_route import experiment_policy
    b = four_layer_board()
    track(b, (20, 20), (21, 20), layer=pcbnew.In1_Cu)
    via(b, 30, 30)
    f = capture(b, tmp_path)
    p = policy_for(f)
    result = experiment_policy.validate(p, f)
    assert result['status'] == 'FAIL', result
    assert any('forbidden layer In1.Cu' in r for r in result['reasons'])
    p['allowed_trace_layers'].append('In1.Cu')
    p['allowed_via_spans'] = []
    result = experiment_policy.validate(p, f)
    assert result['status'] == 'FAIL'
    assert any('forbidden span' in r for r in result['reasons'])


def test_native_arc_is_explicitly_unqualified(tmp_path):
    from kicad_route import experiment_policy
    b = pcbnew.BOARD()
    a = pcbnew.PCB_ARC(b)
    a.SetStart(mm(0, 0))
    a.SetMid(mm(1, 1))
    a.SetEnd(mm(2, 0))
    a.SetWidth(200000)
    a.SetLayer(pcbnew.F_Cu)
    b.Add(a)
    f = capture(b, tmp_path)
    result = experiment_policy.validate(policy_for(f), f)
    assert result['status'] == 'NOT_RUN'
    assert any('geometry' in r for r in result['reasons'])


def test_native_disjoint_areas_remain_separate(tmp_path):
    b = pcbnew.BOARD()
    area(b, [(0, 0), (2, 0), (2, 2), (0, 2)])
    area(b, [(4, 0), (6, 0), (6, 2), (4, 2)])
    f = capture(b, tmp_path)
    assert f['rule_areas_status'] == 'PASS'
    assert len(f['rule_areas']) == 2
    assert {tuple(p) for a in f['rule_areas'] for p in a['polygons'][0]['outline']} == {
        (0, 0), (2000000, 0), (2000000, 2000000), (0, 2000000),
        (4000000, 0), (6000000, 0), (6000000, 2000000), (4000000, 2000000)}


@pytest.mark.parametrize('corrupt', [
    lambda f: f.pop('rule_areas_coverage'),
    lambda f: f.update(rule_areas_status='NOT_RUN'),
    lambda f: f.update(rule_areas=None),
    lambda f: f['rule_areas'][0].pop('polygons'),
    lambda f: f['rule_areas'][0].update(owner={'kind': 'footprint', 'uuid': None, 'reference': 'U1'}),
    lambda f: f['rule_areas'][0].update(no_vias=None),
    lambda f: f['rule_areas'][0].update(layers=['In31.Cu']),
    lambda f: f['rule_areas'][0]['polygons'][0].update(outline=[[0, 0], [5, 5], [0, 5], [5, 0]]),
    lambda f: f['vias'][0].pop('layer_diameters'),
    lambda f: f['vias'][0].update(annulus_geometry='unsupported'),
    lambda f: f['vias'][0].update(layers=['B.Cu', 'F.Cu']),
    lambda f: f['vias'][0].update(via_type='unknown'),
    lambda f: f['vias'][0].pop('uuid'),
])
def test_incomplete_native_evidence_is_not_run(tmp_path, corrupt):
    from kicad_route import experiment_policy
    b = pcbnew.BOARD()
    area(b, [(0, 0), (10, 0), (10, 10), (0, 10)])
    via(b, 20, 20)
    f = capture(b, tmp_path)
    p = policy_for(f)
    corrupt(f)
    assert experiment_policy.validate(p, f)['status'] == 'NOT_RUN'


def test_policy_load_and_rule_hash_are_enforced(tmp_path):
    from kicad_route import experiment_policy
    f = capture(pcbnew.BOARD(), tmp_path)
    p = policy_for(f)
    path = tmp_path / 'policy.json'
    path.write_text(json.dumps(p))
    assert experiment_policy.load(path) == p
    p['rule_areas_sha256'] = '0' * 64
    assert experiment_policy.validate(p, f)['status'] == 'FAIL'
    p['allowed_trace_layers'] = ['In31.Cu']
    assert experiment_policy.validate(p, f)['status'] == 'NOT_RUN'
    path.write_text('[]')
    with pytest.raises(ValueError):
        experiment_policy.load(path)


def test_native_per_layer_via_width_is_used(tmp_path):
    from kicad_route import experiment_policy
    b = four_layer_board()
    z = area(b, [(0, 0), (10, 0), (10, 10), (0, 10)])
    z.SetLayer(pcbnew.In1_Cu)
    z.SetDoNotAllowVias(True)
    v = via(b, -.4, 5)
    # KiCad's layer-specific setter is the oracle: the inner annulus reaches
    # the boundary while the .6 mm front annulus does not.
    v.Padstack().SetMode(pcbnew.PADSTACK.MODE_CUSTOM)
    v.SetWidth(pcbnew.In1_Cu, 1000000)
    f = capture(b, tmp_path)
    assert f['vias'][0]['diameter'] == 600000
    assert next(s['diameter'] for s in f['vias'][0]['layer_diameters'] if s['layer'] == 'In1.Cu') == 1000000
    result = experiment_policy.validate(policy_for(f), f)
    assert result['status'] == 'FAIL', result


def test_transformed_footprint_area_restricts_actual_back_track(tmp_path):
    from kicad_route import experiment_policy
    b = pcbnew.BOARD()
    fp = pcbnew.FOOTPRINT(b)
    fp.SetReference('J1')
    b.Add(fp)
    area(fp, [(1, 2), (3, 2), (3, 4), (1, 4)])
    fp.SetPosition(mm(20, 30))
    fp.SetOrientationDegrees(90)
    fp.Flip(fp.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
    track(b, (17, 27.5), (17, 28.5), layer=pcbnew.B_Cu)
    f = capture(b, tmp_path)
    result = experiment_policy.validate(policy_for(f), f)
    assert result['status'] == 'FAIL', result
    assert any('track' in r and 'rule area' in r for r in result['reasons'])


@pytest.mark.parametrize('duplicate', [
    '"allowed_trace_layers":["F.Cu"],"allowed_trace_layers":["F.Cu","B.Cu"]',
    '"allowed_via_spans":[],"allowed_via_spans":[["F.Cu","B.Cu"]]',
    '"allowed_trace_layers":[{"layer":"F.Cu","layer":"B.Cu"}]',
])
def test_policy_load_rejects_duplicate_json_keys(tmp_path, duplicate):
    from kicad_route import experiment_policy
    # Public declaration boundary: duplicate outer permissions previously used
    # the last, broader value. Nested duplicate keys must be rejected as well.
    fields = ['"schema":"routability-policy/1"', '"experiment_class":"current-policy"',
              '"rule_areas_sha256":"' + '0' * 64 + '"', duplicate]
    if not duplicate.startswith('"allowed_trace_layers"'):
        fields.append('"allowed_trace_layers":["F.Cu"]')
    if not duplicate.startswith('"allowed_via_spans"'):
        fields.append('"allowed_via_spans":[]')
    path = tmp_path / 'duplicate-policy.json'
    path.write_text('{' + ','.join(fields) + '}')
    with pytest.raises(ValueError, match='duplicate JSON object key'):
        experiment_policy.load(path)
