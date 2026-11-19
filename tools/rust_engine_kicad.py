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
import fnmatch
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
ENGINE_MAX_NM = 100_000_000
# Per-document admission, not a bound on parsed/native allocations or runtime.
MAX_DOCUMENT_BYTES = 16 * 1024 * 1024
MAX_SYMLINK_HOPS = 40
_ORIGINAL_SUBPROCESS_RUN = subprocess.run


def read_bounded(path: Path):
    flags = os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0)
    fd = os.open(path, flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError(f'input document must be a regular file: {path}')
        chunks = []
        total = 0
        while total <= MAX_DOCUMENT_BYTES:
            chunk = os.read(fd, min(65536, MAX_DOCUMENT_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        body = b''.join(chunks)
    finally:
        os.close(fd)
    if len(body) > MAX_DOCUMENT_BYTES:
        raise ValueError(f'input document exceeds {MAX_DOCUMENT_BYTES} bytes: {path}')
    return body


class UnsupportedBoard(ValueError):
    pass


class NativeValidationError(ValueError):
    pass


CONSOLE_ERROR_RE = re.compile(
    r'(?im)^(?:\d{2}:\d{2}:\d{2}:\s*)?(?:fatal\s+)?error\b'
)
CRITICAL_PHYSICAL_DRC_CHECKS = frozenset({
    'shorting_items',
    'clearance',
    'copper_edge_clearance',
    'copper_sliver',
    'hole_clearance',
    'holes_co_located',
    'hole_to_hole',
    'track_width',
    'via_diameter',
    'microvia_drill_out_of_range',
    'annular_width',
    'drill_out_of_range',
    'tracks_crossing',
    'zones_intersect',
    'items_not_allowed',
    'isolated_copper',
    'starved_thermal',
    'unconnected_items',
    'connection_width',
    'track_dangling',
    'via_dangling',
})


def native_violations(path: Path, report: Path, *, timeout: float = 30.0,
                      refill_zones: bool = False, save_board: bool = False):
    if not math.isfinite(timeout) or timeout <= 0:
        raise NativeValidationError('native DRC timeout must be positive and finite')
    if save_board and not refill_zones:
        raise ValueError('save_board requires refill_zones=True')
    command = ['kicad-cli', 'pcb', 'drc', '--format', 'json', '--severity-all', '--all-track-errors', '--units', 'mm']
    if refill_zones:
        command.append('--refill-zones')
    if save_board:
        command.append('--save-board')
    command.extend(['--output', str(report), str(path)])
    try:
        stdout = ''
        stderr = ''
        if subprocess.run is not _ORIGINAL_SUBPROCESS_RUN:
            completed = subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout)
            stdout = getattr(completed, 'stdout', '') or ''
            stderr = getattr(completed, 'stderr', '') or ''
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
        console = f'{stdout}\n{stderr}'.strip()
        if console and CONSOLE_ERROR_RE.search(console):
            raise NativeValidationError(f'native DRC emitted console error: {console}')
        data = json.loads(read_bounded(report))
        if not isinstance(data, dict) or not isinstance(data.get('violations'), list) or not isinstance(data.get('unconnected_items'), list):
            raise ValueError('native DRC report has invalid violation/connectivity fields')
        if (subprocess.run is _ORIGINAL_SUBPROCESS_RUN or 'coordinate_units' in data) and data.get('coordinate_units') != 'mm':
            raise NativeValidationError(f"native DRC report unexpected coordinate_units: {data.get('coordinate_units')!r}")
        compile_errs = [v for v in data['violations'] if isinstance(v, dict) and v.get('type') == 'rule_compile_error']
        if compile_errs:
            raise NativeValidationError(f'native DRC reported rule_compile_error: {compile_errs[0].get("description", compile_errs)}')
        ignored_raw = data.get('ignored_checks')
        if isinstance(ignored_raw, list):
            ignored_critical = [
                entry.get('key')
                for entry in ignored_raw
                if isinstance(entry, dict) and entry.get('key') in CRITICAL_PHYSICAL_DRC_CHECKS
            ]
            if ignored_critical:
                raise NativeValidationError(f'native DRC ignored required physical checks: {ignored_critical}')
        return data
    except NativeValidationError:
        raise
    except (OSError, ValueError, KeyError, RecursionError, subprocess.SubprocessError) as error:
        raise NativeValidationError(f'native DRC did not produce a valid result: {error}') from error


def point(value):
    return [int(value.x), int(value.y)]


def item_id(item):
    return item.m_Uuid.AsString()


def _is_canonical_board_alias(path: Path, resolved: Path) -> bool:
    if path == resolved:
        return True
    if path.name != resolved.name:
        return False
    parent = path.parent if path.parent != Path('') else Path('.')
    try:
        return Path(os.path.realpath(parent)) == resolved.parent
    except OSError:
        return False


def _check_sidecar_at(path: Path, resolved: Path):
    if not path.name:
        return
    is_canon = _is_canonical_board_alias(path, resolved)
    for suffix in ('.kicad_pro', '.kicad_dru'):
        sidecar = path.with_suffix(suffix)
        try:
            os.lstat(sidecar)
        except (FileNotFoundError, NotADirectoryError):
            continue
        if not is_canon:
            raise UnsupportedBoard(f'project/custom-rule ambiguous symlink-hop {suffix} sidecar shadows canonical board: {sidecar}')


def _check_pro_framing(raw: bytes):
    try:
        doc = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f'malformed .kicad_pro JSON: {exc}') from exc
    if not isinstance(doc, dict):
        raise ValueError('.kicad_pro root must be a JSON object')
    meta = doc.get('meta')
    if not isinstance(meta, dict) or type(meta.get('version')) is not int or meta['version'] not in (1, 2, 3):
        raise UnsupportedBoard('.kicad_pro requires meta.version in 1..=3')


def _check_dru_framing(raw: bytes):
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise ValueError(f'.kicad_dru is not valid UTF-8: {exc}') from exc
    depth = 0
    in_str = False
    esc = False
    in_comment = False
    for ch in text:
        if in_comment:
            if ch == '\n':
                in_comment = False
            continue
        if in_str:
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '#':
            in_comment = True
        elif ch == '"':
            in_str = True
        elif ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth < 0:
                raise ValueError('unbalanced parentheses in .kicad_dru')
    if in_str or depth != 0:
        raise ValueError('unterminated string or unbalanced parentheses in .kicad_dru')
    stripped = '\n'.join(l for l in text.splitlines() if not l.lstrip().startswith('#')).strip()
    if not re.match(r'\(\s*version\s+1\s*\)', stripped):
        raise UnsupportedBoard('.kicad_dru requires (version 1) header')


def resolve_sidecars(path: Path, *, project_path: Path | None = None) -> tuple[bytes | None, bytes | None]:
    resolved = Path(os.path.realpath(path))
    cur = path
    seen: set[Path] = set()
    for hop in range(MAX_SYMLINK_HOPS + 1):
        if cur not in seen:
            seen.add(cur)
            _check_sidecar_at(cur, resolved)
        parent = cur.parent if cur.parent != Path('') else Path('.')
        canon_parent: Path | None = None
        if cur.name:
            try:
                canon_parent = Path(os.path.realpath(parent))
                norm_cur = canon_parent / cur.name
                if norm_cur not in seen:
                    seen.add(norm_cur)
                    _check_sidecar_at(norm_cur, resolved)
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
        cur = target if target.is_absolute() else (canon_parent or parent) / target
    if resolved not in seen:
        _check_sidecar_at(resolved, resolved)

    if project_path is not None:
        if project_path.suffix != '.kicad_pro' or project_path.stem != path.stem:
            raise ValueError(f'--project {project_path} must be a .kicad_pro matching board stem {path.stem}')
        proj_parent_real = Path(os.path.realpath(project_path.parent if project_path.parent != Path('') else Path('.')))
        if proj_parent_real != resolved.parent:
            raise ValueError(f'--project {project_path} directory does not match board directory {resolved.parent}')
        if not os.path.exists(project_path):
            raise ValueError(f'--project file does not exist: {project_path}')

    pro_bytes = None
    dru_bytes = None
    for suffix in ('.kicad_pro', '.kicad_dru'):
        sidecar = resolved.with_suffix(suffix)
        try:
            st = os.lstat(sidecar)
        except (FileNotFoundError, NotADirectoryError):
            continue
        if stat.S_ISLNK(st.st_mode):
            try:
                st = os.stat(sidecar)
            except FileNotFoundError as exc:
                raise ValueError(f'dangling sidecar symlink: {sidecar}') from exc
        if not stat.S_ISREG(st.st_mode):
            raise ValueError(f'sidecar must be a regular file: {sidecar}')
        try:
            raw = read_bounded(sidecar)
        except OSError as exc:
            raise ValueError(f'cannot read sidecar {sidecar}: {exc}') from exc
        if suffix == '.kicad_pro':
            _check_pro_framing(raw)
            pro_bytes = raw
        else:
            _check_dru_framing(raw)
            dru_bytes = raw
    return pro_bytes, dru_bytes


def reject_external_rules(path: Path):
    return resolve_sidecars(path)


@contextmanager
def source_snapshot(path: Path, *, project_path: Path | None = None):
    pro_before, dru_before = resolve_sidecars(path, project_path=project_path)
    body = read_bounded(path)
    pro_after, dru_after = resolve_sidecars(path, project_path=project_path)
    if pro_before != pro_after or dru_before != dru_after:
        raise ValueError('sidecar changed during snapshot admission')
    with tempfile.TemporaryDirectory(prefix='kicad-route-source-') as directory:
        snapshot = Path(directory) / 'source.kicad_pcb'
        snapshot.write_bytes(body)
        if pro_after is not None:
            snapshot.with_suffix('.kicad_pro').write_bytes(pro_after)
        if dru_after is not None:
            snapshot.with_suffix('.kicad_dru').write_bytes(dru_after)
        yield snapshot


def _cu_stack(board):
    copper = list(board.GetEnabledLayers().CuStack())
    if not (2 <= len(copper) <= 32):
        raise UnsupportedBoard('board copper stackup must have 2 to 32 layers')
    if copper[0] != pcbnew.F_Cu or copper[-1] != pcbnew.B_Cu:
        raise UnsupportedBoard('board copper stackup must start at F.Cu and end at B.Cu')
    valid_inner = {getattr(pcbnew, f'In{i}_Cu') for i in range(1, 31) if hasattr(pcbnew, f'In{i}_Cu')}
    for lid in copper[1:-1]:
        if lid not in valid_inner:
            raise UnsupportedBoard('unsupported inner copper layer in stackup')
    return copper, [pcbnew.LayerName(lid) for lid in copper]


def _extract_drill_shape(pad, cu_layers=None):
    for getter in (pad.GetSecondaryDrillSize, pad.GetTertiaryDrillSize):
        if point(getter()) != [0, 0]:
            raise UnsupportedBoard('secondary or tertiary pad drills are not represented')
    check_layers = cu_layers if cu_layers is not None else (pcbnew.F_Cu, pcbnew.B_Cu)
    if hasattr(pad, 'IsBackdrilledOrPostMachined'):
        if any(pad.IsBackdrilledOrPostMachined(layer) for layer in check_layers):
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


def _is_collinear_ring(pts, *, check_area=True):
    n = len(pts)
    if n < 3:
        return True
    a, b = pts[0], pts[1]
    if all(_cross(a, b, pts[i]) == 0 for i in range(2, n)):
        return True
    if not check_area:
        return False
    area2 = sum(
        pts[i][0] * pts[(i + 1) % n][1] - pts[(i + 1) % n][0] * pts[i][1]
        for i in range(n)
    )
    return area2 == 0


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


def _decode_effective_pad_shape(pad, radius_hint=None, *, layer_id=pcbnew.F_Cu):
    raw_effective = pad.GetEffectiveShape(layer_id)
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


def pad_shape(pad, cu_layers=None):
    """Decode the pad shape, returning None for standalone stencil apertures."""
    raw_copper = [layer for layer in pad.GetLayerSet().Seq() if pcbnew.IsCopperLayer(layer)]
    attrib = pad.GetAttribute()
    if cu_layers is not None:
        cu_set = set(cu_layers)
        if attrib == pcbnew.PAD_ATTRIB_PTH:
            copper = [layer for layer in raw_copper if layer in cu_set]
        elif attrib == pcbnew.PAD_ATTRIB_NPTH:
            copper = []
        else:
            if any(layer not in cu_set for layer in raw_copper):
                raise UnsupportedBoard('pad references a disabled copper layer')
            copper = raw_copper
    else:
        copper = [] if attrib == pcbnew.PAD_ATTRIB_NPTH else raw_copper
    layer_set = pad.GetLayerSet()
    has_f_mask = layer_set.Contains(pcbnew.F_Mask)
    has_b_mask = layer_set.Contains(pcbnew.B_Mask)
    if attrib == pcbnew.PAD_ATTRIB_SMD:
        if any(point(getter()) != [0, 0] for getter in (
                pad.GetDrillSize, pad.GetSecondaryDrillSize, pad.GetTertiaryDrillSize)):
            raise UnsupportedBoard('drilled pads are not represented')
        if not copper and not pad.GetNumber() and pad.GetNetCode() == 0:
            layers = set(layer_set.Seq())
            if layers and layers <= {pcbnew.F_Paste, pcbnew.B_Paste}:
                return None
        if len(copper) != 1:
            raise UnsupportedBoard('SMD pads must sit on exactly one copper layer')
        cu_layer = copper[0]
        if (has_f_mask and cu_layer != pcbnew.F_Cu) or (has_b_mask and cu_layer != pcbnew.B_Cu):
            raise UnsupportedBoard('SMD pad solder-mask layer must match its copper layer')
    elif attrib == pcbnew.PAD_ATTRIB_PTH:
        if not copper:
            raise UnsupportedBoard('through-hole pads must include at least one copper layer')
        if (has_f_mask and pcbnew.F_Cu not in copper) or (has_b_mask and pcbnew.B_Cu not in copper):
            raise UnsupportedBoard('PTH pad solder-mask layer must match its copper layers')
        if hasattr(pad, 'GetPadstackMode') and hasattr(pcbnew, 'PAD_MODE_NORMAL'):
            if pad.GetPadstackMode() != pcbnew.PAD_MODE_NORMAL:
                raise UnsupportedBoard('non-uniform PTH padstack mode is not represented')
        if any(v <= 0 for v in point(pad.GetDrillSize())):
            raise ValueError('pad drill dimensions must be positive')
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

    return _decode_effective_pad_shape_checked(pad, copper, attrib, size, kind, radius, angle, is_ortho, is_even, offset)


def _pad_shape_on_layer(pad, layer_id, size, kind, radius, angle, is_ortho, is_even, offset):
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
            raw_effective = pad.GetEffectiveShape(layer_id)
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
        shape_pos = point(pad.ShapePos(layer_id))
        if size[0] % 2 == 0 and shape_pos == point(pad.GetPosition()):
            return {'kind': 'circle', 'radius_nm': size[0] // 2}
        return {'kind': 'box_exact', 'center_2x': [2 * shape_pos[0], 2 * shape_pos[1]],
                'half_size_2x': [size[0], size[0]], 'radius_2x': size[0]}

    if is_ortho and (kind == pcbnew.PAD_SHAPE_RECT or (kind == pcbnew.PAD_SHAPE_ROUNDRECT and radius <= min(size) // 2)):
        w, h = size[0], size[1]
        if angle == 90:
            w, h = h, w
        shape_pos = point(pad.ShapePos(layer_id))
        if kind == pcbnew.PAD_SHAPE_ROUNDRECT:
            raw_effective = pad.GetEffectiveShape(layer_id)
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

    return _decode_effective_pad_shape(pad, radius_hint=radius if kind == pcbnew.PAD_SHAPE_ROUNDRECT else None, layer_id=layer_id)


def _decode_effective_pad_shape_checked(pad, copper, attrib, size, kind, radius, angle, is_ortho, is_even, offset):
    ref_layer = copper[0] if copper else pcbnew.F_Cu
    shape = _pad_shape_on_layer(pad, ref_layer, size, kind, radius, angle, is_ortho, is_even, offset)
    if attrib == pcbnew.PAD_ATTRIB_PTH and len(copper) > 1:
        for extra_layer in copper[1:]:
            if _pad_shape_on_layer(pad, extra_layer, size, kind, radius, angle, is_ortho, is_even, offset) != shape:
                raise UnsupportedBoard('non-uniform PTH padstack across copper layers is not represented')
    return shape


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


def _extract_text_shapes(drawing, *, layer, base_id, context_label):
    if drawing.IsKnockout():
        raise UnsupportedBoard(f'{context_label} knockout text graphics are not represented')
    eff = drawing.GetEffectiveShape(layer)
    if eff is None or eff.Type() == pcbnew.SH_NULL:
        return []
    if eff.Type() == pcbnew.SH_COMPOUND:
        comp = pcbnew.Cast_to_SHAPE_COMPOUND(eff)
        if comp is None:
            raise UnsupportedBoard(f'{context_label} non-shape graphics are not represented')
        subshapes = list(comp.GetSubshapes())
    else:
        subshapes = [eff]
    shapes = []
    for sub in subshapes:
        stype = sub.Type()
        if stype == pcbnew.SH_SEGMENT:
            seg_shape = pcbnew.Cast_to_SHAPE_SEGMENT(sub)
            if seg_shape is None:
                raise UnsupportedBoard(f'{context_label} non-shape graphics are not represented')
            seg = seg_shape.GetSeg()
            a, b = point(seg.A), point(seg.B)
            width_nm = int(seg_shape.GetWidth())
            if width_nm < 0:
                raise UnsupportedBoard(f'{context_label} negative-width text stroke is not represented')
            if a == b:
                if width_nm >= 2:
                    shapes.append({'kind': 'circle', 'center': a, 'radius_nm': width_nm // 2,
                                   'width_nm': 0, 'filled': True})
                continue
            shapes.append({'kind': 'segment', 'start': a, 'end': b, 'width_nm': width_nm})
        elif stype == pcbnew.SH_RECT:
            rect = pcbnew.Cast_to_SHAPE_RECT(sub)
            if rect is None:
                raise UnsupportedBoard(f'{context_label} non-shape graphics are not represented')
            pos = point(rect.GetPosition())
            sz = point(rect.GetSize())
            width_nm = max(0, int(rect.GetWidth()))
            filled = bool(rect.IsSolid())
            if sz[0] <= 0 or sz[1] <= 0:
                raise UnsupportedBoard(f'{context_label} degenerate text rectangle is not represented')
            corners = [pos, [pos[0] + sz[0], pos[1]], [pos[0] + sz[0], pos[1] + sz[1]], [pos[0], pos[1] + sz[1]]]
            shapes.append({'kind': 'polygon', 'outer': corners, 'width_nm': width_nm, 'filled': filled})
        elif stype == pcbnew.SH_CIRCLE:
            circ = pcbnew.Cast_to_SHAPE_CIRCLE(sub)
            if circ is None:
                raise UnsupportedBoard(f'{context_label} non-shape graphics are not represented')
            radius_nm = int(circ.GetRadius())
            if radius_nm <= 0:
                raise UnsupportedBoard(f'{context_label} degenerate text circle is not represented')
            shapes.append({
                'kind': 'circle',
                'center': point(circ.GetCenter()),
                'radius_nm': radius_nm,
                'width_nm': max(0, int(circ.GetWidth())),
                'filled': bool(circ.IsSolid()),
            })
        elif stype == pcbnew.SH_POLY_SET:
            poly_set = pcbnew.Cast_to_SHAPE_POLY_SET(sub)
            if poly_set is None:
                raise UnsupportedBoard(f'{context_label} non-shape graphics are not represented')
            width_nm = max(0, int(poly_set.GetWidth()))
            filled = bool(poly_set.IsSolid())
            if filled:
                poly_set = pcbnew.SHAPE_POLY_SET(poly_set)
                poly_set.Simplify()
            count = poly_set.OutlineCount()
            if count <= 0:
                raise UnsupportedBoard('empty polygon graphic is not represented')
            for o_idx in range(count):
                outline_ring = poly_set.Outline(o_idx)
                if outline_ring.ArcCount() != 0:
                    raise UnsupportedBoard(f'{context_label} curved arc text polygons are not represented')
                outer = _extract_poly_ring(outline_ring)
                if len(outer) < 3 or _is_collinear_ring(outer, check_area=filled):
                    raise UnsupportedBoard('degenerate polygon graphic is not represented')
                holes = []
                for h in range(poly_set.HoleCount(o_idx)):
                    hole_ring = poly_set.Hole(o_idx, h)
                    if hole_ring.ArcCount() != 0:
                        raise UnsupportedBoard(f'{context_label} curved arc text holes are not represented')
                    hole = _extract_poly_ring(hole_ring)
                    if len(hole) < 3 or _is_collinear_ring(hole):
                        raise UnsupportedBoard('degenerate polygon hole graphic is not represented')
                    holes.append(hole)
                shape_obj = {'kind': 'polygon', 'outer': outer, 'width_nm': width_nm, 'filled': filled}
                if holes:
                    shape_obj['holes'] = holes
                shapes.append(shape_obj)
        else:
            raise UnsupportedBoard(f'{context_label} non-shape graphics are not represented')
    base_uuid = uuid.UUID(base_id)
    return [(str(uuid.uuid5(base_uuid, str(idx))), shape_obj) for idx, shape_obj in enumerate(shapes)]


def _extract_graphic_poly_rings(drawing, *, context_label):
    raw_set = drawing.GetPolyShape()
    for idx in range(raw_set.OutlineCount()):
        if raw_set.Outline(idx).ArcCount() != 0:
            raise UnsupportedBoard(f'{context_label} curved arc polygon graphics are not represented')
        for h in range(raw_set.HoleCount(idx)):
            if raw_set.Hole(idx, h).ArcCount() != 0:
                raise UnsupportedBoard(f'{context_label} curved arc polygon holes are not represented')
    poly_set = raw_set
    if bool(drawing.IsSolidFill()):
        poly_set = pcbnew.SHAPE_POLY_SET(raw_set)
        poly_set.Simplify()
    count = poly_set.OutlineCount()
    if count <= 0:
        raise UnsupportedBoard('empty polygon graphic is not represented')
    rings = []
    for idx in range(count):
        outline_ring = poly_set.Outline(idx)
        if outline_ring.ArcCount() != 0:
            raise UnsupportedBoard(f'{context_label} curved arc polygon graphics are not represented')
        outer = _extract_poly_ring(outline_ring)
        if len(outer) < 3 or _is_collinear_ring(outer, check_area=bool(drawing.IsSolidFill())):
            raise UnsupportedBoard('degenerate polygon graphic is not represented')
        holes = []
        for h in range(poly_set.HoleCount(idx)):
            hole_ring = poly_set.Hole(idx, h)
            if hole_ring.ArcCount() != 0:
                raise UnsupportedBoard(f'{context_label} curved arc polygon holes are not represented')
            hole = _extract_poly_ring(hole_ring)
            if len(hole) < 3 or _is_collinear_ring(hole):
                raise UnsupportedBoard('degenerate polygon hole graphic is not represented')
            holes.append(hole)
        rings.append((outer, holes))
    return rings


def _check_graphic_drawing(drawing, *, context_label, layer=None):
    if isinstance(drawing, pcbnew.PCB_DIMENSION_BASE):
        raise UnsupportedBoard(f'{context_label} non-shape graphics are not represented')
    if type(drawing) is pcbnew.PCB_TEXT:
        if drawing.IsKnockout():
            raise UnsupportedBoard(f'{context_label} knockout text graphics are not represented')
        _extract_text_shapes(drawing, layer=drawing.GetLayer() if layer is None else layer,
                             base_id=item_id(drawing), context_label=context_label)
        return
    if isinstance(drawing, pcbnew.PCB_TEXTBOX):
        raise UnsupportedBoard(f'{context_label} text box graphics are not represented')
    if not isinstance(drawing, pcbnew.PCB_SHAPE):
        raise UnsupportedBoard(f'{context_label} non-shape graphics are not represented')
    if drawing.GetLocalSolderMaskMargin() is not None:
        raise UnsupportedBoard(f'{context_label} solder-mask margins are not represented')
    effective_layer = drawing.GetLayer() if layer is None else layer
    if pcbnew.IsCopperLayer(effective_layer) and drawing.HasSolderMask():
        raise UnsupportedBoard(f'{context_label} solder-mask openings are not represented')
    if drawing.GetShape() == pcbnew.SHAPE_T_BEZIER:
        raise UnsupportedBoard(f'{context_label} Bezier curves are not represented')
    if drawing.IsHatchedFill():
        raise UnsupportedBoard(f'{context_label} hatched fill graphics are not represented')
    _extract_graphic_shapes(drawing, base_id=item_id(drawing), layer=effective_layer, context_label=context_label)


def _check_arc_points(start, mid, end, *, context_label='arc graphic'):
    if (
        start == mid
        or mid == end
        or start == end
        or (_cross(start, mid, end) == 0 and _dot(start, mid, end) <= 0)
    ):
        raise UnsupportedBoard(f'degenerate {context_label} is not represented')


def _extract_poly_ring(poly):
    pts = [[int(poly.CPoint(j).x), int(poly.CPoint(j).y)] for j in range(poly.PointCount())]
    return _simplify_ring(pts)


def _extract_graphic_shapes(drawing, *, base_id, layer=None, context_label='graphic'):
    if type(drawing) is pcbnew.PCB_TEXT:
        return _extract_text_shapes(drawing, layer=drawing.GetLayer() if layer is None else layer,
                                    base_id=base_id, context_label=context_label)
    if isinstance(drawing, pcbnew.PCB_TEXTBOX):
        raise UnsupportedBoard(f'{context_label} text box graphics are not represented')
    shape_t = drawing.GetShape()
    width_nm = int(drawing.GetWidth())
    if width_nm < 0:
        raise UnsupportedBoard(f'{context_label} negative-width graphic is not represented')
    filled = bool(drawing.IsSolidFill())
    if shape_t == pcbnew.SHAPE_T_SEGMENT:
        a, b = point(drawing.GetStart()), point(drawing.GetEnd())
        if a == b:
            return []
        return [(base_id, {'kind': 'segment', 'start': a, 'end': b, 'width_nm': width_nm})]
    if shape_t == pcbnew.SHAPE_T_ARC:
        start, mid, end = point(drawing.GetStart()), point(drawing.GetArcMid()), point(drawing.GetEnd())
        _check_arc_points(start, mid, end)
        return [(base_id, {
            'kind': 'arc',
            'start': start,
            'mid': mid,
            'end': end,
            'width_nm': width_nm,
        })]
    if shape_t == pcbnew.SHAPE_T_CIRCLE:
        center = point(drawing.GetCenter())
        end = point(drawing.GetEnd())
        radius_nm = int(drawing.GetRadius())
        if radius_nm <= 0 or center == end:
            raise UnsupportedBoard('degenerate circle graphic is not represented')
        return [(base_id, {
            'kind': 'circle',
            'center': center,
            'radius_nm': radius_nm,
            'width_nm': width_nm,
            'filled': filled,
        })]
    if shape_t == pcbnew.SHAPE_T_RECT:
        corners = _simplify_ring([point(p) for p in drawing.GetRectCorners()])
        if len(corners) < 3 or _is_collinear_ring(corners):
            raise UnsupportedBoard('degenerate rectangle graphic is not represented')
        area2 = sum(corners[i][0] * corners[(i + 1) % len(corners)][1]
                    - corners[(i + 1) % len(corners)][0] * corners[i][1]
                    for i in range(len(corners)))
        if area2 < 0:
            corners.reverse()
        return [(base_id, {'kind': 'polygon', 'outer': corners, 'width_nm': width_nm, 'filled': filled})]
    if shape_t == pcbnew.SHAPE_T_POLY:
        rings = _extract_graphic_poly_rings(drawing, context_label=context_label)
        count = len(rings)
        out = []
        for idx, (outer, holes) in enumerate(rings):
            shape_obj = {'kind': 'polygon', 'outer': outer, 'width_nm': width_nm, 'filled': filled}
            if holes:
                shape_obj['holes'] = holes
            item_uuid = base_id if count == 1 else str(uuid.uuid5(uuid.UUID(base_id), str(idx)))
            out.append((item_uuid, shape_obj))
        return out
    raise UnsupportedBoard('unsupported native graphic shape')


def _zone_connection_str(conn, *, allow_inherited: bool = False):
    mapping = {
        pcbnew.ZONE_CONNECTION_NONE: 'none',
        pcbnew.ZONE_CONNECTION_THERMAL: 'thermal',
        pcbnew.ZONE_CONNECTION_FULL: 'solid',
        pcbnew.ZONE_CONNECTION_THT_THERMAL: 'tht_thermal',
    }
    if allow_inherited:
        mapping[pcbnew.ZONE_CONNECTION_INHERITED] = 'inherited'
    if conn not in mapping:
        raise UnsupportedBoard(f'unsupported zone pad connection mode: {conn}')
    return mapping[conn]


def _island_removal_str(mode):
    mapping = {
        pcbnew.ISLAND_REMOVAL_MODE_ALWAYS: 'always',
        pcbnew.ISLAND_REMOVAL_MODE_NEVER: 'never',
        pcbnew.ISLAND_REMOVAL_MODE_AREA: 'area_limit',
    }
    if mode not in mapping:
        raise UnsupportedBoard(f'unsupported zone island removal mode: {mode}')
    return mapping[mode]


def _extract_zone_polygons(zone, *, label: str):
    outline = zone.Outline()
    if outline is None or outline.OutlineCount() < 1:
        raise UnsupportedBoard(f'{label} must have at least one outline polygon')
    polygons = []
    for idx in range(outline.OutlineCount()):
        ring = outline.Outline(idx)
        if ring.ArcCount() != 0:
            raise UnsupportedBoard(f'{label} curved arc outlines are not represented')
        if not ring.IsClosed():
            raise UnsupportedBoard(f'{label} open outline is not represented')
        outer = _extract_poly_ring(ring)
        if len(outer) < 3 or _is_collinear_ring(outer):
            raise UnsupportedBoard(f'{label} outline polygon is degenerate')
        holes = []
        for h in range(outline.HoleCount(idx)):
            hr = outline.Hole(idx, h)
            if hr.ArcCount() != 0:
                raise UnsupportedBoard(f'{label} curved arc holes are not represented')
            if not hr.IsClosed():
                raise UnsupportedBoard(f'{label} open hole is not represented')
            hole = _extract_poly_ring(hr)
            if len(hole) < 3 or _is_collinear_ring(hole):
                raise UnsupportedBoard(f'{label} hole polygon is degenerate')
            holes.append(hole)
        poly = {'outer': outer}
        if holes:
            poly['holes'] = holes
        polygons.append(poly)
    return polygons


def _extract_rule_area_polygon(zone):
    return _extract_zone_polygons(zone, label='rule area')[0]


def _all_board_zones(board):
    zones = list(board.Zones())
    for footprint in board.GetFootprints():
        zones.extend(footprint.Zones())
    return zones


def _board_has_teardrops(board, all_zones=None) -> bool:
    zones = _all_board_zones(board) if all_zones is None else all_zones
    if hasattr(board, 'LegacyTeardrops') and board.LegacyTeardrops():
        return True
    if any(z.IsTeardropArea() for z in zones):
        return True
    if any(pad.GetTeardropsEnabled() for pad in board.GetPads()):
        return True
    if any(
        isinstance(track, pcbnew.PCB_VIA) and track.GetTeardropsEnabled()
        for track in board.GetTracks()
    ):
        return True
    return False


def _validate_zone(zone, cu_set):
    raw_copper = [l for l in zone.GetLayerSet().Seq() if pcbnew.IsCopperLayer(l)]
    if any(l not in cu_set for l in raw_copper):
        raise UnsupportedBoard('zone or rule area references a disabled copper layer')
    if zone.GetIsRuleArea():
        if zone.GetPlacementAreaEnabled():
            raise UnsupportedBoard('placement rule areas are not represented')
        _extract_zone_polygons(zone, label='rule area')
        return
    if zone.IsTeardropArea():
        raise UnsupportedBoard('teardrop zones are not represented')
    if zone.GetFillMode() != pcbnew.ZONE_FILL_MODE_POLYGONS:
        raise UnsupportedBoard('hatched copper zones are not represented')
    if not raw_copper:
        raise UnsupportedBoard('copper zone has no active copper layers')
    _zone_connection_str(zone.GetPadConnection(), allow_inherited=False)
    _island_removal_str(zone.GetIslandRemovalMode())
    _extract_zone_polygons(zone, label='copper zone')


def _via_kind_str(via_type):
    if via_type == pcbnew.VIATYPE_THROUGH:
        return 'through'
    if via_type in (pcbnew.VIATYPE_BLIND, pcbnew.VIATYPE_BURIED):
        return 'blind_buried'
    if via_type == pcbnew.VIATYPE_MICROVIA:
        return 'micro'
    raise UnsupportedBoard('unsupported via type')


def _extract_via(via, cu_layers):
    kind = _via_kind_str(via.GetViaType())
    if hasattr(via, 'IsBackdrilledOrPostMachined') and any(via.IsBackdrilledOrPostMachined(l) for l in cu_layers):
        raise UnsupportedBoard('backdrilled or post-machined vias are not represented')
    if via.HasSolderMask():
        raise UnsupportedBoard('vias with explicit solder-mask openings are not represented')
    if via.GetLocalSolderMaskMargin() is not None:
        raise UnsupportedBoard('via solder-mask margins are not represented')
    top, bot = via.TopLayer(), via.BottomLayer()
    if top not in cu_layers or bot not in cu_layers:
        raise UnsupportedBoard('via references a disabled copper layer')
    i0, i1 = cu_layers.index(top), cu_layers.index(bot)
    lo, hi = min(i0, i1), max(i0, i1)
    span = cu_layers[lo:hi + 1]
    if (pcbnew.F_Cu in span and not via.IsTented(pcbnew.F_Mask)) or (
        pcbnew.B_Cu in span and not via.IsTented(pcbnew.B_Mask)
    ):
        raise UnsupportedBoard('untented vias are not represented')
    widths = {int(via.GetWidth(l)) for l in span}
    if len(widths) != 1:
        raise UnsupportedBoard('non-uniform per-layer via widths are not represented')
    diam = next(iter(widths))
    drill = int(via.GetDrillValue())
    if diam <= 0 or drill <= 0 or diam < drill:
        raise ValueError('via diameter and drill must be positive with diameter >= drill')
    return {
        'id': item_id(via),
        'net': int(via.GetNetCode()),
        'at': point(via.GetPosition()),
        'diameter_nm': diam,
        'drill_nm': drill,
        'start_layer': pcbnew.LayerName(cu_layers[lo]),
        'end_layer': pcbnew.LayerName(cu_layers[hi]),
        'kind': kind,
        'locked': bool(via.IsLocked()),
    }


def _extract_npth_mask_apertures(pad, settings):
    layer_set = pad.GetLayerSet()
    mask_layers = [layer for layer in (pcbnew.F_Mask, pcbnew.B_Mask) if layer_set.Contains(layer)]
    if not mask_layers:
        return []
    size = point(pad.GetSize())
    drill = point(pad.GetDrillSize())
    if (
        size == drill
        and settings.m_SolderMaskExpansion == 0
        and settings.m_SolderMaskToCopperClearance == 0
    ):
        return []
    if point(pad.GetOffset()) != [0, 0] or pad.GetOrientationDegrees() % 90 != 0:
        raise UnsupportedBoard('NPTH solder-mask apertures with offset or non-orthogonal rotation are not represented')
    kind = pad.GetShape()
    if kind not in (pcbnew.PAD_SHAPE_CIRCLE, pcbnew.PAD_SHAPE_RECT):
        raise UnsupportedBoard('NPTH solder-mask apertures with non-trivial shape are not represented')
    if kind == pcbnew.PAD_SHAPE_CIRCLE and (size[0] != size[1] or size[0] % 2 != 0):
        raise UnsupportedBoard('NPTH solder-mask apertures with non-trivial shape are not represented')
    if kind == pcbnew.PAD_SHAPE_RECT and (size[0] % 2 != 0 or size[1] % 2 != 0):
        raise UnsupportedBoard('NPTH solder-mask apertures with non-trivial shape are not represented')
    base_uuid = uuid.UUID(item_id(pad))
    cx, cy = point(pad.GetPosition())
    out = []
    for ml in mask_layers:
        lname = 'F.Mask' if ml == pcbnew.F_Mask else 'B.Mask'
        if kind == pcbnew.PAD_SHAPE_CIRCLE:
            shape_obj = {'kind': 'circle', 'center': [cx, cy], 'radius_nm': size[0] // 2, 'width_nm': 0, 'filled': True}
        else:
            hx, hy = (size[0] // 2, size[1] // 2) if pad.GetOrientationDegrees() % 180 == 0 else (size[1] // 2, size[0] // 2)
            shape_obj = {
                'kind': 'polygon',
                'outer': [[cx - hx, cy - hy], [cx + hx, cy - hy], [cx + hx, cy + hy], [cx - hx, cy + hy]],
                'width_nm': 0,
                'filled': True,
            }
        exp_nm = max(0, int(pad.GetSolderMaskExpansion(ml)))
        aperture = {'id': str(uuid.uuid5(base_uuid, lname)), 'layer': lname, 'shape': shape_obj}
        if exp_nm > 0:
            aperture['expansion_nm'] = exp_nm
        out.append(aperture)
    return out


def load_supported(path: Path, *, project_path: Path | None = None):
    resolve_sidecars(path, project_path=project_path)
    validate_native_pad_scalars(read_bounded(path))
    board = pcbnew.LoadBoard(str(path))
    if board is None:
        raise ValueError(f'KiCad could not load {path}')
    cu_layers, _ = _cu_stack(board)
    cu_set = set(cu_layers)
    for zone in board.Zones():
        _validate_zone(zone, cu_set)
    if any(drawing.GetLayer() == pcbnew.Margin for drawing in board.GetDrawings()):
        raise UnsupportedBoard('board Margin geometry is not represented')
    settings = board.GetDesignSettings()
    if any(value < 0 for value in (
            settings.m_SolderMaskExpansion, settings.m_SolderMaskMinWidth,
            settings.m_SolderMaskToCopperClearance)):
        raise UnsupportedBoard('negative solder-mask rules are not represented')
    if len(cu_layers) >= 2 and (not settings.m_TentViasFront or not settings.m_TentViasBack):
        raise UnsupportedBoard('untented vias are not represented')
    for drawing in board.GetDrawings():
        layer = drawing.GetLayer()
        if layer in cu_set:
            _check_graphic_drawing(drawing, context_label='board copper', layer=layer)
        elif pcbnew.IsCopperLayer(layer):
            raise UnsupportedBoard('board copper on disabled layer is not represented')
        elif layer in (pcbnew.F_Mask, pcbnew.B_Mask):
            _check_graphic_drawing(drawing, context_label='board solder-mask', layer=layer)
    for footprint in board.GetFootprints():
        fp_clr = footprint.GetLocalClearance()
        if fp_clr is not None and fp_clr < 0:
            raise UnsupportedBoard('negative footprint clearance overrides are not represented')
        if footprint.GetLocalSolderMaskMargin() is not None:
            raise UnsupportedBoard('footprint solder-mask margins are not represented')
        for zone in footprint.Zones():
            if not zone.GetIsRuleArea():
                raise UnsupportedBoard('footprint copper zones are not represented')
            _validate_zone(zone, cu_set)
        if footprint.IsNetTie():
            raise UnsupportedBoard('net-tie connectivity is not represented')
        for field in footprint.GetFields():
            field_layer = field.GetLayer()
            if pcbnew.IsCopperLayer(field_layer):
                raise UnsupportedBoard('footprint copper fields are not represented')
            if (
                field_layer in (pcbnew.F_Mask, pcbnew.B_Mask)
                and field.IsVisible()
                and not footprint.AllowSolderMaskBridges()
            ):
                raise UnsupportedBoard('footprint solder-mask fields are not represented')
        for drawing in footprint.GraphicalItems():
            layer = drawing.GetLayer()
            if layer == pcbnew.Margin:
                raise UnsupportedBoard('footprint Margin geometry is not represented')
            if layer in cu_set:
                _check_graphic_drawing(drawing, context_label='footprint copper', layer=layer)
            elif pcbnew.IsCopperLayer(layer):
                raise UnsupportedBoard('footprint copper on disabled layer is not represented')
            elif layer in (pcbnew.F_Mask, pcbnew.B_Mask):
                if not footprint.AllowSolderMaskBridges():
                    _check_graphic_drawing(drawing, context_label='footprint solder-mask', layer=layer)
        for pad in footprint.Pads():
            pad_shape(pad, cu_layers)
            if pad.GetAttribute() in (pcbnew.PAD_ATTRIB_PTH, pcbnew.PAD_ATTRIB_NPTH):
                _extract_drill_shape(pad, cu_layers)
            pad_clr = pad.GetLocalClearance()
            if pad_clr is not None and pad_clr < 0:
                raise UnsupportedBoard('negative pad clearance overrides are not represented')
            if pad.GetLocalSolderMaskMargin() is not None:
                raise UnsupportedBoard('pad solder-mask margins are not represented')
            if pad.GetAttribute() == pcbnew.PAD_ATTRIB_NPTH and not footprint.AllowSolderMaskBridges():
                _extract_npth_mask_apertures(pad, settings)
    for track in board.GetTracks():
        if type(track) is pcbnew.PCB_TRACK and track.GetLayer() in cu_set:
            continue
        if type(track) is pcbnew.PCB_ARC and track.GetLayer() in cu_set:
            _check_arc_points(point(track.GetStart()), point(track.GetMid()), point(track.GetEnd()),
                              context_label='copper arc')
            continue
        if type(track) is pcbnew.PCB_VIA:
            _extract_via(track, cu_layers)
            continue
        raise UnsupportedBoard('track or via references unsupported type or disabled layer')
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
        if len(outer) < 3 or _is_collinear_ring(outer):
            raise UnsupportedBoard('board outline polygon is degenerate')
        for x, y in outer:
            min_x, max_x = min(min_x, x), max(max_x, x)
            min_y, max_y = min(min_y, y), max(max_y, y)
        holes = []
        for h in range(outlines.HoleCount(i)):
            hole = _extract_poly_ring(outlines.Hole(i, h))
            if len(hole) < 3 or _is_collinear_ring(hole):
                raise UnsupportedBoard('board outline hole polygon is degenerate')
            holes.append(hole)
        poly_obj = {'outer': outer}
        if holes:
            poly_obj['holes'] = holes
        polygons.append(poly_obj)
    return {'min': [min_x, min_y], 'max': [max_x, max_y]}, {'polygons': polygons}


def export_board(path: Path, rules: dict | None = None, max_expansions: int = 100000,
                 limits: dict | None = None, *, original_source: Path | None = None,
                 project_path: Path | None = None):
    if original_source is not None:
        resolve_sidecars(original_source)
    with source_snapshot(path, project_path=project_path) as snapshot:
        return export_snapshot(snapshot, rules, max_expansions, limits)


def _unescape_sexpr_str(s: str) -> str:
    if '\\' not in s:
        return s
    out: list[str] = []
    i = 0
    n = len(s)
    escapes = {'n': '\n', 'r': '\r', 't': '\t', '\\': '\\', '"': '"'}
    while i < n:
        ch = s[i]
        if ch == '\\' and i + 1 < n:
            nxt = s[i + 1]
            if nxt in escapes:
                out.append(escapes[nxt])
                i += 2
                continue
        out.append(ch)
        i += 1
    return ''.join(out)


def _parse_pcb_stackup(body: bytes) -> list[dict]:
    """Extract (setup (stackup (layer ...))) entries from a .kicad_pcb S-expression."""
    text = body.decode('utf-8')
    tokens = re.finditer(r'\(|\)|"(?:\\.|[^"\\])*"|[^\s()"]+', text)
    stack: list[list] = []
    root = None
    for m in tokens:
        tok = m.group()
        if tok == '(':
            stack.append([])
        elif tok == ')':
            if not stack:
                break
            node = stack.pop()
            if stack:
                stack[-1].append(node)
            else:
                root = node
                break
        else:
            if tok.startswith('"') and tok.endswith('"') and len(tok) >= 2:
                tok = _unescape_sexpr_str(tok[1:-1])
            if stack:
                stack[-1].append(tok)
    if not isinstance(root, list):
        return []
    setup = next((c for c in root if isinstance(c, list) and c and c[0] == 'setup'), None)
    if setup is None:
        return []
    stackup_node = next((c for c in setup if isinstance(c, list) and c and c[0] == 'stackup'), None)
    if stackup_node is None:
        return []
    out = []
    for child in stackup_node[1:]:
        if not isinstance(child, list) or len(child) < 2 or child[0] != 'layer':
            continue
        name = str(child[1])
        kind = 'other'
        thickness_nm = 0
        epsilon_r = None
        material = None
        for sub in child[2:]:
            if not isinstance(sub, list) or len(sub) < 2:
                continue
            key, val = sub[0], sub[1]
            if key == 'type':
                t_low = str(val).lower()
                if t_low == 'copper':
                    kind = 'copper'
                elif t_low == 'core':
                    kind = 'core'
                elif t_low == 'prepreg':
                    kind = 'prepreg'
                elif 'mask' in t_low:
                    kind = 'solder_mask'
                else:
                    kind = 'other'
            elif key == 'thickness':
                try:
                    t_mm = float(val)
                    if math.isfinite(t_mm) and t_mm >= 0:
                        thickness_nm = int(round(t_mm * 1_000_000.0))
                except (TypeError, ValueError):
                    pass
            elif key == 'epsilon_r':
                try:
                    er = float(val)
                    if math.isfinite(er) and er > 0:
                        epsilon_r = er
                except (TypeError, ValueError):
                    pass
            elif key == 'material':
                material = str(val)
        entry = {'name': name, 'kind': kind, 'thickness_nm': thickness_nm}
        if epsilon_r is not None:
            entry['epsilon_r'] = epsilon_r
        if material is not None:
            entry['material'] = material
        out.append(entry)
    return out


def _visit_exported_shape_coords(shape: dict, fn_1x, fn_2x) -> None:
    kind = shape.get('kind')
    if kind == 'box_exact':
        fn_2x(shape['center_2x'])
    elif kind == 'capsule':
        fn_1x(shape['start_nm'])
        fn_1x(shape['end_nm'])
    elif kind == 'convex_polygon':
        for pt in shape['vertices']:
            fn_1x(pt)


def _visit_exported_poly_coords(poly: dict, fn_1x) -> None:
    for pt in poly['outer']:
        fn_1x(pt)
    for hole in poly.get('holes', ()):
        for pt in hole:
            fn_1x(pt)


def _visit_exported_graphic_coords(item: dict, fn_1x) -> None:
    shape = item['shape']
    kind = shape.get('kind')
    if kind == 'segment':
        fn_1x(shape['start'])
        fn_1x(shape['end'])
    elif kind == 'arc':
        fn_1x(shape['start'])
        fn_1x(shape['mid'])
        fn_1x(shape['end'])
    elif kind == 'circle':
        fn_1x(shape['center'])
    elif kind == 'polygon':
        _visit_exported_poly_coords(shape, fn_1x)


def _visit_exported_coords(result: dict, fn_1x, fn_2x) -> None:
    bounds = result['bounds']
    fn_1x(bounds['min'])
    fn_1x(bounds['max'])
    if 'board_outline' in result:
        for poly in result['board_outline']['polygons']:
            _visit_exported_poly_coords(poly, fn_1x)
    for hole in result.get('holes', ()):
        fn_1x(hole['at'])
        _visit_exported_shape_coords(hole['shape'], fn_1x, fn_2x)
    for pad in result.get('pads', ()):
        fn_1x(pad['at'])
        _visit_exported_shape_coords(pad['shape'], fn_1x, fn_2x)
        if 'drill' in pad:
            _visit_exported_shape_coords(pad['drill'], fn_1x, fn_2x)
    for track in result.get('tracks', ()):
        fn_1x(track['start'])
        fn_1x(track['end'])
    for via in result.get('vias', ()):
        fn_1x(via['at'])
    for ra in result.get('rule_areas', ()):
        _visit_exported_poly_coords(ra, fn_1x)
        for ep in ra.get('extra_polygons', ()):
            _visit_exported_poly_coords(ep, fn_1x)
    for z in result.get('zones', ()):
        _visit_exported_poly_coords(z, fn_1x)
        for ep in z.get('extra_polygons', ()):
            _visit_exported_poly_coords(ep, fn_1x)
        for lf in z.get('layer_fills', ()):
            for fp in lf['polygons']:
                _visit_exported_poly_coords(fp, fn_1x)
    for g in result.get('graphics', ()):
        _visit_exported_graphic_coords(g, fn_1x)
    for ma in result.get('mask_apertures', ()):
        _visit_exported_graphic_coords(ma, fn_1x)


def _center_exported_coordinates_if_needed(result: dict) -> None:
    min_2x = [None, None]
    max_2x = [None, None]

    def accum_1x(pt):
        for k in (0, 1):
            v2 = 2 * pt[k]
            min_2x[k] = v2 if min_2x[k] is None else min(min_2x[k], v2)
            max_2x[k] = v2 if max_2x[k] is None else max(max_2x[k], v2)

    def accum_2x(pt2):
        for k in (0, 1):
            v2 = pt2[k]
            min_2x[k] = v2 if min_2x[k] is None else min(min_2x[k], v2)
            max_2x[k] = v2 if max_2x[k] is None else max(max_2x[k], v2)

    _visit_exported_coords(result, accum_1x, accum_2x)
    limit_2x = 2 * ENGINE_MAX_NM
    shift = [0, 0]
    for k in (0, 1):
        lo, hi = min_2x[k], max_2x[k]
        if lo is None or hi is None:
            continue
        if lo >= -limit_2x and hi <= limit_2x:
            shift[k] = 0
        else:
            s_lo = (-limit_2x - lo + 1) // 2
            s_hi = (limit_2x - hi) // 2
            if s_lo > s_hi:
                raise UnsupportedBoard(
                    f'board coordinates span more than {2 * ENGINE_MAX_NM} nm on axis {k}'
                )
            shift[k] = (s_lo + s_hi) // 2
    if shift == [0, 0]:
        return

    def shift_1x(pt):
        pt[0] += shift[0]
        pt[1] += shift[1]

    def shift_2x(pt2):
        pt2[0] += 2 * shift[0]
        pt2[1] += 2 * shift[1]

    _visit_exported_coords(result, shift_1x, shift_2x)


def export_snapshot(path: Path, rules: dict | None = None, max_expansions: int = 100000,
                    limits: dict | None = None):
    if limits is not None and not isinstance(limits, dict):
        raise ValueError('limits must be an object')
    board = load_supported(path)
    cu_layers, layer_names = _cu_stack(board)
    cu_set = set(cu_layers)
    settings = board.GetDesignSettings()
    minima = {'width_nm': settings.m_TrackMinWidth,
              'clearance_nm': settings.m_MinClearance,
              'edge_clearance_nm': settings.m_CopperEdgeClearance}
    if rules is not None:
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
    all_zones = _all_board_zones(board)
    has_copper_zones = any(not z.GetIsRuleArea() and not z.IsTeardropArea() for z in all_zones)
    pads = []
    holes = []
    components = []
    for footprint in board.GetFootprints():
        fp_clr = footprint.GetLocalClearance()
        fp_zc = footprint.GetLocalZoneConnection()
        fp_ref = footprint.GetReference() or ''
        fp_pad_nums = []
        fp_net_ids = set()
        for pad in footprint.Pads():
            if pad.GetAttribute() == pcbnew.PAD_ATTRIB_NPTH:
                drill = _extract_drill_shape(pad, cu_layers)
                holes.append({'id': item_id(pad), 'net': 0,
                              'at': point(pad.GetPosition()), 'shape': drill})
                continue
            shape = pad_shape(pad, cu_layers)
            if shape is None:
                continue
            pad_layers = [pcbnew.LayerName(l) for l in pad.GetLayerSet().Seq() if l in cu_set]
            pad_net = int(pad.GetNetCode())
            entry = {'id': item_id(pad), 'net': pad_net,
                     'at': point(pad.GetPosition()), 'shape': shape}
            if pad_layers != ['F.Cu'] or len(layer_names) > 2:
                entry['layers'] = pad_layers
            pad_clr = pad.GetLocalClearance()
            eff_clr = int(pad_clr) if pad_clr is not None else (int(fp_clr) if fp_clr is not None else None)
            if eff_clr is not None and eff_clr > 0:
                entry['local_clearance_nm'] = eff_clr
            if pad.GetAttribute() == pcbnew.PAD_ATTRIB_PTH:
                entry['drill'] = _extract_drill_shape(pad, cu_layers)
            if has_copper_zones:
                zc = pad.GetLocalZoneConnection()
                if zc == pcbnew.ZONE_CONNECTION_INHERITED:
                    zc = fp_zc
                if zc != pcbnew.ZONE_CONNECTION_INHERITED:
                    entry['zone_connection'] = _zone_connection_str(zc, allow_inherited=False)
                tg = pad.GetLocalThermalGapOverride()
                if tg is not None:
                    entry['thermal_gap_nm'] = int(tg)
                ts = pad.GetLocalThermalSpokeWidthOverride()
                if ts is not None:
                    entry['thermal_spoke_width_nm'] = int(ts)
                entry['thermal_spoke_angle_mdeg'] = int(round(pad.GetThermalSpokeAngleDegrees() * 1000.0))
                orient_mdeg = int(round(pad.GetOrientationDegrees() * 1000.0))
                if orient_mdeg != 0:
                    entry['orientation_mdeg'] = orient_mdeg
            pad_num = pad.GetNumber() or ''
            if fp_ref:
                entry['footprint_ref'] = fp_ref
            if pad_num:
                entry['pad_number'] = pad_num
                fp_pad_nums.append(pad_num)
            if fp_ref and pad_num:
                entry['fromto_id'] = f'{fp_ref}-{pad_num}'
            if pad_net != 0:
                fp_net_ids.add(pad_net)
            pads.append(entry)
        if fp_ref:
            components.append({
                'ref_des': fp_ref,
                'dnp': bool(footprint.IsDNP()) if hasattr(footprint, 'IsDNP') else False,
                'exclude_from_bom': bool(footprint.IsExcludedFromBOM()) if hasattr(footprint, 'IsExcludedFromBOM') else False,
                'exclude_from_pos': bool(footprint.IsExcludedFromPosFiles()) if hasattr(footprint, 'IsExcludedFromPosFiles') else False,
                'pad_numbers': fp_pad_nums,
                'net_ids': sorted(fp_net_ids),
            })
    pads.sort(key=lambda item: (item['net'], item['at'], item['id']))
    holes.sort(key=lambda item: (item['at'], item['id']))
    components.sort(key=lambda item: (item['ref_des'], item['pad_numbers']))

    tracks = []
    vias = []
    graphics = []
    for track in board.GetTracks():
        if type(track) is pcbnew.PCB_TRACK and track.GetLayer() in cu_set:
            lname = pcbnew.LayerName(track.GetLayer())
            item = {'id': item_id(track), 'net': int(track.GetNetCode()),
                    'start': point(track.GetStart()), 'end': point(track.GetEnd()),
                    'width_nm': int(track.GetWidth()), 'locked': bool(track.IsLocked())}
            if lname != 'F.Cu':
                item['layer'] = lname
            tracks.append(item)
        elif type(track) is pcbnew.PCB_ARC and track.GetLayer() in cu_set:
            layer_name = pcbnew.LayerName(track.GetLayer())
            start, mid, end = point(track.GetStart()), point(track.GetMid()), point(track.GetEnd())
            _check_arc_points(start, mid, end, context_label='copper arc')
            graphics.append({
                'id': item_id(track),
                'net': int(track.GetNetCode()),
                'layer': layer_name,
                'shape': {
                    'kind': 'arc',
                    'start': start,
                    'mid': mid,
                    'end': end,
                    'width_nm': int(track.GetWidth()),
                },
            })
        elif type(track) is pcbnew.PCB_VIA:
            vias.append(_extract_via(track, cu_layers))
        else:
            raise UnsupportedBoard('track or via references unsupported type or disabled layer')
    tracks.sort(key=lambda item: (item.get('layer', 'F.Cu'), item['net'], item['start'], item['end'],
                                  item['width_nm'], item['locked'], item['id']))
    vias.sort(key=lambda item: (item['net'], item['at'], item['id']))

    if has_copper_zones:
        zone_vec = pcbnew.ZONES()
        for z in all_zones:
            zone_vec.append(z)
        pcbnew.ZONE_FILLER(board).Fill(zone_vec)

    rule_areas = []
    zones = []
    for zone in all_zones:
        zlayers = [pcbnew.LayerName(l) for l in cu_layers if zone.GetLayerSet().Contains(l)]
        if not zlayers:
            continue
        polys = _extract_zone_polygons(zone, label='rule area' if zone.GetIsRuleArea() else 'copper zone')
        if zone.GetIsRuleArea():
            ra = {
                'id': item_id(zone),
                'layers': zlayers,
                'outer': polys[0]['outer'],
                'no_tracks': bool(zone.GetDoNotAllowTracks()),
                'no_vias': bool(zone.GetDoNotAllowVias()),
            }
            zname = zone.GetZoneName()
            if zname:
                ra['name'] = zname
            if 'holes' in polys[0]:
                ra['holes'] = polys[0]['holes']
            if len(polys) > 1:
                ra['extra_polygons'] = polys[1:]
            if zone.GetDoNotAllowPads():
                ra['no_pads'] = True
            if zone.GetDoNotAllowFootprints():
                ra['no_footprints'] = True
            if zone.GetDoNotAllowZoneFills():
                ra['no_zone_fills'] = True
            rule_areas.append(ra)
        else:
            layer_fills = []
            for lid in cu_layers:
                if not zone.GetLayerSet().Contains(lid) or not zone.HasFilledPolysForLayer(lid):
                    continue
                fill = zone.GetFilledPolysList(lid)
                fpolys = []
                for idx in range(fill.OutlineCount()):
                    outer = _extract_poly_ring(fill.Outline(idx))
                    if len(outer) < 3 or _is_collinear_ring(outer):
                        continue
                    fholes = []
                    for h in range(fill.HoleCount(idx)):
                        hr = _extract_poly_ring(fill.Hole(idx, h))
                        if len(hr) >= 3 and not _is_collinear_ring(hr):
                            fholes.append(hr)
                    fpoly = {'outer': outer}
                    if fholes:
                        fpoly['holes'] = fholes
                    fpolys.append(fpoly)
                if fpolys:
                    layer_fills.append({'layer': pcbnew.LayerName(lid), 'polygons': fpolys})
            z_entry = {
                'id': item_id(zone),
                'net': int(zone.GetNetCode()),
                'layers': zlayers,
                'outer': polys[0]['outer'],
                'priority': int(zone.GetAssignedPriority()),
                'local_clearance_nm': max(0, int(zone.GetLocalClearance())),
                'min_thickness_nm': max(0, int(zone.GetMinThickness())),
                'connection': _zone_connection_str(zone.GetPadConnection(), allow_inherited=False),
                'thermal_gap_nm': max(0, int(zone.GetThermalReliefGap())),
                'thermal_spoke_width_nm': max(0, int(zone.GetThermalReliefSpokeWidth())),
                'island_removal': _island_removal_str(zone.GetIslandRemovalMode()),
                'island_area_min_nm2': max(0, int(zone.GetMinIslandArea())),
                'fill_mode': 'polygon',
                'filled': bool(zone.IsFilled()),
                'refillable': True,
                'locked': bool(zone.IsLocked()),
            }
            zname = zone.GetZoneName()
            if zname:
                z_entry['name'] = zname
            if 'holes' in polys[0]:
                z_entry['holes'] = polys[0]['holes']
            if len(polys) > 1:
                z_entry['extra_polygons'] = polys[1:]
            if layer_fills:
                z_entry['layer_fills'] = layer_fills
            zones.append(z_entry)
    rule_areas.sort(key=lambda item: item['id'])
    zones.sort(key=lambda item: (-item['priority'], item['net'], item['id']))

    mask_apertures = []
    for drawing in board.GetDrawings():
        layer = drawing.GetLayer()
        if layer in cu_set:
            layer_name = pcbnew.LayerName(layer)
            net = int(drawing.GetNetCode()) if hasattr(drawing, 'GetNetCode') else 0
            for item_uuid, shape_obj in _extract_graphic_shapes(drawing, base_id=item_id(drawing), layer=layer, context_label='board copper'):
                graphics.append({'id': item_uuid, 'net': max(0, net), 'layer': layer_name, 'shape': shape_obj})
        elif layer in (pcbnew.F_Mask, pcbnew.B_Mask):
            if isinstance(drawing, pcbnew.PCB_SHAPE) or type(drawing) is pcbnew.PCB_TEXT:
                layer_name = 'F.Mask' if layer == pcbnew.F_Mask else 'B.Mask'
                exp_nm = max(0, int(drawing.GetSolderMaskExpansion())) if isinstance(drawing, pcbnew.PCB_SHAPE) else 0
                base_id = item_id(drawing)
                extracted = _extract_graphic_shapes(drawing, base_id=base_id, layer=layer, context_label='board solder-mask')
                for item_uuid, shape_obj in extracted:
                    aperture = {'id': item_uuid, 'layer': layer_name, 'shape': shape_obj}
                    if len(extracted) > 1:
                        aperture['group_id'] = base_id
                    if exp_nm > 0:
                        aperture['expansion_nm'] = exp_nm
                    mask_apertures.append(aperture)
            else:
                _check_graphic_drawing(drawing, context_label='board solder-mask', layer=layer)

    for footprint in board.GetFootprints():
        fp_nets = {int(p.GetNetCode()) for p in footprint.Pads() if int(p.GetNetCode()) != 0}
        fp_mask_net = next(iter(fp_nets)) if len(fp_nets) == 1 else None
        if not footprint.AllowSolderMaskBridges():
            for pad in footprint.Pads():
                if pad.GetAttribute() == pcbnew.PAD_ATTRIB_NPTH:
                    mask_apertures.extend(_extract_npth_mask_apertures(pad, settings))
        for drawing in footprint.GraphicalItems():
            layer = drawing.GetLayer()
            if layer in cu_set:
                layer_name = pcbnew.LayerName(layer)
                net = int(drawing.GetNetCode()) if hasattr(drawing, 'GetNetCode') else 0
                for item_uuid, shape_obj in _extract_graphic_shapes(drawing, base_id=item_id(drawing), layer=layer, context_label='footprint copper'):
                    graphics.append({'id': item_uuid, 'net': max(0, net), 'layer': layer_name, 'shape': shape_obj})
            elif layer in (pcbnew.F_Mask, pcbnew.B_Mask):
                if not footprint.AllowSolderMaskBridges():
                    if isinstance(drawing, pcbnew.PCB_SHAPE) or type(drawing) is pcbnew.PCB_TEXT:
                        layer_name = 'F.Mask' if layer == pcbnew.F_Mask else 'B.Mask'
                        exp_nm = max(0, int(drawing.GetSolderMaskExpansion())) if isinstance(drawing, pcbnew.PCB_SHAPE) else 0
                        base_id = item_id(drawing)
                        extracted = _extract_graphic_shapes(drawing, base_id=base_id, layer=layer, context_label='footprint solder-mask')
                        for item_uuid, shape_obj in extracted:
                            aperture = {'id': item_uuid, 'layer': layer_name, 'shape': shape_obj}
                            if len(extracted) > 1:
                                aperture['group_id'] = base_id
                            if exp_nm > 0:
                                aperture['expansion_nm'] = exp_nm
                            if fp_mask_net is not None:
                                aperture['net'] = fp_mask_net
                            mask_apertures.append(aperture)
                    else:
                        _check_graphic_drawing(drawing, context_label='footprint solder-mask', layer=layer)

    graphics.sort(key=lambda item: (item['layer'], item['net'], item['id']))
    mask_apertures.sort(key=lambda item: (item['layer'], item['id']))

    nets = [{'id': int(code), 'name': net.GetNetname()}
            for code, net in board.GetNetsByNetcode().items()]
    nets.sort(key=lambda item: (item['id'], item['name']))
    source_bytes = read_bounded(path)
    stackup = _parse_pcb_stackup(source_bytes)
    result = {'version': INPUT_VERSION, 'source_id': hashlib.sha256(source_bytes).hexdigest(),
              'layer': 'F.Cu', 'layers': layer_names, 'bounds': bounds,
              'nets': nets, 'pads': pads, 'tracks': tracks, 'max_expansions': max_expansions}
    if rules is not None:
        out_rules = dict(rules)
        has_drills = bool(holes) or any('drill' in p for p in pads) or bool(vias)
        if has_drills:
            out_rules['hole_clearance_nm'] = max(out_rules.get('hole_clearance_nm', 0), int(settings.m_HoleClearance))
        if settings.m_SolderMaskExpansion > 0:
            out_rules['solder_mask_expansion_nm'] = max(out_rules.get('solder_mask_expansion_nm', 0), int(settings.m_SolderMaskExpansion))
        if settings.m_SolderMaskMinWidth > 0:
            out_rules['solder_mask_min_width_nm'] = max(out_rules.get('solder_mask_min_width_nm', 0), int(settings.m_SolderMaskMinWidth))
        if settings.m_SolderMaskToCopperClearance > 0:
            out_rules['solder_mask_to_copper_clearance_nm'] = max(out_rules.get('solder_mask_to_copper_clearance_nm', 0), int(settings.m_SolderMaskToCopperClearance))
        result['rules'] = out_rules

    bds = {
        'min_track_width_nm': int(settings.m_TrackMinWidth),
        'min_clearance_nm': int(settings.m_MinClearance),
        'min_edge_clearance_nm': int(settings.m_CopperEdgeClearance),
        'min_via_diameter_nm': int(settings.m_ViasMinSize),
        'min_via_annular_width_nm': int(settings.m_ViasMinAnnularWidth),
        'min_through_hole_diameter_nm': int(settings.m_MinThroughDrill),
        'min_hole_clearance_nm': int(settings.m_HoleClearance),
        'min_hole_to_hole_nm': int(settings.m_HoleToHoleMin),
        'min_microvia_diameter_nm': int(settings.m_MicroViasMinSize),
        'min_microvia_drill_nm': int(settings.m_MicroViasMinDrill),
        'solder_mask_expansion_nm': int(settings.m_SolderMaskExpansion),
        'solder_mask_min_width_nm': int(settings.m_SolderMaskMinWidth),
        'solder_mask_to_copper_clearance_nm': int(settings.m_SolderMaskToCopperClearance),
        'cap_vias': bool(getattr(settings, 'm_CapVias', False)),
        'fill_vias': bool(getattr(settings, 'm_FillVias', False)),
    }
    # Geometry uses KiCad's stable layer IDs. DRU selectors can also match
    # the display names stored in the board.
    aliases = {}
    for layer, canonical in zip(cu_layers, layer_names):
        display = board.GetLayerName(layer)
        if display != canonical:
            aliases[canonical] = display
    if aliases:
        bds['layer_aliases'] = aliases
    net_settings = getattr(settings, 'm_NetSettings', None)
    default_nc = net_settings.GetDefaultNetclass() if net_settings is not None else None
    if default_nc is not None:
        bds['default_clearance_nm'] = max(0, int(default_nc.GetClearance()))
        bds['default_track_width_nm'] = max(0, int(default_nc.GetTrackWidth()))
        bds['default_via_diameter_nm'] = max(0, int(default_nc.GetViaDiameter()))
        bds['default_via_drill_nm'] = max(0, int(default_nc.GetViaDrill()))
        bds['default_microvia_diameter_nm'] = max(0, int(default_nc.GetuViaDiameter()))
        bds['default_microvia_drill_nm'] = max(0, int(default_nc.GetuViaDrill()))
    if net_settings is not None:
        nc_items = []
        seen_nc = set()
        candidates = ([default_nc] if default_nc is not None else []) + [
            nc for _, nc in sorted(net_settings.GetNetclasses().items(), key=lambda kv: str(kv[0]))
        ]
        for nc in candidates:
            nc_name = nc.GetName()
            if not nc_name or nc_name in seen_nc:
                continue
            seen_nc.add(nc_name)
            tp = nc.GetTuningProfile() if hasattr(nc, 'GetTuningProfile') else ''
            nc_obj = {
                'name': nc_name,
                'priority': int(nc.GetPriority()) if hasattr(nc, 'GetPriority') else (2147483647 if nc_name == 'Default' else 0),
                'clearance_nm': max(0, int(nc.GetClearance())),
                'track_width_nm': max(0, int(nc.GetTrackWidth())),
                'via_diameter_nm': max(0, int(nc.GetViaDiameter())),
                'via_drill_nm': max(0, int(nc.GetViaDrill())),
                'microvia_diameter_nm': max(0, int(nc.GetuViaDiameter())),
                'microvia_drill_nm': max(0, int(nc.GetuViaDrill())),
                'diff_pair_width_nm': max(0, int(nc.GetDiffPairWidth())),
                'diff_pair_gap_nm': max(0, int(nc.GetDiffPairGap())),
                'diff_pair_via_gap_nm': max(0, int(nc.GetDiffPairViaGap())),
            }
            if tp:
                nc_obj['tuning_profile'] = tp
            nc_items.append(nc_obj)
        if nc_items:
            bds['netclasses'] = nc_items
        nc_assign = {}
        for code, net in board.GetNetsByNetcode().items():
            if code > 0:
                nc_name = net.GetNetClassName()
                if nc_name and nc_name != 'Default':
                    nc_assign[net.GetNetname()] = [nc_name]
        if nc_assign:
            bds['netclass_assignments'] = nc_assign
    if hasattr(settings, 'm_UseHeightForLengthCalcs'):
        bds['use_height_for_length_calcs'] = bool(settings.m_UseHeightForLengthCalcs)
    if hasattr(settings, 'GetBoardThickness'):
        bds['board_thickness_nm'] = max(0, int(settings.GetBoardThickness()))
    if hasattr(board, 'GetEnabledLayers'):
        enabled_layers = board.GetEnabledLayers()
        bds['outer_solder_mask_thickness_nm'] = (
            (10_000 if enabled_layers.Contains(pcbnew.F_Mask) else 0)
            + (10_000 if enabled_layers.Contains(pcbnew.B_Mask) else 0)
        )
    pro_bytes, dru_bytes = resolve_sidecars(path)
    pro_text = pro_bytes.decode('utf-8') if pro_bytes is not None else None
    pro_min_spokes = None
    pro_use_height = None
    if pro_text is not None:
        try:
            pro_doc = json.loads(pro_text)
            board_doc = pro_doc.get('board') if isinstance(pro_doc, dict) else None
            ds_doc = board_doc.get('design_settings', {}) if isinstance(board_doc, dict) else None
            if isinstance(ds_doc, dict):
                rules_doc = ds_doc.get('rules', {})
                if isinstance(rules_doc, dict):
                    if type(rules_doc.get('use_height_for_length_calcs')) is bool:
                        pro_use_height = rules_doc['use_height_for_length_calcs']
                    val = rules_doc.get('min_resolved_spokes')
                    if type(val) is int and val >= 0:
                        pro_min_spokes = val
        except (ValueError, AttributeError):
            pro_min_spokes = None
            pro_use_height = None
    if pro_use_height is not None:
        bds['use_height_for_length_calcs'] = pro_use_height
    min_resolved_spokes = pro_min_spokes if pro_min_spokes is not None else int(getattr(settings, 'm_MinResolvedSpokes', 2))
    bds['min_resolved_spokes'] = min_resolved_spokes
    bds['max_error_nm'] = max(0, int(getattr(settings, 'm_MaxError', 5000)))
    result['board_design_settings'] = bds
    if pro_text is not None:
        result['project_rules'] = pro_text
    if dru_bytes is not None:
        result['custom_rules'] = dru_bytes.decode('utf-8')
    if vias:
        result['vias'] = vias
    if rule_areas:
        result['rule_areas'] = rule_areas
    if zones:
        result['zones'] = zones
    if holes:
        result['holes'] = holes
    if board_outline is not None:
        result['board_outline'] = board_outline
    if graphics:
        result['graphics'] = graphics
    if mask_apertures:
        result['mask_apertures'] = mask_apertures
    if components:
        result['components'] = components
    if stackup:
        result['stackup'] = stackup
    _center_exported_coordinates_if_needed(result)
    # Resource policy and defaults belong to Rust. Preserve supplied limits
    # exactly; omitting them retains the existing input contract.
    if limits is not None:
        result['limits'] = limits
    return result


class _PourUnionFind:
    def __init__(self):
        self.parent = {}

    def find(self, a):
        self.parent.setdefault(a, a)
        while self.parent[a] != a:
            self.parent[a] = self.parent[self.parent[a]]
            a = self.parent[a]
        return a

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            if ra < rb:
                self.parent[rb] = ra
            else:
                self.parent[ra] = rb


def _pour_seg_dist2(p, a, b):
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
    cx, cy = ax + t * dx, ay + t * dy
    return (px - cx) ** 2 + (py - cy) ** 2


def _read_pro_min_resolved_spokes(board_path: Path) -> int | None:
    pro_path = board_path.with_suffix('.kicad_pro')
    if not pro_path.is_file():
        return None
    try:
        doc = json.loads(read_bounded(pro_path).decode('utf-8'))
        val = doc.get('board', {}).get('design_settings', {}).get('rules', {}).get('min_resolved_spokes')
        if type(val) is int and val >= 0:
            return val
    except (OSError, ValueError, AttributeError):
        return None
    return None


def _extract_pour_facts(board_path: Path) -> dict:
    board = pcbnew.LoadBoard(str(board_path))
    if board is None:
        raise NativeValidationError(f'KiCad could not reload board for pour facts: {board_path}')
    bds = board.GetDesignSettings()
    max_err = int(bds.m_MaxError)
    pro_min = _read_pro_min_resolved_spokes(board_path)
    min_resolved_spokes = pro_min if pro_min is not None else int(getattr(bds, 'm_MinResolvedSpokes', 2))
    lname = pcbnew.LayerName
    nm = 1_000_000
    edge_band = 20_000
    cell = 2 * nm
    leaf_vertices = 256
    leaf_size = nm // 2
    conn_names = {
        pcbnew.ZONE_CONNECTION_NONE: 'none',
        pcbnew.ZONE_CONNECTION_THERMAL: 'thermal',
        pcbnew.ZONE_CONNECTION_FULL: 'solid',
        pcbnew.ZONE_CONNECTION_THT_THERMAL: 'tht_thermal',
    }

    def shape(item, layer, clearance=0, err=pcbnew.ERROR_INSIDE):
        s = pcbnew.SHAPE_POLY_SET()
        item.TransformShapeToPolygon(s, layer, int(clearance), max_err, err)
        return s

    def overlap(a, b):
        if not a.BBox().Intersects(b.BBox()):
            return 0.0
        x = pcbnew.SHAPE_POLY_SET(a)
        x.BooleanIntersection(b)
        return x.Area()

    def rect_poly(x0, y0, x1, y1):
        r = pcbnew.SHAPE_POLY_SET()
        r.NewOutline()
        for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
            r.Append(int(x), int(y))
        return r

    def bounds(bb):
        return bb.GetLeft(), bb.GetTop(), bb.GetRight(), bb.GetBottom()

    def hits(a, b):
        return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]

    def tile(poly, bb=None):
        bb = bb or bounds(poly.BBox())
        if poly.TotalVertices() <= leaf_vertices or max(bb[2] - bb[0], bb[3] - bb[1]) <= leaf_size:
            return [(bounds(poly.BBox()), poly)]
        mx, my = (bb[0] + bb[2]) // 2, (bb[1] + bb[3]) // 2
        out = []
        for q in ((bb[0], bb[1], mx, my), (mx, bb[1], bb[2], my), (bb[0], my, mx, bb[3]), (mx, my, bb[2], bb[3])):
            part = pcbnew.SHAPE_POLY_SET(poly)
            part.BooleanIntersection(rect_poly(*q))
            if part.OutlineCount():
                out += tile(part, q)
        return out

    def area_on(s, tiles):
        sb, total = bounds(s.BBox()), 0.0
        for tb, piece in tiles:
            if hits(sb, tb):
                total += overlap(s, piece)
        return total

    def mm(v):
        return round(v / nm, 4)

    def box(s):
        bb = s.BBox()
        return [mm(bb.GetLeft()), mm(bb.GetTop()), mm(bb.GetRight()), mm(bb.GetBottom())]

    zones = [
        z for z in _all_board_zones(board)
        if not z.GetIsRuleArea() and not z.IsTeardropArea() and z.GetNetCode() > 0
    ]
    zone_nets = {z.GetNetname() for z in zones}

    pads = {}
    for fp in board.GetFootprints():
        seen = set()
        for pad in fp.Pads():
            key = pad.GetNumber() or f'<unnumbered@{pad.GetPosition().x},{pad.GetPosition().y}>'
            while key in seen:
                key += "'"
            seen.add(key)
            if pad.GetNetname() in zone_nets:
                pads[f'{fp.GetReference()}.{key}'] = pad

    tracks, vias = [], []
    for t in board.GetTracks():
        if t.GetNetname() not in zone_nets:
            continue
        if isinstance(t, pcbnew.PCB_VIA):
            vias.append(t)
        else:
            tracks.append(t)

    def track_points(t):
        pts = [(t.GetStart().x, t.GetStart().y)]
        if isinstance(t, pcbnew.PCB_ARC):
            pts.append((t.GetMid().x, t.GetMid().y))
        pts.append((t.GetEnd().x, t.GetEnd().y))
        return pts

    uf = _PourUnionFind()
    polygons = {}
    filled_layers = set()
    for zi, z in enumerate(sorted(zones, key=lambda z: (z.GetNetname(), z.GetZoneName(), -z.GetAssignedPriority(), item_id(z)))):
        net = z.GetNetname()
        for layer in z.GetLayerSet().CuStack():
            if not z.HasFilledPolysForLayer(layer):
                continue
            fill = z.GetFilledPolysList(layer)
            filled_layers.add((net, layer))
            pieces = []
            for i in range(fill.OutlineCount()):
                s = pcbnew.SHAPE_POLY_SET()
                s.AddOutline(fill.COutline(i))
                pieces.append(s)
            pieces.sort(key=lambda s: -s.Area())
            for i, s in enumerate(pieces):
                base_pid = f"{net}@{lname(layer)}/{z.GetZoneName() or 'zone' + str(zi)}#{i}"
                pid = base_pid if base_pid not in polygons else f"{net}@{lname(layer)}/{z.GetZoneName() or 'zone'}#{zi}:{i}"
                polygons[pid] = {
                    'net': net, 'layer': lname(layer), 'zone': z.GetZoneName(),
                    'area_mm2': round(s.Area() / nm / nm, 4), 'bbox_mm': box(s),
                    'pads': [], 'vias': 0, 'tracks': 0, '_tiles': tile(s),
                    '_bounds': bounds(s.BBox()), '_layer': layer, '_zone_idx': zi,
                }
                uf.find(('poly', pid))

    def local_fill(net, layer, bb):
        out = pcbnew.SHAPE_POLY_SET()
        for p in polygons.values():
            if p['net'] != net or p['_layer'] != layer or not hits(bb, p['_bounds']):
                continue
            for tb, piece in p['_tiles']:
                if hits(bb, tb):
                    out.BooleanAdd(piece)
        if out.OutlineCount():
            out.BooleanIntersection(rect_poly(*bb))
        return out

    def in_fill(net, layer, pt):
        b = (pt.x, pt.y, pt.x, pt.y)
        return any(
            hits(b, tb) and piece.Contains(pt)
            for p in polygons.values()
            if p['net'] == net and p['_layer'] == layer and hits(b, p['_bounds'])
            for tb, piece in p['_tiles']
        )

    ids = sorted(polygons)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            pa, pb = polygons[a], polygons[b]
            if (pa['net'] == pb['net'] and pa['_layer'] == pb['_layer'] and pa['_zone_idx'] != pb['_zone_idx']
                    and hits(pa['_bounds'], pb['_bounds'])
                    and any(hits(ta, tb) and overlap(qa, qb) > 0 for ta, qa in pa['_tiles'] for tb, qb in pb['_tiles'])):
                uf.union(('poly', a), ('poly', b))

    def polys_on(net, layer):
        return [(pid, p) for pid, p in polygons.items() if p['net'] == net and p['_layer'] == layer]

    pad_shapes = {}
    for key, pad in pads.items():
        uf.find(('pad', key))
        for layer in pad.GetLayerSet().CuStack():
            s = shape(pad, layer)
            pad_shapes[(key, layer)] = s
            for pid, p in polys_on(pad.GetNetname(), layer):
                if area_on(s, p['_tiles']) > 0:
                    p['pads'].append(key)
                    uf.union(('pad', key), ('poly', pid))

    via_shapes = []
    via_fill = set()
    for vi, v in enumerate(vias):
        uf.find(('via', vi))
        per_layer = {}
        for layer in v.GetLayerSet().CuStack():
            if not v.FlashLayer(layer):
                continue
            s = shape(v, layer)
            per_layer[layer] = s
            for pid, p in polys_on(v.GetNetname(), layer):
                if area_on(s, p['_tiles']) > 0:
                    p['vias'] += 1
                    via_fill.add(vi)
                    uf.union(('via', vi), ('poly', pid))
        via_shapes.append(per_layer)

    grid = {}

    def cells(x0, y0, x1, y1):
        for gx in range(int(x0 // cell), int(x1 // cell) + 1):
            for gy in range(int(y0 // cell), int(y1 // cell) + 1):
                yield gx, gy

    def insert(net, layer, bb, item):
        for c in cells(bb[0], bb[1], bb[2], bb[3]):
            grid.setdefault((net, layer, c), []).append(item)

    def near(net, layer, pt):
        return grid.get((net, layer, (int(pt[0] // cell), int(pt[1] // cell))), [])

    for (key, layer), s in pad_shapes.items():
        bb = s.BBox()
        insert(pads[key].GetNetname(), layer, (bb.GetLeft(), bb.GetTop(), bb.GetRight(), bb.GetBottom()),
               ('pad', key, s))
    for vi, per_layer in enumerate(via_shapes):
        for layer, s in per_layer.items():
            bb = s.BBox()
            insert(vias[vi].GetNetname(), layer, (bb.GetLeft(), bb.GetTop(), bb.GetRight(), bb.GetBottom()),
                   ('via', vi, s))
    track_data = []
    for ti, t in enumerate(tracks):
        pts, w = track_points(t), t.GetWidth()
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        track_data.append((t.GetLayer(), t.GetNetname(), pts, w))
        insert(t.GetNetname(), t.GetLayer(), (min(xs) - w, min(ys) - w, max(xs) + w, max(ys) + w), ('track', ti, None))

    track_pads = {}
    for ti, (layer, net, pts, w) in enumerate(track_data):
        uf.find(('track', ti))
        s = shape(tracks[ti], layer)
        for pid, p in polys_on(net, layer):
            if area_on(s, p['_tiles']) > 0:
                p['tracks'] += 1
                uf.union(('track', ti), ('poly', pid))
        for end in (pts[0], pts[-1]):
            seen = set()
            for kind, ident, other in near(net, layer, end):
                if (kind, ident) in seen or (kind == 'track' and ident == ti):
                    continue
                seen.add((kind, ident))
                if kind == 'track':
                    _, _, opts, ow = track_data[ident]
                    tol2 = (max(w, ow) / 2) ** 2
                    hit = any(_pour_seg_dist2(end, q0, q1) <= tol2 for q0, q1 in zip(opts, opts[1:]))
                else:
                    hit = other.Contains(pcbnew.VECTOR2I(int(end[0]), int(end[1])))
                if hit:
                    uf.union(('track', ti), (kind, ident))
                    if kind == 'pad' and (ti, layer) not in track_pads.get(ident, []):
                        track_pads.setdefault(ident, []).append((ti, layer))

    comp_area = {}
    for pid, p in polygons.items():
        root = uf.find(('poly', pid))
        comp_area.setdefault(p['net'], {}).setdefault(root, 0.0)
        comp_area[p['net']][root] += p['area_mm2']
    main_comp = {net: max(c.items(), key=lambda kv: (kv[1], str(kv[0])))[0] for net, c in comp_area.items()}
    for pid, p in polygons.items():
        p['in_main'] = uf.find(('poly', pid)) == main_comp.get(p['net'])

    pad_records = {}
    for z in sorted(zones, key=lambda z: -z.GetAssignedPriority()):
        net = z.GetNetname()
        zone_conn = z.GetPadConnection()
        for layer in z.GetLayerSet().CuStack():
            for key, pad in pads.items():
                rec_key = f'{key}@{lname(layer)}'
                if pad.GetNetname() != net or (key, layer) not in pad_shapes or rec_key in pad_records:
                    continue
                fp = pad.GetParentFootprint()
                conn = pad.GetLocalZoneConnection()
                if conn == pcbnew.ZONE_CONNECTION_INHERITED and fp is not None:
                    conn = fp.GetLocalZoneConnection()
                if conn == pcbnew.ZONE_CONNECTION_INHERITED:
                    conn = zone_conn
                pth = pad.GetAttribute() == pcbnew.PAD_ATTRIB_PTH
                if conn == pcbnew.ZONE_CONNECTION_THT_THERMAL:
                    conn = pcbnew.ZONE_CONNECTION_THERMAL if pth else pcbnew.ZONE_CONNECTION_FULL
                gap = pad.GetLocalThermalGapOverride()
                gap = int(gap) if gap is not None else int(z.GetThermalReliefGap())
                reach = shape(pad, layer, gap)
                if overlap(reach, z.Outline()) <= 0:
                    continue
                ps = pad_shapes[(key, layer)]
                rb = bounds(reach.BBox())
                fill = local_fill(net, layer, rb) if (net, layer) in filled_layers else None
                touches = fill is not None and overlap(ps, fill) > 0
                spokes, solid, track_spokes = 0, False, 0
                if fill is not None:
                    ring = shape(pad, layer, edge_band)
                    ring.BooleanSubtract(shape(pad, layer, -edge_band, pcbnew.ERROR_OUTSIDE))
                    hit = pcbnew.SHAPE_POLY_SET(ring)
                    hit.BooleanIntersection(fill)
                    spokes = hit.OutlineCount()
                    solid = any(hit.HoleCount(i) > 0 for i in range(hit.OutlineCount()))
                for ti in {ti for ti, lyr in track_pads.get(key, []) if lyr == layer}:
                    ends = [pcbnew.VECTOR2I(int(x), int(y)) for x, y in (track_data[ti][2][0], track_data[ti][2][-1])]
                    inside = [ps.Contains(e) for e in ends]
                    if inside.count(True) == 1 and in_fill(net, layer, ends[inside.index(False)]):
                        track_spokes += 1
                on_layer = [ti for ti, lyr in track_pads.get(key, []) if lyr == layer]
                via_spokes = sum(
                    1 for vi in via_fill
                    if vias[vi].GetNetname() == net and layer in via_shapes[vi]
                    and overlap(via_shapes[vi][layer], ps) > 0
                )
                pos = pad.GetPosition()
                pad_records[rec_key] = {
                    'pad': key, 'net': net, 'layer': lname(layer), 'zone': z.GetZoneName(),
                    'position_mm': [mm(pos.x), mm(pos.y)], 'pth': pth,
                    'connection': conn_names.get(conn, str(conn)), 'thermal_gap_mm': mm(gap),
                    'touches_fill': touches, 'spokes': spokes, 'solid': solid,
                    'polygons': sorted(pid for pid, p in polys_on(net, layer) if key in p['pads']),
                    'in_main': uf.find(('pad', key)) == main_comp.get(net),
                    'tracks_on_layer': len(on_layer), 'tracks_attached': len(track_pads.get(key, [])),
                    'track_spokes': track_spokes, 'via_spokes': via_spokes,
                }

    layers = {}
    for p in polygons.values():
        lk = f"{p['net']}@{p['layer']}"
        rec = layers.setdefault(lk, {'net': p['net'], 'layer': p['layer'], 'area_mm2': 0.0,
                                     'polygons': 0, 'islands_off_main': 0})
        rec['area_mm2'] = round(rec['area_mm2'] + p['area_mm2'], 4)
        rec['polygons'] += 1
        rec['islands_off_main'] += 0 if p['in_main'] else 1
    return {
        'min_resolved_spokes': min_resolved_spokes,
        'zones': zones,
        'layers': dict(sorted(layers.items())),
        'polygons': {pid: {k: v for k, v in p.items() if not k.startswith('_')} | {'pads': sorted(p['pads'])}
                     for pid, p in sorted(polygons.items())},
        'pads': dict(sorted(pad_records.items())),
    }


def _compare_pour_facts(ref: dict, cand: dict, *, area_loss_pct: float | None = None,
                        min_spokes: int | None = None) -> tuple[bool, list[dict]]:
    board_min = int(ref.get('min_resolved_spokes') or 0)
    minimum = max(board_min, int(min_spokes or 0))
    failures = []
    for lk in sorted(set(ref['layers']) | set(cand['layers'])):
        r, c = ref['layers'].get(lk), cand['layers'].get(lk)
        ra, ca = (r or {}).get('area_mm2', 0.0), (c or {}).get('area_mm2', 0.0)
        pct = round(100.0 * (ca - ra) / ra, 3) if ra else None
        if area_loss_pct is not None and pct is not None and -pct > area_loss_pct:
            failures.append({'kind': 'area_loss', 'net_layer': lk, 'area_mm2': [ra, ca],
                             'delta_pct': pct, 'limit_pct': area_loss_pct})

    def eff_spokes(rec):
        return float('inf') if rec['solid'] else rec['spokes'] + rec['track_spokes']

    for key in sorted(set(ref['pads']) | set(cand['pads'])):
        r, c = ref['pads'].get(key), cand['pads'].get(key)
        if r is None or c is None:
            if r is not None and r['connection'] != 'none' and r['touches_fill']:
                failures.append({'kind': 'lost_pad_reach', 'pad': key, 'connection': r['connection']})
            continue
        if r['connection'] == 'none':
            continue
        if r['touches_fill'] and not c['touches_fill']:
            failures.append({'kind': 'lost_fill_contact', 'pad': key})
            continue
        if r['in_main'] and not c['in_main']:
            failures.append({'kind': 'isolated_island', 'pad': key})
            continue
        es_r, es_c = eff_spokes(r), eff_spokes(c)
        if r['connection'] == 'thermal' and r['touches_fill'] and es_r >= minimum > es_c:
            failures.append({'kind': 'spokes_below_minimum', 'pad': key,
                             'spokes': [r['spokes'], c['spokes']], 'minimum': minimum})
    return not failures, failures


def apply_plan(source: Path, plan: dict, destination: Path, *, expected_request: bytes,
               original_source: Path | None = None, drc_timeout: float = 30.0,
               validation_report: Path | None = None, pour_area_loss: float | None = None,
               pour_min_spokes: int | None = None):
    if source.resolve() == destination.resolve():
        raise ValueError('output must be a new path distinct from the input')
    if os.path.lexists(destination):
        raise FileExistsError(f'output already exists: {destination}')
    if validation_report is not None and os.path.lexists(validation_report):
        raise FileExistsError(f'validation report already exists: {validation_report}')
    if original_source is not None:
        resolve_sidecars(original_source)
    with source_snapshot(source) as snapshot:
        apply_snapshot(snapshot, plan, destination, expected_request=expected_request,
                       drc_timeout=drc_timeout, validation_report=validation_report,
                       pour_area_loss=pour_area_loss, pour_min_spokes=pour_min_spokes)


def _net_matches_filter_token(pattern: str, net_name: str) -> bool:
    if fnmatch.fnmatchcase(net_name, pattern):
        return True
    if '/' not in pattern:
        leaf = net_name.rsplit('/', 1)[-1]
        if fnmatch.fnmatchcase(leaf, pattern):
            return True
    return False


def _filter_active_net_codes(board, net_filters: list[str]) -> set[int]:
    pos = [pat for pat in net_filters if not pat.startswith('!')]
    neg = [pat[1:] for pat in net_filters if pat.startswith('!')]
    active = set()
    for code, net in board.GetNetsByNetcode().items():
        if int(code) == 0:
            continue
        name = net.GetNetname()
        pos_ok = not pos or any(_net_matches_filter_token(p, name) for p in pos)
        neg_hit = any(_net_matches_filter_token(p, name) for p in neg)
        if pos_ok and not neg_hit:
            active.add(int(code))
    return active


def _verify_saved_connectivity(saved, before_drc: dict, after_drc: dict, net_filters: list[str]) -> None:
    key = lambda item: json.dumps(item, sort_keys=True)
    if not net_filters:
        if saved.GetConnectivity().GetUnconnectedCount(False) != 0:
            raise NativeValidationError('native connectivity still has open connections')
        if after_drc['unconnected_items']:
            raise NativeValidationError('native DRC still reports open connections')
        return
    introduced_unconnected = Counter(map(key, after_drc['unconnected_items'])) - Counter(
        map(key, before_drc['unconnected_items'])
    )
    if introduced_unconnected:
        raise NativeValidationError('native DRC reports new open connections')
    active_codes = _filter_active_net_codes(saved, net_filters)
    if not active_codes:
        return
    conn = saved.GetConnectivity()
    pads_by_net: dict[int, list] = {}
    for pad in saved.GetPads():
        code = int(pad.GetNetCode())
        if code in active_codes:
            pads_by_net.setdefault(code, []).append(pad)
    for code, net_pads in pads_by_net.items():
        if len(net_pads) <= 1:
            continue
        pad_uuids = {p.m_Uuid.AsString() for p in net_pads}
        visited = set()
        queue = [net_pads[0]]
        while queue:
            cur = queue.pop()
            cid = cur.m_Uuid.AsString()
            if cid in visited:
                continue
            visited.add(cid)
            for item in conn.GetConnectedItems(cur, 0):
                iid = item.m_Uuid.AsString()
                if iid not in visited:
                    queue.append(item)
        if not pad_uuids.issubset(visited):
            raise NativeValidationError('native connectivity still has open connections on selected nets')


def apply_snapshot(source: Path, plan: dict, destination: Path, *, expected_request: bytes,
                   drc_timeout: float = 30.0, validation_report: Path | None = None,
                   pour_area_loss: float | None = None, pour_min_spokes: int | None = None):
    if not math.isfinite(drc_timeout) or drc_timeout <= 0:
        raise ValueError('drc timeout must be positive and finite')
    if pour_area_loss is not None and (not math.isfinite(pour_area_loss) or not 0.0 <= pour_area_loss <= 100.0):
        raise ValueError('pour area loss percentage must be finite and in 0..=100')
    if pour_min_spokes is not None and (type(pour_min_spokes) is not int or not 0 <= pour_min_spokes <= 16):
        raise ValueError('pour min spokes must be an integer in 0..=16')
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
    try:
        req_doc = json.loads(expected_request.decode('utf-8'))
    except (UnicodeDecodeError, ValueError) as error:
        raise ValueError('expected request must be valid JSON') from error
    req_bounds = req_doc.get('bounds') if isinstance(req_doc, dict) else None
    if (
        not isinstance(req_bounds, dict)
        or not isinstance(req_bounds.get('min'), list)
        or not isinstance(req_bounds.get('max'), list)
        or len(req_bounds['min']) != 2
        or len(req_bounds['max']) != 2
        or any(type(v) is not int for v in (*req_bounds['min'], *req_bounds['max']))
        or any(
            not (-ENGINE_MAX_NM <= req_bounds['min'][k] < req_bounds['max'][k] <= ENGINE_MAX_NM)
            for k in (0, 1)
        )
    ):
        raise ValueError('Rust plan expected_request bounds are invalid')
    for counter in ('expanded_states', 'work_used'):
        if type(plan.get(counter)) is not int or not 0 <= plan[counter] < 2**64:
            raise ValueError(f'Rust plan requires {counter} as an unsigned 64-bit integer')
    source_digest = hashlib.sha256(read_bounded(source)).hexdigest()
    if plan.get('source_id') != source_digest:
        raise ValueError('Rust plan does not match the input board')
    removals = plan.get('removed_track_ids')
    if not isinstance(removals, list) or any(not isinstance(ident, str) for ident in removals):
        raise ValueError('Rust result requires an explicit removed_track_ids list of strings')
    seen_removals = set()
    for ident in removals:
        try:
            parsed = uuid.UUID(ident)
        except ValueError as error:
            raise ValueError('Rust result removed_track_ids entries must be canonical nonzero lowercase unique UUIDs') from error
        if str(parsed) != ident or parsed.int == 0 or ident in seen_removals:
            raise ValueError('Rust result removed_track_ids entries must be canonical nonzero lowercase unique UUIDs')
        seen_removals.add(ident)
    if removals:
        raise UnsupportedBoard('this slice cannot remove input copper')
    additions = plan.get('added_tracks')
    if not isinstance(additions, list):
        raise ValueError('Rust plan must contain an explicit added_tracks list')
    added_vias = plan.get('added_vias', [])
    if not isinstance(added_vias, list):
        raise ValueError('Rust plan added_vias must be a list when present')
    board = load_supported(source)
    orig_bounds, _ = _board_bounds_and_outline(board)
    shift = []
    for k in (0, 1):
        s_k = req_bounds['min'][k] - orig_bounds['min'][k]
        if req_bounds['max'][k] - orig_bounds['max'][k] != s_k:
            raise ValueError('Rust plan request bounds do not match source board bounds')
        shift.append(s_k)
    cu_layers, layer_names = _cu_stack(board)
    layer_id_by_name = dict(zip(layer_names, cu_layers))
    layer_idx_by_name = {name: idx for idx, name in enumerate(layer_names)}
    used_ids = {item_id(x) for x in board.GetTracks()} | {item_id(x) for x in board.GetPads()}
    used_ids |= {item_id(x) for x in board.GetFootprints()} | {item_id(x) for x in board.GetDrawings()}
    used_ids |= {item_id(x) for fp in board.GetFootprints() for x in fp.GraphicalItems()}
    used_ids |= {item_id(x) for x in board.Zones()}
    used_ids |= {item_id(x) for fp in board.GetFootprints() for x in fp.Zones()}
    all_zones = _all_board_zones(board)
    has_copper_zones = any(not z.GetIsRuleArea() and not z.IsTeardropArea() for z in all_zones)
    has_teardrops = _board_has_teardrops(board, all_zones)
    needs_refill = has_copper_zones or has_teardrops
    nets = set(board.GetNetsByNetcode())
    allowed_track_keys = {'id', 'net', 'layer', 'start', 'end', 'width_nm', 'locked'}
    required_track_keys = {'id', 'net', 'start', 'end', 'width_nm', 'locked'}
    for item in additions:
        if not isinstance(item, dict) or not required_track_keys.issubset(item) or not set(item).issubset(allowed_track_keys):
            raise ValueError('Rust output track contains invalid keys')
        layer_name = item.get('layer', 'F.Cu')
        if not isinstance(layer_name, str) or layer_name not in layer_id_by_name:
            raise ValueError(f'Rust output track references unknown copper layer: {layer_name}')
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
        orig_start = [item['start'][0] - shift[0], item['start'][1] - shift[1]]
        orig_end = [item['end'][0] - shift[0], item['end'][1] - shift[1]]
        if any(abs(n) > 1_000_000_000 for n in (*orig_start, *orig_end)):
            raise ValueError('Rust output coordinates exceed the supported native range')
        if orig_start == orig_end:
            raise ValueError('Rust output track must have distinct endpoints')
        track = pcbnew.PCB_TRACK(board)
        track.SetUuid(pcbnew.KIID(ident))
        track.SetNetCode(item['net'])
        track.SetLayer(layer_id_by_name[layer_name])
        track.SetStart(pcbnew.VECTOR2I(*orig_start))
        track.SetEnd(pcbnew.VECTOR2I(*orig_end))
        track.SetWidth(item['width_nm'])
        track.SetLocked(item['locked'])
        board.Add(track)
        used_ids.add(ident)
    via_keys = {'id', 'net', 'at', 'diameter_nm', 'drill_nm', 'start_layer', 'end_layer', 'kind', 'locked'}
    for v in added_vias:
        if not isinstance(v, dict) or set(v) != via_keys:
            raise ValueError('Rust output via contains invalid keys')
        ident = v['id']
        if not isinstance(ident, str) or str(uuid.UUID(ident)) != ident or uuid.UUID(ident).int == 0 or ident in used_ids:
            raise ValueError('Rust output via identity must be canonical, nonzero and unique')
        if type(v['net']) is not int or v['net'] not in nets or v['net'] == 0:
            raise ValueError('Rust output via requires a known nonzero integer net')
        if type(v['diameter_nm']) is not int or type(v['drill_nm']) is not int or v['drill_nm'] <= 0 or v['diameter_nm'] < v['drill_nm'] or v['diameter_nm'] > 1_000_000_000:
            raise ValueError('Rust output via diameter and drill must be positive with diameter >= drill')
        if v['start_layer'] not in layer_idx_by_name or v['end_layer'] not in layer_idx_by_name:
            raise ValueError('Rust output via references unknown copper layer')
        s_idx, e_idx = layer_idx_by_name[v['start_layer']], layer_idx_by_name[v['end_layer']]
        if s_idx >= e_idx:
            raise ValueError('Rust output via start_layer must be strictly above end_layer')
        if v['kind'] == 'through':
            kicad_via_type = pcbnew.VIATYPE_THROUGH
        elif v['kind'] == 'blind_buried':
            kicad_via_type = pcbnew.VIATYPE_BLIND if (s_idx == 0 or e_idx == len(cu_layers) - 1) else pcbnew.VIATYPE_BURIED
        elif v['kind'] == 'micro':
            kicad_via_type = pcbnew.VIATYPE_MICROVIA
        else:
            raise ValueError('Rust output via kind is invalid')
        if type(v['locked']) is not bool:
            raise ValueError('Rust output via lock flag is invalid')
        if not isinstance(v['at'], list) or len(v['at']) != 2 or any(type(n) is not int or abs(n) > 1_000_000_000 for n in v['at']):
            raise ValueError('Rust output via coordinates exceed supported native range')
        orig_at = [v['at'][0] - shift[0], v['at'][1] - shift[1]]
        if any(abs(n) > 1_000_000_000 for n in orig_at):
            raise ValueError('Rust output via coordinates exceed supported native range')
        via = pcbnew.PCB_VIA(board)
        via.SetUuid(pcbnew.KIID(ident))
        via.SetNetCode(v['net'])
        via.SetViaType(kicad_via_type)
        via.SetLayerPair(cu_layers[s_idx], cu_layers[e_idx])
        via.SetPosition(pcbnew.VECTOR2I(*orig_at))
        for lid in cu_layers[s_idx:e_idx + 1]:
            via.SetWidth(lid, v['diameter_nm'])
        via.SetDrill(v['drill_nm'])
        via.SetLocked(v['locked'])
        board.Add(via)
        used_ids.add(ident)
    # Keep the candidate unpublished until the saved native representation
    # independently confirms completion and DRC non-regression.
    with tempfile.TemporaryDirectory(prefix=f'.{destination.stem}-', dir=destination.parent) as directory:
        directory = Path(directory)
        temporary = directory / destination.name
        if not pcbnew.SaveBoard(str(temporary), board):
            raise OSError('KiCad did not save the candidate board')
        pro_bytes, dru_bytes = resolve_sidecars(source)
        drc_pro_bytes = pro_bytes
        if pro_bytes is not None:
            pro_doc = json.loads(pro_bytes.decode('utf-8'))
            if isinstance(pro_doc, dict):
                board_doc = pro_doc.get('board')
                if 'board' in pro_doc and not isinstance(board_doc, dict):
                    raise ValueError('.kicad_pro board must be an object')
                if isinstance(board_doc, dict):
                    ds_doc = board_doc.get('design_settings')
                    if isinstance(ds_doc, dict) and ds_doc.get('drc_exclusions'):
                        ds_doc['drc_exclusions'] = []
                        drc_pro_bytes = json.dumps(pro_doc).encode('utf-8')
        if drc_pro_bytes is not None:
            temporary.with_suffix('.kicad_pro').write_bytes(drc_pro_bytes)
        if dru_bytes is not None:
            temporary.with_suffix('.kicad_dru').write_bytes(dru_bytes)
        before_dir = directory / 'before_drc'
        before_dir.mkdir()
        before_board = before_dir / Path(os.path.realpath(source)).name
        before_board.write_bytes(source.read_bytes())
        if drc_pro_bytes is not None:
            before_board.with_suffix('.kicad_pro').write_bytes(drc_pro_bytes)
        if dru_bytes is not None:
            before_board.with_suffix('.kicad_dru').write_bytes(dru_bytes)
        if needs_refill:
            before = native_violations(before_board, directory / 'before-drc.json',
                                       timeout=drc_timeout, refill_zones=True, save_board=True)
            after = native_violations(temporary, directory / 'after-drc.json',
                                      timeout=drc_timeout, refill_zones=True, save_board=True)
            saved = pcbnew.LoadBoard(str(temporary))
            if saved is None:
                raise NativeValidationError('KiCad could not reload the refilled candidate')
            saved.BuildConnectivity()
        else:
            saved = pcbnew.LoadBoard(str(temporary))
            if saved is None:
                raise NativeValidationError('KiCad could not reload the saved candidate')
            saved.BuildConnectivity()
            before = native_violations(before_board, directory / 'before-drc.json', timeout=drc_timeout)
            after = native_violations(temporary, directory / 'after-drc.json', timeout=drc_timeout)
        ro_doc = req_doc.get('routing_options') if isinstance(req_doc, dict) else None
        net_filters = ro_doc.get('net_filters', []) if isinstance(ro_doc, dict) else []
        if not isinstance(net_filters, list):
            net_filters = []
        _verify_saved_connectivity(saved, before, after, [str(x) for x in net_filters])
        key = lambda violation: json.dumps(violation, sort_keys=True)
        introduced = Counter(map(key, after['violations'])) - Counter(map(key, before['violations']))
        if introduced:
            raise NativeValidationError('candidate introduces native DRC violations')
        pour_integrity_checked = False
        pour_integrity_passed = False
        if has_copper_zones:
            try:
                ref_pour = _extract_pour_facts(before_board)
                cand_pour = _extract_pour_facts(temporary)
                pour_ok, pour_failures = _compare_pour_facts(
                    ref_pour, cand_pour, area_loss_pct=pour_area_loss, min_spokes=pour_min_spokes
                )
            except NativeValidationError:
                raise
            except Exception as error:
                raise NativeValidationError(f'pour_integrity check failed: {error}') from error
            pour_integrity_checked = True
            pour_integrity_passed = pour_ok
            if not pour_ok:
                summary = '; '.join(
                    f"{f['kind']}:{f.get('pad') or f.get('net_layer')}" for f in pour_failures[:5]
                )
                raise NativeValidationError(f'pour_integrity failed: {summary}')
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
                'added_via_count': len(added_vias),
                'has_copper_zones': has_copper_zones,
                'pour_integrity_checked': pour_integrity_checked,
                'pour_integrity_passed': pour_integrity_passed,
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
    export.add_argument('--rules', type=Path, default=None)
    export.add_argument('--project', type=Path, default=None)
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
    apply.add_argument('--pour-area-loss', type=float, default=None,
                       help='fail pour_integrity if any net/layer loses more than PCT percent fill area')
    apply.add_argument('--pour-min-spokes', type=int, default=None,
                       help='tighten minimum thermal spoke requirement to max(board_min, N)')
    args = parser.parse_args()
    try:
        if args.command == 'export':
            limits = read_object(args.limits) if args.limits is not None else None
            rules_obj = read_object(args.rules) if args.rules is not None else None
            result = export_board(args.board, rules_obj, args.max_expansions, limits,
                                  original_source=args.original_source,
                                  project_path=args.project)
            write_request(result, args.output)
        else:
            apply_plan(args.board, read_object(args.plan), args.output,
                       expected_request=read_bounded(args.request),
                       original_source=args.original_source,
                       drc_timeout=args.drc_timeout,
                       validation_report=args.validation_report,
                       pour_area_loss=args.pour_area_loss,
                       pour_min_spokes=args.pour_min_spokes)
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
