"""Synthetic artifact transport tests: no SSH, Docker, native jobs or large caches."""
import importlib.util
import json
from pathlib import Path

import pytest

spec=importlib.util.spec_from_file_location('filtered_export',Path(__file__).with_name('export-archon-evidence.py'))
f=importlib.util.module_from_spec(spec);spec.loader.exec_module(f)


def put(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value) if isinstance(value,dict) else value)
    return path


@pytest.fixture
def setup(tmp_path):
    root=tmp_path/'evidence';root.mkdir()
    helper=put(root/'export-archon-evidence.py','immutable transport helper')
    indexer=put(root/'index-archon-evidence.py','original frozen indexer')
    before=put(root/'before.json',{'host':'archon'})
    put(root/'after.json',{'host':'archon'})
    put(root/'metadata/matrix.json',{'status':'PASS'})
    home=root/'preparation-reference/out/.home'
    put(home/'.cache/uv/download.bin','downloadcache')
    raw=put(root/'board/reference-0/run/result.json',{'exit_code':0})
    put(root/'board/reference-0/time.json',{'wall_s':1})
    put(root/'board/reference-0/stdout.log','native stdout')
    put(root/'board/reference-0/stderr.log','native stderr')
    put(root/'board/reference-0/run/work/native-output.bin','native work')
    put(root/'board/reference-out/.cache/baseline/gen/generation.json',{'generation':'gen'})
    put(root/'board/reference-out/attempts.jsonl','{"native":"ledger"}\n')
    put(root/'source-boards/a.kicad_pcb','source board')
    put(root/'preparation-reference/out/update/build.log','cargo build log')
    external=put(tmp_path/'grid_router.so','native audit bytes')
    protocol={'metadata_output':str(root/'metadata'),'host_after_attestation_path':str(root/'after.json'),
              'host_before_attestation':{'path':str(before),'sha256':f.digest(before)},
              'retention_index':{'script':str(indexer)},'hash_prerequisites':[
                  {'path':str(indexer),'sha256':f.digest(indexer)},
                  {'path':str(raw),'sha256':f.digest(raw)},
                  {'path':str(external),'sha256':f.digest(external)}]}
    profile=put(root/'archon-protocol.json',protocol)
    policy={'profile_sha256':f.digest(profile),'original_indexer_sha256':f.digest(indexer),
            'allowed_omission_relative_prefixes':['preparation-reference/out/.home']}
    policyfile=put(root/'archon-export-policy.json',policy)
    return root,root/'archon-filtered-export',profile,policyfile


def test_transport_retains_raw_work_baseline_ledger_boards_logs_and_external_module(setup):
    root,output,profile,policy=setup
    receipt=f.create_export(*setup)
    index=f.read(output/'archon-evidence-index.json')
    mapped={row['source']:row for row in index['files']}
    for original in [root/'board/reference-0/run/result.json',root/'board/reference-0/time.json',
                     root/'board/reference-0/stdout.log',root/'board/reference-0/stderr.log',
                     root/'board/reference-0/run/work/native-output.bin',
                     root/'board/reference-out/.cache/baseline/gen/generation.json',
                     root/'board/reference-out/attempts.jsonl',root/'source-boards/a.kicad_pcb',
                     root/'preparation-reference/out/update/build.log',root.parent/'grid_router.so']:
        row=mapped[str(original)]
        assert f.digest(output/row['retained'])==row['sha256']==f.digest(original)
    assert not (output/'preparation-reference/out/.home').exists()
    omitted=f.read(output/'transport-omissions.json')
    assert omitted['totals']=={'files':1,'bytes':13,'symlinks':0}
    assert receipt['sha256']==f.digest(output/'archon-evidence-index.json')
    assert index['transport']['original_indexer_executed'] is False
    for key in ['helper','policy','omissions','original_indexer']:
        claim=index['transport'][key];assert mapped[claim['source']]['sha256']==claim['sha256']
    assert (root/'preparation-reference/out/.home/.cache/uv/download.bin').read_text()=='downloadcache'


def test_live_matrix_refused_before_traversing_or_writing(setup,monkeypatch):
    root,output,profile,policy=setup
    put(root/'metadata/matrix.json',{'status':'RUNNING'})
    monkeypatch.setattr(f,'omission_inventory',lambda path:pytest.fail('live traversal'))
    with pytest.raises(ValueError,match='terminal'):f.create_export(*setup)
    assert not output.exists()


def test_retained_json_regular_file_reference_into_home_refuses_atomic_export(setup):
    root,output,_,_=setup
    put(root/'extra.json',{'required':str(root/'preparation-reference/out/.home/.cache/uv/download.bin')})
    with pytest.raises(ValueError,match='locator would be omitted'):f.create_export(*setup)
    assert not output.exists() and not list(root.glob('.archon-filtered-export-*.writing'))


def test_home_directory_metadata_is_allowed_but_unknown_home_is_retained(setup):
    root,output,_,_=setup
    put(root/'extra.json',{'HOME':str(root/'preparation-reference/out/.home')})
    put(root/'unknown/.home/keep.txt','not an allowed omission')
    f.create_export(*setup)
    assert (output/'unknown/.home/keep.txt').is_file()


def test_explicit_prerequisite_inside_omission_is_rejected(setup):
    root,output,profile,policy=setup
    value=f.read(profile);p=root/'preparation-reference/out/.home/.cache/uv/download.bin'
    value['hash_prerequisites'].append({'path':str(p),'sha256':f.digest(p)});put(profile,value)
    value=f.read(policy);value['profile_sha256']=f.digest(profile);put(policy,value)
    with pytest.raises(ValueError,match='overlaps'):f.create_export(*setup)
    assert not output.exists()


@pytest.mark.parametrize('bad',['../.home','/absolute/.home','board/reference-out/.cache'])
def test_policy_cannot_escape_or_omit_baseline_cache(setup,bad):
    _,output,_,policy=setup
    value=f.read(policy);value['allowed_omission_relative_prefixes']=[bad];put(policy,value)
    with pytest.raises(ValueError,match='HOME prefixes'):f.create_export(*setup)
    assert not output.exists()


def test_omission_symlink_cannot_hide_external_required_files(setup):
    root,output,_,policy=setup
    (root/'linked.home').symlink_to(root/'preparation-reference/out/.home',target_is_directory=True)
    value=f.read(policy);value['allowed_omission_relative_prefixes']=['linked.home/.home'];put(policy,value)
    with pytest.raises(ValueError):f.create_export(*setup)
    assert not output.exists()


def test_frozen_indexer_drift_rejected_and_missing_after_refuses(setup):
    root,output,_,_=setup
    (root/'after.json').unlink()
    with pytest.raises(ValueError,match='Host-after'):f.create_export(*setup)
    put(root/'after.json',{})
    put(root/'index-archon-evidence.py','changed')
    with pytest.raises(ValueError,match='indexer provenance'):f.create_export(*setup)
    assert not output.exists()
