"""Bounded CPU-profile verifier checks: retained JSON reads and synthetic mutations only."""
import copy
import hashlib
import importlib.util
import json
import shutil
from pathlib import Path
import tempfile

Q=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('f',Q/'finalize-acceptance.py')
f=importlib.util.module_from_spec(spec);spec.loader.exec_module(f)


def main():
    checks=[]
    def reject(name,callback):
        try:callback()
        except (RuntimeError,KeyError,ValueError):checks.append('PASS rejection: '+name)
        else:raise AssertionError('not rejected: '+name)
    protocol=f.cpu_rate_profile()
    selected=f.read(protocol['calibration_selection']['path'])
    base=selected['caps']['1'][0]
    f.verify_calibration(protocol);checks.append('PASS actual calibration: all6 trials independently recomputed; selected1CPU')
    changes=[('too-fast work under claimed cap',lambda t:t.update(wall_s=2)),
             ('escaped completed worker',lambda t:t['workers'][0].update(cgroup_after='/escaped')),
             ('escaped parent-observed worker',lambda t:t['parent_observed_worker_membership'].update({str(t['workers'][0]['pid']):'/escaped'})),
             ('missing worker',lambda t:t['workers'].pop()),
             ('forged service verdict',lambda t:t['verdict'].update(service_fraction=2)),
             ('forged cgroup CPU delta',lambda t:t.update(cgroup_work_cpu_usec=1)),
             ('inadequate delivered CPU work',lambda t:t['workers'][0].update(cpu_s=2.9)),
             ('wrong actual CPU quota',lambda t:t['before']['ancestry'][0].update({'cpu.max':'max 100000'})),
             ('calibration control error',lambda t:t.update(status='ERROR'))]
    for name,change in changes:
        value=copy.deepcopy(base);change(value)
        reject(name,lambda value=value:f.calibration_trial(value,1))
    initial=f.read(Path(protocol['metadata_output'])/'initial-resources.json')
    f.rate_snapshot(initial,protocol);checks.append('PASS actual scope: quota/period/burst/weight/priority/affinity/ancestors')
    changes=[('changed own quota',lambda s:s['ancestors'][0]['controls']['cpu.max'].update(text='max 100000')),
             ('lower ancestor quota',lambda s:s['ancestors'][1]['controls']['cpu.max'].update(text='50000 100000')),
             ('missing ancestor',lambda s:s['ancestors'].pop(1)),
             ('mismatched raw membership',lambda s:s.update(proc_self_cgroup='0::/escaped\n')),
             ('changed affinity',lambda s:s.update(cpu_affinity=[0])),
             ('duplicate CPU accounting',lambda s:s['ancestors'][0]['observations']['cpu.stat'].update(text='usage_usec 1\nusage_usec 2\n'))]
    for name,change in changes:
        value=copy.deepcopy(initial);change(value)
        reject(name,lambda value=value:f.rate_snapshot(value,protocol))
    with tempfile.TemporaryDirectory(dir=Q,prefix='cpu-verifier-check-') as directory:
        root=Path(directory);metadata=root/'meta';(metadata/'samples').mkdir(parents=True)
        fake=copy.deepcopy(protocol);fake['metadata_output']=str(metadata)
        fake['reference_cli']='/synthetic/reference-cli';fake['candidate_cli']='/synthetic/candidate-cli'
        for b in fake['boards']:
            b['output']=str(root/b['name']);Path(b['output']).mkdir()
        clock=initial['monotonic_s']
        (metadata/'initial-resources.json').write_text(json.dumps(initial))
        for board in fake['boards']:
            raw={'warmup':{},'reference':[],'candidate':[]}
            for index in range(6):
                for side in (('reference','candidate') if index%2==0 else ('candidate','reference')):
                    label=f"{board['name']}-{side}-{index}";clock+=3
                    before=copy.deepcopy(initial);before['monotonic_s']=clock
                    after=copy.deepcopy(before);after['monotonic_s']=clock+2
                    rows=[{'pid':100,'birth_ticks':1,'gnu_time_root_pid':100,'observed_monotonic_s':clock+1,
                           'status':'observed','group':initial['group'],'in_scope':True,'argv':['/usr/bin/time']},
                          {'pid':101,'birth_ticks':2,'gnu_time_root_pid':100,'observed_monotonic_s':clock+1,
                           'status':'observed','group':initial['group'],'in_scope':True,'argv':['python3',fake[side+'_cli']]}]
                    monitor={'sampler_calls':10,'observed_births':2,'members':rows,'outside_scope':[],'read_errors':[],'exit_races':[]}
                    value={'label':label,'before':before,'after':after,'driver_call_wall_s':1,
                           'membership_monitor':monitor,'resource_violations':[]}
                    (metadata/'samples'/(label+'.json')).write_text(json.dumps(value))
                    native={'memory_sampling':{'samples':10}}
                    if index==0:raw['warmup'][side]=native
                    else:raw[side].append(native)
            (Path(board['output'])/'samples.json').write_text(json.dumps(raw))
        proof=f.verify_cpu_rate_resources(fake)
        assert proof['completed_samples']==48 and proof['observed_in_scope_births']==96
        checks.append('PASS exact population:48 controls/96 birth observations/raw RSS sample count')
        target=metadata/'samples'/'diff-pair-reference-0.json';saved=target.read_text()
        def mutate(change):
            value=json.loads(saved);change(value);target.write_text(json.dumps(value))
        changes=[('escaped raw descendant despite empty summary',lambda s:s['membership_monitor']['members'][1].update(group='/escaped')),
                 ('missing actual CLI observation',lambda s:s['membership_monitor']['members'][1].update(argv=['other'])),
                 ('missing GNUtime root observation',lambda s:s['membership_monitor']['members'][0].update(pid=102)),
                 ('RSS/membership count mismatch',lambda s:s['membership_monitor'].update(sampler_calls=9)),
                 ('duplicate observed birth',lambda s:s['membership_monitor']['members'][1].update(pid=100,birth_ticks=1)),
                 ('unrecorded membership read error',lambda s:s['membership_monitor']['members'][1].update(status='read-error')),
                 ('resource record overlaps next attempt',lambda s:s['after'].update(monotonic_s=clock+100)),
                 ('resource clock shorter than driver execution',lambda s:s.update(driver_call_wall_s=3)),
                 ('postexecution ancestor controls changed',lambda s:s['after']['ancestors'][1]['controls']['cpu.weight'].update(text='101'))]
        for name,change in changes:
            mutate(change);reject(name,lambda:f.verify_cpu_rate_resources(fake));target.write_text(saved)
        target.unlink();reject('dropped one of48 resource records',lambda:f.verify_cpu_rate_resources(fake));target.write_text(saved)
        extra=target.with_name('extra.json');extra.write_text(saved)
        reject('extra resource record',lambda:f.verify_cpu_rate_resources(fake));extra.unlink()
    captured=f.read(Q/'diff-pair-cpu-rate/samples.json')['warmup']['candidate']
    result=f.read(captured['result'])
    f.verify_sample_binding(captured,result,protocol,protocol['boards'][0],'candidate')
    checks.append('PASS actual completed candidate: raw tool/build/input/CLI bindings')
    changes=[('forged captured build identity',lambda sample,raw:sample['tool_identity']['backend'].update(source_sha256='wrong')),
             ('raw result build differs from summary',lambda sample,raw:raw['tool_identity']['backend'].update(extension_module_sha256='wrong')),
             ('raw candidate execution differs from tool evidence',lambda sample,raw:raw['backend']['execution_identity'].update(venv_python_version='wrong')),
             ('wrong CLI bytes',lambda sample,raw:sample.update(cli_sha256='wrong')),
             ('missing input file',lambda sample,raw:sample['reference_files'].pop(next(iter(sample['reference_files'])))),
             ('edited raw input hash',lambda sample,raw:raw['evidence']['reference_files'].update({'pair.kicad_pcb':'wrong'})),
             ('extra input file',lambda sample,raw:sample['reference_files'].update({'extra.kicad_pcb':'wrong'}))]
    for name,change in changes:
        sample,raw=copy.deepcopy(captured),copy.deepcopy(result);change(sample,raw)
        reject(name,lambda sample=sample,raw=raw:f.verify_sample_binding(sample,raw,protocol,protocol['boards'][0],'candidate'))
    historical=f.read(Q/'route-demo-prepared/samples.json')['warmup']['reference']
    sample,raw=copy.deepcopy(captured),copy.deepcopy(result)
    sample['reference_files']=raw['evidence']['reference_files']=historical['reference_files']
    f.verify_sample_binding(sample,raw,protocol,protocol['boards'][3],'candidate')
    checks.append('PASS route-demo native population excludes GUI/generator files while all prerequisites stay pinned')
    with tempfile.TemporaryDirectory(dir=Q,prefix='snapshot-verifier-check-') as directory:
        root=Path(directory);shutil.copy(Q/'cpu-rate-protocol.json',root/'cpu-rate-protocol.json')
        shutil.copytree(Q/'cpu-rate-prerequisite-snapshots',root/'cpu-rate-prerequisite-snapshots')
        original_q=f.Q;f.Q=root
        try:
            f.cpu_rate_profile();compare=f.comparison_module()
            assert callable(compare.compare)
            checks.append('PASS frozen adjudicator/prerequisites load from retained snapshots without live source paths')
            index=root/'cpu-rate-prerequisite-snapshots/index.json';saved=index.read_text()
            content=json.loads(saved);payload=index.parent/content['files'][0]['snapshot'];original=payload.read_bytes()
            payload.write_bytes(original+b'\n')
            reject('edited captured prerequisite bytes',f.cpu_rate_profile);payload.write_bytes(original)
            content['files'][0]['original_path']='/other';index.write_text(json.dumps(content))
            reject('misbound original prerequisite path',f.cpu_rate_profile);index.write_text(saved)
            content=json.loads(saved);content['files'].pop();index.write_text(json.dumps(content))
            reject('missing captured prerequisite',f.cpu_rate_profile);index.write_text(saved)
            content=json.loads(saved);content['files'][0]['snapshot']='../cpu-rate-protocol.json';index.write_text(json.dumps(content))
            reject('escaping captured prerequisite path',f.cpu_rate_profile);index.write_text(saved)
        finally:f.Q=original_q
    original_profile,original_read=f.cpu_rate_profile,f.read
    f.cpu_rate_profile=lambda:{'metadata_output':'/synthetic/nonterminal'};f.read=lambda path:{'status':'RUNNING'}
    try:reject('RUNNING cannot finalize with claimed exit0',lambda:f.verify_final_matrix('94696',0))
    finally:f.cpu_rate_profile,f.read=original_profile,original_read
    reject('old matrix session cannot qualify',lambda:f.verify_final_matrix('18111',0))
    print('Finalizer SHA256: '+hashlib.sha256((Q/'finalize-acceptance.py').read_bytes()).hexdigest())
    print('\n'.join(checks));print(f'{len(checks)} bounded CPU-rate verifier checks PASS; no native jobs or real waits.')


if __name__=='__main__':main()
