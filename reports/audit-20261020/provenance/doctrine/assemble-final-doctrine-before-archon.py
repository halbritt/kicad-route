"""Read retained comparisons, assemble final typed evidence, never run routing/tests."""
import hashlib
import json
import math
import os
import statistics
import subprocess
from datetime import datetime, timezone
from pathlib import Path

Q = Path('/home/halbritt/git/kicad-route/build/agent497-qualification')
R = Path('/home/halbritt/.local/share/pincite/release')
CONFIG = json.loads((Q/'doctrine-final-config.json').read_text())
if not CONFIG.get('candidate_ready') or not CONFIG.get('candidate_commit') or not CONFIG.get('source_parity_artifact'):
    raise SystemExit('Final assembly deferred: committed candidate, focused tests and refreshed source parity are not pinned.')
for item in [{'path': CONFIG['source_parity_artifact'], 'sha256': CONFIG['source_parity_sha256']}, CONFIG['focused_tests'], CONFIG['current_coverage_union']]:
    if hashlib.sha256(Path(item['path']).read_bytes()).hexdigest() != item['sha256']:
        raise SystemExit('Final assembly deferred: source/test prerequisite artifact hash changed: ' + item['path'])
if Path('/tmp/kicad-route-agent497-doctrine-second-final.json').exists():
    raise SystemExit('Final doctrine packet already exists; reuse it rather than consuming another evidence pass.')
authority = Path(CONFIG['terminal_authority'])
if not authority.exists():
    raise SystemExit('Final assembly deferred: selected matrix authority does not exist.')
terminal = json.loads(authority.read_text())
if terminal.get('status') != 'PASS' or terminal.get('all_comparisons_passed') is not True or terminal.get('completed_samples') != 48 or len(terminal.get('boards', [])) != 4 or any(row.get('status') != 'PASS' or row.get('completed_samples') != 12 for row in terminal.get('boards', [])):
    raise SystemExit('Final assembly deferred: selected matrix is not terminal PASS with all four boards and48 samples.')
protocol = Path(CONFIG['preregistered_protocol'])
if hashlib.sha256(protocol.read_bytes()).hexdigest() != CONFIG['preregistered_protocol_sha256']:
    raise SystemExit('Final assembly deferred: preregistered protocol changed.')
metadata_protocol = authority.parent/'protocol.json'
if hashlib.sha256(metadata_protocol.read_bytes()).hexdigest() != terminal.get('protocol_sha256') or metadata_protocol.read_bytes() != protocol.read_bytes():
    raise SystemExit('Final assembly deferred: executed protocol differs from preregistered protocol.')
if terminal.get('resource_violations') or terminal.get('cpu_cap') != CONFIG['cpu_cap']:
    raise SystemExit('Final assembly deferred: CPU-rate resource parity did not qualify.')
selection_path = Path(CONFIG['calibration_selection']['path'])
if hashlib.sha256(selection_path.read_bytes()).hexdigest() != CONFIG['calibration_selection']['sha256']:
    raise SystemExit('Final assembly deferred: selected calibration hash changed.')
selection = json.loads(selection_path.read_text())
if selection.get('status') != 'PASS' or selection.get('selected_cap_cpus') != CONFIG['cpu_cap']:
    raise SystemExit('Final assembly deferred: selected calibration is not PASS for declared cap.')
trials = selection.get('caps', {}).get(str(CONFIG['cpu_cap']), [])
if len(trials) != 3 or any(t.get('status') != 'PASS' or t.get('verdict', {}).get('passed') is not True or t.get('verdict', {}).get('service_fraction', 0) < .90 for t in trials):
    raise SystemExit('Final assembly deferred: all three selected calibration trials did not qualify.')
executed_protocol = json.loads(protocol.read_text())
initial = json.loads((authority.parent/'initial-resources.json').read_text())
leaf = initial['ancestors'][0]['controls']
if initial.get('cpu_affinity') != list(range(12)) or initial.get('nice') != 0 or initial.get('scheduler') != 0 or Path(initial['group']).name != executed_protocol['scope_unit'] or leaf['cpu.max'].get('text', '').split() != [str(CONFIG['cpu_cap']*100000), '100000'] or any(leaf[key].get('text', '').strip() != value for key,value in [('cpu.max.burst','0'),('cpu.weight','100'),('cpu.idle','0')]):
    raise SystemExit('Final assembly deferred: actual initial rate profile differs from preregistration.')
def controls(snapshot):
    return {key:snapshot[key] for key in ('group','cpu_affinity','nice','scheduler')} | {'ancestors':[{'path':row['path'],'controls':row['controls']} for row in snapshot['ancestors']]}
expected_controls = controls(initial)
resource_files = []
for board in executed_protocol['boards']:
    for side in ('reference','candidate'):
        for index in range(6):
            path = Path(CONFIG['sample_resources'])/(board['name']+'-'+side+'-'+str(index)+'.json')
            if not path.exists():
                raise SystemExit('Final assembly deferred: missing sample resources: '+str(path))
            row = json.loads(path.read_text())
            if row.get('resource_violations') or controls(row['before']) != expected_controls or controls(row['after']) != expected_controls:
                raise SystemExit('Final assembly deferred: per-sample scope/ancestor controls did not match.')
            observed = row['membership_monitor']
            if observed.get('outside_scope') or observed.get('read_errors') or observed.get('observed_births', 0) < 1 or observed.get('sampler_calls', 0) < 1:
                raise SystemExit('Final assembly deferred: sampled child membership did not qualify.')
            if any(member.get('in_scope') is False for member in observed['members']):
                raise SystemExit('Final assembly deferred: observed descendant outside scope.')
            resource_files.append({'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
NAMES = CONFIG['comparisons']
now = datetime.now(timezone.utc).isoformat()
paths = [Q/name/'comparison.json' for name in NAMES]
missing = [str(p) for p in paths if not p.exists()]
if missing:
    raise SystemExit('Final assembly deferred: missing terminal comparisons: ' + ', '.join(missing))
observations = []
all_errors = []
for name, path in zip(NAMES, paths):
    d = json.loads(path.read_text())
    errors = []
    verdict = d['verdict']
    if not verdict.get('passed'): errors.append('Terminal comparison failed')
    if d.get('mode') != 'migration-equivalence': errors.append('Wrong equivalence mode')
    if len(d['reference']) != 5 or len(d['candidate']) != 5: errors.append('Expected 5 measured samples per side')
    if set(d.get('warmup', {})) != {'reference', 'candidate'}: errors.append('Missing separate warmups')
    samples = d['reference'] + d['candidate'] + list(d.get('warmup', {}).values())
    fingerprint, copper, files, preservation = (None,)*4
    for sample in samples:
        q = sample.get('quality', {})
        if sample.get('exit_code') != 0: errors.append('CLI did not exit 0')
        if q.get('process') != 'PASS' or q.get('routing') != 'COMPLETE' or q.get('validation') != 'PASS': errors.append('Native quality not PASS/COMPLETE/PASS')
        if q.get('unconnected') != 0 or q.get('open_nets') != [] or q.get('new_violations') != 0: errors.append('Connections or new violations not clean')
        if q.get('wall_clock_stop') is not False: errors.append('Missing/true wall-clock stop')
        if not isinstance(q.get('pairs'), list) or any(p.get('routed') is not True or p.get('status') != 'ROUTED' for p in q.get('pairs', [])): errors.append('Missing/failed pair evidence')
        if not q.get('preservation') or any(v != 'PASS' for v in q['preservation'].values()): errors.append('Missing/failed preservation')
        if not q.get('input_fingerprint') or not q.get('copper') or not sample.get('reference_files'): errors.append('Missing input/copper identity')
        if fingerprint is None: fingerprint, copper, files, preservation = q.get('input_fingerprint'), q.get('copper'), sample.get('reference_files'), set(q.get('preservation', {}))
        if (q.get('input_fingerprint'), q.get('copper'), sample.get('reference_files'), set(q.get('preservation', {}))) != (fingerprint, copper, files, preservation): errors.append('Input/copper/preservation identity differs')
        if not sample.get('tool_identity') or not sample.get('backend') or not sample.get('cli_sha256'): errors.append('Missing tool/backend identities')
        if (sample.get('tool_identity') or {}).get('state_manifest_drift') != []: errors.append('Missing/dirty state manifest drift evidence')
    recalculated = {}
    for metric in ['wall_s', 'max_rss_kib', 'process_tree_peak_rss_kib']:
        values = [s.get(metric) for s in d['reference'] + d['candidate']]
        if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in values):
            errors.append('Invalid metric: ' + metric)
            continue
        before = statistics.median(s[metric] for s in d['reference'])
        after = statistics.median(s[metric] for s in d['candidate'])
        reported = verdict['metrics'][metric]
        recalculated[metric] = {'reference_median': before, 'candidate_median': after, 'ratio': after/before, 'limit': 1.1}
        if reported != recalculated[metric] or after > before*1.1: errors.append('Metric identity/1.10 threshold failed: ' + metric)
    if verdict.get('copper_geometry_equal') is not True: errors.append('Copper equivalence failed')
    candidate_identities = [s['backend'].get('execution_identity') for s in [*d['candidate'], d['warmup']['candidate']]]
    if any(v != candidate_identities[0] for v in candidate_identities): errors.append('Candidate execution identity changed')
    candidate_samples = [*d['candidate'], d['warmup']['candidate']]
    if any(s['backend'].get('commit') != CONFIG['candidate_commit'] for s in candidate_samples): errors.append('Candidate commit differs from pinned final source')
    if any(Path(s['backend'].get('runtime', {}).get('generation', '')).name != CONFIG['candidate_generation'] for s in candidate_samples): errors.append('Candidate generation differs from pinned final environment')
    if candidate_identities[0]['source_sha256'] != CONFIG['candidate_source_sha256']: errors.append('Candidate owned source differs from audited source')
    row = {'name': name, 'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'mode': d['mode'], 'cpu_affinity': d['cpu_affinity'], 'counts': {'reference': len(d['reference']), 'candidate': len(d['candidate']), 'warmups': len(d['warmup'])}, 'metrics': recalculated, 'copper_geometry_equal': verdict.get('copper_geometry_equal'), 'candidate_execution_identity': candidate_identities[0], 'errors': sorted(set(errors)), 'measurement_definition': d['measurement']}
    observations.append(row)
    all_errors.extend([name+': '+e for e in sorted(set(errors))])
summary = {'schema': 'kicad-route/final-matrix-observation/1', 'observed_utc': now, 'method': 'Read retained terminal comparisons; independently recompute medians and verify all measured/warmup native quality, identity and threshold fields. No route/test/DRC execution.', 'configuration': CONFIG, 'cpu_rate_resource_files':resource_files, 'cpu_rate_terminal_authority':terminal, 'cpu_rate_terminal_authority_sha256':hashlib.sha256(authority.read_bytes()).hexdigest(), 'observations': observations, 'errors': all_errors, 'qualified_observed_matrix': not all_errors, 'limits': 'Four finite sanitized workloads, one warmup +5 measured per side; sampled summed RSS is a lower bound on true peak. No universal performance or installed-build measurement classification is inferred. Historical failed matrices retained.'}
(Q/'final-matrix-observation.json').write_text(json.dumps(summary, indent=2)+'\n')
if all_errors:
    raise SystemExit('Final doctrine reassembly held: qualification errors remain; inspect final-matrix-observation.json. No final evidence pass was consumed.')
record = {'schema_version':'evidence-record/1', 'id':'final-matrix-observations', 'evidence_class':'evidence-benchmarks', 'summary': ('All four final configured comparisons and their raw warmup/measured samples satisfy unchanged1.10 wall/individualRSS/sampledprocess-treeRSS gates, exact copper equivalence, COMPLETE routing, PASS native process/validation/preservation,0new violations/open connections, successful pairs and no wall-clock stops. Independent receipt-field/median audit found no errors.' if not all_errors else 'Final configured matrix is not qualified. Independent retained-comparison audit reports: '+ '; '.join(all_errors)) + ' Each board has5measured samples per side plus1warmup per side, full affinity0–11 inside the preregistered1CPU aggregate-rate scope, retained input/tool/backend identities, cgroup/ancestor/birth-membership evidence and raw samples. Candidate commit, generation and owned source are pinned by doctrine-final-config.json and its refreshed source-parity artifact. This can qualify only the declared conditionalCPU-rate profile. Quota is a maximum, not reserved service; native throttling/waiting remains inside fullCLIwall. Host variability and sampledRSS limits remain; no unrestricted12CPU performance or self-acceptance is inferred. Prior failed performance matrices remain retained.', 'provenance':[{'locator': str(Q/'final-matrix-observation.json'),'method':summary['method'],'observed_at':now,'content_sha256':hashlib.sha256((Q/'final-matrix-observation.json').read_bytes()).hexdigest()}, *[{'locator':row['path'],'method':'Read raw comparison and independently verify fields','observed_at':now,'content_sha256':row['sha256']} for row in observations]], 'satisfies':['claim-relative observation such as accepted requirement, reproduction, incident, recurring change, operational constraint, or representative measurement']}
p = Path('/tmp/kicad-route-agent497-final-matrix-observations.json')
p.write_text(json.dumps(record,indent=2)+'\n')
prep = json.loads((Q/'doctrine-preparation.json').read_text())
args = [str(R/'doctrine/bin/pincite'), '--index', str(R/'doctrine/runtime/doctrine-index.sqlite3'), '--doctrine-root', str(R/'doctrine'), '--role','coding-agent','--task','defect-repair','--question','How should authorized evidence, publication and update defects be repaired while preserving board and filesystem boundaries?','--signal','failure paths','--language','Python','--risk','correctness','--budget','5000','--detail','compact']
for item in prep['records']: args += ['--evidence', item['path']]
args += ['--evidence', str(p), '--out', '/tmp/kicad-route-agent497-doctrine-second-final.json', '--render','markdown']
env = dict(os.environ, PINCITE_TRACE_DIR=str(Path.home()/'.local/share/pincite/trace'))
with (Q/'doctrine-second-final.md').open('w') as out, (Q/'doctrine-second-final.stderr').open('w') as err:
    result = subprocess.run(args, env=env, stdout=out, stderr=err, check=False)
if result.returncode: raise SystemExit(result.returncode)
print(json.dumps({'qualified_observed_matrix':not all_errors,'errors':all_errors,'packet':'/tmp/kicad-route-agent497-doctrine-second-final.json','render':str(Q/'doctrine-second-final.md')},indent=2))
