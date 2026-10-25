"""Project-less routing must satisfy the defaults used by native grading."""
import sys

import pcbnew

from helpers import ROOT

sys.path.insert(0, str(ROOT / 'src/kicad_route/router/py_router'))
import routing_defaults


def test_fallback_geometry_meets_native_default_netclass():
    # An empty .kicad_pro has no serialized class to override these fallbacks.
    # Native validation still resolves its Default class, including via size.
    native = pcbnew.NETCLASS('Default')
    for setting, accessor in (
        ('TRACK_WIDTH', 'GetTrackWidth'), ('CLEARANCE', 'GetClearance'),
        ('VIA_SIZE', 'GetViaDiameter'), ('VIA_DRILL', 'GetViaDrill'),
    ):
        assert getattr(routing_defaults, setting) >= pcbnew.ToMM(getattr(native, accessor)()), setting
