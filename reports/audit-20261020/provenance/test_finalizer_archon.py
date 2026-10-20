"""Bounded finalizer bridge checks; no native jobs or final acceptance files."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
sys.dont_write_bytecode=True
Q=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('f',Q/'finalize-acceptance.py');f=importlib.util.module_from_spec(spec);spec.loader.exec_module(f)


def main():
    results=[]
    arguments=('8896',0,Q/'archon-import/archon-evidence-index.json',
        '19b5fca94da3fdf939bd13259fa28829573906acbae5fd694441e1b49669eb4e',Q/'archon-final-verification.json',
        '8c5e7b8fa6983546441cdd21ac005f9feccda84d1a2fec6b2e4db365668a82b3')
    matrix=f.verify_final_matrix(*arguments)
    assert matrix['host']=='archon' and matrix['complete_qualified_attempts']==48 and len(matrix['slots'])==4
    results.append('PASS actual immutable Archon proof/index bridge,48native attempts')
    cache,ledger=f.observe_current_cache_and_ledger(matrix)
    assert cache['cache_population_observed_payloads']==3 and all(cache['payload_hashes_verified'].values())
    assert ledger['matches'] and ledger['matching_ledger_rows']==6
    results.append('PASS actual retained Archon baseline3payloads/grader/schema/native identity and executed-build ledger reconstruction')
    def reject(name,callback):
        try:callback()
        except RuntimeError:results.append('PASS rejection: '+name)
        else:raise AssertionError('not rejected: '+name)
    wrong=list(arguments);wrong[3]='wrong'
    reject('wrong externally observed indexSHA',lambda:f.verify_final_matrix(*wrong))
    wrong=list(arguments);wrong[5]='wrong'
    reject('wrong independent verificationSHA',lambda:f.verify_final_matrix(*wrong))
    draft=f.read(Q/'final-acceptance-draft.json');f.verify_original_clauses(draft)
    altered=copy.deepcopy(draft);altered['tickets'][0]['criteria'][0]['original_acceptance']+='edited'
    reject('changed original acceptance string',lambda:f.verify_original_clauses(altered))
    comments=f.read(Q/'closure-comments-draft.json')
    runtime={'landed_git_head':'synthetic-formatting-only','durable_receipt':f.REPORT+'/runtime/synthetic/receipt.json',
        'durable_doctor':f.REPORT+'/runtime/synthetic/doctor/doctor/doctor.json'}
    rendered=f.final_comments(comments,matrix,runtime,741)
    assert len(rendered['comments'])==14
    for ident,row in rendered['comments'].items():
        assert row['status']=='ready_after_durable_report_commit' and row['acceptance']=='met'
        assert 'draft_acceptance' not in row
        assert all(row['body'].lower().find(marker)<0 for marker in ('pending','draft','parent-owned','quiet-admission','cpu-rate'))
        assert f.REPORT+'/final-acceptance.json' in row['intended_proof_paths']
        assert '686 passed and 2 failed' in row['body'] and '741 current test nodes' in row['body']
        assert all('/performance/final/' not in path for path in row['intended_proof_paths'])
    assert all(any('/performance/archon/'+board+'-archon/comparison.json' in path for path in
        rendered['comments']['KICADROUTE-73']['intended_proof_paths']) for board in ('diff-pair','four-layer','multipoint','route-demo'))
    results.append('PASS synthetic formatting:all14item bodies/proof paths removeobsolete profiles and retain multi-invocation coverage honesty')
    assert rendered['comments']['KICADROUTE-81']['intended_proof_paths'][0].endswith('/performance/archon/multipoint-archon/comparison.json')
    results.append('PASS regression:ticket81 stale quiet-profile proof path replaced with actual Archon path')
    with tempfile.TemporaryDirectory(dir=Q,prefix='runtime-gate-check-') as folder:
        root=Path(folder);receipt=root/'receipt.json';doctor=root/'doctor.json'
        receipt.write_text(json.dumps({'status':'FAIL','gates':[{'passed':False}]}));doctor.write_text(json.dumps({'status':'FAIL'}))
        reject('failed default-runtime proof cannot finalize',lambda:f.verify_landed_runtime(receipt,doctor,draft))
    reject('local CPU study cannot finalize73',lambda:f.finalize(draft,comments,{'host':'proximal'},runtime))
    print('Finalizer SHA256: '+hashlib.sha256((Q/'finalize-acceptance.py').read_bytes()).hexdigest())
    print('\n'.join(results));print(f'{len(results)} bounded finalizer bridge checks PASS; no native jobs or final acceptance files emitted.')


if __name__=='__main__':main()
