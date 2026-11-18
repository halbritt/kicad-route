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


def _extract_drill_shape(pad):
    for getter in (pad.GetSecondaryDrillSize, pad.GetTertiaryDrillSize):
        if point(getter()) != [0, 0]:
            raise UnsupportedBoard('secondary or tertiary pad drills are not represented')
    if hasattr(pad, 'IsBackdrilledOrPostMachined'):
        if any(pad.IsBackdrilledOrPostMachined(layer) for layer in (pcbnew.F_Cu, pcbnew.B_Cu)):
            raise UnsupportedBoard('backdrilled or post-machined pads are not represented')
    drill_shape = pad.GetDrillShape()
    if drill_shape not in (pcbnew.PAD_DRILL_SHAPE_CIRCLE, pcbnew.PAD_DRILL_SHAPE_OBLONG):
        raise UnsupportedBoard('only circular and oblong pad drills are supported')
    drill_size = point(pad.GetDrillSize())
    if any(v <= 0 for v in drill_size):
        raise ValueError('pad drill dimensions must be positive')
    hole = pad.GetEffectiveHoleShape()
    if hole is None or hole.Type() == pcbnew.SH_NULL:
        raise UnsupportedBoard('native pad hole effective shape is unavailable')
    pad_pos = point(pad.GetPosition())
    if hole.Type() == pcbnew.SH_CIRCLE:
        circle = pcbnew.Cast_to_SHAPE_CIRCLE(hole)
        if circle is None:
            raise UnsupportedBoard('native pad hole circle is unavailable')
        center = point(circle.GetCenter())
        diam = 2 * int(circle.GetRadius())
        if drill_size[0] == drill_size[1]:
            diam = max(diam, drill_size[0])
        if center == pad_pos:
            return {'kind': 'circle', 'diameter_nm': diam}
        return {'kind': 'capsule', 'start_nm': center, 'end_nm': center, 'width_nm': diam}
    if hole.Type() == pcbnew.SH_SEGMENT:
        seg = pcbnew.Cast_to_SHAPE_SEGMENT(hole)
        if seg is None:
            raise UnsupportedBoard('native pad hole segment is unavailable')
        s = seg.GetSeg()
        a, b = [int(s.A.x), int(s.A.y)], [int(s.B.x), int(s.B.y)]
        w = int(seg.GetWidth())
        if a == b and a == pad_pos:
            return {'kind': 'circle', 'diameter_nm': w}
        return {'kind': 'capsule', 'start_nm': a, 'end_nm': b, 'width_nm': w}
    raise UnsupportedBoard('native pad hole effective shape is not represented')


def _simplify_ring(pts):
    dedup = []
    for pt in pts:
        if not dedup or dedup[-1] != pt:
            dedup.append(pt)
    if len(dedup) > 1 and dedup[0] == dedup[-1]:
        dedup.pop()
    return dedup


def _cross(a, b, c):
    return (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])


def _dot(a, b, c):
    return (b[0] - a[0]) * (c[0] - b[0]) + (b[1] - a[1]) * (c[1] - b[1])


def _orient_ccw_convex(pts):
    pts = _simplify_ring(pts)
    if len(pts) < 3:
        return pts, False
    area2 = sum(pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1]
                for i in range(len(pts)))
    if area2 == 0:
        return pts, False
    if area2 < 0:
        pts.reverse()
    cleaned = []
    n = len(pts)
    for i in range(n):
        a, b, c = pts[(i - 1) % n], pts[i], pts[(i + 1) % n]
        if _cross(a, b, c) == 0 and _dot(a, b, c) >= 0:
            continue
        cleaned.append(b)
    if len(cleaned) < 3:
        return cleaned, False
    m = len(cleaned)
    for i in range(m):
        if _cross(cleaned[i], cleaned[(i + 1) % m], cleaned[(i + 2) % m]) <= 0:
            return cleaned, False
    return cleaned, True


def _decode_effective_pad_shape(pad, radius_hint=None):
    raw_effective = pad.GetEffectiveShape(pcbnew.F_Cu)
    if raw_effective is None or raw_effective.Type() == pcbnew.SH_NULL:
        raise UnsupportedBoard('native pad effective shape is unavailable')
    if raw_effective.Type() == pcbnew.SH_COMPOUND:
        comp = pcbnew.Cast_to_SHAPE_COMPOUND(raw_effective)
        if comp is None:
            raise UnsupportedBoard('native pad effective shape is unavailable')
        subs = list(comp.GetSubshapes())
    else:
        subs = [raw_effective]
    pad_pos = point(pad.GetPosition())
    if len(subs) == 1 and subs[0].Type() == pcbnew.SH_CIRCLE:
        circle = pcbnew.Cast_to_SHAPE_CIRCLE(subs[0])
        if circle is None:
            raise UnsupportedBoard('native pad effective circle is unavailable')
        center = point(circle.GetCenter())
        r = int(circle.GetRadius())
        if center == pad_pos:
            return {'kind': 'circle', 'radius_nm': r}
        return {'kind': 'box_exact', 'center_2x': [2 * center[0], 2 * center[1]],
                'half_size_2x': [2 * r, 2 * r], 'radius_2x': 2 * r}
    if len(subs) == 1 and subs[0].Type() == pcbnew.SH_SEGMENT:
        seg = pcbnew.Cast_to_SHAPE_SEGMENT(subs[0])
        if seg is None:
            raise UnsupportedBoard('native pad effective segment is unavailable')
        s = seg.GetSeg()
        a, b = [int(s.A.x), int(s.A.y)], [int(s.B.x), int(s.B.y)]
        w = int(seg.GetWidth())
        if a == b and a == pad_pos and w % 2 == 0:
            return {'kind': 'circle', 'radius_nm': w // 2}
        return {'kind': 'capsule', 'start_nm': a, 'end_nm': b, 'width_nm': w}
    if len(subs) == 1 and subs[0].Type() == pcbnew.SH_RECT:
        rc = pcbnew.Cast_to_SHAPE_RECT(subs[0])
        if rc is None:
            raise UnsupportedBoard('native pad effective rect is unavailable')
        pos, sz = point(rc.GetPosition()), point(rc.GetSize())
        return {'kind': 'box_exact',
                'center_2x': [2 * pos[0] + sz[0], 2 * pos[1] + sz[1]],
                'half_size_2x': sz, 'radius_2x': 0}
    if len(subs) == 5 and subs[0].Type() == pcbnew.SH_RECT and all(s.Type() == pcbnew.SH_SEGMENT for s in subs[1:]):
        widths = {int(pcbnew.Cast_to_SHAPE_SEGMENT(s).GetWidth()) for s in subs[1:]}
        if len(widths) != 1:
            raise UnsupportedBoard('native rounded-pad segment widths are inconsistent')
        w_seg = next(iter(widths))
        if w_seg % 2 != 0:
            raise UnsupportedBoard('native rounded-pad segment width is odd')
        rc = pcbnew.Cast_to_SHAPE_RECT(subs[0])
        if rc is None:
            raise UnsupportedBoard('native pad effective rect is unavailable')
        pos, sz = point(rc.GetPosition()), point(rc.GetSize())
        return {'kind': 'box_exact',
                'center_2x': [2 * pos[0] + sz[0], 2 * pos[1] + sz[1]],
                'half_size_2x': [sz[0] + w_seg, sz[1] + w_seg], 'radius_2x': w_seg}
    if subs and subs[0].Type() == pcbnew.SH_SIMPLE:
        r_nm = 0
        if len(subs) == 5 and all(s.Type() == pcbnew.SH_SEGMENT for s in subs[1:]):
            widths = {int(pcbnew.Cast_to_SHAPE_SEGMENT(s).GetWidth()) for s in subs[1:]}
            if len(widths) != 1:
                raise UnsupportedBoard('native rounded-pad segment widths are inconsistent')
            w_seg = next(iter(widths))
            if w_seg % 2 != 0:
                raise UnsupportedBoard('native rounded-pad segment width is odd')
            r_nm = w_seg // 2
        elif len(subs) != 1:
            raise UnsupportedBoard('native pad effective compound shape is not represented')
        elif radius_hint is not None:
            r_nm = radius_hint
        poly = pcbnew.Cast_to_SHAPE_SIMPLE(subs[0])
        if poly is None:
            raise UnsupportedBoard('native pad effective polygon is unavailable')
        raw_pts = [[int(poly.CPoint(j).x), int(poly.CPoint(j).y)] for j in range(poly.PointCount())]
        pts = _simplify_ring(raw_pts)
        if len(pts) == 1:
            c = pts[0]
            if r_nm <= 0:
                raise UnsupportedBoard('degenerate zero-radius pad polygon')
            if c == pad_pos:
                return {'kind': 'circle', 'radius_nm': r_nm}
            return {'kind': 'box_exact', 'center_2x': [2 * c[0], 2 * c[1]],
                    'half_size_2x': [2 * r_nm, 2 * r_nm], 'radius_2x': 2 * r_nm}
        if len(pts) == 2:
            if r_nm <= 0:
                raise UnsupportedBoard('degenerate zero-width pad segment')
            return {'kind': 'capsule', 'start_nm': pts[0], 'end_nm': pts[1], 'width_nm': 2 * r_nm}
        oriented, ok = _orient_ccw_convex(pts)
        if ok:
            return {'kind': 'convex_polygon', 'vertices': oriented, 'radius_nm': r_nm}
        if r_nm > 0 and len(pts) >= 3:
            # Collinear rounded-rect corners collapse to a capsule along the major axis.
            best = max(((pts[i], pts[j]) for i in range(len(pts)) for j in range(i + 1, len(pts))),
                       key=lambda pair: (pair[0][0] - pair[1][0]) ** 2 + (pair[0][1] - pair[1][1]) ** 2)
            return {'kind': 'capsule', 'start_nm': best[0], 'end_nm': best[1], 'width_nm': 2 * r_nm}
        raise UnsupportedBoard('non-convex pad polygon is not supported')
    raise UnsupportedBoard('native pad effective shape is not represented')


def pad_shape(pad):
    """Decode admitted native copper; leave original native pad metadata intact."""
    copper = [layer for layer in pad.GetLayerSet().Seq() if pcbnew.IsCopperLayer(layer)]
    attrib = pad.GetAttribute()
    if attrib == pcbnew.PAD_ATTRIB_SMD:
        if copper != [pcbnew.F_Cu]:
            raise UnsupportedBoard('only front-copper SMD pads are supported')
        if any(point(getter()) != [0, 0] for getter in (
                pad.GetDrillSize, pad.GetSecondaryDrillSize, pad.GetTertiaryDrillSize)):
            raise UnsupportedBoard('drilled pads are not represented')
    elif attrib == pcbnew.PAD_ATTRIB_PTH:
        if pcbnew.F_Cu not in copper:
            raise UnsupportedBoard('through-hole pads must include front copper')
        if any(v <= 0 for v in point(pad.GetDrillSize())):
            raise UnsupportedBoard('PTH pad requires a positive drill size')
    elif attrib == pcbnew.PAD_ATTRIB_NPTH:
        if copper or int(pad.GetNetCode()) != 0:
            raise UnsupportedBoard('NPTH mounting holes cannot carry copper layers or a net')
    else:
        raise UnsupportedBoard('only SMD, PTH and NPTH pads are supported')
    size = point(pad.GetSize())
    if any(v <= 0 for v in size):
        raise ValueError('pad dimensions must be positive')
    kind = pad.GetShape()
    if kind in (pcbnew.PAD_SHAPE_CHAMFERED_RECT, pcbnew.PAD_SHAPE_CUSTOM):
        if kind == pcbnew.PAD_SHAPE_CHAMFERED_RECT:
            raise UnsupportedBoard('chamfered pads are not represented')
        raise UnsupportedBoard('custom pads are not represented')
    if kind not in (pcbnew.PAD_SHAPE_CIRCLE, pcbnew.PAD_SHAPE_RECT,
                    pcbnew.PAD_SHAPE_OVAL, pcbnew.PAD_SHAPE_ROUNDRECT,
                    pcbnew.PAD_SHAPE_TRAPEZOID):
        raise UnsupportedBoard('only circular, rectangular, oval, rounded-rectangle and trapezoid pads are supported')
    if kind == pcbnew.PAD_SHAPE_CIRCLE and size[0] != size[1]:
        raise UnsupportedBoard('circular pads require equal dimensions')
    radius = 0
    if kind == pcbnew.PAD_SHAPE_ROUNDRECT:
        if pad.GetChamferPositions() != 0:
            raise UnsupportedBoard('chamfered pads are not represented')
        ratio = pad.GetRoundRectRadiusRatio()
        native_radius = pad.GetRoundRectCornerRadius()
        if not math.isfinite(ratio) or not 0 <= ratio <= 0.5 or not math.isfinite(native_radius):
            raise ValueError('rounded pad radius ratio must be finite and between zero and one half')
        radius = int(native_radius)
        max_radius = (min(size) + 1) // 2 if ratio == 0.5 else min(size) // 2
        if radius != native_radius or not 0 <= radius <= max_radius:
            raise ValueError('rounded pad radius must be an integer within either half size')

    offset = point(pad.GetOffset())
    angle = pad.GetOrientationDegrees() % 180
    is_ortho = angle in (0, 90)
    is_even = size[0] % 2 == 0 and size[1] % 2 == 0

    if offset == [0, 0] and is_ortho and is_even and kind in (
            pcbnew.PAD_SHAPE_CIRCLE, pcbnew.PAD_SHAPE_RECT,
            pcbnew.PAD_SHAPE_OVAL, pcbnew.PAD_SHAPE_ROUNDRECT):
        if kind == pcbnew.PAD_SHAPE_CIRCLE:
            return {'kind': 'circle', 'radius_nm': size[0] // 2}
        half = [v // 2 for v in size]
        if kind == pcbnew.PAD_SHAPE_RECT:
            radius = 0
        elif kind == pcbnew.PAD_SHAPE_OVAL:
            radius = min(half)
        else:
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

    if kind == pcbnew.PAD_SHAPE_CIRCLE or (kind == pcbnew.PAD_SHAPE_OVAL and size[0] == size[1]):
        shape_pos = point(pad.ShapePos(pcbnew.F_Cu))
        if size[0] % 2 == 0 and shape_pos == point(pad.GetPosition()):
            return {'kind': 'circle', 'radius_nm': size[0] // 2}
        return {'kind': 'box_exact', 'center_2x': [2 * shape_pos[0], 2 * shape_pos[1]],
                'half_size_2x': [size[0], size[0]], 'radius_2x': size[0]}

    if is_ortho and (kind == pcbnew.PAD_SHAPE_RECT or (kind == pcbnew.PAD_SHAPE_ROUNDRECT and radius <= min(size) // 2)):
        w, h = size[0], size[1]
        if angle == 90:
            w, h = h, w
        shape_pos = point(pad.ShapePos(pcbnew.F_Cu))
        if kind == pcbnew.PAD_SHAPE_ROUNDRECT:
            raw_effective = pad.GetEffectiveShape(pcbnew.F_Cu)
            if raw_effective is None or raw_effective.Type() == pcbnew.SH_NULL:
                raise UnsupportedBoard('native rounded-pad effective shape is unavailable')
            if raw_effective.Type() == pcbnew.SH_CIRCLE:
                circle = pcbnew.Cast_to_SHAPE_CIRCLE(raw_effective)
                if circle is None:
                    raise UnsupportedBoard('native rounded-pad effective circle is not represented')
                c_pos = point(circle.GetCenter())
                c_r = int(circle.GetRadius())
                if c_pos == point(pad.GetPosition()):
                    return {'kind': 'circle', 'radius_nm': c_r}
                return {'kind': 'box_exact', 'center_2x': [2 * c_pos[0], 2 * c_pos[1]],
                        'half_size_2x': [2 * c_r, 2 * c_r], 'radius_2x': 2 * c_r}
            if raw_effective.Type() == pcbnew.SH_COMPOUND:
                effective = pcbnew.Cast_to_SHAPE_COMPOUND(raw_effective)
                if effective is None:
                    raise UnsupportedBoard('native rounded-pad effective shape is not represented')
                if effective.Size() == 1 and effective.UniqueSubshape().Type() == pcbnew.SH_CIRCLE:
                    circle = pcbnew.Cast_to_SHAPE_CIRCLE(effective.UniqueSubshape())
                    if circle is None:
                        raise UnsupportedBoard('native rounded-pad effective circle is not represented')
                    c_pos = point(circle.GetCenter())
                    c_r = int(circle.GetRadius())
                    if c_pos == point(pad.GetPosition()):
                        return {'kind': 'circle', 'radius_nm': c_r}
                    return {'kind': 'box_exact', 'center_2x': [2 * c_pos[0], 2 * c_pos[1]],
                            'half_size_2x': [2 * c_r, 2 * c_r], 'radius_2x': 2 * c_r}
        radius_2x = 0 if kind == pcbnew.PAD_SHAPE_RECT else 2 * radius
        return {'kind': 'box_exact', 'center_2x': [2 * shape_pos[0], 2 * shape_pos[1]],
                'half_size_2x': [w, h], 'radius_2x': radius_2x}

    return _decode_effective_pad_shape(pad, radius_hint=radius if kind == pcbnew.PAD_SHAPE_ROUNDRECT else None)


def _check_decimal_nm(value, *, min_mm, max_mm, message):
    if '_' in value:
        raise ValueError(message)
    try:
        num = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(message) from error
    if not num.is_finite():
        raise ValueError(message)
    if num <= min_mm if min_mm == 0 and message.startswith('pad dimensions') else not (min_mm <= num <= max_mm):
        raise ValueError(message)
    if min_mm == 0 and num > max_mm:
        raise ValueError(message)
    _, digits, exponent = num.as_tuple()
    if exponent < -6 and any(digits[max(0, len(digits) + exponent + 6):]):
        raise ValueError('pad dimensions must be exact integer nanometres')
    return num


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
            in_drill_direct = bool(stack and stack[-1][0] == 'drill')
            stack.append([None, [], in_pad_descendant, in_pad_direct, False, in_drill_direct])
        elif token == ')':
            if not stack:
                raise ValueError('unbalanced native board expression')
            head, values, in_pad_descendant, in_pad_direct, saw_size, in_drill_direct = stack.pop()
            if in_pad_descendant and head == 'drill':
                pad_frame = next((frame for frame in reversed(stack) if frame[0] == 'pad'), None)
                pad_attrib = pad_frame[1][1] if pad_frame and len(pad_frame[1]) >= 2 else None
                drill_vals = values[1:] if values and values[0] == 'oval' else values
                if not drill_vals:
                    if pad_attrib == 'smd' and saw_size:
                        continue
                    if pad_attrib not in ('thru_hole', 'np_thru_hole'):
                        raise UnsupportedBoard('drilled pads are not represented')
                    raise ValueError('pad drill dimensions must be declared explicitly')
                if pad_attrib not in ('thru_hole', 'np_thru_hole'):
                    raise UnsupportedBoard('drilled pads are not represented')
                if not 1 <= len(drill_vals) <= 2:
                    raise ValueError('pad drill dimensions must be declared explicitly')
                for v in drill_vals:
                    num = _check_decimal_nm(v, min_mm=0, max_mm=200,
                                            message='pad drill dimensions must be finite positive scalars <= 200 mm')
                    if num <= 0:
                        raise ValueError('pad drill dimensions must be positive and at most 200 mm')
                continue
            if in_pad_descendant and in_drill_direct and head == 'offset':
                if len(values) != 2:
                    raise ValueError('drill offset requires two coordinates')
                for v in values:
                    _check_decimal_nm(v, min_mm=-200, max_mm=200,
                                      message='drill offset must be finite scalars within +/-200 mm')
                if stack and stack[-1][0] == 'drill':
                    stack[-1][4] = True
                continue
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
            elif stack[-1][0] == 'pad':
                stack[-1][1].append(token)
            elif stack[-1][3] and stack[-1][0] in ('size', 'roundrect_rratio', 'drill'):
                stack[-1][1].append(token)
            elif stack[-1][5] and stack[-1][0] == 'offset':
                stack[-1][1].append(token)
    if stack:
        raise ValueError('unbalanced native board expression')


def _check_graphic_drawing(drawing, *, context_label):
    if not isinstance(drawing, pcbnew.PCB_SHAPE):
        raise UnsupportedBoard(f'{context_label} non-shape graphics are not represented')
    if drawing.GetShape() == pcbnew.SHAPE_T_BEZIER:
        raise UnsupportedBoard(f'{context_label} Bezier curves are not represented')
    if drawing.IsHatchedFill():
        raise UnsupportedBoard(f'{context_label} hatched fill graphics are not represented')


def _extract_poly_ring(poly):
    pts = [[int(poly.CPoint(j).x), int(poly.CPoint(j).y)] for j in range(poly.PointCount())]
    return _simplify_ring(pts)


def _extract_graphic_shapes(drawing, *, base_id):
    shape_t = drawing.GetShape()
    width_nm = int(drawing.GetWidth())
    filled = bool(drawing.IsSolidFill())
    if shape_t == pcbnew.SHAPE_T_SEGMENT:
        a, b = point(drawing.GetStart()), point(drawing.GetEnd())
        if a == b:
            return []
        return [(base_id, {'kind': 'segment', 'start': a, 'end': b, 'width_nm': width_nm})]
    if shape_t == pcbnew.SHAPE_T_ARC:
        return [(base_id, {
            'kind': 'arc',
            'start': point(drawing.GetStart()),
            'mid': point(drawing.GetArcMid()),
            'end': point(drawing.GetEnd()),
            'width_nm': width_nm,
        })]
    if shape_t == pcbnew.SHAPE_T_CIRCLE:
        return [(base_id, {
            'kind': 'circle',
            'center': point(drawing.GetCenter()),
            'radius_nm': int(drawing.GetRadius()),
            'width_nm': width_nm,
            'filled': filled,
        })]
    if shape_t == pcbnew.SHAPE_T_RECT:
        corners = _simplify_ring([point(p) for p in drawing.GetRectCorners()])
        area2 = sum(corners[i][0] * corners[(i + 1) % len(corners)][1]
                    - corners[(i + 1) % len(corners)][0] * corners[i][1]
                    for i in range(len(corners)))
        if area2 < 0:
            corners.reverse()
        return [(base_id, {'kind': 'polygon', 'outer': corners, 'width_nm': width_nm, 'filled': filled})]
    if shape_t == pcbnew.SHAPE_T_POLY:
        poly_set = drawing.GetPolyShape()
        if filled:
            poly_set = pcbnew.SHAPE_POLY_SET(poly_set)
            poly_set.Simplify()
        count = poly_set.OutlineCount()
        if count <= 0:
            raise UnsupportedBoard('empty polygon graphic is not represented')
        out = []
        for idx in range(count):
            outer = _extract_poly_ring(poly_set.Outline(idx))
            holes = [_extract_poly_ring(poly_set.Hole(idx, h)) for h in range(poly_set.HoleCount(idx))]
            shape_obj = {'kind': 'polygon', 'outer': outer, 'width_nm': width_nm, 'filled': filled}
            if holes:
                shape_obj['holes'] = holes
            item_uuid = base_id if count == 1 else str(uuid.uuid5(uuid.UUID(base_id), str(idx)))
            out.append((item_uuid, shape_obj))
        return out
    raise UnsupportedBoard('unsupported native graphic shape')


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
    if any(value < 0 for value in (
            settings.m_SolderMaskExpansion, settings.m_SolderMaskMinWidth,
            settings.m_SolderMaskToCopperClearance)):
        raise UnsupportedBoard('negative solder-mask rules are not represented')
    mask_rules_active = (settings.m_SolderMaskMinWidth != 0 or settings.m_SolderMaskToCopperClearance != 0)
    for drawing in board.GetDrawings():
        layer = drawing.GetLayer()
        if pcbnew.IsCopperLayer(layer):
            _check_graphic_drawing(drawing, context_label='board copper')
        elif layer in (pcbnew.F_Mask, pcbnew.B_Mask) and (mask_rules_active or isinstance(drawing, pcbnew.PCB_SHAPE)):
            _check_graphic_drawing(drawing, context_label='board solder-mask')
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
            layer = drawing.GetLayer()
            if layer == pcbnew.Margin:
                raise UnsupportedBoard('footprint Margin geometry is not represented')
            if pcbnew.IsCopperLayer(layer):
                _check_graphic_drawing(drawing, context_label='footprint copper')
            elif layer in (pcbnew.F_Mask, pcbnew.B_Mask) and (mask_rules_active or isinstance(drawing, pcbnew.PCB_SHAPE)):
                if not footprint.AllowSolderMaskBridges() and not settings.m_AllowSoldermaskBridgesInFPs:
                    _check_graphic_drawing(drawing, context_label='footprint solder-mask')
        for pad in footprint.Pads():
            pad_shape(pad)
            if pad.GetAttribute() in (pcbnew.PAD_ATTRIB_PTH, pcbnew.PAD_ATTRIB_NPTH):
                _extract_drill_shape(pad)
            if pad.GetLocalClearance() is not None:
                raise UnsupportedBoard('pad clearance overrides are not represented')
            if pad.GetLocalSolderMaskMargin() is not None:
                raise UnsupportedBoard('pad solder-mask margins are not represented')
    for track in board.GetTracks():
        if type(track) is pcbnew.PCB_TRACK and track.GetLayer() == pcbnew.F_Cu:
            continue
        if type(track) is pcbnew.PCB_ARC and track.GetLayer() in (pcbnew.F_Cu, pcbnew.B_Cu):
            continue
        raise UnsupportedBoard('vias and non-front tracks are not supported')
    _board_bounds_and_outline(board)
    return board


def _try_rectangular_bounds(board):
    for fp in board.GetFootprints():
        if any(d.GetLayer() == pcbnew.Edge_Cuts for d in fp.GraphicalItems()):
            return None
    edges = []
    for drawing in board.GetDrawings():
        if drawing.GetLayer() == pcbnew.Edge_Cuts:
            if not isinstance(drawing, pcbnew.PCB_SHAPE) or drawing.GetShape() != pcbnew.SHAPE_T_SEGMENT:
                return None
            a, b = tuple(point(drawing.GetStart())), tuple(point(drawing.GetEnd()))
            edges.append(frozenset((a, b)))
    if len(edges) != 4:
        return None
    vertices = set().union(*edges)
    if len(vertices) != 4:
        return None
    xs, ys = sorted({p[0] for p in vertices}), sorted({p[1] for p in vertices})
    if len(xs) != 2 or len(ys) != 2:
        return None
    corners = [(xs[0], ys[0]), (xs[1], ys[0]), (xs[1], ys[1]), (xs[0], ys[1])]
    expected = {frozenset((corners[i], corners[(i + 1) % 4])) for i in range(4)}
    if set(edges) != expected:
        return None
    return {'min': list(corners[0]), 'max': list(corners[2])}


def rectangular_bounds(board):
    bounds = _try_rectangular_bounds(board)
    if bounds is not None:
        return bounds
    raise UnsupportedBoard('the board outline must be a single axis-aligned rectangle')


def _board_bounds_and_outline(board):
    rect = _try_rectangular_bounds(board)
    if rect is not None:
        return rect, None
    outlines = pcbnew.SHAPE_POLY_SET()
    if not board.GetBoardPolygonOutlines(outlines, False) or outlines.OutlineCount() == 0:
        raise UnsupportedBoard('board outline is missing, open, or invalid')
    polygons = []
    min_x = min_y = 10**12
    max_x = max_y = -10**12
    for i in range(outlines.OutlineCount()):
        outer = _extract_poly_ring(outlines.Outline(i))
        if len(outer) < 3:
            raise UnsupportedBoard('board outline polygon is degenerate')
        for x, y in outer:
            min_x, max_x = min(min_x, x), max(max_x, x)
            min_y, max_y = min(min_y, y), max(max_y, y)
        holes = []
        for h in range(outlines.HoleCount(i)):
            hole = _extract_poly_ring(outlines.Hole(i, h))
            if len(hole) < 3:
                raise UnsupportedBoard('board outline hole polygon is degenerate')
            holes.append(hole)
        poly_obj = {'outer': outer}
        if holes:
            poly_obj['holes'] = holes
        polygons.append(poly_obj)
    return {'min': [min_x, min_y], 'max': [max_x, max_y]}, {'polygons': polygons}


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
    required_keys = {'width_nm', 'clearance_nm', 'grid_nm', 'edge_clearance_nm'}
    optional_rule_keys = {
        'hole_clearance_nm', 'same_net_hole_clearance_nm',
        'solder_mask_expansion_nm', 'solder_mask_min_width_nm',
        'solder_mask_to_copper_clearance_nm',
    }
    if not isinstance(rules, dict) or not required_keys.issubset(rules) or not set(rules).issubset(required_keys | optional_rule_keys):
        raise ValueError('all four explicit routing rules are required')
    if any(type(rules[k]) is not int or rules[k] <= 0 for k in required_keys):
        raise ValueError('routing rules must be positive integer nanometres')
    if any(type(rules[k]) is not int or rules[k] < 0 for k in set(rules) - required_keys):
        raise ValueError('optional routing rules must be nonnegative integer nanometres')
    for key, floor in minima.items():
        if rules[key] < floor:
            raise ValueError(f'{key}={rules[key]} is below native floor {floor}')
    if type(max_expansions) is not int or max_expansions <= 0:
        raise ValueError('max_expansions must be positive')
    bounds, board_outline = _board_bounds_and_outline(board)
    pads = []
    holes = []
    for footprint in board.GetFootprints():
        for pad in footprint.Pads():
            if pad.GetAttribute() == pcbnew.PAD_ATTRIB_NPTH:
                drill = _extract_drill_shape(pad)
                holes.append({'id': item_id(pad), 'net': 0,
                              'at': point(pad.GetPosition()), 'shape': drill})
                continue
            shape = pad_shape(pad)
            entry = {'id': item_id(pad), 'net': int(pad.GetNetCode()),
                     'at': point(pad.GetPosition()), 'shape': shape}
            if pad.GetAttribute() == pcbnew.PAD_ATTRIB_PTH:
                entry['drill'] = _extract_drill_shape(pad)
            pads.append(entry)
    pads.sort(key=lambda item: (item['net'], item['at'], item['id']))
    holes.sort(key=lambda item: (item['at'], item['id']))

    tracks = []
    graphics = []
    for track in board.GetTracks():
        if type(track) is pcbnew.PCB_TRACK and track.GetLayer() == pcbnew.F_Cu:
            tracks.append({'id': item_id(track), 'net': int(track.GetNetCode()),
                           'start': point(track.GetStart()), 'end': point(track.GetEnd()),
                           'width_nm': int(track.GetWidth()), 'locked': bool(track.IsLocked())})
        elif type(track) is pcbnew.PCB_ARC and track.GetLayer() in (pcbnew.F_Cu, pcbnew.B_Cu):
            layer_name = 'F.Cu' if track.GetLayer() == pcbnew.F_Cu else 'B.Cu'
            graphics.append({
                'id': item_id(track),
                'net': int(track.GetNetCode()),
                'layer': layer_name,
                'shape': {
                    'kind': 'arc',
                    'start': point(track.GetStart()),
                    'mid': point(track.GetMid()),
                    'end': point(track.GetEnd()),
                    'width_nm': int(track.GetWidth()),
                },
            })
        else:
            raise UnsupportedBoard('vias and non-front tracks are not supported')
    tracks.sort(key=lambda item: (item['net'], item['start'], item['end'],
                                  item['width_nm'], item['locked'], item['id']))

    mask_rules_from_json = (
        rules.get('solder_mask_to_copper_clearance_nm', 0) > 0
        or rules.get('solder_mask_min_width_nm', 0) > 0
    )
    mask_apertures = []
    for drawing in board.GetDrawings():
        layer = drawing.GetLayer()
        if pcbnew.IsCopperLayer(layer):
            layer_name = 'F.Cu' if layer == pcbnew.F_Cu else 'B.Cu'
            net = int(drawing.GetNetCode()) if hasattr(drawing, 'GetNetCode') else 0
            for item_uuid, shape_obj in _extract_graphic_shapes(drawing, base_id=item_id(drawing)):
                graphics.append({'id': item_uuid, 'net': max(0, net), 'layer': layer_name, 'shape': shape_obj})
        elif layer in (pcbnew.F_Mask, pcbnew.B_Mask):
            if isinstance(drawing, pcbnew.PCB_SHAPE):
                layer_name = 'F.Mask' if layer == pcbnew.F_Mask else 'B.Mask'
                for item_uuid, shape_obj in _extract_graphic_shapes(drawing, base_id=item_id(drawing)):
                    mask_apertures.append({'id': item_uuid, 'layer': layer_name, 'shape': shape_obj})
            elif mask_rules_from_json:
                _check_graphic_drawing(drawing, context_label='board solder-mask')

    for footprint in board.GetFootprints():
        fp_nets = {int(p.GetNetCode()) for p in footprint.Pads() if int(p.GetNetCode()) != 0}
        fp_mask_net = next(iter(fp_nets)) if len(fp_nets) == 1 else None
        for drawing in footprint.GraphicalItems():
            layer = drawing.GetLayer()
            if pcbnew.IsCopperLayer(layer):
                layer_name = 'F.Cu' if layer == pcbnew.F_Cu else 'B.Cu'
                net = int(drawing.GetNetCode()) if hasattr(drawing, 'GetNetCode') else 0
                for item_uuid, shape_obj in _extract_graphic_shapes(drawing, base_id=item_id(drawing)):
                    graphics.append({'id': item_uuid, 'net': max(0, net), 'layer': layer_name, 'shape': shape_obj})
            elif layer in (pcbnew.F_Mask, pcbnew.B_Mask):
                if not footprint.AllowSolderMaskBridges() and not settings.m_AllowSoldermaskBridgesInFPs:
                    if isinstance(drawing, pcbnew.PCB_SHAPE):
                        layer_name = 'F.Mask' if layer == pcbnew.F_Mask else 'B.Mask'
                        for item_uuid, shape_obj in _extract_graphic_shapes(drawing, base_id=item_id(drawing)):
                            aperture = {'id': item_uuid, 'layer': layer_name, 'shape': shape_obj}
                            if fp_mask_net is not None:
                                aperture['net'] = fp_mask_net
                            mask_apertures.append(aperture)
                    elif mask_rules_from_json:
                        _check_graphic_drawing(drawing, context_label='footprint solder-mask')

    graphics.sort(key=lambda item: (item['layer'], item['net'], item['id']))
    mask_apertures.sort(key=lambda item: (item['layer'], item['id']))

    out_rules = dict(rules)
    has_drills = bool(holes) or any('drill' in p for p in pads)
    if has_drills:
        out_rules['hole_clearance_nm'] = max(out_rules.get('hole_clearance_nm', 0), int(settings.m_HoleClearance))
    if settings.m_SolderMaskExpansion > 0:
        out_rules['solder_mask_expansion_nm'] = max(out_rules.get('solder_mask_expansion_nm', 0), int(settings.m_SolderMaskExpansion))
    if settings.m_SolderMaskMinWidth > 0:
        out_rules['solder_mask_min_width_nm'] = max(out_rules.get('solder_mask_min_width_nm', 0), int(settings.m_SolderMaskMinWidth))
    if settings.m_SolderMaskToCopperClearance > 0:
        out_rules['solder_mask_to_copper_clearance_nm'] = max(out_rules.get('solder_mask_to_copper_clearance_nm', 0), int(settings.m_SolderMaskToCopperClearance))

    nets = [{'id': int(code), 'name': net.GetNetname()}
            for code, net in board.GetNetsByNetcode().items()]
    nets.sort(key=lambda item: (item['id'], item['name']))
    result = {'version': INPUT_VERSION, 'source_id': hashlib.sha256(read_bounded(path)).hexdigest(),
              'layer': 'F.Cu', 'bounds': bounds, 'rules': out_rules,
              'nets': nets, 'pads': pads, 'tracks': tracks, 'max_expansions': max_expansions}
    if holes:
        result['holes'] = holes
    if board_outline is not None:
        result['board_outline'] = board_outline
    if graphics:
        result['graphics'] = graphics
    if mask_apertures:
        result['mask_apertures'] = mask_apertures
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
    used_ids |= {item_id(x) for fp in board.GetFootprints() for x in fp.GraphicalItems()}
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
