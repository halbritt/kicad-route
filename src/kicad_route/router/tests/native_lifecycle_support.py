"""Small library-free boards and mandatory native grading for lifecycle regressions."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid


def write_input(path, pcb, *, omit_segments=()):
    """Author the original board, excluding copper a preceding route just added."""
    path = Path(path)
    parts = ['(kicad_pcb (version 20260206) (generator "lifecycle-regression")',
             '(general (thickness 1.6)) (paper "A4")',
             '(layers (0 "F.Cu" signal) (2 "B.Cu" signal) (7 "F.Mask" user) (44 "Edge.Cuts" user))',
             '(setup (pad_to_mask_clearance 0))']
    for net in pcb.nets.values():
        parts.append(f'(net {net.net_id} "{net.name}")')
    for pads in pcb.pads_by_net.values():
        for pad in pads:
            uid = uuid.uuid5(uuid.NAMESPACE_URL, 'kicad-route/lifecycle/' + pad.component_ref)
            parts.append(f'''(footprint "test:terminal" (layer "F.Cu") (uuid "{uid}")
              (at {pad.global_x} {pad.global_y})
              (property "Reference" "{pad.component_ref}" (at 0 -1 0) (layer "F.SilkS"))
              (property "Value" "test" (at 0 1 0) (layer "F.Fab"))
              (pad "{pad.pad_number}" smd circle (at 0 0) (size {pad.size_x} {pad.size_y})
                (layers "F.Cu" "F.Mask") (net {pad.net_id} "{pad.net_name}")))''')
    omitted = {id(s) for s in omit_segments}
    for seg in pcb.segments:
        if id(seg) not in omitted:
            parts.append(f'(segment (start {seg.start_x} {seg.start_y}) (end {seg.end_x} {seg.end_y}) '
                         f'(width {seg.width}) (layer "{seg.layer}") (net {seg.net_id}) '
                         f'(uuid "{uuid.uuid4()}"))')
    parts.append('(gr_rect (start 0 0) (end 10 8) (stroke (width .05) (type default)) '
                 '(fill none) (layer "Edge.Cuts"))')
    path.write_text('\n'.join(parts) + '\n)\n')
    project = {'meta': {'filename': path.with_suffix('.kicad_pro').name, 'version': 3},
               'board': {'design_settings': {'rules': {'min_clearance': .2,
                                                     'min_track_width': .2,
                                                     'min_via_diameter': .6,
                                                     'min_through_hole_diameter': .3}}},
               'net_settings': {'classes': [{'name': 'Default', 'clearance': .2,
                                             'track_width': .2, 'via_diameter': .6,
                                             'via_drill': .3}]}}
    path.with_suffix('.kicad_pro').write_text(json.dumps(project))


def native_grade(path):
    """Run actual KiCad; missing tools, parse errors and missing reports fail."""
    path = Path(path)
    cli = shutil.which('kicad-cli')
    if not cli:
        raise AssertionError('native KiCad CLI is required')
    report = path.with_suffix('.drc.json')
    command = [cli, 'pcb', 'drc', '--refill-zones', '--save-board', '--format', 'json',
               '--severity-all', '--output', str(report), str(path)]
    home = path.parent / '.home'
    home.mkdir(exist_ok=True)
    env = dict(os.environ, HOME=str(home.resolve()),
               KICAD10_CONFIG_HOME=str((home / 'kicad').resolve()))
    proc = subprocess.run(command, capture_output=True, text=True, timeout=60, env=env)
    path.with_suffix('.native-command.json').write_text(json.dumps(
        {'argv': command, 'exit_code': proc.returncode}, indent=2))
    path.with_suffix('.drc.log').write_text(proc.stdout + proc.stderr)
    if proc.returncode:
        raise AssertionError(f'native DRC failed ({proc.returncode}): {proc.stdout} {proc.stderr}')
    data = json.loads(report.read_text())
    return data
