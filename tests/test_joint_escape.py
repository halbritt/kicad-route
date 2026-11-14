"""Unit tests for joint multi-pin escape planning on fine-pitch perimeter packages."""
import sys
from helpers import ROOT
sys.path.insert(0, str(ROOT / 'src/kicad_route/router/py_router'))
import pytest
from kicad_parser import BoardInfo, Footprint, Net, Pad, PCBData, Segment
from qfn_fanout.joint_escape import (
    FINE_PITCH_THRESHOLD_MM,
    apply_joint_escapes,
    find_fine_pitch_footprints,
    plan_all_fine_pitch_escapes,
    plan_joint_edge_escapes,
)
from qfn_fanout.layout import analyze_qfn_layout


def make_test_pcb(pitch=0.5, pad_count=16, fp_ref="U1"):
    """Create a minimal synthetic PCBData with a 16-pin 4-sided perimeter package."""
    pads = []
    # 4 pads on top (y=-2.0), 4 on bottom (y=2.0), 4 on left (x=-2.0), 4 on right (x=2.0)
    xs = [-1.5 * pitch, -0.5 * pitch, 0.5 * pitch, 1.5 * pitch]
    pad_idx = 1
    # Top
    for x in xs:
        pad = Pad(
            component_ref=fp_ref,
            pad_number=str(pad_idx),
            global_x=100.0 + x,
            global_y=100.0 - 2.0,
            local_x=x,
            local_y=-2.0,
            size_x=0.25,
            size_y=0.8,
            shape="rect",
            layers=["F.Cu"],
            net_id=pad_idx,
            net_name=f"NET_{pad_idx}",
        )
        pads.append(pad)
        pad_idx += 1

    # Bottom
    for x in reversed(xs):
        pad = Pad(
            component_ref=fp_ref,
            pad_number=str(pad_idx),
            global_x=100.0 + x,
            global_y=100.0 + 2.0,
            local_x=x,
            local_y=2.0,
            size_x=0.25,
            size_y=0.8,
            shape="rect",
            layers=["F.Cu"],
            net_id=pad_idx,
            net_name=f"NET_{pad_idx}",
        )
        pads.append(pad)
        pad_idx += 1

    # Left
    for y in xs:
        pad = Pad(
            component_ref=fp_ref,
            pad_number=str(pad_idx),
            global_x=100.0 - 2.0,
            global_y=100.0 + y,
            local_x=-2.0,
            local_y=y,
            size_x=0.8,
            size_y=0.25,
            shape="rect",
            layers=["F.Cu"],
            net_id=pad_idx,
            net_name=f"NET_{pad_idx}",
        )
        pads.append(pad)
        pad_idx += 1

    # Right
    for y in reversed(xs):
        pad = Pad(
            component_ref=fp_ref,
            pad_number=str(pad_idx),
            global_x=100.0 + 2.0,
            global_y=100.0 + y,
            local_x=2.0,
            local_y=y,
            size_x=0.8,
            size_y=0.25,
            shape="rect",
            layers=["F.Cu"],
            net_id=pad_idx,
            net_name=f"NET_{pad_idx}",
        )
        pads.append(pad)
        pad_idx += 1

    fp = Footprint(
        reference=fp_ref,
        footprint_name="QFN-16",
        x=100.0,
        y=100.0,
        rotation=0.0,
        layer="F.Cu",
        pads=pads,
    )

    board_info = BoardInfo(
        layers={0: "F.Cu", 31: "B.Cu"},
        copper_layers=["F.Cu", "B.Cu"],
    )

    # Add external destination pad for each net on a passive R1
    remote_pads = [
        Pad(
            component_ref="R1",
            pad_number=str(p.net_id),
            global_x=120.0,
            global_y=120.0,
            local_x=0.0,
            local_y=0.0,
            size_x=0.5,
            size_y=0.5,
            shape="rect",
            layers=["F.Cu"],
            net_id=p.net_id,
            net_name=p.net_name,
        )
        for p in pads
    ]

    pcb = PCBData(
        board_info=board_info,
        nets={p.net_id: Net(p.net_id, p.net_name) for p in pads},
        footprints={fp_ref: fp},
        vias=[],
        segments=[],
        pads_by_net={p.net_id: [p, remote_pads[i]] for i, p in enumerate(pads)},
    )
    return pcb, fp


def test_fine_pitch_detection():
    # 0.5mm package is fine-pitch, regardless of reference designator
    pcb_fine, fp_fine = make_test_pcb(pitch=0.5, fp_ref="IC1")
    fps_fine = find_fine_pitch_footprints(pcb_fine)
    assert len(fps_fine) == 1
    assert fps_fine[0][0].reference == "IC1"

    # 1.27mm package is not fine-pitch
    pcb_coarse, fp_coarse = make_test_pcb(pitch=1.27, fp_ref="IC1")
    fps_coarse = find_fine_pitch_footprints(pcb_coarse)
    assert len(fps_coarse) == 0


def test_order_fine_pitch_perimeter_nets():
    from net_ordering import order_fine_pitch_perimeter_nets
    pcb, fp = make_test_pcb(pitch=0.5, fp_ref="IC1")
    # Top edge pads: 1 (corner), 2 (inner), 3 (inner), 4 (corner)
    # Suppose initial net order has corner pad 1 before inner pad 2
    initial_order = [(f"NET_{i}", i) for i in range(1, 17)]
    ordered = order_fine_pitch_perimeter_nets(pcb, initial_order)
    ordered_nids = [nid for _, nid in ordered]

    # Inner pads 2 and 3 have fewer open exit directions than corner pads 1 and 4,
    # so on the top edge, 2 must appear before 1, and 3 must appear before 4.
    assert ordered_nids.index(2) < ordered_nids.index(1)
    assert ordered_nids.index(3) < ordered_nids.index(4)


def test_joint_edge_escapes_chutes():
    pcb, fp = make_test_pcb(pitch=0.5)
    layout = analyze_qfn_layout(fp)
    assert layout is not None
    chutes = plan_joint_edge_escapes(fp, layout, pcb)
    assert len(chutes) == 16

    # Top edge pads should escape in -Y direction (uy < 0)
    top_chutes = [c for c in chutes if c['side'] == 'top']
    assert len(top_chutes) == 4
    for c in top_chutes:
        assert c['end'][1] < c['start'][1]
        assert c['end'][0] == pytest.approx(c['start'][0], abs=1e-3)

    # Bottom edge pads should escape in +Y direction (uy > 0)
    bottom_chutes = [c for c in chutes if c['side'] == 'bottom']
    assert len(bottom_chutes) == 4
    for c in bottom_chutes:
        assert c['end'][1] > c['start'][1]
        assert c['end'][0] == pytest.approx(c['start'][0], abs=1e-3)


def test_apply_joint_escapes():
    pcb, fp = make_test_pcb(pitch=0.5)
    chutes = plan_all_fine_pitch_escapes(pcb)
    assert len(chutes) == 16
    added = apply_joint_escapes(pcb, chutes)
    assert added == 16
    assert len(pcb.segments) == 16

    # Duplicate application is idempotent
    added_again = apply_joint_escapes(pcb, chutes)
    assert added_again == 0
    assert len(pcb.segments) == 16


def test_connector_excluded_from_fine_pitch():
    # Connectors/headers (1 or 2 rows of pads, not 4 perimeter sides) are structurally excluded
    pads = []
    pitch = 0.5
    for i in range(8):
        x = i * pitch
        pads.append(Pad(
            component_ref="J1", pad_number=str(i + 1),
            global_x=100.0 + x, global_y=99.0, local_x=x, local_y=-1.0,
            size_x=0.25, size_y=0.8, shape="rect", layers=["F.Cu"],
            net_id=i + 1, net_name=f"NET_{i + 1}",
        ))
        pads.append(Pad(
            component_ref="J1", pad_number=str(i + 9),
            global_x=100.0 + x, global_y=101.0, local_x=x, local_y=1.0,
            size_x=0.25, size_y=0.8, shape="rect", layers=["F.Cu"],
            net_id=i + 9, net_name=f"NET_{i + 9}",
        ))
    fp = Footprint(
        reference="J1", footprint_name="Connector_USB:USB_C_Receptacle_HRO_TYPE-C-31-M-12",
        x=100.0, y=100.0, rotation=0.0, layer="F.Cu", pads=pads,
    )
    board_info = BoardInfo(layers={0: "F.Cu", 31: "B.Cu"}, copper_layers=["F.Cu", "B.Cu"])
    pcb = PCBData(
        board_info=board_info, nets={p.net_id: Net(p.net_id, p.net_name) for p in pads},
        footprints={"J1": fp}, vias=[], segments=[],
        pads_by_net={p.net_id: [p] for p in pads},
    )
    fps = find_fine_pitch_footprints(pcb)
    assert len(fps) == 0
