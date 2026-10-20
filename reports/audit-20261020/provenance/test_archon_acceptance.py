"""Bounded Archon verifier checks. Every native/host outcome below is synthetic."""
import copy
import datetime as dt
import hashlib
import importlib.util
import json
from pathlib import Path, PurePosixPath
import sys
import tempfile
import types
sys.dont_write_bytecode=True
Q=Path(__file__).resolve().parent
sys.path.insert(0,str(Q))
import archon_acceptance as a
from acceptance_profile import EvidenceError


class FixtureStore:
    def __init__(self,index):self.data={};self.index_path=index;self.index={}
    def add(self,path,value,binary=False):
        self.data[path]=value if binary else (json.dumps(value,sort_keys=True)+'\n').encode()
    def read(self,path):return json.loads(self.data[path])
    def artifact(self,path):
        return {'source':path,'retained':'synthetic/'+hashlib.sha256(path.encode()).hexdigest(),
                'sha256':hashlib.sha256(self.data[path]).hexdigest()}
    def descendants(self,root,pattern=None):
        return {p for p in self.data if PurePosixPath(p).is_relative_to(root) and PurePosixPath(p)!=PurePosixPath(root) and
                (pattern is None or PurePosixPath(p).relative_to(root).match(pattern))}
    def load_comparison(self,path,expected):
        assert self.artifact(path)['sha256']==expected
        module=types.ModuleType('synthetic_frozen_compare');exec(compile(self.data[path],path,'exec'),module.__dict__);return module


def fixture(index):
    store=FixtureStore(index);protocol=json.loads((Q/'archon-protocol.json').read_text())
    setup=Q/'archon-setup/retained-setup/evidence'
    host=json.loads((setup/'before-host-attestation.json').read_text())
    tools=json.loads((setup/'container-tools.json').read_text());store.add('/evidence/container-tools.json',tools)
    protocol['hash_prerequisites']=[]
    def prerequisite(path,value,binary=False):
        store.add(path,value,binary);protocol['hash_prerequisites'].append({'path':path,'sha256':store.artifact(path)['sha256']})
    driver=(Q/'cpu-rate-prerequisite-snapshots/00-compare_router.py').read_bytes()
    prerequisite(protocol['driver'],driver,True);protocol['driver_sha256']=store.artifact(protocol['driver'])['sha256']
    prerequisite(protocol['harness'],b'# synthetic frozen fixture harness\n',True)
    prerequisite(protocol['retention_index']['script'],b'# synthetic frozen original indexer\n',True)
    protocol['harness_sha256']=store.artifact(protocol['harness'])['sha256']
    for side in ('reference','candidate'):
        prerequisite(protocol[side+'_cli'],b'# synthetic frozen CLI\n',True)
        manifest=json.loads((setup/f'preparation-{side}/manifest.json').read_text())
        runtime=protocol['target_runtime'][side];module=b'synthetic extension '+side.encode()
        prerequisite(runtime['module'],module,True);runtime['module_sha256']=store.artifact(runtime['module'])['sha256']
        manifest['krt']['extension_module']['sha256']=runtime['module_sha256']
        path=protocol[side+'_state']+'/manifest.json';prerequisite(path,manifest)
        runtime['manifest_sha256']=store.artifact(path)['sha256']
        prep=json.loads((setup/f'preparation-{side}/preparation.json').read_text());prep['runtime']=copy.deepcopy(runtime)
        prerequisite(f'/evidence/preparation-{side}/preparation.json',prep)
    for board in protocol['boards']:
        name=board['name']+'.kicad_pcb';prerequisite(board['project']+'/'+name,b'(synthetic native board)',True)
    before_path=protocol['host_before_attestation']['path'];store.add(before_path,host)
    protocol['host_before_attestation']['sha256']=store.artifact(before_path)['sha256']
    after=copy.deepcopy(host);after['observed_utc']='2026-10-20T23:10:00+00:00';store.add(protocol['host_after_attestation_path'],after)
    control=host['ancestors'][0]['controls'];stats=control['cpu.stat']
    initial={'utc':'2026-10-20T23:00:00Z','monotonic_s':1,'proc_self_cgroup':'0::/\n','group':'/',
             'mount':'/sys/fs/cgroup','leaf':'/sys/fs/cgroup','cpu_affinity':list(range(16)),'nice':0,'scheduler':0,
             'ancestors':[{'path':'/sys/fs/cgroup','controls':{k:{'text':control[k]+'\n'} for k in a.CPU_CONTROLS},
                           'observations':{'cpu.stat':{'text':stats}}}],
             'host_resources':{k:{'text':'synthetic diagnostic only'} for k in ['/proc/stat','/proc/pressure/cpu','/proc/pressure/io','/proc/pressure/memory']}}
    metadata=protocol['metadata_output'];store.add(metadata+'/initial-resources.json',initial)
    compare=store.load_comparison(protocol['driver'],protocol['driver_sha256']);clock=1;rows=[]
    for board in protocol['boards']:
        report={'schema':'kicad-route/engine-comparison/2','project':board['project'],'args':board['args'],
                'reference':[],'candidate':[],'warmup':{},'mode':'migration-equivalence','cpu_affinity':list(range(16)),
                'archon_protocol':metadata+'/protocol.json'}
        for index in range(6):
            for side in (('reference','candidate') if index%2==0 else ('candidate','reference')):
                runtime=protocol['target_runtime'][side]
                backend={'name':'krt','tag':runtime['source']['build'],'version':runtime['source']['version'],
                         'grid_router_version':runtime['source']['version'],'source_sha256':runtime['source']['source_sha256'],
                         'extension_module_sha256':runtime['module_sha256'],'venv_packages_sha256':runtime['packages_sha256']}
                if side=='candidate':backend['venv_python_version']='3.12.3'
                identity={'kicad_route':{'commit':runtime['source']['commit'],'dirty':False},
                          'kicad_cli':dict(tools['kicad_cli'],version=tools['kicad_version']),
                          'pcbnew':{'build_version':tools['pcbnew_build'],'python':'/usr/bin/python3'},
                          'python':{'version':'3.12.3','executable':'/usr/bin/python3'},'backend':backend,
                          'state_manifest_sha256':runtime['manifest_sha256'],'state_manifest_drift':[]}
                emitted={'execution_identity':copy.deepcopy(backend)} if side=='candidate' else {}
                native={'schema':'kicad-route/result/2','exit_code':0,'pairs':[],'input':{'fingerprint':'same-input'},
                        'process':{'status':'PASS','commands':[{}]},'routing':{'status':'COMPLETE','unconnected_total':0,'unrouted_nets':[]},
                        'validation':{'status':'PASS','preservation':{'board':'PASS'},'drc':{'new_violations':{'count':0}}},
                        'evidence':{'reference_files':a.expected_input(protocol,board),'candidate_copper_signature':{'sha256':'same-copper'}},
                        'tool_identity':identity,'backend':emitted}
                result=board['output']+f'/{side}-{index}/run/result.json';store.add(result,native)
                board_path=str(PurePosixPath(result).parent/'candidate/board.kicad_pcb')
                store.add(board_path,b'(synthetic final native candidate)',True)
                native['candidate']={'board':board_path}
                native['validation']['candidate_board_sha256']=store.artifact(board_path)['sha256']
                native['evidence']['candidate_board_sha256']=store.artifact(board_path)['sha256']
                record_path=str(PurePosixPath(result).parent/'attempt.json');store.add(record_path,{'synthetic':'native attempt'})
                native['evidence']['records']={'attempt.json':store.artifact(record_path)['sha256']}
                store.add(result,native)
                sample={'result':result,'exit_code':0,'quality':compare.quality(native),'wall_s':1,'user_s':.5,'system_s':.1,
                        'max_rss_kib':100,'process_tree_peak_rss_kib':110,'memory_sampling':{'samples':10},
                        'tool_identity':identity,'backend':emitted,'reference_files':native['evidence']['reference_files'],
                        'cli_sha256':store.artifact(protocol[side+'_cli'])['sha256']}
                store.add(board['output']+f'/{side}-{index}/time.json',{k:sample[k] for k in ('wall_s','user_s','system_s','max_rss_kib')})
                if index==0:report['warmup'][side]=sample
                else:report[side].append(sample)
                clock+=3;first=copy.deepcopy(initial);last=copy.deepcopy(initial)
                first.update(monotonic_s=clock,utc=(dt.datetime(2026,10,20,23,tzinfo=dt.timezone.utc)+dt.timedelta(seconds=clock)).isoformat())
                last.update(monotonic_s=clock+2,utc=(dt.datetime(2026,10,20,23,tzinfo=dt.timezone.utc)+dt.timedelta(seconds=clock+2)).isoformat())
                members=[{'pid':100,'birth_ticks':1,'gnu_time_root_pid':100,'observed_monotonic_s':clock+1,
                          'status':'observed','group':'/','in_scope':True,'argv':['/usr/bin/time','python3',protocol[side+'_cli']]},
                         {'pid':101,'birth_ticks':2,'gnu_time_root_pid':100,'observed_monotonic_s':clock+1,
                          'status':'observed','group':'/','in_scope':True,'argv':['python3',protocol[side+'_cli']]}]
                label=f"{board['name']}-{side}-{index}"
                store.add(metadata+'/samples/'+label+'.json',{'label':label,'before':first,'after':last,'driver_call_wall_s':1,
                    'membership_monitor':{'sampler_calls':10,'observed_births':2,'members':members,'outside_scope':[],'read_errors':[],'exit_races':[]},'resource_violations':[]})
        store.add(board['output']+'/samples.json',report);report['verdict']=compare.compare(report['reference'],report['candidate'],report['warmup'],report['mode'])
        store.add(board['output']+'/comparison.json',report)
        rows.append({'name':board['name'],'output':board['output'],'status':'PASS','completed_samples':12,'verdict':report['verdict']})
    store.add(a.PROTOCOL,protocol);store.add(metadata+'/protocol.json',protocol)
    protocol_sha=store.artifact(a.PROTOCOL)['sha256']
    store.add(metadata+'/matrix.json',{'schema':'kicad-route/archon-matrix/1','status':'PASS','all_comparisons_passed':True,
        'completed_samples':48,'boards':rows,'resource_violations':[],'protocol_sha256':protocol_sha})
    helper='/evidence/export-archon-evidence.py';policy_path='/evidence/archon-export-policy.json';omission_path='/evidence/archon-filtered-export/transport-omissions.json'
    store.add(helper,b'# synthetic reviewed transport helper',True)
    prefixes=[f'preparation-{side}/out/.home' for side in ('reference','candidate')]
    prefixes += [str(PurePosixPath(b['output']).relative_to('/evidence')/(side+'-out')/'.home') for b in protocol['boards'] for side in ('reference','candidate')]
    indexer=protocol['retention_index']['script'];indexer_sha=store.artifact(indexer)['sha256']
    store.add(policy_path,{'profile_sha256':protocol_sha,'original_indexer_sha256':indexer_sha,'allowed_omission_relative_prefixes':prefixes})
    store.add(omission_path,{'policy_sha256':store.artifact(policy_path)['sha256'],'omissions':[],
        'totals':dict(files=0,bytes=0,symlinks=0),'required_regular_file_json_locators_checked':True,
        'native_results_timing_metadata_boards_work_baselinecache_ledger_buildlogs_retained':True})
    store.index={'transport':{'schema':'kicad-route/filtered-evidence-transport/1','original_indexer_executed':False,
        **{name:{'source':source,'sha256':store.artifact(source)['sha256']} for name,source in
            [('helper',helper),('policy',policy_path),('omissions',omission_path),('original_indexer',indexer)]}}}
    return store,protocol,protocol_sha


def main():
    checks=[]
    with tempfile.TemporaryDirectory(dir=Q,prefix='archon-verifier-check-') as folder:
        index=Path(folder)/'synthetic-index.json';index.write_text('{}')
        store,protocol,protocol_sha=fixture(index)
        old_protocol_sha,old_harness_sha=a.PROTOCOL_SHA,a.HARNESS_SHA
        old_helper_sha,old_policy_sha=a.TRANSPORT_HELPER_SHA,a.TRANSPORT_POLICY_SHA
        a.TRANSPORT_HELPER_SHA=store.index['transport']['helper']['sha256'];a.TRANSPORT_POLICY_SHA=store.index['transport']['policy']['sha256']
        a.PROTOCOL_SHA,a.HARNESS_SHA=protocol_sha,protocol['harness_sha256']
        try:
            proof=a.verify_archon(store,'8896',0)
            assert proof['complete_qualified_attempts']==48 and proof['completed_boards']==4 and proof['resources']['observed_in_scope_births']==96
            checks.append('PASS synthetic complete48native/4board/96birth/resource/tool/input/rawGNU/strict gates')
            pristine=copy.deepcopy(store.data)
            def reject(name,action):
                action()
                try:a.verify_archon(store,'8896',0)
                except (EvidenceError,KeyError,ValueError):checks.append('PASS rejection: '+name)
                else:raise AssertionError('not rejected: '+name)
                finally:store.data=copy.deepcopy(pristine)
            def edit(path,change):
                value=store.read(path);change(value);store.add(path,value)
            meta=protocol['metadata_output'];sample=meta+'/samples/diff-pair-reference-0.json'
            result=protocol['boards'][0]['output']+'/candidate-1/run/result.json'
            reject('nonterminal RUNNING',lambda:edit(meta+'/matrix.json',lambda r:r.update(status='RUNNING')))
            reject('one dropped resource record',lambda:store.data.pop(sample))
            reject('extra resource record',lambda:store.add(meta+'/samples/extra.json',store.read(sample)))
            reject('wrong actual Docker image',lambda:edit(protocol['host_after_attestation_path'],lambda r:r.update(image_id='wrong')))
            reject('host initPID changed',lambda:edit(protocol['host_after_attestation_path'],lambda r:r.update(init_pid=1)))
            reject('finite hidden host quota',lambda:edit(protocol['host_after_attestation_path'],lambda r:r['ancestors'][1]['controls'].update({'cpu.max':'100000 100000'})))
            reject('host-after precedes completion',lambda:edit(protocol['host_after_attestation_path'],lambda r:r.update(observed_utc='2026-10-20T22:00:00+00:00')))
            reject('namespace root escape dotdot',lambda:edit(sample,lambda r:r['membership_monitor']['members'][1].update(group='/../escape')))
            reject('namespace doubled slash',lambda:edit(sample,lambda r:r['membership_monitor']['members'][1].update(group='//child')))
            reject('GNUtime argv cannot substitute actual CLI child',lambda:edit(sample,lambda r:r['membership_monitor']['members'][1].update(argv=['other'])))
            reject('forged empty membership summary',lambda:edit(sample,lambda r:r['membership_monitor']['members'][1].update(status='read-error')))
            reject('missing native actual build',lambda:edit(result,lambda r:r['tool_identity']['backend'].pop('extension_module_sha256')))
            reject('candidate manifest drift',lambda:edit(result,lambda r:r['tool_identity'].update(state_manifest_drift=['changed'])))
            reject('captured interpreter changed',lambda:edit(result,lambda r:r['backend']['execution_identity'].update(venv_python_version='wrong')))
            reject('raw GNU differs from summary',lambda:edit(protocol['boards'][0]['output']+'/candidate-1/time.json',lambda r:r.update(wall_s=2)))
            reject('captured result lost',lambda:store.data.pop(result))
            reject('changed container CPU affinity',lambda:edit(sample,lambda r:r['after'].update(cpu_affinity=[0])))
            reject('postexecution ancestor weight changed',lambda:edit(sample,lambda r:r['after']['ancestors'][0]['controls']['cpu.weight'].update(text='101')))
            reject('RSS/membership sampler mismatch',lambda:edit(sample,lambda r:r['membership_monitor'].update(sampler_calls=9)))
            reject('forged timing clock span',lambda:edit(sample,lambda r:r.update(driver_call_wall_s=3)))
            reject('prepared runtime receipt failed',lambda:edit('/evidence/preparation-candidate/preparation.json',lambda r:r.update(status='FAIL')))
            reject('captured module bytes changed',lambda:store.add(protocol['target_runtime']['candidate']['module'],b'changed',True))
            reject('wrong final validation digest',lambda:edit(result,lambda r:r['validation'].update(candidate_board_sha256='wrong')))
            reject('owned evidence final digest stale',lambda:edit(result,lambda r:r['evidence'].update(candidate_board_sha256='wrong')))
            reject('native grading record missing',lambda:store.data.pop(str(PurePosixPath(result).parent/'attempt.json')))
            reject('native grading record digest changed',lambda:store.add(str(PurePosixPath(result).parent/'attempt.json'),{'edited':'record'}))
            reject('omission policy bytes changed',lambda:edit('/evidence/archon-export-policy.json',lambda r:r['allowed_omission_relative_prefixes'].append('native-work/.home')))
            reject('omission totals changed',lambda:edit('/evidence/archon-filtered-export/transport-omissions.json',lambda r:r['totals'].update(files=1)))
            for session,exit_code in [('94696',0),('8896',1)]:
                try:a.verify_archon(store,session,exit_code)
                except EvidenceError:checks.append('PASS rejection: wrong launcher identity/exit '+str((session,exit_code)))
                else:raise AssertionError('launcher identity accepted')
        finally:
            a.PROTOCOL_SHA,a.HARNESS_SHA=old_protocol_sha,old_harness_sha
            a.TRANSPORT_HELPER_SHA,a.TRANSPORT_POLICY_SHA=old_helper_sha,old_policy_sha
    print('Adapter SHA256: '+hashlib.sha256((Q/'archon_acceptance.py').read_bytes()).hexdigest())
    print('\n'.join(checks));print(f'{len(checks)} bounded Archon adapter checks PASS; all native outcomes synthetic, no remote/native jobs.')


if __name__=='__main__':main()
