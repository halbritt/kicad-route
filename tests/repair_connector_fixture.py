"""Native scratch fixture and bounded runner for the existing rescue connector."""
from collections import Counter
from dataclasses import asdict, replace
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
NAMESPACE = uuid.UUID('51997bae-1974-4dc2-908c-ac80387f736c')


def uid(name):
    return str(uuid.uuid5(NAMESPACE, name))


def checksum(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True,
                                   default=lambda item: sorted(item) if isinstance(item, set) else item.item()) + '\n')


def fixture(directory, variant):
    """One native board with two barrier gates and immutable locked copper."""
    import pcbnew as k
    from synth import RoutingBoard

    source = directory / 'input.kicad_pcb'
    board = RoutingBoard(w=16, h=12)
    for ref, net, x, y in (('A1', 'A', 6, 6), ('A2', 'A', 13, 6),
                           ('B1', 'B', 9, 2), ('B2', 'B', 9, 10),
                           ('C1', 'FIXED', 12, 1), ('C2', 'FIXED', 14, 1)):
        board.pad(ref, x, y, net, sx=.6, sy=.6)
    board.seg(12, 1, 14, 1, 'FIXED', w=.2, locked=True)
    if variant == 'multipin':
        board.pad('B3', 11, 10, 'B', sx=.6, sy=.6)
    # The shared factory uses readable synthetic identifiers; native source
    # requires real, stable UUIDs for every pad, footprint and copper survivor.
    text = re.sub(r'synth-(?:\d+|edge)', lambda match: uid(match[0]), board.text())
    source.write_text(text)
    native = k.LoadBoard(str(source))
    for y in (4, 8):
        for index, (left, right) in enumerate(((0, 2.6), (3.4, 8.6), (9.4, 16))):
            zone = k.ZONE(native)
            zone.SetIsRuleArea(True)
            zone.SetLayer(k.F_Cu)
            zone.SetDoNotAllowTracks(True)
            zone.SetDoNotAllowVias(True)
            zone.SetDoNotAllowZoneFills(True)
            zone.SetDoNotAllowPads(False)
            zone.SetDoNotAllowFootprints(False)
            zone.SetZoneName(f'barrier-{y}-{index}')
            zone.SetUuid(k.KIID(uid(f'barrier-{y}-{index}')))
            outline = zone.Outline()
            outline.NewOutline()
            for x, py in ((left, y-.2), (right, y-.2), (right, y+.2), (left, y+.2)):
                outline.Append(k.FromMM(x), k.FromMM(py))
            native.Add(zone)
    k.SaveBoard(str(source), native)
    project = json.loads((ROOT / 'fixtures/route_demo/route_demo.kicad_pro').read_text())
    project['net_settings']['classes'] = [row for row in project['net_settings']['classes']
                                         if row['name'] == 'Default']
    project['net_settings']['netclass_patterns'] = []
    write(source.with_suffix('.kicad_pro'), project)
    source.with_suffix('.kicad_dru').write_text('(version 1)\n')
    return source


def native_tools():
    directory = ROOT / 'src/kicad_route/kicad'
    sys.path.insert(0, str(directory))
    import facts
    spec = importlib.util.spec_from_file_location('repair_native_components', directory / 'native_components.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return facts, module


def native_evidence(path, tools):
    from kicad_route.native_components import coverage

    facts, components = tools
    facts_path = path.with_suffix('.facts.json')
    facts.main(path, facts_path)
    before = checksum(path)
    terminals = components.read(path)
    terminals.update(board_sha256_before=before, board_sha256_after=checksum(path),
                     native_identity=components.native_identity())
    assert coverage(terminals), 'native partition lacks complete terminal coverage'
    assert terminals['board_sha256_before'] == terminals['board_sha256_after']
    write(path.with_suffix('.terminals.json'), terminals)
    drc_path = path.with_suffix('.drc.json')
    command = ['kicad-cli', 'pcb', 'drc', '--format', 'json', '--output', str(drc_path), str(path)]
    process = subprocess.run(command, capture_output=True, text=True, timeout=10)
    path.with_suffix('.drc.log').write_text(process.stdout + process.stderr)
    assert process.returncode == 0, process.stderr
    drc = json.loads(drc_path.read_text())
    assert not [v for v in drc['violations'] if v['severity'] == 'error'], drc
    return json.loads(facts_path.read_text()), terminals, drc


def remap_ids(data):
    """Exercise parser IDs different from native codes without changing nets."""
    mapping = {net: net+1000 for net in data.nets if net}
    objects = [*data.nets.values(), *data.segments, *data.vias, *data.zones]
    for fp in data.footprints.values():
        objects.extend(fp.pads)
    for pads in data.pads_by_net.values():
        objects.extend(pads)
    for net in data.nets.values():
        objects.extend(net.pads)
    seen = set()
    for obj in objects:
        if id(obj) not in seen:
            seen.add(id(obj))
            obj.net_id = mapping.get(obj.net_id, obj.net_id)
    data.nets = {mapping.get(net, net): value for net, value in data.nets.items()}
    data.pads_by_net = {mapping.get(net, net): value for net, value in data.pads_by_net.items()}
    data.net_id_to_name = {net: value.name for net, value in data.nets.items()}


def emit(source, output, data, edges):
    import pcbnew as k

    board = k.LoadBoard(str(source))
    resolved = []
    for edge_index, edge in enumerate(edges):
        assert not edge['new_vias'], 'this single-layer fixture must not emit vias'
        for index, segment in enumerate(edge['new_segments']):
            name = data.nets[segment.net_id].name
            net = board.GetNetsByName()[name]
            resolved.append((segment.net_id, name, net.GetNetCode()))
            track = k.PCB_TRACK(board)
            track.SetStart(k.VECTOR2I(k.FromMM(segment.start_x), k.FromMM(segment.start_y)))
            track.SetEnd(k.VECTOR2I(k.FromMM(segment.end_x), k.FromMM(segment.end_y)))
            track.SetWidth(k.FromMM(segment.width))
            track.SetLayer(board.GetLayerID(segment.layer))
            track.SetNet(net)
            track.SetUuid(k.KIID(uid(f'{output.stem}-{edge_index}-{index}')))
            board.Add(track)
    k.SaveBoard(str(output), board)
    for extension in ('.kicad_pro', '.kicad_dru'):
        shutil.copyfile(source.with_suffix(extension), output.with_suffix(extension))
    return resolved


def verify_saved(original, current, edges, data):
    """Native geometry and complete terminal equivalences are authoritative."""
    import pcbnew as k

    old, old_terminals, old_drc = original
    new, new_terminals, new_drc = current
    ignored = {'board', 'sha256', 'tracks', 'vias'}
    assert {key: value for key, value in old.items() if key not in ignored} == {
        key: value for key, value in new.items() if key not in ignored}
    assert old['vias'] == new['vias'] == []
    survivors = {track['uuid']: track for track in new['tracks']}
    assert all(survivors.get(track['uuid']) == track for track in old['tracks'])
    original_ids = {track['uuid'] for track in old['tracks']}
    generated = [track for track in new['tracks'] if track['uuid'] not in original_ids]
    expected = []
    for edge in edges:
        for segment in edge['new_segments']:
            ends = frozenset(((k.FromMM(segment.start_x), k.FromMM(segment.start_y)),
                              (k.FromMM(segment.end_x), k.FromMM(segment.end_y))))
            expected.append((data.nets[segment.net_id].name, ends,
                             k.FromMM(segment.width), segment.layer))
    assert Counter(expected) == Counter((track['net'], frozenset((tuple(track['start']), tuple(track['end']))),
                                         track['width'], track['layer']) for track in generated)
    assert old_terminals['inventory'] == new_terminals['inventory']
    assert old_terminals['jumpers'] == new_terminals['jumpers']
    for name, groups in old_terminals['nets'].items():
        assert all(any(set(group) <= set(later) for later in new_terminals['nets'][name]) for group in groups)
    assert Counter(json.dumps(v, sort_keys=True) for v in old_drc['violations']) == Counter(
        json.dumps(v, sort_keys=True) for v in new_drc['violations']), 'routing introduced a native finding'


def run(directory, variant):
    from kicad_route import engine
    from kicad_parser import parse_kicad_pcb
    from routing_config import GridRouteConfig
    import net_rescue

    assert Path(net_rescue.__file__).resolve() == engine.SOURCE_ROOT / 'py_router/net_rescue.py'
    source = fixture(directory, variant)
    source_hashes = {str(path): checksum(path) for path in directory.glob('input.kicad_*')}
    tools = native_tools()
    original = native_evidence(source, tools)
    data = parse_kicad_pcb(str(source))
    remap_ids(data)
    ids = {value.name: net for net, value in data.nets.items()}
    config = GridRouteConfig(layers=['F.Cu'], grid_step=.1, track_width=.2, clearance=.2,
                            via_size=.6, via_drill=.3, max_iterations=100000)
    if variant == 'unequal':
        config.net_track_widths = {ids['A']: .2, ids['B']: .25}
    settings = asdict(config)
    write(directory / 'settings.json', settings)
    fixed = [(item, asdict(item)) for item in [*data.segments, *data.vias]]
    terminals = {id(pad): asdict(pad) for pads in data.pads_by_net.values() for pad in pads}
    constraints = asdict(data.board_info)
    all_edges, steps = [], []
    deadline = time.monotonic() + 15
    for name in ('A', 'B'):
        width = config.get_net_track_width(ids[name], config.layers[0])
        chosen = replace(config, track_width=width)
        assert chosen.net_track_widths == config.net_track_widths
        write(directory / f'settings-{name}.json', asdict(chosen))
        count_before = net_rescue._net_component_info(data, ids[name])[0]
        old_ids = {id(segment) for segment in data.segments}
        edges, fully = net_rescue._attempt_net_at_geometry(
            data, ids[name], chosen, {net: .2 for net in data.nets},
            cancel_check=lambda: time.monotonic() >= deadline)
        count_after = net_rescue._net_component_info(data, ids[name])[0]
        assert fully and count_after == 1
        assert count_before == (3 if name == 'B' and variant == 'multipin' else 2)
        added = [segment for edge in edges for segment in edge['new_segments']]
        assert added, 'connector closed an open net without returning routed additions'
        assert {id(segment) for segment in data.segments if id(segment) not in old_ids} == {id(segment) for segment in added}
        assert all(segment.net_id == ids[name] and segment.layer == 'F.Cu' and segment.width == width for segment in added)
        assert all(any(item is live for live in [*data.segments, *data.vias]) and asdict(item) == saved for item, saved in fixed)
        assert terminals == {id(pad): asdict(pad) for pads in data.pads_by_net.values() for pad in pads}
        assert constraints == asdict(data.board_info)
        assert asdict(config) == settings
        all_edges.extend(edges)
        output = directory / f'after-{name}.kicad_pcb'
        resolved = emit(source, output, data, all_edges)
        assert all(model_id != native_code for model_id, _, native_code in resolved)
        evidence = native_evidence(output, tools)
        verify_saved(original, evidence, all_edges, data)
        partitions = {net: len(groups) for net, groups in evidence[1]['nets'].items()}
        assert partitions == {'A': 1, 'B': (3 if variant == 'multipin' else 2) if name == 'A' else 1, 'FIXED': 1}
        assert len(evidence[2]['unconnected_items']) == ((2 if variant == 'multipin' else 1) if name == 'A' else 0)
        for extension in ('.kicad_pro', '.kicad_dru'):
            assert output.with_suffix(extension).read_bytes() == source.with_suffix(extension).read_bytes()
        steps.append({'name': name, 'components_before': count_before, 'components_after': count_after,
                      'edges': len(edges), 'width_mm': width, 'net_resolution': resolved,
                      'native_opens': len(evidence[2]['unconnected_items']),
                      'native_warnings': len(evidence[2]['violations']), 'output_sha256': checksum(output)})
    assert all(checksum(path) == digest for path, digest in source_hashes.items())
    import grid_router
    write(directory / 'receipt.json', {'variant': variant, 'steps': steps,
          'extension_sha256': checksum(grid_router.__file__), 'connector_sha256': checksum(net_rescue.__file__),
          'source_hashes': source_hashes, 'scope': 'direct connector only; no zones, vias, custody or public worker'})


def main():
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(ROOT / 'src'))
    from kicad_route import engine
    sys.path.insert(0, str(engine.SOURCE_ROOT / 'py_router'))
    sys.path.insert(0, str(engine.SOURCE_ROOT / 'tests'))
    extension = os.environ.get('KERNEL_EXTENSION')
    if extension:
        if not Path(extension).is_file():
            print('required owned kernel extension unavailable:', extension)
            return 77
        spec = importlib.util.spec_from_file_location('grid_router', extension)
        grid = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(grid)
        sys.modules['grid_router'] = grid
    else:
        try:
            import grid_router
        except ModuleNotFoundError as error:
            if error.name != 'grid_router':
                raise
            print('required owned kernel extension unavailable in selected numeric interpreter')
            return 77
    run(Path(sys.argv[1]), sys.argv[2])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
