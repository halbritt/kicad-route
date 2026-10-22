"""Custody retains complete native per-layer via geometry without accepting extras."""
import copy
import json
import subprocess

import pytest

from helpers import FIXTURE, ROOT
from kicad_route import copper_ownership as custody


@pytest.fixture(scope='module')
def native_inventory(tmp_path_factory):
    path = tmp_path_factory.mktemp('native-copper-inventory') / 'facts.json'
    subprocess.run(['/usr/bin/python3', str(ROOT / 'src/kicad_route/kicad/facts.py'),
                    str(FIXTURE / 'route_demo.kicad_pcb'), str(path)],
                   check=True, capture_output=True)
    return json.loads(path.read_text())


def test_exact_native_via_layer_geometry_is_retained(native_inventory):
    actual = custody.inventory(native_inventory)
    via = native_inventory['vias'][0]
    assert via['annulus_geometry'] == 'circular-layer-discs'
    assert via['layer_diameters'] == [
        {'layer': 'F.Cu', 'diameter': via['diameter']},
        {'layer': 'B.Cu', 'diameter': via['diameter']}]
    assert actual[via['uuid']] == {'collection': 'vias', **via}
    assert len(actual) == len(native_inventory['tracks']) + len(native_inventory['vias'])


@pytest.mark.parametrize('change', [
    lambda f: f['vias'][0].update(unknown=True),
    lambda f: f['vias'][0].pop('annulus_geometry'),
    lambda f: f['vias'][0].update(annulus_geometry='unsupported'),
    lambda f: f['vias'][0].pop('layer_diameters'),
    lambda f: f['vias'][0].update(layer_diameters=[]),
    lambda f: f['vias'][0]['layer_diameters'].pop(),
    lambda f: f['vias'][0]['layer_diameters'].reverse(),
    lambda f: f['vias'][0]['layer_diameters'][1].update(layer='F.Cu'),
    lambda f: f['vias'][0]['layer_diameters'][0].update(layer='In1.Cu'),
    lambda f: f['vias'][0]['layer_diameters'][0].update(diameter=True),
    lambda f: f['vias'][0]['layer_diameters'][0].update(diameter=600000.0),
    lambda f: f['vias'][0]['layer_diameters'][0].update(diameter=0),
    lambda f: f['vias'][0]['layer_diameters'][0].update(diameter=700000),
    lambda f: f['vias'][0]['layer_diameters'][0].pop('diameter'),
    lambda f: f['vias'][0]['layer_diameters'][0].update(unknown=True),
    lambda f: f.pop('copper_layers'),
    lambda f: f['copper_layers'].append(f['copper_layers'][0]),
    lambda f: f['vias'][0]['layers'].reverse(),
    lambda f: f['vias'][0].pop('uuid'),
    lambda f: f['vias'][1].update(uuid=f['vias'][0]['uuid']),
    lambda f: f['tracks'][0].update(unknown=True),
])
def test_malformed_or_unknown_native_geometry_is_refused(native_inventory, change):
    facts = copy.deepcopy(native_inventory)
    change(facts)
    with pytest.raises(custody.OwnershipError):
        custody.inventory(facts)
