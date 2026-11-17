#!/usr/bin/env python3
"""Check declared engine ownership against real paths; never infer full acceptance."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import re


RESPONSIBILITIES = {'state', 'geometry_legality', 'search', 'orchestration', 'rip_restore', 'output'}


def inspect(root, implementation):
    root = Path(root).resolve()
    catalog = json.loads((root / 'docs/routing-requirements.json').read_text())
    contract = json.loads((root / 'docs/engine-ownership.json').read_text())
    source = catalog['sources'][contract['source_id']]
    if not source.get('ref') or not source.get('kind') or not source.get('date'):
        raise ValueError('architectural authority requires an exact source, kind and date')
    requirements = {r['id']: r for r in catalog['requirements']}
    if contract['source_id'] not in requirements[contract['requirement']]['source_ids']:
        raise ValueError('ownership authority is not linked to its requirement')
    rows = contract['implementations'][implementation]['responsibilities']
    if set(rows) != RESPONSIBILITIES:
        raise ValueError('ownership inventory must account for every lifecycle responsibility')
    evidence, failures = [], []
    for responsibility, row in rows.items():
        path = (root / row['path']).resolve()
        if not path.is_relative_to(root):
            raise ValueError('ownership path escapes the selected source tree')
        data = path.read_bytes()
        language = {'.py': 'Python', '.rs': 'Rust'}.get(path.suffix)
        if language == 'Python':
            symbols = {n.name for n in ast.walk(ast.parse(data))
                       if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))}
        elif language == 'Rust':
            # This checks a declaration locator, not Rust call-graph semantics.
            symbols = set(re.findall(r'\b(?:fn|struct|enum)\s+(\w+)', data.decode()))
        else:
            raise ValueError(f'unrecognized implementation language: {row["path"]}')
        if row['symbol'] not in symbols:
            raise ValueError(f'missing declared symbol: {row["path"]}:{row["symbol"]}')
        evidence.append({'responsibility': responsibility, **row, 'language': language,
                         'sha256': hashlib.sha256(data).hexdigest()})
        if language != contract['required_language']:
            failures.append(f'{responsibility} is {language}, requires {contract["required_language"]}')
    exceptions = []
    for exception in contract['exceptions']:
        if exception['source_id'] not in catalog['sources']:
            raise ValueError('an exception lacks source authority')
        path = (root / exception['path']).resolve()
        if not path.is_relative_to(root):
            raise ValueError('exception path escapes the selected source tree')
        exceptions.append({**exception, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    return {'schema': 'kicad-route/ownership-check/1', 'implementation': implementation,
            'requirement': contract['requirement'], 'authority': source,
            'inventory_passed': not failures, 'failures': failures, 'paths': evidence,
            'exceptions': exceptions, 'runtime_boundary': 'NOT_EVALUATED',
            'architecture_acceptance': 'NOT_EVALUATED', 'outcome_fitness': 'NOT_EVALUATED',
            'limits': contract['limitations']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--implementation', choices=['current-production', 'rust-candidate'], required=True)
    args = parser.parse_args()
    result = inspect(args.root, args.implementation)
    print(json.dumps(result, indent=2))
    return 0 if result['inventory_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
