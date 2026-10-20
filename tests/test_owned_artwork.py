"""Native retained artwork and the small passage it closes to new routes."""
import json
import shutil
import subprocess

import pcbnew

from helpers import FIXTURE, run, tree_hash
from kicad_route import cli as route


def artwork_project(directory):
    directory.mkdir(parents=True)
    board = pcbnew.LoadBoard(str(FIXTURE / 'route_demo.kicad_pcb'))
    for item in [*board.GetTracks(), *board.GetFootprints(), *board.Zones()]:
        board.Delete(item)
    for net in list(board.GetNetsByName().values()):
        if net.GetNetCode():
            board.Delete(net)
    net = pcbnew.NETINFO_ITEM(board, '/ART')
    board.Add(net)
    def xy(x, y):
        return pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y))
    for ref, x in (('J1', 110), ('J2', 140)):
        fp = pcbnew.FOOTPRINT(board)
        fp.SetReference(ref)
        fp.SetPosition(xy(x, 112))
        board.Add(fp)
        pad = pcbnew.PAD(fp)
        pad.SetNumber('1')
        pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        pad.SetShape(pcbnew.PAD_SHAPE_RECT)
        pad.SetSize(xy(1, 1))
        layers = pcbnew.LSET()
        layers.AddLayer(pcbnew.F_Cu)
        pad.SetLayerSet(layers)
        pad.SetPosition(xy(x, 112))
        pad.SetNet(net)
        fp.Add(pad)
        if ref == 'J1':
            # A land-pattern tab belongs to the pad-bearing footprint. It must
            # stay copper, including the part touching its own pad edge.
            tab = pcbnew.PCB_SHAPE(fp)
            tab.SetShape(pcbnew.SHAPE_T_RECT)
            tab.SetStart(xy(109.5, 112.5))
            tab.SetEnd(xy(110.5, 114))
            tab.SetLayer(pcbnew.F_Cu)
            tab.SetWidth(pcbnew.FromMM(.1))
            tab.SetFilled(True)
            fp.Add(tab)
    guard = pcbnew.NETINFO_ITEM(board, '/GUARD')
    board.Add(guard)
    tagged = pcbnew.PCB_SHAPE(board)
    tagged.SetShape(pcbnew.SHAPE_T_RECT)
    tagged.SetStart(xy(118, 111.8))
    tagged.SetEnd(xy(120, 112.2))
    tagged.SetLayer(pcbnew.F_Cu)
    tagged.SetWidth(pcbnew.FromMM(.1))
    tagged.SetFilled(True)
    tagged.SetNet(guard)
    board.Add(tagged)
    for layer in (pcbnew.F_Cu, pcbnew.B_Cu):
        for low, high in ((100.5, 110.5), (113.5, 129.5)):
            shape = pcbnew.PCB_SHAPE(board)
            shape.SetShape(pcbnew.SHAPE_T_RECT)
            shape.SetStart(xy(124, low))
            shape.SetEnd(xy(126, high))
            shape.SetLayer(layer)
            shape.SetWidth(pcbnew.FromMM(.1))
            shape.SetFilled(True)
            board.Add(shape)
        text = pcbnew.PCB_TEXT(board)
        text.SetText('((' if layer == pcbnew.F_Cu else ')')
        text.SetPosition(xy(125, 112))
        text.SetTextSize(xy(.5, .5))
        text.SetTextThickness(pcbnew.FromMM(.1))
        text.SetLayer(layer)
        board.Add(text)
    board.Save(str(directory / 'artwork.kicad_pcb'))
    shutil.copyfile(FIXTURE / 'route_demo.kicad_pro', directory / 'artwork.kicad_pro')
    return directory


def test_native_copper_artwork_survives_routing_through_its_passage(tmp_path):
    directory = artwork_project(tmp_path / 'project')
    before = tree_hash(directory)
    process, run_dir, result = run('run', '--backend', 'krt', '--dir', str(directory),
                                  '--budget', '60', '--label', 'retained-artwork')
    assert tree_hash(directory) == before
    assert process.returncode == 0, (process.stdout, process.stderr, result)
    assert result['process']['status'] == 'PASS', result['process']
    assert result['validation']['status'] == 'PASS', result['validation']
    assert result['validation']['preservation']['copper_graphics'] == 'PASS'
    assert result['validation']['drc']['new_violations']['count'] == 0
    assert result['routing']['status'] == 'COMPLETE', result['routing']
    facts = json.loads((run_dir / 'validate/candidate-facts.json').read_text())
    tagged = [row for row in facts['copper_graphics'] if row.get('net') == '/GUARD']
    assert len(tagged) == 1 and tagged[0]['layer'] == 'F.Cu', tagged

    def footprint_copper(board):
        rows = []
        for fp in board.GetFootprints():
            for item in fp.GraphicalItems():
                if isinstance(item, pcbnew.PCB_SHAPE) and item.IsOnCopperLayer():
                    start, end = item.GetStart(), item.GetEnd()
                    rows.append((fp.GetReference(), item.GetLayer(), int(item.GetShape()),
                                 item.GetWidth(), item.IsFilled(),
                                 (start.x, start.y), (end.x, end.y)))
        return sorted(rows)

    reference = pcbnew.LoadBoard(str(directory / 'artwork.kicad_pcb'))
    delivered = pcbnew.LoadBoard(str(run_dir / 'candidate/project/artwork.kicad_pcb'))
    reference_copper = footprint_copper(reference)
    assert len(reference_copper) == 1 and reference_copper[0][0] == 'J1', reference_copper
    assert footprint_copper(delivered) == reference_copper
    assert {row['layer'] for row in facts['routing_copper_text']} == {'F.Cu', 'B.Cu'}
    assert {row['text'] for row in facts['routing_copper_text']} == {'((', ')'}
    # It had to go around retained text, not just preserve it after a straight
    # route had already crossed the copper at the centre of the passage.
    tracks = [row for row in facts['tracks'] if row['net'] == '/ART']
    assert tracks
    assert any(row['start'][1] != 112_000_000 or row['end'][1] != 112_000_000 for row in tracks)


def test_unsupported_copper_is_refused_without_relocation(tmp_path):
    board = tmp_path / 'curve.kicad_pcb'
    board.write_text('(kicad_pcb (gr_curve (pts (xy 1 2) (xy 2 3) (xy 3 2) (xy 4 3)) (layer "F.Cu")))')
    original = board.read_bytes()
    assert 'cannot model gr_curve' in route.krt_preflight(board, {}, tmp_path)
    assert board.read_bytes() == original


def test_direct_parser_refuses_missing_or_changed_native_text_evidence(tmp_path):
    directory = artwork_project(tmp_path / 'project')
    board = directory / 'artwork.kicad_pcb'
    source = route.KRT_DIR / 'py_router'
    code = ("import sys;sys.path.insert(0,sys.argv[1]);"
            "from kicad_parser import parse_kicad_pcb;parse_kicad_pcb(sys.argv[2])")
    environment = route.common.tool_env()
    environment.pop('KICAD_ROUTE_COPPER_TEXT', None)
    output = subprocess.run([str(route.route_python()), '-c', code, str(source), str(board)],
                            env=environment, capture_output=True, text=True, timeout=10)
    assert output.returncode != 0
    assert 'requires native bounding-box evidence' in output.stderr
    evidence = tmp_path / 'wrong.json'
    evidence.write_text(json.dumps({'signature': 'wrong', 'texts': []}))
    output = subprocess.run([str(route.route_python()), '-c', code, str(source), str(board)],
                            env={**environment, 'KICAD_ROUTE_COPPER_TEXT': str(evidence)},
                            capture_output=True, text=True, timeout=10)
    assert output.returncode != 0
    assert 'changed since native bounding-box preflight' in output.stderr
