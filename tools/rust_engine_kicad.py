#!/usr/bin/env python3
"""Native KiCad I/O for the standalone Rust engine's first supported subset.

This module makes no routing decisions. Rust owns the edit list; pcbnew owns
native decoding and encoding. Unsupported native features are rejected.
"""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import uuid

import pcbnew


INPUT_VERSION = 1
RESULT_VERSION = 3
# Per-document admission, not a bound on parsed/native allocations or runtime.
MAX_DOCUMENT_BYTES = 16 * 1024 * 1024
MAX_SYMLINK_HOPS = 40
_ORIGINAL_SUBPROCESS_RUN = subprocess.run


def read_bounded(path: Path):
    with path.open('rb') as stream:
        body = stream.read(MAX_DOCUMENT_BYTES + 1)
    if len(body) > MAX_DOCUMENT_BYTES:
        raise ValueError(f'input document exceeds {MAX_DOCUMENT_BYTES} bytes: {path}')
    return body


class UnsupportedBoard(ValueError):
    pass


class NativeValidationError(ValueError):
    pass


def native_violations(path: Path, report: Path, *, timeout: float = 30.0):
    if not math.isfinite(timeout) or timeout <= 0:
        raise NativeValidationError('native DRC timeout must be positive and finite')
    command = ['kicad-cli', 'pcb', 'drc', '--format', 'json', '--severity-all', '--all-track-errors',
               '--output', str(report), str(path)]
    try:
        if subprocess.run is not _ORIGINAL_SUBPROCESS_RUN:
            subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout)
        else:
            proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            in_main = threading.current_thread() is threading.main_thread()
            previous_sigterm = None
            if in_main:
                def _on_sigterm(_signum, _frame):
                    raise InterruptedError('native DRC interrupted by SIGTERM')
                previous_sigterm = signal.signal(signal.SIGTERM, _on_sigterm)
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
                if proc.returncode != 0:
                    raise subprocess.CalledProcessError(proc.returncode, command, output=stdout, stderr=stderr)
            except subprocess.TimeoutExpired as error:
                proc.kill()
                proc.wait()
                raise NativeValidationError(f'native DRC timed out after {timeout}s') from error
            except BaseException:
                if proc.poll() is None:
                    proc.kill()
                proc.wait()
                raise
            finally:
                if in_main and previous_sigterm is not None:
                    signal.signal(signal.SIGTERM, previous_sigterm)
        data = json.loads(read_bounded(report))
        if not isinstance(data, dict) or not isinstance(data.get('violations'), list) or not isinstance(data.get('unconnected_items'), list):
            raise ValueError('native DRC report has invalid violation/connectivity fields')
        return data
    except NativeValidationError:
        raise
    except (OSError, ValueError, KeyError, RecursionError, subprocess.SubprocessError) as error:
        raise NativeValidationError(f'native DRC did not produce a valid result: {error}') from error


def point(value):
    return [int(value.x), int(value.y)]


def item_id(item):
    return item.m_Uuid.AsString()


def _check_sidecar_at(path: Path):
    if not path.name:
        return
    for suffix in ('.kicad_pro', '.kicad_dru'):
        sidecar = path.with_suffix(suffix)
        try:
            os.lstat(sidecar)
        except (FileNotFoundError, NotADirectoryError):
            continue
        raise UnsupportedBoard(f'project/custom-rule import is not implemented: {suffix}')


def reject_external_rules(path: Path):
    cur = path
    seen: set[Path] = set()
    for hop in range(MAX_SYMLINK_HOPS + 1):
        if cur not in seen:
            seen.add(cur)
            _check_sidecar_at(cur)
        parent = cur.parent if cur.parent != Path('') else Path('.')
        canon_parent: Path | None = None
        if cur.name:
            try:
                canon_parent = Path(os.path.realpath(parent))
                norm_cur = canon_parent / cur.name
                if norm_cur not in seen:
                    seen.add(norm_cur)
                    _check_sidecar_at(norm_cur)
            except OSError:
                canon_parent = None
        try:
            st = os.lstat(cur)
        except OSError:
            break
        if not stat.S_ISLNK(st.st_mode):
            break
        if hop >= MAX_SYMLINK_HOPS:
            raise OSError(errno.ELOOP, 'too many levels of symbolic links', str(path))
        target = Path(os.readlink(cur))
        if target.is_absolute():
            cur = target
        else:
            base = canon_parent if canon_parent is not None else parent
            cur = base / target
    try:
        resolved = path.resolve()
    except OSError:
        return
    if resolved not in seen:
        _check_sidecar_at(resolved)


@contextmanager
def source_snapshot(path: Path):
    # Reject rules beside the real source before isolating its standalone bytes.
    # One read defines the source identity, parsed geometry and DRC baseline.
    reject_external_rules(path)
    body = read_bounded(path)
    reject_external_rules(path)
    with tempfile.TemporaryDirectory(prefix='kicad-route-source-') as directory:
        snapshot = Path(directory) / 'source.kicad_pcb'
        snapshot.write_bytes(body)
        yield snapshot


def pad_shape(pad):
    """Decode admitted native copper; leave original native pad metadata intact."""
    copper = [layer for layer in pad.GetLayerSet().Seq() if pcbnew.IsCopperLayer(layer)]
    if pad.GetAttribute() != pcbnew.PAD_ATTRIB_SMD or copper != [pcbnew.F_Cu]:
        raise UnsupportedBoard('only front-copper SMD pads are supported')
    if any(point(getter()) != [0, 0] for getter in (
            pad.GetDrillSize, pad.GetSecondaryDrillSize, pad.GetTertiaryDrillSize)):
        raise UnsupportedBoard('drilled pads are not represented')
    size = point(pad.GetSize())
    if any(v <= 0 for v in size):
        raise ValueError('pad dimensions must be positive')
    kind = pad.GetShape()
    if kind == pcbnew.PAD_SHAPE_CIRCLE:
        if size[0] != size[1] or size[0] % 2 != 0:
            raise UnsupportedBoard('circular pads require equal even dimensions')
        return {'kind': 'circle', 'radius_nm': size[0] // 2}
    if kind not in (pcbnew.PAD_SHAPE_RECT, pcbnew.PAD_SHAPE_OVAL, pcbnew.PAD_SHAPE_ROUNDRECT):
        raise UnsupportedBoard('only circular, rectangular, oval and rounded-rectangle pads are supported')
    angle = pad.GetOrientationDegrees() % 180
    if angle not in (0, 90) or any(v % 2 for v in size):
        raise UnsupportedBoard('noncircular pads require orthogonal rotation and even dimensions')
    half = [v // 2 for v in size]
    if kind == pcbnew.PAD_SHAPE_RECT:
        radius = 0
    elif kind == pcbnew.PAD_SHAPE_OVAL:
        radius = min(half)
    else:
        if pad.GetChamferPositions() != 0:
            raise UnsupportedBoard('chamfered pads are not represented')
        ratio = pad.GetRoundRectRadiusRatio()
        native_radius = pad.GetRoundRectCornerRadius()
        if not math.isfinite(ratio) or not 0 <= ratio <= 0.5 or not math.isfinite(native_radius):
            raise ValueError('rounded pad radius ratio must be finite and between zero and one half')
        radius = int(native_radius)
        if radius != native_radius or not 0 <= radius <= min(half):
            raise ValueError('rounded pad radius must be an integer within either half size')
        # KiCad can collapse near-circle roundrects to one effective circle.
        # Read that actual native shape instead of assuming a version-specific
        # collapse threshold. Nominal metadata stays on the untouched pad.
        raw_effective = pad.GetEffectiveShape(pcbnew.F_Cu)
        if raw_effective is None or raw_effective.Type() == pcbnew.SH_NULL:
            raise UnsupportedBoard('native rounded-pad effective shape is unavailable')
        if raw_effective.Type() == pcbnew.SH_CIRCLE:
            circle = pcbnew.Cast_to_SHAPE_CIRCLE(raw_effective)
            if circle is None or point(circle.GetCenter()) != point(pad.GetPosition()) or circle.GetRadius() != radius:
                raise UnsupportedBoard('native rounded-pad effective circle is not represented')
            return {'kind': 'circle', 'radius_nm': radius}
        effective = pcbnew.Cast_to_SHAPE_COMPOUND(raw_effective)
        if effective is None:
            raise UnsupportedBoard('native rounded-pad effective shape is not represented')
        if effective.Size() == 1 and effective.UniqueSubshape().Type() == pcbnew.SH_CIRCLE:
            circle = pcbnew.Cast_to_SHAPE_CIRCLE(effective.UniqueSubshape())
            if circle is None or point(circle.GetCenter()) != point(pad.GetPosition()) or circle.GetRadius() != radius:
                raise UnsupportedBoard('native rounded-pad effective circle is not represented')
            return {'kind': 'circle', 'radius_nm': radius}
    if angle == 90:
        half.reverse()
    if kind == pcbnew.PAD_SHAPE_RECT:
        return {'kind': 'rect', 'half_size_nm': half}
    return {'kind': 'round_rect', 'half_size_nm': half, 'radius_nm': radius}


def validate_native_pad_scalars(body: bytes):
    """Reject native values pcbnew otherwise clamps before exposing its model.

    This small S-expression token walk validates source scalars only. Native
    decoding still owns the board, and Rust owns all copper geometry decisions.
    Quoted text cannot manufacture a pad/size/radius expression.
    """
    stack = []
    tokens = re.finditer(r'\(|\)|"(?:\\.|[^"\\])*"|[^\s()"]+|"', body.decode('utf-8'))
    for match in tokens:
        token = match.group()
        if token == '"':
            raise ValueError('unterminated string in native board expression')
        if token == '(':
            in_pad_descendant = bool(stack and (stack[-1][0] == 'pad' or stack[-1][2]))
            in_pad_direct = bool(stack and stack[-1][0] == 'pad')
            stack.append([None, [], in_pad_descendant, in_pad_direct, False])
        elif token == ')':
            if not stack:
                raise ValueError('unbalanced native board expression')
            head, values, in_pad_descendant, in_pad_direct, saw_size = stack.pop()
            if in_pad_descendant and head == 'drill':
                raise UnsupportedBoard('drilled pads are not represented')
            if in_pad_descendant and head == 'padstack':
                raise UnsupportedBoard('custom pad stacks are not represented')
            if in_pad_descendant and head in ('chamfer', 'chamfer_ratio'):
                raise UnsupportedBoard('chamfered pads are not represented')
            if head == 'pad' and not saw_size:
                raise ValueError('pad dimensions must be declared explicitly')
            if not in_pad_direct or head not in ('size', 'roundrect_rratio'):
                continue
            if any('_' in value for value in values):
                raise ValueError('pad dimensions/radius must be finite numeric scalars')
            try:
                numbers = [Decimal(value) for value in values]
            except InvalidOperation as error:
                raise ValueError('pad dimensions/radius must be finite numeric scalars') from error
            if not all(v.is_finite() for v in numbers):
                raise ValueError('pad dimensions/radius must be finite numeric scalars')
            if head == 'size':
                if len(numbers) != 2 or any(v <= 0 or v > 200 for v in numbers):
                    raise ValueError('pad dimensions must be positive and at most 200 mm')
                for value in numbers:
                    _, digits, exponent = value.as_tuple()
                    if exponent < -6 and any(digits[max(0, len(digits) + exponent + 6):]):
                        raise ValueError('pad dimensions must be exact integer nanometres')
                stack[-1][4] = True
            if head == 'roundrect_rratio' and (len(numbers) != 1 or not 0 <= numbers[0] <= Decimal('.5')):
                raise ValueError('rounded pad radius ratio must be between zero and one half')
        elif stack:
            if stack[-1][0] is None:
                stack[-1][0] = token
            elif stack[-1][3] and stack[-1][0] in ('size', 'roundrect_rratio'):
                stack[-1][1].append(token)
    if stack:
        raise ValueError('unbalanced native board expression')


def load_supported(path: Path):
    reject_external_rules(path)
    validate_native_pad_scalars(read_bounded(path))
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
            pad_shape(pad)
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


def export_board(path: Path, rules: dict, max_expansions: int, limits: dict | None = None,
                 *, original_source: Path | None = None):
    if original_source is not None:
        reject_external_rules(original_source)
    with source_snapshot(path) as snapshot:
        return export_snapshot(snapshot, rules, max_expansions, limits)


def export_snapshot(path: Path, rules: dict, max_expansions: int, limits: dict | None = None):
    if limits is not None and not isinstance(limits, dict):
        raise ValueError('limits must be an object')
    board = load_supported(path)
    settings = board.GetDesignSettings()
    minima = {'width_nm': settings.m_TrackMinWidth,
              'clearance_nm': max(settings.m_MinClearance, settings.GetBiggestClearanceValue()),
              'edge_clearance_nm': settings.m_CopperEdgeClearance}
    if not isinstance(rules, dict) or set(rules) != {'width_nm', 'clearance_nm', 'grid_nm', 'edge_clearance_nm'}:
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
            shape = pad_shape(pad)
            pads.append({'id': item_id(pad), 'net': int(pad.GetNetCode()),
                         'at': point(pad.GetPosition()), 'shape': shape})
    pads.sort(key=lambda item: (item['net'], item['at'], item['id']))
    tracks = []
    for track in board.GetTracks():
        if type(track) is not pcbnew.PCB_TRACK or track.GetLayer() != pcbnew.F_Cu:
            raise UnsupportedBoard('vias, arcs and non-front tracks are not supported')
        tracks.append({'id': item_id(track), 'net': int(track.GetNetCode()),
                       'start': point(track.GetStart()), 'end': point(track.GetEnd()),
                       'width_nm': int(track.GetWidth()), 'locked': bool(track.IsLocked())})
    tracks.sort(key=lambda item: (item['net'], item['start'], item['end'],
                                  item['width_nm'], item['locked'], item['id']))
    nets = [{'id': int(code), 'name': net.GetNetname()}
            for code, net in board.GetNetsByNetcode().items()]
    nets.sort(key=lambda item: (item['id'], item['name']))
    result = {'version': INPUT_VERSION, 'source_id': hashlib.sha256(read_bounded(path)).hexdigest(),
              'layer': 'F.Cu', 'bounds': rectangular_bounds(board), 'rules': rules,
              'nets': nets, 'pads': pads, 'tracks': tracks, 'max_expansions': max_expansions}
    # Resource policy and defaults belong to Rust. Preserve supplied limits
    # exactly; omitting them retains the existing input contract.
    if limits is not None:
        result['limits'] = limits
    return result


def apply_plan(source: Path, plan: dict, destination: Path, *, expected_request: bytes,
               original_source: Path | None = None, drc_timeout: float = 30.0,
               validation_report: Path | None = None):
    if source.resolve() == destination.resolve():
        raise ValueError('output must be a new path distinct from the input')
    if os.path.lexists(destination):
        raise FileExistsError(f'output already exists: {destination}')
    if validation_report is not None and os.path.lexists(validation_report):
        raise FileExistsError(f'validation report already exists: {validation_report}')
    if original_source is not None:
        reject_external_rules(original_source)
    with source_snapshot(source) as snapshot:
        apply_snapshot(snapshot, plan, destination, expected_request=expected_request,
                       drc_timeout=drc_timeout, validation_report=validation_report)


def apply_snapshot(source: Path, plan: dict, destination: Path, *, expected_request: bytes,
                   drc_timeout: float = 30.0, validation_report: Path | None = None):
    if not math.isfinite(drc_timeout) or drc_timeout <= 0:
        raise ValueError('drc timeout must be positive and finite')
    if os.path.lexists(destination):
        raise FileExistsError(f'output already exists: {destination}')
    if validation_report is not None and os.path.lexists(validation_report):
        raise FileExistsError(f'validation report already exists: {validation_report}')
    if not isinstance(plan, dict) or type(plan.get('version')) is not int or plan.get('version') != RESULT_VERSION or plan.get('status') != 'complete':
        raise ValueError('only a complete version3 Rust result can be written')
    if not isinstance(expected_request, bytes):
        raise ValueError('expected request must be the exact request bytes')
    if len(expected_request) > MAX_DOCUMENT_BYTES:
        raise ValueError(f'expected request exceeds {MAX_DOCUMENT_BYTES} bytes')
    # Bind to the complete retained request, including rules and limits, before
    # loading or applying native edits. This checks consistency, not provenance.
    # Do not parse/re-serialize: whitespace and explicit defaults affect identity.
    request_digest = hashlib.sha256(expected_request).hexdigest()
    if plan.get('request_sha256') != request_digest:
        raise ValueError('Rust plan request_sha256 does not match the expected request')
    for counter in ('expanded_states', 'work_used'):
        if type(plan.get(counter)) is not int or not 0 <= plan[counter] < 2**64:
            raise ValueError(f'Rust plan requires {counter} as an unsigned 64-bit integer')
    source_digest = hashlib.sha256(read_bounded(source)).hexdigest()
    if plan.get('source_id') != source_digest:
        raise ValueError('Rust plan does not match the input board')
    removals = plan.get('removed_track_ids')
    if not isinstance(removals, list) or any(not isinstance(ident, str) for ident in removals):
        raise ValueError('Rust result requires an explicit removed_track_ids list of strings')
    if removals:
        raise UnsupportedBoard('this slice cannot remove input copper')
    additions = plan.get('added_tracks')
    if not isinstance(additions, list):
        raise ValueError('Rust plan must contain an explicit added_tracks list')
    board = load_supported(source)
    used_ids = {item_id(x) for x in board.GetTracks()} | {item_id(x) for x in board.GetPads()}
    used_ids |= {item_id(x) for x in board.GetFootprints()} | {item_id(x) for x in board.GetDrawings()}
    nets = set(board.GetNetsByNetcode())
    for item in additions:
        if not isinstance(item, dict) or set(item) != {'id', 'net', 'start', 'end', 'width_nm', 'locked'}:
            raise ValueError('Rust output track must contain exactly id, net, start, end, width_nm and locked')
        ident = item['id']
        if not isinstance(ident, str) or str(uuid.UUID(ident)) != ident or uuid.UUID(ident).int == 0 or ident in used_ids:
            raise ValueError('Rust output item identity must be canonical, nonzero and unique')
        if type(item['net']) is not int or item['net'] not in nets or item['net'] == 0 or type(item['width_nm']) is not int or not 0 < item['width_nm'] <= 1_000_000_000:
            raise ValueError('Rust output track requires a known integer net and width within the supported native range')
        if type(item['locked']) is not bool:
            raise ValueError('Rust output lock flag must be boolean')
        if item['width_nm'] < board.GetDesignSettings().m_TrackMinWidth:
            raise NativeValidationError('Rust output track width is below the native minimum')
        if any(not isinstance(item[key], list) or len(item[key]) != 2 or any(type(n) is not int or abs(n) > 1_000_000_000 for n in item[key]) for key in ('start', 'end')):
            raise ValueError('Rust output coordinates exceed the supported native range')
        if item['start'] == item['end']:
            raise ValueError('Rust output track must have distinct endpoints')
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
        before_unconnected = saved.GetConnectivity().GetUnconnectedCount(False)
        if before_unconnected != 0:
            raise NativeValidationError('native connectivity still has open connections')
        before = native_violations(source, directory / 'before-drc.json', timeout=drc_timeout)
        after = native_violations(temporary, directory / 'after-drc.json', timeout=drc_timeout)
        if after['unconnected_items']:
            raise NativeValidationError('native DRC still reports open connections')
        key = lambda violation: json.dumps(violation, sort_keys=True)
        introduced = Counter(map(key, after['violations'])) - Counter(map(key, before['violations']))
        if introduced:
            raise NativeValidationError('candidate introduces native DRC violations')
        if validation_report is not None:
            digest = hashlib.sha256()
            candidate_bytes = 0
            with temporary.open('rb') as stream:
                while chunk := stream.read(65536):
                    digest.update(chunk)
                    candidate_bytes += len(chunk)
            report_payload = {
                'schema': 'kicad-route/native-apply-report/1',
                'version': 1,
                'source_id': source_digest,
                'request_sha256': request_digest,
                'candidate_sha256': digest.hexdigest(),
                'candidate_bytes': candidate_bytes,
                'before_unconnected_count': len(before['unconnected_items']),
                'after_unconnected_count': len(after['unconnected_items']),
                'before_violation_count': len(before['violations']),
                'after_violation_count': len(after['violations']),
                'introduced_violation_count': sum(introduced.values()),
                'added_track_count': len(additions),
                'kicad_version': str(pcbnew.Version()) if hasattr(pcbnew, 'Version') else 'unknown',
            }
        # Atomic create-if-absent: a concurrent writer's destination is preserved.
        os.link(temporary, destination)
        if validation_report is not None:
            write_request(report_payload, validation_report)


def read_object(path: Path):
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f'duplicate JSON field: {key}')
            result[key] = value
        return result

    try:
        result = json.loads(read_bounded(path), object_pairs_hook=unique_keys)
    except RecursionError as error:
        raise ValueError('JSON input exceeds parser nesting capacity') from error
    if not isinstance(result, dict):
        raise ValueError('JSON input must be an object')
    return result


def write_request(result: dict, destination: Path):
    try:
        # Indentation grows quadratically with nesting before admission can run.
        body = json.dumps(result, separators=(',', ':')) + '\n'
    except RecursionError as error:
        raise ValueError('exported JSON exceeds encoder nesting capacity') from error
    # ensure_ascii's default makes every serialized character exactly one byte.
    if len(body) > MAX_DOCUMENT_BYTES:
        raise ValueError(f'exported JSON exceeds {MAX_DOCUMENT_BYTES} bytes')
    with tempfile.TemporaryDirectory(prefix=f'.{destination.stem}-', dir=destination.parent) as directory:
        temporary = Path(directory) / destination.name
        temporary.write_text(body, encoding='utf-8')
        os.link(temporary, destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    export = sub.add_parser('export')
    export.add_argument('board', type=Path)
    export.add_argument('output', type=Path)
    export.add_argument('--rules', type=Path, required=True)
    export.add_argument('--max-expansions', type=int, default=100000)
    export.add_argument('--limits', type=Path,
                        help='optional Rust resource limits JSON; Rust validates policy and defaults')
    export.add_argument('--original-source', type=Path, default=None,
                        help='optional original source board path for sidecar checks before snapshotting')
    apply = sub.add_parser('apply')
    apply.add_argument('board', type=Path)
    apply.add_argument('plan', type=Path)
    apply.add_argument('output', type=Path)
    apply.add_argument('--request', type=Path, required=True,
                       help='exact retained request JSON passed to the Rust engine')
    apply.add_argument('--original-source', type=Path, default=None,
                       help='optional original source board path for sidecar checks before snapshotting')
    apply.add_argument('--drc-timeout', type=float, default=30.0,
                       help='timeout in seconds for each native KiCad DRC invocation')
    apply.add_argument('--validation-report', type=Path, default=None,
                       help='optional atomic output path for native apply validation metadata')
    args = parser.parse_args()
    try:
        if args.command == 'export':
            limits = read_object(args.limits) if args.limits is not None else None
            result = export_board(args.board, read_object(args.rules), args.max_expansions, limits,
                                  original_source=args.original_source)
            write_request(result, args.output)
        else:
            apply_plan(args.board, read_object(args.plan), args.output,
                       expected_request=read_bounded(args.request),
                       original_source=args.original_source,
                       drc_timeout=args.drc_timeout,
                       validation_report=args.validation_report)
    except (UnsupportedBoard, NativeValidationError, FileExistsError, OSError, ValueError) as error:
        if isinstance(error, UnsupportedBoard):
            status = 'unsupported'
        elif isinstance(error, NativeValidationError):
            status = 'native_validation_failed'
        elif isinstance(error, FileExistsError):
            status = 'output_exists'
        elif isinstance(error, OSError):
            status = 'io_error'
        else:
            status = 'invalid'
        print(json.dumps({'status': status, 'reason': str(error)}), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
