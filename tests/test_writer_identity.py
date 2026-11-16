"""Opt-in copper UUIDs through the owned parser and native KiCad reader."""
from pathlib import Path
import re
import sys
import uuid

import pcbnew
import pytest

from helpers import FIXTURE, ROOT

sys.path.insert(0, str(ROOT / 'src/kicad_route/router/py_router'))
from kicad_parser import parse_kicad_pcb
from kicad_writer import generate_segment_sexpr, generate_via_sexpr

ITEM_UUID = '3c0aa486-94c1-4d88-90da-322caec19c64'
PROTECTION = {'tenting': '(front yes) (back no)',
              'covering': '(front no) (back yes)',
              'plugging': '(front yes) (back no)',
              'capping': 'yes', 'filling': 'yes'}


def segment(**kwargs):
    return generate_segment_sexpr((111.123456, 112.654321), (115.234567, 116.765432),
                                  .35, 'F.Cu', 5, '/SIG_A', **kwargs)


def via(**kwargs):
    # All pre-existing optional arguments still work positionally.
    return generate_via_sexpr(121.123456, 123.654321, .7, .3, ['F.Cu', 'In1.Cu'],
                              5, True, '/SIG_A', PROTECTION, True, 'blind', **kwargs)


def item_id(text):
    return re.search(r'\(uuid "([^"]+)"\)', text)[1]


@pytest.fixture
def board_file(tmp_path):
    board = pcbnew.LoadBoard(str(FIXTURE / 'route_demo.kicad_pcb'))
    board.SetCopperLayerCount(4)
    layers = board.GetEnabledLayers()
    layers.AddLayer(pcbnew.In1_Cu)
    layers.AddLayer(pcbnew.In2_Cu)
    board.SetEnabledLayers(layers)
    path = tmp_path / 'identity.kicad_pcb'
    pcbnew.SaveBoard(str(path), board)
    return path


def append_item(path, text):
    content = path.read_text()
    end = content.rfind(')')
    path.write_text(content[:end] + text + '\n' + content[end:])


def native_item(board):
    items = [item for item in board.GetTracks() if item.m_Uuid.AsString() == ITEM_UUID]
    assert len(items) == 1
    return items[0]


@pytest.mark.parametrize('layer', ['F.Cu', 'B.Cu'])
def test_segment_identity_native_roundtrip(board_file, layer):
    args = ((111.123456, 112.654321), (115.234567, 116.765432), .35, layer, 5, '/SIG_A')
    text = generate_segment_sexpr(*args, item_uuid=ITEM_UUID)
    assert text == generate_segment_sexpr(*args, item_uuid=ITEM_UUID)
    append_item(board_file, text)
    for _ in range(2):  # also survive native save and reload
        parsed = parse_kicad_pcb(str(board_file))
        track, = [s for s in parsed.segments if s.uuid == ITEM_UUID]
        assert (track.start_x, track.start_y, track.end_x, track.end_y, track.width) == pytest.approx(
            (111.123456, 112.654321, 115.234567, 116.765432, .35))
        assert track.layer == layer and parsed.nets[track.net_id].name == '/SIG_A'
        board = pcbnew.LoadBoard(str(board_file))
        native = native_item(board)
        assert native.GetNetname() == '/SIG_A' and board.GetLayerName(native.GetLayer()) == layer
        assert (pcbnew.ToMM(native.GetStart().x), pcbnew.ToMM(native.GetStart().y),
                pcbnew.ToMM(native.GetEnd().x), pcbnew.ToMM(native.GetEnd().y),
                pcbnew.ToMM(native.GetWidth())) == pytest.approx(
                    (111.123456, 112.654321, 115.234567, 116.765432, .35))
        pcbnew.SaveBoard(str(board_file), board)


@pytest.mark.parametrize('kind,layers,native_type', [
    (None, ['F.Cu', 'B.Cu'], pcbnew.VIATYPE_THROUGH),
    ('blind', ['F.Cu', 'In1.Cu'], pcbnew.VIATYPE_BLIND),
    ('buried', ['In1.Cu', 'In2.Cu'], pcbnew.VIATYPE_BURIED),
    ('micro', ['F.Cu', 'In1.Cu'], pcbnew.VIATYPE_MICROVIA),
])
def test_via_identity_and_fields_native_roundtrip(board_file, kind, layers, native_type):
    args = (121.123456, 123.654321, .7, .3, layers, 5, True, '/SIG_A', PROTECTION, True, kind)
    text = generate_via_sexpr(*args, item_uuid=ITEM_UUID)
    assert text == generate_via_sexpr(*args, item_uuid=ITEM_UUID)
    append_item(board_file, text)
    for _ in range(2):
        parsed = parse_kicad_pcb(str(board_file))
        hole, = [v for v in parsed.vias if v.uuid == ITEM_UUID]
        assert (hole.x, hole.y, hole.size, hole.drill) == pytest.approx((121.123456, 123.654321, .7, .3))
        assert hole.layers == layers and hole.via_type == (kind or 'through')
        assert hole.free and parsed.nets[hole.net_id].name == '/SIG_A'
        assert {k: ' '.join(v.split()) for k, v in hole.tenting_attrs.items()} == PROTECTION
        board = pcbnew.LoadBoard(str(board_file))
        native = native_item(board)
        assert isinstance(native, pcbnew.PCB_VIA) and native.GetViaType() == native_type
        assert [board.GetLayerName(native.TopLayer()), board.GetLayerName(native.BottomLayer())] == layers
        assert native.GetNetname() == '/SIG_A' and native.GetIsFree()
        assert (pcbnew.ToMM(native.GetPosition().x), pcbnew.ToMM(native.GetPosition().y),
                pcbnew.ToMM(native.GetWidth(native.TopLayer())), pcbnew.ToMM(native.GetDrillValue())) == pytest.approx(
                    (121.123456, 123.654321, .7, .3))
        assert native.GetFrontTentingMode() == pcbnew.TENTING_MODE_TENTED
        assert native.GetBackTentingMode() == pcbnew.TENTING_MODE_NOT_TENTED
        assert native.GetFrontCoveringMode() == pcbnew.COVERING_MODE_NOT_COVERED
        assert native.GetBackCoveringMode() == pcbnew.COVERING_MODE_COVERED
        assert native.GetFrontPluggingMode() == pcbnew.PLUGGING_MODE_PLUGGED
        assert native.GetBackPluggingMode() == pcbnew.PLUGGING_MODE_NOT_PLUGGED
        assert native.GetCappingMode() == pcbnew.CAPPING_MODE_CAPPED
        assert native.GetFillingMode() == pcbnew.FILLING_MODE_FILLED
        pcbnew.SaveBoard(str(board_file), board)


@pytest.mark.parametrize('writer', [segment, via])
def test_omitted_identity_is_fresh_and_changes_no_other_field(writer):
    explicit = writer(item_uuid=ITEM_UUID)
    outputs = [writer(), writer(), writer(item_uuid=None)]
    ids = [item_id(text) for text in outputs]
    assert len(set(ids)) == 3 and ITEM_UUID not in ids
    assert all(uuid.UUID(value).version == 4 for value in ids)
    for text, identity in zip(outputs, ids):
        assert text.replace(identity, ITEM_UUID) == explicit


@pytest.mark.parametrize('writer', [segment, via])
@pytest.mark.parametrize('bad', ['', 'not-a-uuid', ITEM_UUID.upper(), ITEM_UUID.replace('-', ''),
                                 '{' + ITEM_UUID + '}', ' ' + ITEM_UUID,
                                 '00000000-0000-0000-0000-000000000000', '\") (segment'])
def test_malformed_or_noncanonical_identity_rejected(writer, bad):
    with pytest.raises(ValueError, match='item_uuid'):
        writer(item_uuid=bad)


@pytest.mark.parametrize('writer', [segment, via])
@pytest.mark.parametrize('bad', [42, False, b'uuid', uuid.UUID(ITEM_UUID)])
def test_identity_requires_text(writer, bad):
    with pytest.raises(TypeError, match='item_uuid'):
        writer(item_uuid=bad)


def test_new_identity_argument_is_keyword_only():
    with pytest.raises(TypeError):
        generate_segment_sexpr((1, 2), (3, 4), .2, 'F.Cu', 5, '/SIG_A', ITEM_UUID)
    with pytest.raises(TypeError):
        generate_via_sexpr(1, 2, .6, .3, ['F.Cu', 'B.Cu'], 5, False, '/SIG_A', {}, False, None, ITEM_UUID)
