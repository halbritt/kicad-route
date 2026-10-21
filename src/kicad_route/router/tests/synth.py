"""#382 E7: canonical synthetic-object builders for tests.

~28 test scripts hand-roll their own `_pad()` / `_seg()` / `_via()` / `_pcb()`
helpers, each repeating the full dataclass constructor with slightly different
defaults. These are the ONE shared set: real kicad_parser dataclasses, sensible
defaults, everything overridable by keyword (and passthrough **kw for the long
tail of optional fields like rect_rotation, drill offsets, roundrect_rratio).

A test migrates by importing the builder it needs::

    from synth import make_pad, make_seg, make_via, make_pcb, make_net

Existing tests keep working unchanged; adopt these incrementally.
"""
from __future__ import annotations

import os
import sys

# So `import synth` and the kicad modules resolve when a test file adds tests/
# to sys.path (the existing convention) or runs from the repo root.
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_TESTS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from kicad_parser import Pad, Segment, Via, Net, PCBData


def make_pad(net_id: int = 1, x: float = 0.0, y: float = 0.0, *,
             ref: str = 'U1', num: str = '1', net_name: str = '',
             size_x: float = 0.5, size_y: float = 0.5, shape: str = 'rect',
             layers=('F.Cu',), local_x: float = 0.0, local_y: float = 0.0,
             drill: float = 0.0, pad_type: str = 'smd', **kw) -> Pad:
    """A Pad with all required fields filled. SMD 0.5x0.5 rect on F.Cu by
    default; pass drill>0 / pad_type='thru_hole' for through-hole."""
    return Pad(component_ref=ref, pad_number=num, global_x=x, global_y=y,
               local_x=local_x, local_y=local_y, size_x=size_x, size_y=size_y,
               shape=shape, layers=list(layers), net_id=net_id,
               net_name=net_name, drill=drill, pad_type=pad_type, **kw)


def make_seg(x1: float, y1: float, x2: float, y2: float, *,
             layer: str = 'F.Cu', net_id: int = 1, width: float = 0.2,
             **kw) -> Segment:
    """A track Segment (0.2 mm wide on F.Cu by default)."""
    return Segment(start_x=x1, start_y=y1, end_x=x2, end_y=y2, width=width,
                   layer=layer, net_id=net_id, **kw)


def make_via(x: float, y: float, *, net_id: int = 1, size: float = 0.5,
             drill: float = 0.3, layers=('F.Cu', 'B.Cu'), **kw) -> Via:
    """A through Via (0.5/0.3 spanning F.Cu-B.Cu by default)."""
    return Via(x=x, y=y, size=size, drill=drill, layers=list(layers),
               net_id=net_id, **kw)


def make_net(net_id: int, name: str = '', **kw) -> Net:
    """A Net."""
    return Net(net_id=net_id, name=name, **kw)


def make_pcb(*, nets=None, footprints=None, vias=None, segments=None,
             pads_by_net=None, board_info=None, **kw) -> PCBData:
    """A PCBData; every container defaults to empty. Pass what the test needs."""
    return PCBData(board_info=board_info, nets=nets or {},
                   footprints=footprints or {}, vias=list(vias or []),
                   segments=list(segments or []), pads_by_net=pads_by_net or {},
                   **kw)


class RoutingBoard:
    """A small routable .kicad_pcb as TEXT (#1261): footprints with one pad
    each, tracks and vias that may carry `(locked yes)` in KiCad's canonical
    token position, and a rectangular outline. `layers` names the copper
    stack. Uuids are deterministic (`uid` overrides one) so a test can find
    an item again in the routed output."""

    _IDS = {'F.Cu': 0, 'In1.Cu': 1, 'In2.Cu': 2, 'B.Cu': 31}

    def __init__(self, w=40, h=30, layers=('F.Cu', 'B.Cu')):
        self.w, self.h, self.layers = w, h, list(layers)
        self.nets = ['']
        self.items = []
        self._n = 0

    def _uid(self):
        self._n += 1
        return 'synth-%04d' % self._n

    def net(self, name):
        if name not in self.nets:
            self.nets.append(name)
        return self.nets.index(name)

    def pad(self, ref, x, y, net, sx=0.8, sy=0.8, tht=False):
        n = self.net(net)
        if tht:
            pad = ('(pad "1" thru_hole circle (at 0 0) (size %s %s) (drill 0.8) '
                   '(layers "*.Cu" "*.Mask") (net %d "%s") (uuid "%s"))'
                   % (sx, sy, n, net, self._uid()))
        else:
            pad = ('(pad "1" smd rect (at 0 0) (size %s %s) (layers "F.Cu" '
                   '"F.Mask") (net %d "%s") (uuid "%s"))'
                   % (sx, sy, n, net, self._uid()))
        self.items.append(
            '  (footprint "synth:P" (layer "F.Cu") (uuid "%s") (at %s %s)\n'
            '    (property "Reference" "%s" (at 0 -2 0) (layer "F.SilkS") '
            '(uuid "%s")\n      (effects (font (size 0.5 0.5) (thickness 0.1))))\n'
            '    %s\n  )' % (self._uid(), x, y, ref, self._uid(), pad))

    def seg(self, x1, y1, x2, y2, net, w=0.3, layer='F.Cu', locked=False,
            uid=None):
        self.items.append(
            '  (segment (start %s %s) (end %s %s) (width %s)%s (layer "%s") '
            '(net %d) (uuid "%s"))'
            % (x1, y1, x2, y2, w, ' (locked yes)' if locked else '', layer,
               self.net(net), uid or self._uid()))

    def via(self, x, y, net, size=0.5, drill=0.3, locked=False, uid=None):
        self.items.append(
            '  (via (at %s %s) (size %s) (drill %s)%s (layers "F.Cu" "B.Cu") '
            '(net %d) (uuid "%s"))'
            % (x, y, size, drill, ' (locked yes)' if locked else '',
               self.net(net), uid or self._uid()))

    def ring(self, pts, net, w=0.2, layer='F.Cu', locked=False):
        for (x1, y1), (x2, y2) in zip(pts, list(pts[1:]) + [pts[0]]):
            self.seg(x1, y1, x2, y2, net, w=w, layer=layer, locked=locked)

    def text(self):
        lay = ' '.join('(%d "%s" signal)' % (self._IDS[l], l) for l in self.layers)
        out = ['(kicad_pcb (version 20241229) (generator "synth") '
               '(generator_version "9.0")\n'
               '  (general (thickness 1.6) (legacy_teardrops no))\n'
               '  (paper "A4")\n'
               '  (layers %s (37 "F.SilkS" user) (39 "F.Mask" user) '
               '(38 "B.Mask" user) (44 "Edge.Cuts" user))\n'
               '  (setup (pad_to_mask_clearance 0))' % lay]
        out += ['  (net %d "%s")' % (i, n) for i, n in enumerate(self.nets)]
        out += self.items
        out.append('  (gr_rect (start 0 0) (end %s %s) (layer "Edge.Cuts") '
                   '(stroke (width 0.1) (type solid)) (fill none) '
                   '(uuid "synth-edge"))' % (self.w, self.h))
        return '\n'.join(out) + '\n)\n'


def locked_items(path):
    """Every `(locked yes)` track/via line of a board written in the
    single-line form RoutingBoard emits -- the output writer copies input
    copper verbatim, so a locked item that survived comes back byte-identical."""
    with open(path, encoding='utf-8') as fh:
        text = fh.read()
    import re
    return sorted(re.findall(r'\((?:segment|via) [^\n]*\(locked yes\)[^\n]*\)',
                             text))
