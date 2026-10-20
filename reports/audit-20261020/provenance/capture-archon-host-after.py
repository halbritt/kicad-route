#!/usr/bin/env python3
"""Host-side post-terminal metadata capture; no native jobs or control changes."""
import datetime
import json
import os
from pathlib import Path
import subprocess

ROOT = Path('/home/halbritt/.cache/agent-worktrees/kicad-route/agent497-archon')
container = 'kicadroute-agent497-archon'
matrix = json.loads((ROOT/'evidence/archon-metadata/matrix.json').read_text())
if matrix['status'] not in ('PASS','FAIL','EXECUTION_ERROR','INTERRUPTED'):
    raise SystemExit('Refusing host-after capture before matrix terminal')
info = json.loads(subprocess.check_output(['sudo','-n','docker','inspect',container],text=True))[0]
pid = info['State']['Pid']
raw = Path(f'/proc/{pid}/cgroup').read_text()
relative = next(line.split('::',1)[1] for line in raw.splitlines() if line.startswith('0::'))
mount = Path('/sys/fs/cgroup');leaf = mount/relative.lstrip('/');ancestors=[]
for path in (leaf,*leaf.parents):
    if path != mount and not path.is_relative_to(mount):break
    row={'path':str(path),'controls':{}}
    for key in ('cpu.max','cpu.max.burst','cpu.weight','cpu.idle','cpuset.cpus.effective','memory.max','memory.high','memory.current','cpu.stat','cpu.pressure','io.pressure'):
        file=path/key;row['controls'][key]=file.read_text().strip() if file.exists() else None
    ancestors.append(row)
    if path==mount:break
value={'schema':'kicad-route/archon-host-container-attestation/1',
       'observed_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'host':'archon','actual_hostname':os.uname().nodename,
       'container_id':info['Id'],'container_hostname':info['Config']['Hostname'],'image_id':info['Image'],
       'init_pid':pid,'raw_init_cgroup':raw,'host_leaf':str(leaf),'init_affinity':sorted(os.sched_getaffinity(pid)),
       'init_nice':os.getpriority(os.PRIO_PROCESS,pid),'init_scheduler':os.sched_getscheduler(pid),'ancestors':ancestors,
       'docker_resource_config':{key:info['HostConfig'].get(key) for key in ['NanoCpus','CpuShares','CpuPeriod','CpuQuota','CpuRealtimePeriod','CpuRealtimeRuntime','CpusetCpus','CpusetMems','Memory','MemorySwap','CgroupParent']},
       'host_meminfo':Path('/proc/meminfo').read_text(),'host_cpu_pressure':Path('/proc/pressure/cpu').read_text(),
       'host_io_pressure':Path('/proc/pressure/io').read_text(),
       'owned_container_processes':subprocess.check_output(['sudo','-n','docker','top',container,'-eo','pid,ppid,comm,args'],text=True),
       'observation_scope':'After parent observed sole matrix8896 terminal; metadata capture only, no native reruns or controls changed',
       'observed_matrix_status':matrix['status'],'observed_completed_samples':matrix['completed_samples']}
(ROOT/'evidence/after-container-inspect.json').write_text(json.dumps(info,indent=2)+'\n')
output=ROOT/'evidence/after-host-attestation.json'
if output.exists():raise SystemExit('Refusing to replace prior host-after attestation')
output.write_text(json.dumps(value,indent=2)+'\n')
print(json.dumps(value,indent=2))
