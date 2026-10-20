"""Refresh read-only source and retained-test provenance after the bounded patch."""
import hashlib
import json
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

Q = Path('/home/halbritt/git/kicad-route/build/agent497-qualification')
P = Path('/home/halbritt/.cache/agent-worktrees/kicad-route/agent497-primary')
STATE = Path('/home/halbritt/git/kicad-route/build/agent497-bytecode-state')
COMMIT = 'f0f73da86835ec583f2a2c9128e6893fa59c03b7'
BASE = 'b27a004bc23d0d92330ab4aa9192619a1667b811'
C6 = 'c6c09054f6845442867ed99141ecbcfdc78f39f622cda06ff401e8230bd9bab1'
now = datetime.now(timezone.utc).isoformat()
def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def git(*args): return subprocess.check_output(['git', '-C', str(P), *args], text=True).strip()
def source_sha(root):
    h = hashlib.sha256()
    paths = [p for p in sorted(root.rglob('*')) if p.is_file() and '__pycache__' not in p.parts]
    for p in paths:
        h.update(p.relative_to(root).as_posix().encode()+b'\0')
        h.update(p.read_bytes())
    return {'sha256': h.hexdigest(), 'files': len(paths)}
assert git('rev-parse', 'HEAD') == COMMIT and not git('status', '--porcelain')
changes = git('diff', '--name-only', BASE, COMMIT).splitlines()
assert changes == ['src/kicad_route/cli.py', 'src/kicad_route/common.py', 'tests/test_tool_environment_scope.py']
assert git('diff', '--name-only', BASE, COMMIT, '--', 'src/kicad_route/router') == ''
manifest_path = STATE/'manifest.json'; manifest = json.loads(manifest_path.read_text())
generation = STATE/manifest['krt']['generation']
sources = {'primary': source_sha(P/'src/kicad_route/router'), 'prepared_generation': source_sha(generation/'router')}
assert all(v['sha256'] == C6 for v in sources.values()) and manifest['krt']['source_sha256'] == C6
extension = STATE/manifest['krt']['extension_module']['path']
assert sha(extension) == manifest['krt']['extension_module']['sha256']
union_path = Q/'coverage-union.json'; union = json.loads(union_path.read_text())
assert union['current_source_commit'] == COMMIT
collection = Path(union['current_collection']['path']); assert sha(collection) == union['current_collection']['sha256']
nodes = set(line for line in collection.read_text().splitlines() if line.startswith('tests/') and '::' in line)
assert len(nodes) == 741 and nodes == set(union['passing_evidence_by_node'])
file_mismatches = [name for name, value in union['current_test_file_sha256'].items() if sha(P/name) != value]
assert not file_mismatches and len(union['current_test_file_sha256']) == 47
passing = []; runs = []
for item in union['runs']:
    path = Path(item['path']); assert sha(path) == item['sha256']
    cases = ET.parse(path).getroot().findall('.//testcase')
    passed = set(); failed = []
    for c in cases:
        parts = c.attrib['classname'].split('.')
        for i in range(len(parts), 0, -1):
            name = '/'.join(parts[:i])+'.py'
            if name in union['current_test_file_sha256']:
                node = name+'::'+'::'.join([*parts[i:], c.attrib['name']]); break
        else: raise AssertionError('Unmapped JUnit class: '+c.attrib['classname'])
        if any(c.find(k) is not None for k in ['failure','error','skipped']): failed.append(node)
        else: passed.add(node)
    passing.append(passed)
    runs.append({'path':str(path),'sha256':item['sha256'],'cases':len(cases),'passed':len(passed),'failed_or_skipped':failed})
bad_links = [(node,index) for node, indices in union['passing_evidence_by_node'].items() for index in indices if node not in passing[index]]
assert not bad_links and all(any(node in s for s in passing) for node in nodes)
old_union = Q/'coverage-union-before-env-reuse.json'; assert sha(old_union) == '2a1b72f5fb57ef22592162adcf593f72cdcd3dcd58969126c265e5171da3c603'
probe = json.loads((Q/'environment-reuse-microprobe.json').read_text())
assert probe['common_sha256'] == sha(P/'src/kicad_route/common.py')
proof = {'schema':'kicad-route/env-reuse-source-parity/1','observed_utc':now,'scope':'Read-only Git/source/hash/retained JUnit inspection. No test collection, routing, DRC, build, environment write, tracker or source change.', 'primary':{'path':str(P),'head':COMMIT,'tree':git('rev-parse','HEAD^{tree}'),'status_porcelain':''},'baseline_merge':BASE,'history_merge_proof':str(Q/'doctrine-history-merge.json'),'changed_paths':changes,'routing_engine_changed':False,'owned_source_parity':sources,'source_matches_manifest':True,'generation':str(generation),'manifest_sha256':sha(manifest_path),'extension_sha256':sha(extension),'package_versions':manifest['krt']['venv']['packages'],'package_versions_sha256':manifest['krt']['venv']['packages_sha256'],'production_sha256':{n:sha(P/n) for n in changes if n.startswith('src/')},'environment_scope':{'cached_value':'Only the major version selecting KICAD<n> environment variable names.','lifetime':'ContextVar token reset per decorated run_attempt, nested scopes restore the outer value, outside-scope calls probe fresh.','cache_key':['PATH-resolved executable realpath','st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns'],'failure_policy':'Missing/failed version or unusable executable fingerprint is never cached.','fresh_surfaces':['full environment dict','OUT/HOME/hermetic configuration paths','authoritative full kicad-cli version/hash','source/module/packages/Python identities','manifest/cache/ledger/repeat identities'],'limits':'Executable-stat race and within-attempt same-binary dynamic version changes are not a full identity claim; only major-based environment-name selection is reused.'},'focused_test_observation':runs[-1],'new_test_cases':15,'coverage_union':{'path':str(union_path),'sha256':sha(union_path),'current_cases':741,'current_test_files':47,'junit_sources':5,'collection':str(collection),'collection_sha256':sha(collection),'all_current_nodes_have_valid_passing_links':True,'bad_links':bad_links,'test_file_hash_mismatches':file_mismatches,'runs':runs,'method':union['method']},'historical_union':{'path':str(old_union),'sha256':sha(old_union),'cases':726,'note':'The old final-source-parity.json hash at its former coverage-union.json locator now resolves to this explicitly retained historical copy; the old audit is not rewritten.'},'microprobe':{'path':str(Q/'environment-reuse-microprobe.json'),'sha256':sha(Q/'environment-reuse-microprobe.json'),'medians':probe['medians'],'limitation':probe['limitation']},'benchmark_transfer_boundary':'The new matrix invokes this exact primary commit directly. Prior b2/3bc parity remains historical; no old wrapper-performance pass transfers across this patch. Owned sourceC6 and prepared603 environment remain identical. New four-board five-repeat matrix remains material and pending.','material_gaps':['All four new env-reuse comparisons must independently pass original native quality/equivalence and unchanged1.10 resource gates.']}
(Q/'env-reuse-source-parity.json').write_text(json.dumps(proof,indent=2)+'\n')
print(json.dumps({'proof':str(Q/'env-reuse-source-parity.json'),'primary_commit':COMMIT,'owned_source':sources,'current_cases':741,'junit_sources':len(runs),'focused_passed':runs[-1]['passed'],'new_cases':15},indent=2))
