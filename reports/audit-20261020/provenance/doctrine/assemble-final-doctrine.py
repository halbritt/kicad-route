"""Consume one final doctrine pass only after independent Archon verification.

No remote commands, routing, tests or metric selection. Root must bind observed
terminal report/index/verifier SHA after all48 complete; pending facts fail closed.
"""
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import subprocess

Q=Path('/home/halbritt/git/kicad-route/build/agent497-qualification')
R=Path('/home/halbritt/.local/share/pincite/release')
CONFIG=json.loads((Q/'doctrine-final-config.json').read_text())
SECOND=Path('/tmp/kicad-route-agent497-doctrine-second-final.json')

def checked(path,expected):
    if not path or not expected:raise SystemExit('Final assembly deferred: actual observed artifact/hash not bound.')
    path=Path(path)
    if hashlib.sha256(path.read_bytes()).hexdigest()!=expected:raise SystemExit('Final assembly deferred: artifact hash drift: '+str(path))
    return path

if not CONFIG.get('final_gather_authorized'):raise SystemExit('Final assembly deferred: root final-gather authorization not yet recorded.')
if SECOND.exists():raise SystemExit('Final packet already exists; reuse it rather than consume another evidence pass.')
if CONFIG.get('study_kind')!='archon-same-container-all-available-cpus':raise SystemExit('Final assembly deferred: declared Archon profile not selected.')
for item in [{'path':CONFIG['source_parity_artifact'],'sha256':CONFIG['source_parity_sha256']},CONFIG['focused_tests'],CONFIG['current_coverage_union']]:checked(item['path'],item['sha256'])
protocol=checked(CONFIG['preregistered_protocol'],CONFIG['preregistered_protocol_sha256'])
report_path=checked(CONFIG.get('verification_report'),CONFIG.get('verification_report_sha256'))
index_path=checked(CONFIG.get('retained_index'),CONFIG.get('retained_index_sha256'))
verifier=CONFIG.get('independent_verifier',{})
checked(verifier.get('path'),verifier.get('sha256'))
for item in CONFIG.get('independent_verifier_dependencies',[]):checked(item['path'],item['sha256'])
report=json.loads(report_path.read_text())
if (report.get('evidence_index',{}).get('sha256')!=CONFIG['retained_index_sha256'] or report.get('protocol',{}).get('sha256')!=CONFIG['preregistered_protocol_sha256']):raise SystemExit('Final assembly deferred: verifier protocol/index bindings differ.')
required={'schema':'kicad-route/archon-final-verification/1','status':'PASS','host':'archon',
 'observed_matrix_exit_code':0,'completed_boards':4,'complete_qualified_attempts':48,
 'all_comparisons_passed':True,'gate_limit':1.10,'measured_repetitions_per_side':5,'separate_warmups_per_board':2}
if any(report.get(key)!=value for key,value in required.items()):raise SystemExit('Final assembly deferred: independently verified Archon population/gates not PASS.')
if not CONFIG.get('matrix_session') or report.get('matrix_session')!=CONFIG['matrix_session']:raise SystemExit('Final assembly deferred: observed root matrix session not bound.')
slots=report.get('slots',[])
if len(slots)!=4 or any(row.get('status')!='passed' for row in slots):raise SystemExit('Final assembly deferred: four independently qualified board slots required.')
if not report.get('resources') or not report.get('tool_identity_verification'):raise SystemExit('Final assembly deferred: resource/toolchain proof missing.')
now=datetime.now(timezone.utc).isoformat()
summary={'schema':'kicad-route/final-matrix-observation/2','observed_utc':now,
 'method':'Read hash-pinned independent portable Archon verifier output, exact locator/SHA inventory, frozen protocol and source/test prerequisites. No routing/test or remote execution.',
 'configuration':CONFIG,'independent_verification':report,'qualified_observed_matrix':True,
 'limits':'Four finite sanitized workloads, same Archon container/allavailable16CPU; five measured samples plus separatewarmups per side. Sampled summedRSS lowerbounds truepeak. Historical local failures retained; no universal performance inference or acceptance authority transfer.'}
observation=Q/'final-matrix-observation.json';observation.write_text(json.dumps(summary,indent=2)+'\n')
files=[observation,report_path,index_path,protocol,Path(verifier['path']),*[Path(item['path']) for item in CONFIG.get('independent_verifier_dependencies',[])]]
record={'schema_version':'evidence-record/1','id':'final-matrix-observations','evidence_class':'evidence-runtime-observation',
 'summary':'The independently retained Archon final verification qualifies allfourboards/all48 warmup+measured attempts under the frozen samecontainer16CPU profile: unchanged fullCLIwall, GNUindividualRSS and sampledconcurrenttreeRSS median gates each1.10, exactcopper equivalence, COMPLETErouting/PASSnativevalidation/process/preservation, no newviolations/openconnections/wallclockstops and successfulpairs. Actual source/tool/module/package/manifest identities, host+container controls and observed childmembership are verified, complete rawGNU/result locators resolve through exact SHAinventory. This is a finite samehost/toolchain/resources observation, not universal performance or selfacceptance. Six frozen pre70reference evidence hashes remain explicitly stale, while native finalboard validation hashes match all48 and candidate final evidence hashes match24/24; this is fixclosure, not a native/performance gate waiver. Both local1CPU-ratePASS and earlier localfailed/inconclusive studies remain historical.741production cases are a union of passing observations, not one fullgreensuite.',
 'provenance':[{'locator':str(path),'method':summary['method'],'observed_at':now,'content_sha256':hashlib.sha256(path.read_bytes()).hexdigest()} for path in files],
 'satisfies':['claim-relative observation such as accepted requirement, reproduction, incident, recurring change, operational constraint, or representative measurement']}
record_path=Path('/tmp/kicad-route-agent497-final-matrix-observations.json');record_path.write_text(json.dumps(record,indent=2)+'\n')
prep=json.loads((Q/'doctrine-preparation.json').read_text())
args=[str(R/'doctrine/bin/pincite'),'--index',str(R/'doctrine/runtime/doctrine-index.sqlite3'),'--doctrine-root',str(R/'doctrine'),'--role','coding-agent','--task','defect-repair','--question','How should authorized evidence, publication and update defects be repaired while preserving board and filesystem boundaries?','--signal','failure paths','--language','Python','--risk','correctness','--budget','5000','--detail','compact']
for item in prep['records']:
    checked(item['path'],item['sha256']);args+=['--evidence',item['path']]
args+=['--evidence',str(record_path),'--out',str(SECOND),'--render','markdown']
env=dict(os.environ,PINCITE_TRACE_DIR=str(Path.home()/'.local/share/pincite/trace'))
with (Q/'doctrine-second-final.md').open('w') as out,(Q/'doctrine-second-final.stderr').open('w') as err:
    result=subprocess.run(args,env=env,stdout=out,stderr=err,check=False)
if result.returncode:raise SystemExit(result.returncode)
print(json.dumps({'qualified_observed_matrix':True,'packet':str(SECOND),'render':str(Q/'doctrine-second-final.md')}))
