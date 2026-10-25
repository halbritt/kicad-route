"""Use authored KiCad 10 tuning profiles for nominal group delay estimates.

Native fromTo DRC remains the final useful-path oracle. This estimator drives
meanders; it does not establish connectivity or signal integrity.
"""
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import tempfile


def profiles(board_path, names, *, required=False):
    from kicad_parser import extract_stackup

    board_path = Path(board_path)
    project = board_path.with_suffix('.kicad_pro')
    if not project.is_file():
        if required:
            raise ValueError('delay matching requires a native project with tuning profiles')
        return None
    doc = json.loads(project.read_text())
    declared = doc.get('tuning_profiles', {}).get('tuning_profiles_impedance_geometric', [])
    if not declared and not required:
        return None
    try:
        import pcbnew as k
    except ModuleNotFoundError as exc:
        if exc.name != 'pcbnew':
            raise
        from kicad_exact_fill import find_kicad_python
        python = find_kicad_python()
        if python is None:
            raise ValueError('native delay profiles require an interpreter with pcbnew') from exc
        with tempfile.TemporaryDirectory(prefix='native-delay-') as directory:
            output = Path(directory) / 'profiles.json'
            done = subprocess.run([python, str(Path(__file__).resolve()), str(board_path),
                                   json.dumps(names), str(output)], capture_output=True, text=True, timeout=60)
            if done.returncode != 0:
                raise ValueError('native delay profile read failed: ' + done.stderr[-2000:])
            return json.loads(output.read_text())
    board = k.LoadBoard(str(board_path))
    if not board.GetDesignSettings().m_UseHeightForLengthCalcs:
        raise ValueError('delay matching requires native via-height calculation enabled')
    settings = board.GetDesignSettings().m_NetSettings
    result = {}
    for name in names:
        selected = settings.GetEffectiveNetClass(name).GetTuningProfile()
        found = [p for p in declared if p['profile_name'] == selected]
        if not selected or len(found) != 1:
            raise ValueError(f'{name}: delay matching needs one effective native tuning profile')
        profile = found[0]
        if profile['type'] != 0 or profile['enable_time_domain_tuning'] is not True:
            raise ValueError(f'{name}: delay matching needs an enabled single-ended tuning profile')
        entries = profile['layer_entries']
        layers = [e['signal_layer'] for e in entries]
        if not layers or len(layers) != len(set(layers)):
            raise ValueError(f'{name}: missing or duplicate delay profile layers')
        for entry in entries:
            for field in ('width', 'delay'):
                if type(entry[field]) is not int or entry[field] <= 0:
                    raise ValueError(f'{name}: delay profile needs positive native width and delay values')
        if type(profile['via_prop_delay']) is not int or profile['via_prop_delay'] <= 0:
            raise ValueError(f'{name}: delay profile needs an explicit positive via delay')
        for override in profile['via_overrides']:
            if type(override['delay']) is not int or override['delay'] <= 0:
                raise ValueError(f'{name}: via delay overrides must be positive native values')
        result[name] = profile
    raw = board_path.read_text()
    from sexpr import expression_spans
    stacks = [raw[a:b] for a, b, _ in expression_spans(raw, {'stackup'})]
    if len(stacks) != 1:
        raise ValueError('delay matching requires one explicit native stackup')
    for a, b, _ in expression_spans(stacks[0], {'layer'}):
        layer_text = stacks[0][a:b]
        if not re.search(r'\(type\s+"(?:core|prepreg)"\)', layer_text):
            continue
        tokens = re.findall(r'"(?:\\.|[^"\\])*"|\baddsublayer\b', layer_text)
        sheets = 1 + tokens.count('addsublayer')
        for field in ('thickness', 'epsilon_r'):
            values = [float(layer_text[x:y][1:-1].split()[1])
                      for x, y, _ in expression_spans(layer_text, {field})]
            if len(values) != sheets or any(not math.isfinite(v) or v <= 0 for v in values):
                raise ValueError(f'delay matching requires explicit positive {field} for every dielectric sheet')
    stack = extract_stackup(raw)
    copper = [s.name for s in stack if s.layer_type == 'copper']
    if copper != [str(board.GetLayerName(i)) for i in board.GetEnabledLayers().CuStack()]:
        raise ValueError('delay matching requires an explicit complete native copper/dielectric stackup')
    for i, layer in enumerate(stack):
        if not math.isfinite(layer.thickness) or layer.thickness <= 0:
            raise ValueError('delay matching requires explicit positive stackup thicknesses')
        if layer.layer_type != 'copper':
            if not math.isfinite(layer.epsilon_r) or layer.epsilon_r < 1:
                raise ValueError('delay matching requires explicit dielectric epsilon_r >= 1')
        elif i % 2:
            raise ValueError('delay matching requires alternating copper and dielectric layers')
    for name, profile in result.items():
        for entry in profile['layer_entries']:
            if entry['signal_layer'] not in copper:
                raise ValueError(f'{name}: tuning profile signal layer is absent from the stackup')
    return result


def rate(profile, layer):
    entries = [e for e in profile['layer_entries'] if e['signal_layer'] == layer]
    if len(entries) != 1:
        raise ValueError(f'delay profile {profile["profile_name"]}: no unique rate for {layer}')
    # KiCad uses attoseconds/mm, not femtoseconds/mm (base_units.h).
    return entries[0]['delay'] / 1e6


def estimate(pcb, profile, segments, vias):
    total = 0.0
    for seg in segments:
        total += math.hypot(seg.end_x - seg.start_x, seg.end_y - seg.start_y) * rate(profile, seg.layer)
    copper = pcb.board_info.copper_layers
    for via in vias or []:
        touched = {s.layer for s in segments if s.net_id == via.net_id and
                   any(math.hypot(x-via.x, y-via.y) < 1e-6 for x, y in
                       ((s.start_x, s.start_y), (s.end_x, s.end_y)))}
        if len(touched) < 2:
            raise ValueError('delay matching cannot establish the used via span')
        ordered = sorted(touched, key=copper.index)
        used = ordered[0], ordered[-1]
        overrides = [v for v in profile['via_overrides'] if
                     (v['signal_layer_from'], v['signal_layer_to']) == used and
                     (v['via_layer_from'], v['via_layer_to']) == tuple(via.layers)]
        if len(overrides) > 1:
            raise ValueError('delay matching found duplicate via transition overrides')
        height = pcb.get_via_barrel_length(*used)
        # KiCad GetLayerDistance includes the full outer copper thickness and
        # half the endpoint thickness for each internal copper layer.
        height -= sum(s.thickness / 2 for s in pcb.board_info.stackup
                      if s.name in used and s.name not in ('F.Cu', 'B.Cu'))
        total += (overrides[0]['delay'] / 1e6 if overrides else
                  height * profile['via_prop_delay'] / 1e6)
    return total


def check_geometry(board_path, models):
    """Reject paths on unmodelled layers or widths; native DRC grades their delay."""
    import pcbnew as k
    board = k.LoadBoard(str(board_path))
    for track in board.GetTracks():
        name = str(track.GetNetname())
        if name not in models or track.GetClass() == 'PCB_VIA':
            continue
        layer = str(board.GetLayerName(track.GetLayer()))
        entries = [e for e in models[name]['layer_entries'] if e['signal_layer'] == layer]
        if len(entries) != 1 or track.GetWidth() != entries[0]['width']:
            raise ValueError(f'{name}: saved track layer/width differs from its delay profile')


if __name__ == '__main__':
    Path(sys.argv[3]).write_text(json.dumps(profiles(sys.argv[1], json.loads(sys.argv[2]), required=True)))
