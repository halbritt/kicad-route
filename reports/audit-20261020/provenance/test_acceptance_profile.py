"""Synthetic portability/raw-gate checks, never Archon routing qualification."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
sys.dont_write_bytecode=True
Q=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('p',Q/'acceptance_profile.py');p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)


def main():
    checks=[]
    def reject(name,callback):
        try:callback()
        except (p.EvidenceError,KeyError,ValueError):checks.append('PASS rejection: '+name)
        else:raise AssertionError('not rejected: '+name)
    with tempfile.TemporaryDirectory(dir=Q,prefix='portable-verifier-check-') as folder:
        root=Path(folder);files=[]
        def add(source,value,binary=False):
            name=f'file-{len(files)}'+('.py' if binary else '.json');target=root/name
            target.write_bytes(value if binary else (json.dumps(value)+'\n').encode())
            files.append({'source':source,'retained':name,'sha256':p.digest(target)})
        driver=(Q/'cpu-rate-prerequisite-snapshots/00-compare_router.py').read_bytes()
        add('/remote/frozen/compare.py',driver,True)
        board={'name':'diff-pair','project':'/remote/input/pair','args':['--budget','180'],'output':'/remote/evidence/diff-pair'}
        report={'schema':'kicad-route/engine-comparison/2','project':board['project'],'args':board['args'],
                'reference':[],'candidate':[],'warmup':{},'mode':'migration-equivalence','cpu_affinity':list(range(16))}
        temporary=root/'temporary-driver.py';temporary.write_bytes(driver)
        module=type(sys)('frozen_fixture_compare');exec(compile(driver,str(temporary),'exec'),module.__dict__);temporary.unlink()
        for side in ('reference','candidate'):
            for index in range(6):
                original=f"{board['output']}/{side}-{index}/run/result.json"
                result={'schema':'kicad-route/result/2','exit_code':0,'pairs':[],'input':{'fingerprint':'same-input'},
                        'process':{'status':'PASS','commands':[{}]},
                        'routing':{'status':'COMPLETE','unconnected_total':0,'unrouted_nets':[]},
                        'validation':{'status':'PASS','preservation':{'board':'PASS'},'drc':{'new_violations':{'count':0}}},
                        'evidence':{'reference_files':{},'input_fingerprint':'same-input','candidate_copper_signature':{'sha256':'same-copper'}}}
                sample={'result':original,'exit_code':0,'wall_s':10,'user_s':8,'system_s':1,'max_rss_kib':100,
                        'process_tree_peak_rss_kib':110,'quality':module.quality(result)}
                add(original,result);add(str(Path(original).parent.parent/'time.json'),
                                        {key:sample[key] for key in ('wall_s','user_s','system_s','max_rss_kib')})
                if index==0:report['warmup'][side]=sample
                else:report[side].append(sample)
        raw=copy.deepcopy(report);report['verdict']=module.compare(report['reference'],report['candidate'],report['warmup'],report['mode'])
        add(board['output']+'/samples.json',raw);add(board['output']+'/comparison.json',report)
        def index():
            path=root/'index.json';path.write_text(json.dumps({'host':'archon','files':files}));return path
        def store():
            path=index();return p.EvidenceStore(path,p.digest(path))
        evidence=store();compare=evidence.load_comparison('/remote/frozen/compare.py',hashlib.sha256(driver).hexdigest())
        proof=p.verify_board_gates(evidence,board,compare,list(range(16)))
        assert proof['native_attempts']==12
        checks.append('PASS synthetic remote12attempt raw/native/wall/dualRSS gates without opening remote paths')
        p.validate_declared_profile('archon',list(range(16)),5,1,['diff-pair','four-layer','multipoint','route-demo'],1.1)
        checks.append('PASS declared16CPU Archon profile; no local12CPU assumption')
        reject('unindexed existing local file is never fallback',lambda:evidence.resolve(__file__))
        reject('wrong final host',lambda:p.EvidenceStore(index(),p.digest(index()),required_host='other'))
        reject('wrong index transport hash',lambda:p.EvidenceStore(index(),'wrong'))
        reject('local final host declaration',lambda:p.validate_declared_profile('proximal',[0],5,1,['diff-pair','four-layer','multipoint','route-demo'],1.1))
        reject('relaxed1.11gate',lambda:p.validate_declared_profile('archon',[0],5,1,['diff-pair','four-layer','multipoint','route-demo'],1.11))
        first=files[0];path=root/first['retained'];saved=path.read_bytes();path.write_bytes(saved+b'\n')
        reject('changed captured bytes',lambda:store());path.write_bytes(saved)
        files.append(copy.deepcopy(files[0]));reject('duplicate original/retained identity',lambda:store());files.pop()
        alias=copy.deepcopy(files[0]);alias['source']='/remote/same-bytes/compare.py';files.append(alias)
        aliased=store();assert aliased.resolve(alias['source'])==aliased.resolve(files[0]['source'])
        checks.append('PASS byte-identical retained aliases preserve distinct original identities');files.pop()
        saved=first['retained'];first['retained']='../escape';reject('retained path escape',lambda:store());first['retained']=saved
        saved=first['source'];first['source']='/remote/../escape';reject('original path traversal',lambda:store());first['source']=saved
        candidate=report['candidate'][0];raw_time=next(row for row in files if row['source']==str(Path(candidate['result']).parent.parent/'time.json'))
        path=root/raw_time['retained'];saved=path.read_bytes();value=json.loads(saved);value['wall_s']=20;path.write_text(json.dumps(value));raw_time['sha256']=p.digest(path)
        reject('summary/raw GNU timing mismatch despite valid transfer hash',lambda:p.verify_board_gates(store(),board,compare,list(range(16))))
        path.write_bytes(saved);raw_time['sha256']=p.digest(path)
        removed=files.pop(1);reject('dropped native attempt',lambda:p.verify_board_gates(store(),board,compare,list(range(16))));files.insert(1,removed)
        add(board['output']+'/candidate-extra/run/result.json',result)
        reject('extra native attempt',lambda:p.verify_board_gates(store(),board,compare,list(range(16))));files.pop()
        reject('different declared affinity',lambda:p.verify_board_gates(store(),board,compare,[0]))
        saved=board['args'];board['args']=['--budget','181'];reject('different board settings',lambda:p.verify_board_gates(store(),board,compare,list(range(16))));board['args']=saved
        reject('wrong frozen adjudicator SHA',lambda:store().load_comparison('/remote/frozen/compare.py','wrong'))
    print('Abstraction SHA256: '+p.digest(Q/'acceptance_profile.py'));print('\n'.join(checks));print(f'{len(checks)} bounded portable-profile checks PASS; synthetic fixtures only, no remote or native jobs.')


if __name__=='__main__':main()
