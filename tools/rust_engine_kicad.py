#!/usr/bin/env python3
"""Native KiCad I/O for the standalone Rust engine's first supported subset.

This module makes no routing decisions. Rust owns the edit list; pcbnew owns
native decoding and encoding. Unsupported native features are rejected.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

import pcbnew


class UnsupportedBoard(ValueError):
    pass


class NativeValidationError(ValueError):
    pass


def native_violations(path: Path, report: Path):
    try:
        subprocess.run(
            ['kicad-cli', 'pcb', 'drc', '--format', 'json', '--severity-all',
             '--output', str(report), str(path)],
            check=True, capture_output=True, text=True, timeout=30,
        )
        data = json.loads(report.read_text())
        if not isinstance(data['violations'], list) or not isinstance(data['unconnected_items'], list):
            raise ValueError('native DRC report has invalid violation/connectivity fields')
        return data
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        raise NativeValidationError(f'native DRC did not produce a valid result: {error}') from error


def point(value):
    return [int(value.x), int(value.y)]


def item_id(item):
    return item.m_Uuid.AsString()


def load_supported(path: Path):
    for suffix in ('.kicad_pro', '.kicad_dru'):
        if path.with_suffix(suffix).exists():
            raise UnsupportedBoard(f'project/custom-rule import is not implemented: {suffix}')
    board = pcbnew.LoadBoard(str(path))
    if board is None:
        raise ValueError(f'KiCad could not load {path}')
    if board.Zones():
        raise UnsupportedBoard('zones are not supported by the first Rust slice')
    if board.GetCopperLayerCount() != 2:
        raise UnsupportedBoard('the first native slice requires a two-layer board')
    if any(drawing.GetLayer() == pcbnew.Margin for drawing in board.GetDrawings()):
        raise UnsupportedBoard('board Margin geometry is not represented')
    settings = board.GetDesignSettings()
    if any(value != 0 for value in (
            settings.m_SolderMaskExpansion, settings.m_SolderMaskMinWidth,
            settings.m_SolderMaskToCopperClearance)):
        raise UnsupportedBoard('nondefault solder-mask rules are not represented')
    for footprint in board.GetFootprints():
        if footprint.GetLocalClearance() is not None:
            raise UnsupportedBoard('footprint clearance overrides are not represented')
        if footprint.GetLocalSolderMaskMargin() is not None:
            raise UnsupportedBoard('footprint solder-mask margins are not represented')
        if footprint.Zones():
            raise UnsupportedBoard('footprint zones and rule areas are not represented')
        if footprint.IsNetTie():
            raise UnsupportedBoard('net-tie connectivity is not represented')
        for field in footprint.GetFields():
            if pcbnew.IsCopperLayer(field.GetLayer()):
                raise UnsupportedBoard('footprint copper fields are not represented')
        for drawing in footprint.GraphicalItems():
            if drawing.GetLayer() == pcbnew.Margin:
                raise UnsupportedBoard('footprint Margin geometry is not represented')
            if pcbnew.IsCopperLayer(drawing.GetLayer()) or drawing.GetLayer() == pcbnew.Edge_Cuts:
                raise UnsupportedBoard('footprint copper graphics and board cuts are not represented')
        for pad in footprint.Pads():
            if pad.GetLocalClearance() is not None:
                raise UnsupportedBoard('pad clearance overrides are not represented')
            if pad.GetLocalSolderMaskMargin() is not None:
                raise UnsupportedBoard('pad solder-mask margins are not represented')
            if point(pad.GetOffset()) != [0, 0]:
                raise UnsupportedBoard('offset pad copper is not represented')
    return board


def rectangular_bounds(board):
    edges = []
    for drawing in board.GetDrawings():
        if pcbnew.IsCopperLayer(drawing.GetLayer()):
            raise UnsupportedBoard('copper drawings are not yet represented')
        if drawing.GetLayer() == pcbnew.Edge_Cuts:
            if not isinstance(drawing, pcbnew.PCB_SHAPE) or drawing.GetShape() != pcbnew.SHAPE_T_SEGMENT:
                raise UnsupportedBoard('the board outline must be four straight edges')
            a, b = tuple(point(drawing.GetStart())), tuple(point(drawing.GetEnd()))
            edges.append(frozenset((a, b)))
    if len(edges) != 4:
        raise UnsupportedBoard('the board outline must be a single rectangle')
    vertices = set().union(*edges)
    if len(vertices) != 4:
        raise UnsupportedBoard('the board outline must have four corners')
    xs, ys = sorted({p[0] for p in vertices}), sorted({p[1] for p in vertices})
    if len(xs) != 2 or len(ys) != 2:
        raise UnsupportedBoard('the board outline must be axis-aligned')
    corners = [(xs[0], ys[0]), (xs[1], ys[0]), (xs[1], ys[1]), (xs[0], ys[1])]
    expected = {frozenset((corners[i], corners[(i + 1) % 4])) for i in range(4)}
    if set(edges) != expected:
        raise UnsupportedBoard('the board outline must be closed and non-crossing')
    return {'min': list(corners[0]), 'max': list(corners[2])}


def export_board(path: Path, rules: dict, max_expansions: int):
    board = load_supported(path)
    settings = board.GetDesignSettings()
    minima = {'width_nm': settings.m_TrackMinWidth,
              'clearance_nm': max(settings.m_MinClearance, settings.GetBiggestClearanceValue()),
              'edge_clearance_nm': settings.m_CopperEdgeClearance}
    if set(rules) != {'width_nm', 'clearance_nm', 'grid_nm', 'edge_clearance_nm'}:
        raise ValueError('all four explicit routing rules are required')
    if any(type(v) is not int or v <= 0 for v in rules.values()):
        raise ValueError('routing rules must be positive integer nanometres')
    for key, floor in minima.items():
        if rules[key] < floor:
            raise ValueError(f'{key}={rules[key]} is below native floor {floor}')
    if type(max_expansions) is not int or max_expansions <= 0:
        raise ValueError('max_expansions must be positive')
    pads = []
    for footprint in board.GetFootprints():
        for pad in footprint.Pads():
            copper = [layer for layer in pad.GetLayerSet().Seq() if pcbnew.IsCopperLayer(layer)]
            if pad.GetAttribute() != pcbnew.PAD_ATTRIB_SMD or copper != [pcbnew.F_Cu]:
                raise UnsupportedBoard('only front-copper SMD pads are supported')
            size = point(pad.GetSize())
            if pad.GetShape() == pcbnew.PAD_SHAPE_CIRCLE and size[0] == size[1] and size[0] % 2 == 0:
                shape = {'kind': 'circle', 'radius_nm': size[0] // 2}
            elif pad.GetShape() == pcbnew.PAD_SHAPE_RECT:
                angle = pad.GetOrientationDegrees() % 180
                if angle not in (0, 90) or any(v % 2 for v in size):
                    raise UnsupportedBoard('rectangular pads require orthogonal rotation and even dimensions')
                if angle == 90:
                    size.reverse()
                shape = {'kind': 'rect', 'half_size_nm': [v // 2 for v in size]}
            else:
                raise UnsupportedBoard('only circular and axis-aligned rectangular pads are supported')
            pads.append({'id': item_id(pad), 'net': int(pad.GetNetCode()),
                         'at': point(pad.GetPosition()), 'shape': shape})
    tracks = []
    for track in board.GetTracks():
        if type(track) is not pcbnew.PCB_TRACK or track.GetLayer() != pcbnew.F_Cu:
            raise UnsupportedBoard('vias, arcs and non-front tracks are not supported')
        tracks.append({'id': item_id(track), 'net': int(track.GetNetCode()),
                       'start': point(track.GetStart()), 'end': point(track.GetEnd()),
                       'width_nm': int(track.GetWidth()), 'locked': bool(track.IsLocked())})
    nets = [{'id': int(code), 'name': net.GetNetname()}
            for code, net in board.GetNetsByNetcode().items()]
    return {'version': 1, 'source_id': hashlib.sha256(path.read_bytes()).hexdigest(),
            'layer': 'F.Cu', 'bounds': rectangular_bounds(board), 'rules': rules,
            'nets': nets, 'pads': pads, 'tracks': tracks, 'max_expansions': max_expansions}


def apply_plan(source: Path, plan: dict, destination: Path):
    if source.resolve() == destination.resolve() or destination.exists():
        raise ValueError('output must be a new path distinct from the input')
    if plan.get('version') != 1 or plan.get('status') != 'complete':
        raise ValueError('only a complete version1 Rust plan can be written')
    if plan.get('source_id') != hashlib.sha256(source.read_bytes()).hexdigest():
        raise ValueError('Rust plan does not match the input board')
    if plan.get('removed_track_ids') != []:
        raise UnsupportedBoard('this slice cannot remove input copper')
    additions = plan.get('added_tracks')
    if not isinstance(additions, list):
        raise ValueError('Rust plan must contain an explicit added_tracks list')
    board = load_supported(source)
    used_ids = {item_id(x) for x in board.GetTracks()} | {item_id(x) for x in board.GetPads()}
    used_ids |= {item_id(x) for x in board.GetFootprints()} | {item_id(x) for x in board.GetDrawings()}
    nets = set(board.GetNetsByNetcode())
    for item in additions:
        ident = item['id']
        if str(uuid.UUID(ident)) != ident or uuid.UUID(ident).int == 0 or ident in used_ids:
            raise ValueError('Rust output item identity must be canonical, nonzero and unique')
        if item['net'] not in nets or item['net'] == 0 or type(item['width_nm']) is not int or item['width_nm'] <= 0:
            raise ValueError('Rust output track requires a known net and positive width')
        if item['width_nm'] < board.GetDesignSettings().m_TrackMinWidth:
            raise NativeValidationError('Rust output track width is below the native minimum')
        if any(len(item[key]) != 2 or any(type(n) is not int or abs(n) > 1_000_000_000 for n in item[key]) for key in ('start', 'end')):
            raise ValueError('Rust output coordinates exceed the supported native range')
        track = pcbnew.PCB_TRACK(board)
        track.SetUuid(pcbnew.KIID(ident))
        track.SetNetCode(item['net'])
        track.SetLayer(pcbnew.F_Cu)
        track.SetStart(pcbnew.VECTOR2I(*item['start']))
        track.SetEnd(pcbnew.VECTOR2I(*item['end']))
        track.SetWidth(item['width_nm'])
        track.SetLocked(item['locked'])
        board.Add(track)
        used_ids.add(ident)
    # Keep the candidate unpublished until the saved native representation
    # independently confirms completion and DRC non-regression.
    with tempfile.TemporaryDirectory(prefix=f'.{destination.stem}-', dir=destination.parent) as directory:
        directory = Path(directory)
        temporary = directory / destination.name
        if not pcbnew.SaveBoard(str(temporary), board):
            raise OSError('KiCad did not save the candidate board')
        saved = pcbnew.LoadBoard(str(temporary))
        if saved is None:
            raise NativeValidationError('KiCad could not reload the saved candidate')
        saved.BuildConnectivity()
        if saved.GetConnectivity().GetUnconnectedCount(False) != 0:
            raise NativeValidationError('native connectivity still has open connections')
        before = native_violations(source, directory / 'before-drc.json')
        after = native_violations(temporary, directory / 'after-drc.json')
        if after['unconnected_items']:
            raise NativeValidationError('native DRC still reports open connections')
        key = lambda violation: json.dumps(violation, sort_keys=True)
        introduced = Counter(map(key, after['violations'])) - Counter(map(key, before['violations']))
        if introduced:
            raise NativeValidationError('candidate introduces native DRC violations')
        # Atomic create-if-absent: a concurrent writer's destination is preserved.
        os.link(temporary, destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    export = sub.add_parser('export')
    export.add_argument('board', type=Path)
    export.add_argument('output', type=Path)
    export.add_argument('--rules', type=Path, required=True)
    export.add_argument('--max-expansions', type=int, default=100000)
    apply = sub.add_parser('apply')
    apply.add_argument('board', type=Path)
    apply.add_argument('plan', type=Path)
    apply.add_argument('output', type=Path)
    args = parser.parse_args()
    if args.command == 'export':
        result = export_board(args.board, json.loads(args.rules.read_text()), args.max_expansions)
        with args.output.open('x') as handle:
            json.dump(result, handle, indent=2)
            handle.write('\n')
    else:
        try:
            apply_plan(args.board, json.loads(args.plan.read_text()), args.output)
        except NativeValidationError as error:
            print(json.dumps({'status': 'native_validation_failed', 'reason': str(error)}), file=sys.stderr)
            return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
