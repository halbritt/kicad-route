"""Explicit series-part chains, measured on final native terminal paths."""
import argparse
from decimal import Decimal
import json
import math
from pathlib import Path
import re
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kicad_route import common, length_groups, netnames


def normalize(spec):
    if not isinstance(spec, dict) or set(spec) != {'chains', 'tolerance_mm', 'amplitude_mm'}:
        raise ValueError('AC matching requires chains, tolerance_mm and amplitude_mm')
    chains = spec['chains']
    if not isinstance(chains, list) or not chains:
        raise ValueError('AC matching requires explicit series-part reference chains')
    seen = set()
    for refs in chains:
        if (not isinstance(refs, list) or len(refs) < 2
                or not all(isinstance(ref, str) and ref for ref in refs)):
            raise ValueError('each AC chain needs at least two explicit series-part references')
        if len(set(refs)) != len(refs) or set(refs) & seen:
            raise ValueError('AC series-part references must not repeat or belong to multiple chains')
        seen.update(refs)
    for key in ('tolerance_mm', 'amplitude_mm'):
        value = spec[key]
        if type(value) not in (int, float) or not math.isfinite(value) or value < .000001:
            raise ValueError('AC matching controls must be finite and positive')
    return {**spec, 'chains': [list(refs) for refs in chains]}


def settings(args):
    chains = getattr(args, 'ac_couple_chain', None)
    tolerance = getattr(args, 'ac_match_tolerance', None)
    amplitude = getattr(args, 'ac_meander_amplitude', None)
    if chains is None:
        if tolerance is not None or amplitude is not None:
            raise ValueError('AC matching controls require --ac-couple-chain-json')
        return None
    if args.backend != 'builtin' or getattr(args, 'diff_pairs', None) != 'auto':
        raise ValueError('AC matching requires the builtin engine and --diff-pairs auto')
    return normalize(dict(chains=chains, tolerance_mm=.1 if tolerance is None else tolerance,
                          amplitude_mm=1. if amplitude is None else amplitude))


def resolve(board, facts, pairs, spec):
    """Prove a unique symmetric chain before treating separate nets as a path."""
    import pcbnew as k
    native = k.LoadBoard(str(board))
    by_ref = {}
    for fp in native.GetFootprints():
        ref = fp.GetReference()
        if ref in by_ref:
            raise ValueError(f'AC matching cannot identify duplicate footprint reference {ref!r}')
        by_ref[ref] = fp
    members = {}
    for index, pair in enumerate(pairs):
        for polarity in ('p', 'n'):
            name = pair[polarity]
            if name in members:
                raise ValueError('AC matching has ambiguous pair membership')
            members[name] = (index, polarity)
    result, used_pairs = [], set()
    copper = list(native.GetEnabledLayers().CuStack())
    for refs in spec['chains']:
        edges = {}
        for ref in refs:
            fp = by_ref.get(ref)
            if fp is None or fp.GetAttributes() & k.FP_DNP:
                raise ValueError(f'AC series part {ref!r} is missing or DNP')
            pads = [pad for pad in fp.Pads() if any(pad.IsOnLayer(layer) for layer in copper)]
            if len(pads) != 2:
                raise ValueError(f'AC series part {ref!r} needs exactly two native copper pads')
            names = [str(pad.GetNetname()) for pad in pads]
            if any(name not in members for name in names):
                raise ValueError(f'AC series part {ref!r} has a net outside selected complete pairs')
            (a, pa), (b, pb) = [members[name] for name in names]
            if a == b or pa != pb:
                raise ValueError(f'AC series part {ref!r} bridges the same pair or opposite polarities')
            edge = edges.setdefault(tuple(sorted((a, b))), {})
            if pa in edge:
                raise ValueError(f'AC chain has duplicate parallel bridges at {ref!r}')
            edge[pa] = ref
        if any(set(edge) != {'p', 'n'} for edge in edges.values()):
            raise ValueError('AC chain has asymmetric P/N series bridges')
        adjacency = {}
        for a, b in edges:
            adjacency.setdefault(a, set()).add(b)
            adjacency.setdefault(b, set()).add(a)
        if (len(adjacency) < 2 or len(edges) != len(adjacency) - 1
                or any(len(neighbors) > 2 for neighbors in adjacency.values())):
            raise ValueError('AC bridges must form one nonbranching, acyclic chain')
        reached, pending = set(), [next(iter(adjacency))]
        while pending:
            node = pending.pop()
            if node not in reached:
                reached.add(node)
                pending.extend(adjacency[node] - reached)
        if reached != set(adjacency) or reached & used_pairs:
            raise ValueError('AC chain is disconnected or shares pair members with another chain')
        used_pairs.update(reached)
        p_names = [pairs[i]['p'] for i in sorted(reached)]
        n_names = [pairs[i]['n'] for i in sorted(reached)]
        names = set(p_names + n_names)
        for ref, fp in by_ref.items():
            if ref in refs or fp.GetAttributes() & k.FP_DNP:
                continue
            pads = [pad for pad in fp.Pads() if any(pad.IsOnLayer(layer) for layer in copper)]
            nets = [str(pad.GetNetname()) for pad in pads]
            if len(nets) != 2 or not names.intersection(nets):
                continue
            # Two-pin P/N terminals are endpoints. A further two-pad series
            # component would silently truncate the measured signal path.
            if not all(name in members for name in nets) or members[nets[0]][0] != members[nets[1]][0]:
                raise ValueError(f'AC chain has an unlisted two-pad continuation at {ref!r}')
        path_spec = dict(groups=[p_names + n_names], tolerance_mm=spec['tolerance_mm'],
                         amplitude_mm=spec['amplitude_mm'], spacing_widths=2.)
        paths, = length_groups.paths(path_spec, facts, set(members), pairs)
        result.append(dict(bridges=sorted(refs), p_nets=p_names, n_nets=n_names, paths=paths))
    return result


def run(board, output, spec, pairs, api, *, preflight=False):
    request = output.with_suffix('.request.json')
    common.write_json(request, dict(requirement=spec, pairs=pairs))
    argv = [api.pcbnew_python(), '-B', Path(__file__), board, request, output]
    if preflight:
        argv.append('--preflight')
    rec = common.run_cmd(argv, board.parent, output.with_suffix('.log'), timeout=600)
    report = common.load_json(output)
    if rec['exit_code'] not in (0, 2) or not isinstance(report, dict):
        raise ValueError(f'native AC matching check failed (see {rec["log"]})')
    return report


def check(board, output, spec, pairs, *, preflight=False):
    from kicad_route import cli
    spec = normalize(spec)
    board = board.resolve()
    root = output.with_suffix('.native').resolve()
    if root == board.parent or board.parent in root.parents:
        raise ValueError('AC verification directory must be outside the input project')
    root.mkdir(parents=True)
    common.set_out(root / 'tools')
    original_sha = common.sha256(board)
    facts, info = cli.board_facts(board, root / 'facts.json')
    if facts is None:
        raise ValueError(f'AC native terminal inventory failed (see {info["log"]})')
    if pairs is None:
        native_pairs, info = cli.diff_pairs.probe(board, root / 'pair-facts.json', cli)
        if native_pairs is None:
            raise ValueError(f'AC native pair inventory failed (see {info["log"]})')
        pairs = cli.diff_pairs.detect(native_pairs, set(facts['nets']))
    chains = resolve(board, facts, pairs, spec)
    sources = {path.name: common.sha256(path) for path in
               (board, board.with_suffix('.kicad_pro'), board.with_suffix('.kicad_dru')) if path.is_file()}
    report = dict(status='PASS', board_sha256=original_sha, source_sha256=sources, requirement=spec, chains=chains,
                  measurement='Sum of independently measured native fromTo copper paths; printed rounding bounded',
                  limits='Populated two-pad series parts and two-terminal segments; no component internal delay or SI claim')
    if preflight:
        return report
    project = root / 'project'
    cli.copy_project(board.parent, project)
    copied = project / board.name
    rules = copied.with_suffix('.kicad_dru')
    original = rules.read_text() if rules.exists() else '(version 1)\n'
    all_paths = [path for chain in chains for path in chain['paths']]
    probes = []
    for index, path in enumerate(all_paths):
        condition = f"A.fromTo('{path['start']}', '{path['end']}')"
        probes.append(f'(rule "kicad-route AC path {index}" (condition {json.dumps(condition)}) '
                      '(constraint length (max 0mm)))')
    rules.write_text(original + '\n' + '\n'.join(probes) + '\n')
    native, meta = cli.kicad_drc(copied, root, 'paths', False)
    if native is None:
        raise ValueError(f'native AC path check unavailable (see {meta})')
    values, defects = {}, []
    if common.sha256(copied) != original_sha:
        defects.append('native AC measurement changed board bytes')
    for finding in native['violations']:
        match = re.search(r"rule 'kicad-route AC path (\d+)'", finding.get('description', ''))
        if match is None or finding['type'] != 'length_out_of_range':
            continue
        index = int(match[1])
        amount = re.search(r'actual (\d+\.\d+) mm', finding['description'])
        identified = {name for item in finding.get('items', [])
                      for name in netnames.nets_in_description(item.get('description', ''), set(facts['nets']))}
        if (index >= len(all_paths) or index in values or amount is None
                or identified != {all_paths[index]['net']}):
            defects.append('ambiguous native AC path measurement')
            continue
        value = Decimal(amount[1])
        values[index] = (value, Decimal(1).scaleb(value.as_tuple().exponent) / 2)
    if set(values) != set(range(len(all_paths))):
        defects.append('native AC measurement coverage differs from required paths')
    if native['unconnected_items']:
        defects.append('native board has unconnected items')
    if 'length_out_of_range' in {row.get('key') for row in native.get('ignored_checks', [])}:
        defects.append('required native AC length measurement is ignored')
    if not defects:
        offset = 0
        for chain in chains:
            measurements = {path['net']: values[offset + i] for i, path in enumerate(chain['paths'])}
            p_length = sum(measurements[name][0] for name in chain['p_nets'])
            n_length = sum(measurements[name][0] for name in chain['n_nets'])
            bound = sum(row[1] for row in measurements.values())
            skew = abs(p_length - n_length)
            passed = skew + bound <= Decimal(str(spec['tolerance_mm']))
            chain.update(status='PASS' if passed else 'FAIL', p_length_mm=float(p_length),
                         n_length_mm=float(n_length), skew_mm=float(skew), rounding_bound_mm=float(bound),
                         measurements_mm={name: float(row[0]) for name, row in measurements.items()})
            if not passed:
                defects.append('final native composed path skew cannot meet the requested bound')
            offset += len(chain['paths'])
    if any(not (board.parent / name).is_file() or common.sha256(board.parent / name) != digest
           for name, digest in sources.items()):
        defects.append('native design changed during AC verification')
    report.update(status='FAIL' if defects else 'PASS', defects=defects, native=meta)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('board', type=Path)
    parser.add_argument('request', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--preflight', action='store_true')
    args = parser.parse_args()
    try:
        request = common.load_json(args.request)
        report = check(args.board, args.output, request['requirement'], request.get('pairs'), preflight=args.preflight)
    except ValueError as exc:
        report = dict(status='FAIL', board_sha256=common.sha256(args.board), defects=[str(exc)])
    common.write_json(args.output, report)
    return 0 if report['status'] == 'PASS' else 2


if __name__ == '__main__':
    sys.exit(main())
