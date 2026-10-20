#!/usr/bin/env python3
"""Independently verify retained Archon evidence; no remote/native execution.

Only emits archon-final-verification.json after every original strict gate passes.
The transfer inventory SHA and actual terminal launcher identity are supplied by
root, outside the indexed data. Raw remote JSON locators remain unchanged.
"""
import argparse
import datetime as dt
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import sys
sys.dont_write_bytecode=True
from acceptance_profile import EvidenceStore, digest, require, validate_declared_profile, verify_board_gates

Q=Path(__file__).resolve().parent
PROTOCOL_SHA='a0801e985dedb81e1d0af1d5db5d3fd78ac8d537392c1d24794d9441b5fd888b'
HARNESS_SHA='ebfd140540a7ac794ea6efb00c292da1ffe8ea77e8512b6981f2148ae72842e1'
PROTOCOL='/evidence/archon-protocol.json'
MATRIX_SESSION='8896'
TRANSPORT_HELPER_SHA='af2899d658b36ffb8c0aa7f62cee6f65c8ba5f159ecc829d64f3b7ea4f2d390a'
TRANSPORT_POLICY_SHA='8e30d7b302bba9856a127e1a04159f2efb8571ed59c5ad3e8f4fd27027aab3e1'
CPU_CONTROLS=('cpu.max','cpu.max.burst','cpu.weight','cpu.idle','cpuset.cpus.effective')
HOST_CONTROLS=(*CPU_CONTROLS,'memory.max','memory.high')


def number(value):return type(value) in (int,float) and math.isfinite(value)


def canonical_group(value):
    return isinstance(value,str) and value.startswith('/') and (value=='/' or
        all(part not in ('','.','..') for part in value[1:].split('/')))


def inside(group,parent):
    return canonical_group(group) and canonical_group(parent) and (group==parent or group.startswith(parent.rstrip('/')+'/'))


def cpuset(value):
    result=set()
    for field in value.strip().split(','):
        ends=field.split('-');require(len(ends) in (1,2),'malformed effective CPU set')
        first,last=int(ends[0]),int(ends[-1]);require(0<=first<=last<100000,'invalid effective CPU range')
        for cpu in range(first,last+1):require(cpu not in result,'duplicate effective CPU');result.add(cpu)
    return result


def cpu_stat(value):
    result={}
    for line in value.splitlines():
        fields=line.split();require(len(fields)==2 and fields[0] not in result,'duplicate/malformed raw CPU stat')
        counter=int(fields[1]);require(counter>=0,'negative CPU stat');result[fields[0]]=counter
    require('usage_usec' in result,'missing raw CPU usage');return result


def utc(value):return dt.datetime.fromisoformat(value.replace('Z','+00:00'))


def host_controls(value,protocol):
    require(value['host']==value['actual_hostname']=='archon' and
        value['container_id']==protocol['container_id'] and value['image_id']==protocol['container_image_id'] and
        value['container_hostname']==protocol['container_hostname'],'host/container/image identity differs')
    require(type(value['init_pid']) is int and value['init_pid']>0 and value['init_affinity']==protocol['cpu_affinity'] and
        value['init_nice']==value['init_scheduler']==0,'host initPID/CPU/normal-priority identity differs')
    raw=value['raw_init_cgroup'].splitlines();require(len(raw)==1 and raw[0].startswith('0::'),'invalid host raw cgroup membership')
    group=raw[0][3:];require(canonical_group(group) and PurePosixPath(group).name=='docker-'+protocol['container_id']+'.scope',
        'host container membership does not bind actual container')
    leaf=PurePosixPath('/sys/fs/cgroup')/group.lstrip('/')
    require(str(leaf)==value['host_leaf'],'host cgroup leaf differs from membership')
    config=value['docker_resource_config']
    require(all(config.get(key)==0 for key in ('NanoCpus','CpuShares','CpuPeriod','CpuQuota','CpuRealtimePeriod','CpuRealtimeRuntime','Memory','MemorySwap')) and
        config.get('CpusetCpus')==config.get('CpusetMems')==config.get('CgroupParent')=='','Docker resources differ from unmodified full-CPU profile')
    rows=value['ancestors'];require(rows and rows[0]['path']==str(leaf) and rows[-1]['path']=='/sys/fs/cgroup','host ancestor population incomplete')
    for index,row in enumerate(rows):
        if index:require(PurePosixPath(rows[index-1]['path']).parent==PurePosixPath(row['path']),'omitted host ancestor')
        controls=row['controls'];maximum=controls.get('cpu.max')
        if maximum is not None:
            quota,period=maximum.split();require(quota=='max' and int(period)>0,'finite or invalid hidden ancestor quota')
        else:require(index==len(rows)-1,'nonroot host quota missing')
        require(cpuset(controls['cpuset.cpus.effective'])==set(protocol['cpu_affinity']),'hidden host CPU set differs')
        for name,expected in [('cpu.max.burst','0'),('cpu.weight','100'),('cpu.idle','0')]:
            require(controls.get(name)==expected or index==len(rows)-1 and controls.get(name) is None,'host CPU policy differs')
        cpu_stat(controls['cpu.stat'])
    require(value.get('host_cpu_pressure') and value.get('host_io_pressure'),'host pressure receipts missing')
    return {'container_id':value['container_id'],'image_id':value['image_id'],'init_pid':value['init_pid'],
        'raw_init_cgroup':value['raw_init_cgroup'],'init_affinity':value['init_affinity'],'init_nice':value['init_nice'],
        'init_scheduler':value['init_scheduler'],'docker_resource_config':config,
        'ancestors':[{'path':r['path'],'controls':{key:r['controls'].get(key) for key in HOST_CONTROLS}} for r in rows]}


def container_controls(value,protocol):
    group=value['group'];require(canonical_group(group) and value['proc_self_cgroup'].splitlines()==['0::'+group],
        'container namespace membership noncanonical or misbound')
    require(value['cpu_affinity']==protocol['cpu_affinity'] and value['nice']==value['scheduler']==0 and
        number(value['monotonic_s']),'container affinity/priority/clock differs')
    leaf,mount=PurePosixPath(value['leaf']),PurePosixPath(value['mount'])
    require(leaf.is_relative_to(mount) and ('/'+str(leaf.relative_to(mount)) if leaf!=mount else '/')==group,
        'container cgroup mount/leaf mapping differs')
    rows=value['ancestors'];require(rows and rows[0]['path']==str(leaf) and rows[-1]['path']==str(mount),'container ancestor population incomplete')
    for index,row in enumerate(rows):
        if index:require(PurePosixPath(rows[index-1]['path']).parent==PurePosixPath(row['path']),'omitted container ancestor')
        controls=row['controls'];maximum=controls['cpu.max'].get('text')
        if maximum is not None:
            quota,period=maximum.split();require(quota=='max' and int(period)>0,'finite container CPU quota')
        else:require(index==len(rows)-1 and 'No such file or directory' in controls['cpu.max'].get('error',''),'unusable quota observation')
        effective=controls['cpuset.cpus.effective']
        if 'text' in effective:require(cpuset(effective['text'])==set(protocol['cpu_affinity']),'container effective CPU set differs')
        else:require('No such file or directory' in effective.get('error',''),'unusable cpuset observation')
        idle=controls['cpu.idle'].get('text')
        require(idle is None or idle.strip()=='0','container idle scheduling')
        cpu_stat(row['observations']['cpu.stat']['text'])
    require(all(value['host_resources'][key].get('text') for key in ('/proc/stat','/proc/pressure/cpu','/proc/pressure/io','/proc/pressure/memory')),
        'container host resource diagnostics missing')
    return {'group':group,'cpu_affinity':value['cpu_affinity'],'nice':value['nice'],'scheduler':value['scheduler'],
        'ancestors':[{'path':r['path'],'controls':r['controls']} for r in rows]}


def membership_proof(monitor,group,cli,samples,before,after):
    rows=monitor['members'];require(type(monitor['sampler_calls']) is int and monitor['sampler_calls']==samples and samples>0 and
        monitor['observed_births']==len(rows) and rows,'membership/RSS sampler population differs')
    keys={(r['pid'],r['birth_ticks']) for r in rows};require(len(keys)==len(rows) and
        all(type(pid) is int and pid>0 and type(birth) is int and birth>0 for pid,birth in keys),'duplicate/invalid observed birth')
    roots={r['gnu_time_root_pid'] for r in rows};require(len(roots)==1,'membership mixes native roots')
    allowed={'observed','exited-before-membership-verification','pid-reused-before-membership-verification'}
    for row in rows:
        require(row['status'] in allowed and number(row['observed_monotonic_s']) and
            before<=row['observed_monotonic_s']<=after,'membership status/observation clock invalid')
        if row['status']=='observed':require(inside(row['group'],group) and row.get('in_scope') is True,'observed descendant escaped container scope')
    observed=[r for r in rows if r['status']=='observed']
    require(any(r['pid'] in roots for r in observed),'GNU-time root membership unobserved')
    require(any(r['pid'] not in roots and cli in r.get('argv',[]) for r in observed),'actual CLI child membership unobserved')
    races=[r for r in rows if r['status']!='observed']
    require(monitor['outside_scope']==monitor['read_errors']==[] and monitor['exit_races']==races,'membership summaries differ from raw birth records')
    return {'observed_in_scope':len(observed),'explicit_birth_races':len(races)}


def expected_input(protocol,board):
    project=PurePosixPath(board['project']);suffixes={'.kicad_sch','.kicad_pcb','.kicad_pro','.kicad_dru','.kicad_wks','.kicad_sym','.lib','.dcm','.mod'}
    return {str(PurePosixPath(r['path']).relative_to(project)):r['sha256'] for r in protocol['hash_prerequisites']
        if PurePosixPath(r['path']).is_relative_to(project) and (PurePosixPath(r['path']).suffix in suffixes or
        PurePosixPath(r['path']).name in ('sym-lib-table','fp-lib-table'))}


def tool_proof(store,protocol,tools,board,side,sample):
    result=store.read(sample['result']);identity=result['tool_identity'];runtime=protocol['target_runtime'][side]
    require(sample['tool_identity']==identity and sample['backend']==result['backend'],'raw tool/backend summaries differ')
    require(identity['kicad_route']['commit']==next(r['head'] for r in protocol['source_heads'] if r['path']=='/work/'+side) and
        identity['kicad_route']['dirty'] is False,'actual source checkout identity differs')
    require(identity['kicad_cli']==dict(tools['kicad_cli'],version=tools['kicad_version']) and
        identity['pcbnew']['build_version']==tools['pcbnew_build'] and identity['python']['version']==tools['python_version']=='3.12.3',
        'actual native grading/interpreter differs from prepared toolchain')
    require(identity['state_manifest_sha256']==runtime['manifest_sha256'] and identity['state_manifest_drift']==[],
        'actual native manifest drift or wrong runtime generation')
    expected={'name':'krt','tag':runtime['source']['build'],'version':runtime['source']['version'],
        'grid_router_version':runtime['source']['version'],'source_sha256':runtime['source']['source_sha256'],
        'extension_module_sha256':runtime['module_sha256'],'venv_packages_sha256':runtime['packages_sha256']}
    backend=identity['backend'];require(all(backend.get(k)==v for k,v in expected.items()),'actual executed owned backend differs from prepared build')
    if side=='candidate':
        expected['venv_python_version']='3.12.3'
        require(backend.get('venv_python_version')=='3.12.3' and result['backend']['execution_identity']==expected,
            'candidate actual execution identity differs')
    require(sample['cli_sha256']==next(r['sha256'] for r in protocol['hash_prerequisites'] if r['path']==protocol[side+'_cli']),
        'executed CLI bytes differ')
    files=expected_input(protocol,board);require(files and sample['reference_files']==result['evidence']['reference_files']==files,
        'actual native input population/hashes differ')
    return {'backend':expected,'native':identity['kicad_cli'],'pcbnew':identity['pcbnew'],'wrapper_python':identity['python']}


def native_records_proof(store,sample,side):
    result=store.read(sample['result']);evidence=result['evidence']
    actual=store.artifact(result['candidate']['board'])['sha256']
    require(result['validation']['candidate_board_sha256']==actual,'native validation is not bound to retained final candidate')
    evidence_matches=evidence['candidate_board_sha256']==actual
    require(side=='reference' or evidence_matches,'owned candidate evidence is not final post-refill bytes')
    records=evidence['records'];require(records,'native grading record population missing')
    run=PurePosixPath(sample['result']).parent
    for relative,expected in records.items():
        path=PurePosixPath(relative)
        require(not path.is_absolute() and '..' not in path.parts and
            store.artifact(str(run/path))['sha256']==expected,'native grading record locator/hash does not resolve')
    return {'records_verified':len(records),'final_validation_hash_matches':True,'final_evidence_hash_matches':evidence_matches}


def prepared_runtimes(store,protocol,tools):
    result={}
    for side in ('reference','candidate'):
        runtime=protocol['target_runtime'][side];state=PurePosixPath(protocol[side+'_state'])
        path=str(state/'manifest.json');manifest=store.read(path);krt=manifest['krt']
        require(store.artifact(path)['sha256']==runtime['manifest_sha256'] and
            krt['source_sha256']==runtime['source']['source_sha256'] and krt['version']==runtime['source']['version'] and
            krt['commit']==runtime['source']['commit'],'prepared manifest source identity differs')
        module=str(state/krt['extension_module']['path'])
        require(module==runtime['module'] and store.artifact(module)['sha256']==krt['extension_module']['sha256']==runtime['module_sha256'],
            'captured native extension differs from selected manifest/runtime')
        venv=krt['venv'];packages=venv['packages']
        package_sha=hashlib.sha256(('\n'.join(packages)+'\n').encode()).hexdigest()
        require(venv['python_version']=='3.12.3' and packages==runtime['packages'] and
            package_sha==venv['packages_sha256']==runtime['packages_sha256'],'prepared interpreter/packages differ')
        require(manifest['kicad']['kicad_cli']==dict(tools['kicad_cli'],version=tools['kicad_version']) and
            manifest['kicad']['pcbnew_build_version']==tools['pcbnew_build'],'prepared native grading manifest differs')
        prep_path=f'/evidence/preparation-{side}/preparation.json';prep=store.read(prep_path)
        require(prep['status']=='PASS' and prep['side']==side and prep['state']==str(state) and prep['runtime']==runtime and
            prep['source_commit']==krt['commit'] and prep['gates'] and all(g['passed'] is True for g in prep['gates']) and
            all(step['exit_code']==0 for step in prep['steps']) and runtime['dependency_pyc_count']==1619,
            'prepared runtime receipt is not all-gatesPASS/matched bytecode preparation')
        result[side]={'manifest':store.artifact(path),'module':store.artifact(module),'preparation':store.artifact(prep_path),
            'source_sha256':krt['source_sha256'],'extension_sha256':runtime['module_sha256'],
            'python_version':venv['python_version'],'packages_sha256':package_sha,'dependency_pyc_count':runtime['dependency_pyc_count']}
    require(result['reference']['packages_sha256']==result['candidate']['packages_sha256'],
        'prepared same-container scientific dependencies differ')
    return result


def transport_proof(store,protocol):
    transport=store.index['transport']
    require(transport['schema']=='kicad-route/filtered-evidence-transport/1' and
        transport['original_indexer_executed'] is False,'missing separate filtered transport provenance')
    for name in ('helper','policy','omissions','original_indexer'):
        row=transport[name];require(store.artifact(row['source'])['sha256']==row['sha256'],'transport artifact hash differs: '+name)
    require(transport['helper']['sha256']==TRANSPORT_HELPER_SHA and transport['policy']['sha256']==TRANSPORT_POLICY_SHA,
        'reviewed filtered transport helper/policy bytes differ')
    original=next(r['sha256'] for r in protocol['hash_prerequisites'] if r['path']==protocol['retention_index']['script'])
    require(transport['original_indexer']['source']==protocol['retention_index']['script'] and
        transport['original_indexer']['sha256']==original,'frozen original-indexer provenance differs')
    policy=store.read(transport['policy']['source']);omissions=store.read(transport['omissions']['source'])
    require(policy['profile_sha256']==PROTOCOL_SHA and policy['original_indexer_sha256']==original and
        omissions['policy_sha256']==TRANSPORT_POLICY_SHA,'transport native-profile/policy cross-binding differs')
    expected={f'preparation-{side}/out/.home' for side in ('reference','candidate')}
    expected.update(str(PurePosixPath(b['output']).relative_to('/evidence')/(side+'-out')/'.home')
        for b in protocol['boards'] for side in ('reference','candidate'))
    require(len(policy['allowed_omission_relative_prefixes'])==len(expected) and
        set(policy['allowed_omission_relative_prefixes'])==expected,'transport omits more than the exact10taskHOME prefixes')
    observed=set();totals=dict(files=0,bytes=0,symlinks=0)
    for row in omissions['omissions']:
        relative=row['relative_prefix'];source=row['source']
        require(relative in expected and relative not in observed and source==str(PurePosixPath('/evidence')/relative) and
            bool(row['reason']),'unsafe/duplicate/unexplained omitted prefix')
        for key in totals:
            require(type(row[key]) is int and row[key]>=0,'invalid omitted count/bytes')
            totals[key]+=row[key]
        require(not store.descendants(source),'an omitted HOME prefix also has retained original files')
        observed.add(relative)
    require(omissions['totals']==totals and omissions['required_regular_file_json_locators_checked'] is True and
        omissions['native_results_timing_metadata_boards_work_baselinecache_ledger_buildlogs_retained'] is True,
        'transport omission totals/protected evidence claim differs')
    return {'status':'verified','artifacts':{name:store.artifact(transport[name]['source']) for name in
        ('helper','policy','omissions','original_indexer')},'omitted_prefixes':omissions['omissions'],'omitted_totals':totals,
        'required_locator_resolution':'Every native/raw/resource/runtime proof locator independently required below; no missing-file fallback',
        'scope':'Separate post-terminal transport only. HOME omission metadata is helper-observed; cache contents are deliberately not qualification evidence.'}


def verify_archon(store,session,exit_code):
    require(session==MATRIX_SESSION and exit_code==0,'explicit actual terminal launcher8896/exit0 required')
    require(store.artifact(PROTOCOL)['sha256']==PROTOCOL_SHA,'reviewed Archon protocol bytes changed')
    protocol=store.read(PROTOCOL)
    transport=transport_proof(store,protocol)
    validate_declared_profile('archon',protocol['cpu_affinity'],protocol['measured_repetitions'],protocol['warmups_per_side'],
        [b['name'] for b in protocol['boards']],1.1)
    require(protocol['execution_ready'] is True and protocol['cpu_affinity']==list(range(16)) and
        protocol['allowed_existing_policy_quotas']=={},'frozen full available16CPU profile differs')
    require(store.artifact(protocol['harness'])['sha256']==protocol['harness_sha256']==HARNESS_SHA,'reviewed harness bytes changed')
    for row in [*protocol['hash_prerequisites'],protocol['host_before_attestation']]:
        require(store.artifact(row['path'])['sha256']==row['sha256'],'transferred frozen prerequisite differs')
    compare=store.load_comparison(protocol['driver'],protocol['driver_sha256'])
    metadata=PurePosixPath(protocol['metadata_output']);terminal_path=str(metadata/'matrix.json');terminal=store.read(terminal_path)
    require(terminal['schema']=='kicad-route/archon-matrix/1' and terminal['status']=='PASS' and
        terminal['all_comparisons_passed'] is True and terminal['completed_samples']==48 and len(terminal['boards'])==4 and
        terminal['resource_violations']==[] and terminal['protocol_sha256']==PROTOCOL_SHA and
        store.artifact(str(metadata/'protocol.json'))['sha256']==PROTOCOL_SHA,'Archon matrix lacks complete strict terminalPASS')
    host_before=store.read(protocol['host_before_attestation']['path']);host_after=store.read(protocol['host_after_attestation_path'])
    host_profile=host_controls(host_before,protocol);require(host_controls(host_after,protocol)==host_profile,'host/container ancestor controls or initPID changed')
    for first,last in zip(host_before['ancestors'],host_after['ancestors']):
        old,new=cpu_stat(first['controls']['cpu.stat']),cpu_stat(last['controls']['cpu.stat'])
        require(set(old)==set(new) and all(new[k]>=old[k] for k in old),'hidden host CPU accounting reset')
    initial=store.read(str(metadata/'initial-resources.json'));controls=container_controls(initial,protocol)
    require(utc(host_before['observed_utc'])<=utc(initial['utc']),'host-before attestation was not before native matrix')
    tools=store.read('/evidence/container-tools.json');require(tools['kicad_version']=='10.0.7' and tools['python_version']=='3.12.3','declared native tool versions differ')
    prepared=prepared_runtimes(store,protocol,tools)
    labels={str(metadata/'samples'/f"{b['name']}-{side}-{i}.json") for b in protocol['boards'] for i in range(6) for side in ('reference','candidate')}
    require(store.descendants(str(metadata/'samples'),'*.json')==labels,'not exact48 resource metadata records')
    slots=[];last_clock=initial['monotonic_s'];last_utc=utc(initial['utc']);observed=races=0;identities={};records=[];native_records=0;legacy_reference_stale=0
    for board,row in zip(protocol['boards'],terminal['boards']):
        require(row['name']==board['name'] and row['output']==board['output'] and row['status']=='PASS' and row['completed_samples']==12,
            'terminal board population/identity differs')
        slot=verify_board_gates(store,board,compare,protocol['cpu_affinity']);require(slot['verdict']==row['verdict'],'terminal board verdict differs from raw recomputation')
        report=store.read(str(PurePosixPath(board['output'])/'samples.json'))
        require(store.read(str(PurePosixPath(board['output'])/'comparison.json'))['archon_protocol']==str(metadata/'protocol.json'),'comparison protocol locator differs')
        for index in range(6):
            for side in (('reference','candidate') if index%2==0 else ('candidate','reference')):
                native=report['warmup'][side] if index==0 else report[side][index-1]
                label=f"{board['name']}-{side}-{index}";path=str(metadata/'samples'/(label+'.json'));resource=store.read(path)
                first,last=resource['before'],resource['after']
                require(resource['label']==label and container_controls(first,protocol)==controls==container_controls(last,protocol) and
                    last['monotonic_s']>first['monotonic_s']>=last_clock and number(resource['driver_call_wall_s']) and
                    0<resource['driver_call_wall_s']<=last['monotonic_s']-first['monotonic_s'] and resource['resource_violations']==[],
                    'per-attempt container control/parity/clock/violation differs')
                for old,new in zip(first['ancestors'],last['ancestors']):
                    old,new=cpu_stat(old['observations']['cpu.stat']['text']),cpu_stat(new['observations']['cpu.stat']['text'])
                    require(set(old)==set(new) and all(new[k]>=old[k] for k in old),'visible container CPU accounting reset')
                proof=membership_proof(resource['membership_monitor'],initial['group'],protocol[side+'_cli'],native['memory_sampling']['samples'],
                    first['monotonic_s'],last['monotonic_s'])
                observed+=proof['observed_in_scope'];races+=proof['explicit_birth_races'];last_clock=last['monotonic_s'];last_utc=utc(last['utc'])
                native_proof=native_records_proof(store,native,side);native_records+=native_proof['records_verified']
                legacy_reference_stale+=side=='reference' and not native_proof['final_evidence_hash_matches']
                identity=tool_proof(store,protocol,tools,board,side,native)
                if side in identities:require(identity==identities[side],'actual tool/build identities changed between native attempts')
                identities[side]=identity;records.append(store.artifact(path))
        slot.update(status='passed');slots.append(slot)
    require(utc(host_after['observed_utc'])>=last_utc,'host-after attestation precedes native completion')
    require(identities['reference']['native']==identities['candidate']['native'] and
        identities['reference']['pcbnew']==identities['candidate']['pcbnew'] and identities['reference']['wrapper_python']==identities['candidate']['wrapper_python'],
        'same-container native grading toolchains differ between sides')
    return {'schema':'kicad-route/archon-final-verification/1','status':'PASS','host':'archon','matrix_session':session,
        'observed_matrix_exit_code':exit_code,'completed_boards':4,'complete_qualified_attempts':48,'all_comparisons_passed':True,
        'gate_limit':1.1,'measured_repetitions_per_side':5,'separate_warmups_per_board':2,'mode':'migration-equivalence',
        'protocol':store.artifact(PROTOCOL),'harness':store.artifact(protocol['harness']),'matrix_terminal':store.artifact(terminal_path),
        'evidence_index':{'path':str(store.index_path),'sha256':digest(store.index_path)},'transport':transport,'slots':slots,
        'resources':{'status':'verified','host_before':store.artifact(protocol['host_before_attestation']['path']),
            'host_after':store.artifact(protocol['host_after_attestation_path']),'initial':store.artifact(str(metadata/'initial-resources.json')),
            'sample_metadata':records,'observed_in_scope_births':observed,'explicit_birth_races':races,
            'actual_cli_child_and_gnu_time_root_membership_verified':True,'host_and_container_ancestor_parity_verified':True,
            'limitation':'Once per frozenRSS tracked birth. Reap/reuse races explicit; no continuous migration tracing. Pressure/CPU times diagnostic only; no sample exclusion.'},
        'tool_identity_verification':{'status':'verified','attempts':48,'actual_builds':identities,'prepared_runtimes':prepared,'all_native_inputs_and_settings_bound':True},
        'scope':'User-selected Archon same-container full available16CPU normal-priority unmodified quota profile. Prior local outcomes remain supporting history; no cross-host binary equality claim.',
        'native_record_verification':{'final_validation_hashes_verified':48,'owned_final_evidence_hashes_verified':24,'grading_record_hashes_verified':native_records,
            'legacy_reference_stale_evidence_hashes':legacy_reference_stale,
            'legacy_reference_scope':'Frozen old reference may retain pre-refill evidence hashes; actual native validation hashes bind all retained final reference boards. Owned candidate hashes must match final bytes.'},
        'tracker_writes':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--index',type=Path,required=True)
    parser.add_argument('--index-sha256',required=True);parser.add_argument('--matrix-session',required=True)
    parser.add_argument('--matrix-exit-code',type=int,required=True)
    parser.add_argument('--out',type=Path,default=Q/'archon-final-verification.json');args=parser.parse_args()
    require(not args.out.exists(),'refusing to replace independent final verification')
    result=verify_archon(EvidenceStore(args.index,args.index_sha256),args.matrix_session,args.matrix_exit_code)
    args.out.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps({'status':'PASS','report':str(args.out),'sha256':digest(args.out)}))


if __name__=='__main__':main()
