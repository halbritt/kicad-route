"""Architecture declarations must name real code and retain incomplete evidence."""
import importlib.util
import json
from pathlib import Path
import shutil

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('ownership', ROOT / 'scripts/check_engine_ownership.py')
ownership = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ownership)


def test_current_production_cannot_pass_the_rust_lifecycle_requirement():
    result = ownership.inspect(ROOT, 'current-production')
    assert not result['inventory_passed']
    assert len(result['failures']) == 5
    assert next(r for r in result['paths'] if r['responsibility'] == 'search')['language'] == 'Rust'
    assert 'orchestration is Python, requires Rust' in result['failures']


def test_candidate_inventory_allows_the_sourced_native_seam_without_claiming_acceptance():
    result = ownership.inspect(ROOT, 'rust-candidate')
    assert result['inventory_passed']
    assert {r['responsibility'] for r in result['paths']} == ownership.RESPONSIBILITIES
    assert result['exceptions'][0]['path'] == 'tools/rust_engine_kicad.py'
    assert result['exceptions'][0]['language'] == 'Python'
    assert result['authority']['kind'] == 'owner-relay'
    assert result['authority']['original_user_record'] is None
    assert result['authority']['provenance_gap']
    for key in ('runtime_boundary', 'architecture_acceptance', 'outcome_fitness'):
        assert result[key] == 'NOT_EVALUATED'


@pytest.mark.parametrize('defect', ['missing_path', 'missing_symbol', 'missing_responsibility', 'missing_source'])
def test_unverifiable_inventory_never_passes(tmp_path, defect):
    (tmp_path / 'docs').mkdir()
    for name in ('engine-ownership.json', 'routing-requirements.json'):
        shutil.copyfile(ROOT / 'docs' / name, tmp_path / 'docs' / name)
    contract_path = tmp_path / 'docs/engine-ownership.json'
    contract = json.loads(contract_path.read_text())
    rows = contract['implementations']['rust-candidate']['responsibilities']
    for row in [*rows.values(), *contract['exceptions']]:
        target = tmp_path / row['path']
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / row['path'], target)
    if defect == 'missing_path':
        (tmp_path / rows['state']['path']).unlink()
    elif defect == 'missing_symbol':
        rows['state']['symbol'] = 'InventedStateOwner'
    elif defect == 'missing_responsibility':
        del rows['rip_restore']
    else:
        contract['source_id'] = 'invented-authority'
    contract_path.write_text(json.dumps(contract))
    with pytest.raises((ValueError, KeyError, FileNotFoundError)):
        ownership.inspect(tmp_path, 'rust-candidate')
