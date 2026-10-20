#!/usr/bin/env python3
"""One preregistered Archon container matrix; all available CPUs and no retries."""
import argparse
import hashlib
import importlib.util
import json
import os
import platform
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
CONTROLS = ('cpu.max','cpu.max.burst','cpu.weight','cpu.idle','cpuset.cpus.effective')
OBSERVATIONS = ('cpu.stat','cpu.stat.local','cpu.pressure','io.pressure','memory.pressure')


def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path,value):
    path=Path(path);temporary=path.with_name(path.name+'.writing')
    temporary.write_text(json.dumps(value,indent=2)+'\n');temporary.replace(path)


def read_file(path):
    try:return {'text':Path(path).read_text()}
    except OSError as error:return {'error':str(error)}


def canonical_group(group):
    return isinstance(group,str) and group.startswith('/') and (group=='/' or all(part not in ('','.','..') for part in group[1:].split('/')))


def membership(pid='self'):
    lines=Path(f'/proc/{pid}/cgroup').read_text().splitlines()
    values=[line[3:] for line in lines if line.startswith('0::')]
    if len(values)!=1:raise ValueError('Missing/ambiguous unified cgroup membership')
    return values[0]


def locate_cgroup(group=None):
    group=membership() if group is None else group
    matches=[]
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        left,right=line.split(' - ',1)
        if right.split()[0]!='cgroup2':continue
        fields=left.split();root=Path(fields[3]);mount=Path(fields[4])
        try:relative=Path(group).relative_to(root)
        except ValueError:continue
        matches.append((mount/relative,mount))
    if len(matches)!=1:raise ValueError('Missing/ambiguous cgroup2 mount mapping')
    return group,*matches[0]


def resource_snapshot():
    group,leaf,mount=locate_cgroup();ancestors=[];directory=leaf
    while True:
        ancestors.append({'path':str(directory),'controls':{n:read_file(directory/n) for n in CONTROLS},
                          'observations':{n:read_file(directory/n) for n in OBSERVATIONS}})
        if directory==mount:break
        directory=directory.parent
    return {'utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'monotonic_s':time.monotonic(),
            'proc_self_cgroup':Path('/proc/self/cgroup').read_text(),'group':group,
            'mount':str(mount),'leaf':str(leaf),'ancestors':ancestors,
            'cpu_affinity':sorted(os.sched_getaffinity(0)),'nice':os.getpriority(os.PRIO_PROCESS,0),
            'scheduler':os.sched_getscheduler(0),
            'host_resources':{n:read_file(n) for n in ['/proc/stat','/proc/pressure/cpu','/proc/pressure/io','/proc/pressure/memory']}}


def control_profile(snapshot):
    return {'group':snapshot['group'],'cpu_affinity':snapshot['cpu_affinity'],
            'nice':snapshot['nice'],'scheduler':snapshot['scheduler'],
            'ancestors':[{'path':r['path'],'controls':r['controls']} for r in snapshot['ancestors']]}


def validate_scope(snapshot,protocol):
    if snapshot['cpu_affinity']!=protocol['cpu_affinity'] or snapshot['nice']!=0 or snapshot['scheduler']!=os.SCHED_OTHER:
        raise ValueError('Available CPU affinity/normal-priority scheduler differs from preregistration')
    if not canonical_group(snapshot['group']):raise ValueError('Noncanonical cgroup membership')
    allowed=protocol.get('allowed_existing_policy_quotas',{})
    for ancestor in snapshot['ancestors']:
        controls=ancestor['controls'];value=controls['cpu.max'].get('text')
        if value:
            parts=value.split()
            if len(parts)!=2:raise ValueError('Malformed visible CPU quota')
            if parts[0]!='max' and allowed.get(ancestor['path'])!=value.strip():
                raise ValueError('Unregistered finite host-policy CPU quota')
    idle=snapshot['ancestors'][0]['controls']['cpu.idle'].get('text')
    if idle is not None and idle.strip()!='0':raise ValueError('Container cgroup uses idle scheduling')
    if not snapshot['ancestors'][0]['observations']['cpu.stat'].get('text'):
        raise ValueError('Missing container CPU accounting')



class MembershipMonitor:
    """Observe each birth once using the frozen driver's existing RSS traversal."""
    def __init__(self,driver,group):
        self.driver,self.group=driver,group;self.records={};self.calls=0
    def __enter__(self):
        self.original=self.driver.process_tree_rss
        def observe(root_pid,tracked):
            rss=self.original(root_pid,tracked)  # preserve exact RSS and traversal
            self.calls+=1
            for pid,birth in tracked.items():
                key=(pid,birth)
                if key in self.records:continue
                row={'pid':pid,'birth_ticks':birth,'gnu_time_root_pid':root_pid,
                     'observed_monotonic_s':time.monotonic()}
                try:
                    group=membership(pid)
                    directory=Path(f'/proc/{pid}')
                    fields=(directory/'stat').read_text().rsplit(')',1)[1].split()
                    if int(fields[19])!=birth:
                        row['status']='pid-reused-before-membership-verification'
                    else:
                        row.update(group=group,status='observed',
                            in_scope=canonical_group(group) and (group==self.group or group.startswith(self.group.rstrip('/')+'/')))
                        try:row['argv']=(directory/'cmdline').read_bytes().decode(errors='replace').split('\0')[:-1]
                        except (FileNotFoundError,ProcessLookupError):row['argv_status']='exited-after-membership-verification'
                except (FileNotFoundError,ProcessLookupError):row['status']='exited-before-membership-verification'
                except (OSError,ValueError) as error:row.update(status='read-error',error=str(error))
                self.records[key]=row
            return rss
        self.driver.process_tree_rss=observe;return self
    def __exit__(self,*args):self.driver.process_tree_rss=self.original
    def summary(self):
        rows=list(self.records.values())
        return {'sampler_calls':self.calls,'observed_births':len(rows),'members':rows,
                'outside_scope':[r for r in rows if r.get('in_scope') is False],
                'read_errors':[r for r in rows if r['status']=='read-error'],
                'exit_races':[r for r in rows if r['status'] not in ('observed','read-error')],
                'limitation':'Each birth from the frozen RSS tracked map is checked once. Reaped/reused races are explicit; this is not continuous cgroup-migration tracing.'}


def run_matrix(protocol,driver,snapshot=resource_snapshot,monitor_factory=MembershipMonitor):
    root=Path(protocol['metadata_output']);root.mkdir(parents=True,exist_ok=False)
    resources=root/'samples';resources.mkdir();write_json(root/'protocol.json',protocol)
    matrix={'schema':'kicad-route/archon-matrix/1','status':'RUNNING','protocol_sha256':sha(root/'protocol.json'),
            'profile':'archon-same-container-all-available-cpus','boards':[],'completed_samples':0,'all_comparisons_passed':False,
            'resource_violations':[]}
    def finish(status,error=None):
        matrix['status']=status;matrix['all_comparisons_passed']=status=='PASS'
        if error is not None:matrix['error']=str(error)
        write_json(root/'matrix.json',matrix)
    finish('RUNNING')
    try:
        initial=snapshot();validate_scope(initial,protocol);expected=control_profile(initial)
        write_json(root/'initial-resources.json',initial)
        for board in protocol['boards']:
            output=Path(board['output']);output.mkdir(parents=True,exist_ok=False)
            config=SimpleNamespace(output=output,project=Path(board['project']),arg=board['args'],
                reference_cli=Path(protocol['reference_cli']),reference_state=Path(protocol['reference_state']),
                candidate_cli=Path(protocol['candidate_cli']),candidate_state=Path(protocol['candidate_state']),timeout=protocol['cli_timeout_s'])
            report={'schema':'kicad-route/engine-comparison/2','project':str(config.project),'args':config.arg,
                    'reference':[],'candidate':[],'warmup':{},'mode':'migration-equivalence',
                    'cpu_affinity':initial['cpu_affinity'],'measurement':protocol['measurement'],
                    'archon_protocol':str(root/'protocol.json')}
            row={'name':board['name'],'output':str(output),'status':'RUNNING','completed_samples':0};matrix['boards'].append(row)
            write_json(output/'samples.json',report)
            for index in range(6):
                for side in (('reference','candidate') if index%2==0 else ('candidate','reference')):
                    label=f"{board['name']}-{side}-{index}";print(label+': running',flush=True)
                    before=snapshot();validate_scope(before,protocol)
                    if control_profile(before)!=expected:raise ValueError('Scope/ancestor controls changed before '+label)
                    telemetry={'label':label,'before':before};observation=resources/(label+'.json')
                    write_json(observation,telemetry)
                    started=time.monotonic()
                    monitor=monitor_factory(driver,initial['group'])
                    try:
                        with monitor:
                            sample=driver.run_sample(config,side,index)
                    finally:
                        telemetry['driver_call_wall_s']=time.monotonic()-started
                        try:telemetry['after']=snapshot()
                        except Exception as error:telemetry['after_error']=str(error)
                        telemetry['membership_monitor']=monitor.summary()
                        write_json(observation,telemetry)
                    violations=[]
                    if telemetry.get('after') is None or control_profile(telemetry['after'])!=expected:
                        violations.append('Scope/ancestor controls not verified after completion')
                    if telemetry['membership_monitor']['outside_scope']:violations.append('Observed descendant outside selected scope')
                    if telemetry['membership_monitor']['read_errors']:violations.append('Child membership observation error')
                    telemetry['resource_violations']=violations
                    write_json(observation,telemetry)
                    matrix['resource_violations'].extend(label+': '+v for v in violations)
                    if index==0:report['warmup'][side]=sample
                    else:report[side].append(sample)
                    row['completed_samples']+=1;matrix['completed_samples']+=1
                    write_json(output/'samples.json',report);finish('RUNNING')
            report['verdict']=driver.compare(report['reference'],report['candidate'],report['warmup'],report['mode'])
            write_json(output/'comparison.json',report);row['verdict']=report['verdict'];row['status']='PASS' if report['verdict']['passed'] else 'FAIL'
            print(board['name']+': '+row['status'],flush=True);finish('RUNNING')
        passed=(len(matrix['boards'])==4 and matrix['completed_samples']==48 and not matrix['resource_violations']
                and all(r['status']=='PASS' and r['completed_samples']==12 for r in matrix['boards']))
        finish('PASS' if passed else 'FAIL');return 0 if passed else 1
    except KeyboardInterrupt as error:finish('INTERRUPTED',error);return 130
    except Exception as error:finish('EXECUTION_ERROR',error);return 1



def validate_host_attestation(value,protocol):
    if (value.get('host')!='archon' or value.get('actual_hostname')!='archon'
        or value.get('container_id')!=protocol['container_id']
        or value.get('container_hostname')!=protocol['container_hostname']
        or value.get('image_id')!=protocol['container_image_id']):
        raise ValueError('Host/container/image attestation identity mismatch')
    if value.get('init_affinity')!=protocol['cpu_affinity'] or value.get('init_nice')!=0 or value.get('init_scheduler')!=os.SCHED_OTHER:
        raise ValueError('Host-observed container resource profile differs')
    if not value.get('init_pid') or not value.get('raw_init_cgroup') or not value.get('ancestors'):
        raise ValueError('Missing host initPID/cgroup/ancestor proof')
    docker=value['docker_resource_config']
    if any(docker.get(key)!=0 for key in ('NanoCpus','CpuShares','CpuPeriod','CpuQuota')) or docker.get('CpusetCpus')!='':
        raise ValueError('Unexpected Docker CPU controls')
    for row in value['ancestors']:
        maximum=row['controls'].get('cpu.max')
        if maximum is not None and maximum.split()[0]!='max':raise ValueError('Finite hidden host ancestor quota')


def preflight(protocol):
    if not protocol.get('execution_ready') or not protocol.get('container_image_id') or not protocol.get('cpu_affinity'):
        raise ValueError('Target container/image/runtime/CPU prerequisites are not yet bound')
    if platform.node()!=protocol['container_hostname']:
        raise ValueError('Actual container hostname differs from preregistration')
    if platform.python_version()!='3.12.3':raise ValueError('Native wrapper interpreter is not Python3.12.3')
    if protocol['measured_repetitions']!=5 or [b['name'] for b in protocol['boards']]!=['diff-pair','four-layer','multipoint','route-demo']:
        raise ValueError('Four-board/five-pair profile changed')
    required=protocol.get('host_before_attestation',{})
    if not required.get('path') or not required.get('sha256'):
        raise ValueError('Host Docker/initPID/ancestor attestation is not bound')
    for item in [*protocol['hash_prerequisites'],required]:
        if sha(item['path'])!=item['sha256']:raise ValueError('Frozen prerequisite changed: '+item['path'])
    validate_host_attestation(json.loads(Path(required['path']).read_text()),protocol)
    for item in protocol['source_heads']:
        if subprocess.check_output(['git','-C',item['path'],'rev-parse','HEAD'],text=True).strip()!=item['head']:
            raise ValueError('Frozen source HEAD changed')
        if subprocess.check_output(['git','-C',item['path'],'status','--porcelain','--untracked-files=no'],text=True).strip():
            raise ValueError('Frozen tracked source is dirty')
    for path in [Path(protocol['metadata_output']),*(Path(b['output']) for b in protocol['boards'])]:
        if path.exists():raise ValueError('Immutable Archon output already exists')
    validate_scope(resource_snapshot(),protocol)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--protocol',type=Path,default=HERE/'archon-protocol.json');args=parser.parse_args()
    protocol=json.loads(args.protocol.read_text());preflight(protocol)
    spec=importlib.util.spec_from_file_location('frozen_compare_router',protocol['driver']);driver=importlib.util.module_from_spec(spec);spec.loader.exec_module(driver)
    return run_matrix(protocol,driver)


if __name__=='__main__':raise SystemExit(main())
