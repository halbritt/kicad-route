"""Exact saved-board copper custody and disposable native removal."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from helpers import ROOT
from kicad_route import common


def native_facts(board, out):
    subprocess.run(['/usr/bin/python3', str(ROOT / 'src/kicad_route/kicad/facts.py'),
                    str(board), str(out)], check=True, capture_output=True)
    return json.loads(out.read_text())


def owned_run(tmp_path, width_twin=False, branching=False, reorder=False):
    run = tmp_path / 'run'
    ref = run / 'reference'
    cand = run / 'candidate'
    ref.mkdir(parents=True)
    cand.mkdir()
    shutil.copyfile(ROOT / 'fixtures/route_demo/route_demo.kicad_pro', ref / 'board.kicad_pro')
    shutil.copyfile(ref / 'board.kicad_pro', cand / 'board.kicad_pro')
    script = '''import pcbnew,sys
b=pcbnew.LoadBoard(sys.argv[1]); b.Save(sys.argv[2])
s=next(t for t in b.GetTracks() if not isinstance(t,pcbnew.PCB_VIA))
t=pcbnew.PCB_TRACK(b); t.SetStart(s.GetStart()); t.SetEnd(s.GetEnd()); t.SetWidth(s.GetWidth()+int(sys.argv[4])); t.SetLayer(s.GetLayer()); t.SetNetCode(s.GetNetCode()); b.Add(t)
v=pcbnew.PCB_VIA(b);v.SetPosition(s.GetStart());v.SetWidth(600000);v.SetDrill(300000);v.SetLayerPair(pcbnew.F_Cu,pcbnew.B_Cu);v.SetNetCode(s.GetNetCode());b.Add(v)
if sys.argv[5]=='1':
 for delta in (1000000,2000000):
  q=pcbnew.PCB_TRACK(b);q.SetStart(s.GetStart());q.SetEnd(pcbnew.VECTOR2I(s.GetStart().x+delta,s.GetStart().y+delta));q.SetWidth(s.GetWidth());q.SetLayer(s.GetLayer());q.SetNetCode(s.GetNetCode());b.Add(q)
if sys.argv[6]=='1':
 for q in list(b.GetTracks()):b.Remove(q);b.Add(q)
b.Save(sys.argv[3])
'''
    subprocess.run(['/usr/bin/python3', '-c', script,
                    str(ROOT / 'fixtures/route_demo/route_demo.kicad_pcb'),
                    str(ref / 'board.kicad_pcb'), str(cand / 'board.kicad_pcb'),
                    '100000' if width_twin else '0', '1' if branching else '0', '1' if reorder else '0'], check=True, capture_output=True)
    (run / 'facts').mkdir()
    (run / 'validate').mkdir()
    a = native_facts(ref / 'board.kicad_pcb', run / 'facts/reference-facts.json')
    b = native_facts(cand / 'board.kicad_pcb', run / 'validate/candidate-facts.json')
    attempt = {'backend': 'builtin', 'board': 'board.kicad_pcb', 'reference_dir': 'reference',
               'candidate_dir': 'candidate', 'reference_facts': 'facts/reference-facts.json',
               'scope': {'nets': ['/SIG_B']}, 'settings': {'protect': 'locked'}}
    common.write_json(run / 'attempt.json', attempt)
    from kicad_route import evidence, campaign, native_components
    # Synthetic grade records qualify the authorization boundary in unit tests;
    # the separate real CLI test exercises actual KiCad DRC grading.
    checks = [{'check': n, 'status': 'PASS'} for n in sorted(campaign.REQUIRED_PRESERVATION)]
    common.write_json(run / 'validate/preservation.json', checks)
    common.write_json(run / 'validate/new-findings.json', {'violations': [], 'schematic_parity': []})
    for n in ('candidate-drc.json', 'candidate-refill.json'):
        common.write_json(run / 'validate' / n, {'violations': [], 'schematic_parity': [], 'unconnected_items': []})
    common.write_json(run / 'validate/pour-integrity.json', {})
    native_components.extract(ref / 'board.kicad_pcb', run / 'validate/reference-terminal-components.json')
    native_components.extract(cand / 'board.kicad_pcb', run / 'validate/candidate-terminal-components.json')
    from kicad_route import baselinecache
    terminal = json.loads((run / 'validate/reference-terminal-components.json').read_text())
    identity = {'schema': 2, 'input': 'fixture', 'board': 'board.kicad_pcb',
                'native_terminals': {'producer': terminal['identity']['producer'], 'tool': terminal['identity']['pcbnew']},
                'kicad_cli': terminal['identity']['kicad_cli']}
    generation = run / 'baseline/generation-fixture'
    generation.mkdir(parents=True)
    common.write_json(generation / 'baseline-drc.json', {'violations': [], 'unconnected_items': [], 'schematic_parity': []})
    common.write_json(generation / 'baseline-info.json', {
        'source': {'board_sha256': a['sha256'], 'rules': native_components.rules_identity(ref / 'board.kicad_pcb')},
        'refill': {'board_sha256_before': a['sha256'], 'board_sha256_after': a['sha256']}, 'board_sha256_graded': a['sha256']})
    common.write_json(generation / 'baseline-pour.json', {})
    shutil.copyfile(run / 'validate/reference-terminal-components.json', generation / native_components.FACTS_NAME)
    # Use the declared cache filename rather than a guessed pour sidecar name.
    from kicad_route import pour
    if pour.FACTS_NAME != 'baseline-pour.json':
        (generation / 'baseline-pour.json').rename(generation / pour.FACTS_NAME)
    common.write_json(generation / 'generation.json', {'generation': generation.name, 'identity': identity,
                      'files': {name: common.sha256(generation / name) for name in baselinecache.FILES}})
    attempt.update(input_fingerprint='fixture', baseline={'report': 'baseline/generation-fixture/baseline-drc.json', 'identity': identity})
    common.write_json(run / 'attempt.json', attempt)
    validation = {'status': 'PASS', 'candidate_board_sha256': b['sha256'],
                  'validated_board_sha256': b['sha256'], 'preservation': {c['check']: c['status'] for c in checks},
                  'drc': {'new_violations': {'count': 0}, 'new_parity': {'count': 0}},
                  'drc_commands': [{'argv': ['--refill-zones', '--save-board'], 'exit_code': 0},
                                   {'argv': ['--schematic-parity', '--severity-all'], 'exit_code': 0}]}
    result = {'kind': 'route', 'exit_code': 0,
              'routing': {'status': 'COMPLETE', 'unconnected_in_scope': 0, 'unrouted_nets': []}, 'backend': {'name': 'builtin'}, 'process': {'status': 'PASS'},
              'validation': validation, 'tool_identity': {'backend': {'name': 'builtin', 'source_sha256': 'fixture'}},
              'evidence': evidence.block(run, attempt, run / 'validate', validation)}
    return run, attempt, result, a, b


def seal(run, attempt, result):
    from kicad_route import copper_ownership
    result['copper_ownership'] = copper_ownership.emit(run, attempt, result)
    result['evidence']['records']['validate/copper-ownership.json'] = common.sha256(run / 'validate/copper-ownership.json')
    common.write_json(run / 'result.json', result)


def test_native_saved_twins_remove_only_generated_whole_net(tmp_path):
    from kicad_route import copper_ownership
    run, attempt, result, before, after = owned_run(tmp_path)
    seal(run, attempt, result)
    original = (run / 'candidate/board.kicad_pcb').read_bytes()
    receipt = copper_ownership.prepare_disposable(run, ['/SIG_B'], tmp_path / 'prepared', 20)
    assert receipt['status'] == 'prepared'
    actual = native_facts(tmp_path / 'prepared/board.kicad_pcb', tmp_path / 'out-facts.json')
    assert sorted(actual['tracks'], key=lambda x: x['uuid']) == sorted(before['tracks'], key=lambda x: x['uuid'])
    assert sorted(actual['vias'], key=lambda x: x['uuid']) == sorted(before['vias'], key=lambda x: x['uuid'])
    assert len(receipt['removed_uuids']) == 2
    assert (run / 'candidate/board.kicad_pcb').read_bytes() == original
    assert after['sha256'] != actual['sha256']


@pytest.mark.parametrize('change', ['stale_board', 'rules', 'bad_grade', 'malformed', 'partial', 'pair_split', 'existing', 'symlink'])
def test_refused_preparation_never_changes_incumbent(tmp_path, change):
    from kicad_route import copper_ownership as c
    run, attempt, result, before, after = owned_run(tmp_path)
    if change == 'pair_split':
        attempt['pairs'] = [{'p': '/SIG_B', 'n': '/SIG_A'}]
        common.write_json(run / 'attempt.json', attempt)
        result['evidence']['records']['attempt.json'] = common.sha256(run / 'attempt.json')
    if change == 'bad_grade':
        result['validation']['status'] = 'FAIL'
    seal(run, attempt, result)
    dest = tmp_path / 'prepared'
    if change == 'stale_board':
        with (run / 'candidate/board.kicad_pcb').open('a') as f:
            f.write('\n')
    if change == 'rules':
        with (run / 'candidate/board.kicad_pro').open('a') as f:
            f.write(' ')
    if change in ('malformed', 'partial'):
        path = run / c.MANIFEST
        doc = json.loads(path.read_text())
        if change == 'partial':
            doc['nets']['/SIG_B']['generated_uuids'].pop()
            common.write_json(path, doc)
        else:
            path.write_text('[]')
        result['copper_ownership']['sha256'] = common.sha256(path)
        result['evidence']['records'][c.MANIFEST] = common.sha256(path)
        common.write_json(run / 'result.json', result)
    if change == 'existing':
        dest.mkdir()
    if change == 'symlink':
        dest.symlink_to(tmp_path / 'absent')
    original = (run / 'candidate/board.kicad_pcb').read_bytes()
    with pytest.raises(c.OwnershipError):
        c.prepare_disposable(run, ['/SIG_B'], dest, 20)
    assert (run / 'candidate/board.kicad_pcb').read_bytes() == original
    assert not (dest / 'receipt.json').exists()
    assert not list(tmp_path.glob('.copper-ownership-*'))


@pytest.mark.parametrize('rewrite', ['changed', 'reminted'])
def test_modified_input_lineage_cannot_become_removable(tmp_path, rewrite):
    from kicad_route import copper_ownership as c
    run, attempt, result, before, after = owned_run(tmp_path)
    path = run / 'candidate/board.kicad_pcb'
    if rewrite == 'reminted':
        import uuid
        path.write_text(path.read_text().replace(before['tracks'][0]['uuid'], str(uuid.uuid4())))
    subprocess.run(['/usr/bin/python3', '-c', '''import pcbnew,sys
b=pcbnew.LoadBoard(sys.argv[1]); u=sys.argv[2]
if sys.argv[3]=='changed':
 t=next(t for t in b.GetTracks() if t.m_Uuid.AsString()==u);t.SetWidth(t.GetWidth()+10000)
b.Save(sys.argv[1])
''', str(path), before['tracks'][0]['uuid'], rewrite], check=True, capture_output=True)
    new = native_facts(path, run / 'validate/candidate-facts.json')
    result['validation']['candidate_board_sha256'] = new['sha256']
    result['validation']['validated_board_sha256'] = new['sha256']
    result['evidence']['candidate_board_sha256'] = new['sha256']
    result['evidence']['records']['validate/candidate-facts.json'] = common.sha256(run / 'validate/candidate-facts.json')
    seal(run, attempt, result)
    doc = json.loads((run / c.MANIFEST).read_text())
    assert 'input_lineage_unknown' in doc['nets']['/SIG_B']['reasons']
    assert doc['nets']['/SIG_B']['eligible'] is False
    with pytest.raises(c.OwnershipError):
        c.prepare_disposable(run, ['/SIG_B'], tmp_path / 'prepared', 20)


@pytest.mark.parametrize('failure', ['timeout', 'save', 'mutation'])
def test_native_failure_keeps_source_and_publishes_nothing(tmp_path, monkeypatch, failure):
    from kicad_route import copper_ownership as c
    run, attempt, result, before, after = owned_run(tmp_path)
    seal(run, attempt, result)
    original = (run / 'candidate/board.kicad_pcb').read_bytes()
    real_run = subprocess.run
    def fail(command, **kwargs):
        if 'remove_owned.py' not in str(command[1]):
            return real_run(command, **kwargs)
        if failure == 'timeout':
            raise subprocess.TimeoutExpired(command, kwargs['timeout'])
        # Inject a genuine native Save failure while retaining actual mutation.
        script = Path(command[1])
        inject = tmp_path / 'fail-save.py'
        patch = ("pcbnew.BOARD.Save=lambda *a: False\n" if failure == 'save' else
                 "remove=pcbnew.BOARD.Remove\ndef broken(self,obj):\n remove(self,obj)\n raise RuntimeError('native mutation failed')\npcbnew.BOARD.Remove=broken\n")
        inject.write_text("import sys,runpy,pcbnew\n" + patch +
                          "sys.path.insert(0," + repr(str(script.parent)) + ")\n"
                          "sys.argv=" + repr(command[1:]) + "\n"
                          "runpy.run_path(sys.argv[0],run_name='__main__')\n")
        return real_run([command[0], str(inject)], **kwargs)
    monkeypatch.setattr(c.subprocess, 'run', fail)
    with pytest.raises(c.OwnershipError, match='timed out|native save failed|native mutation failed'):
        c.prepare_disposable(run, ['/SIG_B'], tmp_path / 'prepared', 20)
    assert (run / 'candidate/board.kicad_pcb').read_bytes() == original
    assert not (tmp_path / 'prepared').exists()
    assert not list(tmp_path.glob('.copper-ownership-*'))


def test_width_twin_multipin_branches_are_one_unit_after_reordering(tmp_path):
    from kicad_route import copper_ownership as c
    run, attempt, result, before, after = owned_run(tmp_path, width_twin=True, branching=True, reorder=True)
    seal(run, attempt, result)
    receipt = c.prepare_disposable(run, ['/SIG_B'], tmp_path / 'prepared', 20)
    assert len(receipt['removed_uuids']) == 4
    actual = native_facts(tmp_path / 'prepared/board.kicad_pcb', tmp_path / 'survivors.json')
    assert {x['uuid'] for x in actual['tracks'] + actual['vias']} == {x['uuid'] for x in before['tracks'] + before['vias']}
    assert receipt['inherited_fill'] == 'stale_after_copper_removal'


def test_native_nontrack_pad_uuid_change_is_rejected(tmp_path, monkeypatch):
    from kicad_route import copper_ownership as c
    run, attempt, result, before, after = owned_run(tmp_path)
    seal(run, attempt, result)
    original = (run / 'candidate/board.kicad_pcb').read_bytes()
    real_run = subprocess.run
    def corrupt(command, **kwargs):
        if 'remove_owned.py' not in str(command[1]):
            return real_run(command, **kwargs)
        script = Path(command[1])
        inject = tmp_path / 'change-pad.py'
        inject.write_text(r'''import sys,runpy,pcbnew,re,uuid
from pathlib import Path
saved=pcbnew.BOARD.Save
def altered(self,path):
 r=saved(self,path)
 p=Path(path)
 p.write_text(re.sub(r'(\(pad[\s\S]*?\(uuid ")([^"]+)',lambda m:m[1]+str(uuid.uuid4()),p.read_text(),count=1))
 return r
pcbnew.BOARD.Save=altered
''' + "sys.path.insert(0," + repr(str(script.parent)) + ")\n" +
            "sys.argv=" + repr(command[1:]) + "\nrunpy.run_path(sys.argv[0],run_name='__main__')\n")
        return real_run([command[0], str(inject)], **kwargs)
    monkeypatch.setattr(c.subprocess, 'run', corrupt)
    with pytest.raises(c.OwnershipError, match='canonical non-track'):
        c.prepare_disposable(run, ['/SIG_B'], tmp_path / 'prepared', 20)
    assert (run / 'candidate/board.kicad_pcb').read_bytes() == original
    assert not (tmp_path / 'prepared').exists()


def test_real_owned_route_emits_consumable_final_custody(tmp_path):
    import os
    from kicad_route import copper_ownership as c
    selected = Path(os.environ.get('KERNEL_PYTHON', '/home/halbritt/git/kicad-route/.router-generations/84b30dc286f44df6b465ae8d40aa1363/venv/bin/python'))
    if not selected.exists():
        pytest.skip('selected routing interpreter unavailable')
    project = tmp_path / 'input'
    shutil.copytree(ROOT / 'fixtures/route_demo', project)
    state = tmp_path / 'state'
    state.mkdir()
    (state / '.venv-krt').symlink_to(selected.parents[1], target_is_directory=True)
    env = dict(os.environ, KICAD_ROUTE_STATE=str(state), KICAD_ROUTE_LEDGER='off',
               KICAD_NET_RESCUE='0', KICAD_PLANE_FINALIZE='0', KICAD_GLOBAL_PLAN='0')
    run = tmp_path / 'actual-run'
    cmd = ['/usr/bin/python3', str(ROOT / 'bin/kicad-route'), '--out', str(tmp_path / 'out'),
           'run', '--backend', 'builtin', '--dir', str(project), '--run-dir', str(run),
           '--nets', '/SIG_A', '--default-clearance', '0.4', '--budget', '20', '--max-iterations', '5000', '--protect', 'locked', '--json']
    proc = subprocess.run(cmd, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    (tmp_path / 'actual-route.log').write_text(proc.stdout + '\n' + proc.stderr)
    assert proc.returncode == 0, (proc.stdout[-1500:], proc.stderr[-1500:])
    result = json.loads((run / 'result.json').read_text())
    assert result['validation']['status'] == 'PASS'
    assert result['copper_ownership']['status'] == 'bound', result.get('copper_ownership')
    receipt = c.prepare_disposable(run, ['/SIG_A'], tmp_path / 'prepared', 20)
    assert receipt['removed_uuids']
    actual = native_facts(tmp_path / 'prepared/route_demo.kicad_pcb', tmp_path / 'prepared-facts.json')
    assert len(actual['tracks']) == 5 and len(actual['vias']) == 2
    assert receipt['survivors_verified'] and receipt['nontrack_verified']
    assert receipt['native_grade'] == 'not_run_on_prepared_board'


@pytest.mark.parametrize('change', ['missing_check', 'altered_drc', 'altered_terminals', 'missing_refill', 'dotdot', 'nonfinite', 'bad_selected'])
def test_required_grade_records_and_destination_are_verified(tmp_path, change):
    from kicad_route import copper_ownership as c
    run, attempt, result, before, after = owned_run(tmp_path)
    if change == 'missing_check':
        result['validation']['preservation'].pop('net_completion')
        common.write_json(run / 'validate/preservation.json', [
            {'check': k, 'status': v} for k, v in result['validation']['preservation'].items()])
        result['evidence']['records']['validate/preservation.json'] = common.sha256(run / 'validate/preservation.json')
    if change == 'missing_refill':
        result['evidence']['records'].pop('validate/candidate-refill.json')
    seal(run, attempt, result)
    if change == 'altered_drc':
        common.write_json(run / 'validate/candidate-drc.json', {'violations': [1]})
    if change == 'altered_terminals':
        common.write_json(run / 'validate/candidate-terminal-components.json', {})
    if change == 'nonfinite':
        (run / 'result.json').write_text('{"validation":1e999}')
    original = (run / 'candidate/board.kicad_pcb').read_bytes()
    destination = tmp_path / 'prepared'
    if change == 'dotdot':
        destination = tmp_path / 'outside' / '..' / 'run' / 'prepared'
    selection = [['bad']] if change == 'bad_selected' else ['/SIG_B']
    with pytest.raises(c.OwnershipError):
        c.prepare_disposable(run, selection, destination, 20)
    assert (run / 'candidate/board.kicad_pcb').read_bytes() == original
    assert not (destination / 'receipt.json').exists()


def test_cross_net_remint_blocks_all_generated_ownership(tmp_path):
    from kicad_route import copper_ownership as c
    import uuid
    run, attempt, result, before, after = owned_run(tmp_path)
    path = run / 'candidate/board.kicad_pcb'
    replacement = str(uuid.uuid4())
    path.write_text(path.read_text().replace(before['tracks'][0]['uuid'], replacement))
    subprocess.run(['/usr/bin/python3', '-c', '''import sys,pcbnew
b=pcbnew.LoadBoard(sys.argv[1]);t=next(t for t in b.GetTracks() if t.m_Uuid.AsString()==sys.argv[2]);t.SetStart(pcbnew.VECTOR2I(120000000,100000000));t.SetEnd(pcbnew.VECTOR2I(121000000,100000000));t.SetNetCode(b.FindNet('/SIG_A').GetNetCode());t.SetLocked(False);b.Save(sys.argv[1])
''', str(path), replacement], check=True, capture_output=True)
    new = native_facts(path, run / 'validate/candidate-facts.json')
    attempt['scope']['nets'].append('/SIG_A')
    common.write_json(run / 'attempt.json', attempt)
    result['validation']['candidate_board_sha256'] = new['sha256']
    result['validation']['validated_board_sha256'] = new['sha256']
    result['evidence']['candidate_board_sha256'] = new['sha256']
    for name in ('attempt.json', 'validate/candidate-facts.json'):
        result['evidence']['records'][name] = common.sha256(run / name)
    seal(run, attempt, result)
    doc = json.loads((run / c.MANIFEST).read_text())
    assert 'input_lineage_unknown' in doc['nets']['/SIG_A']['reasons']
    assert doc['nets']['/SIG_A']['eligible'] is False
    with pytest.raises(c.OwnershipError):
        c.prepare_disposable(run, ['/SIG_A'], tmp_path / 'prepared', 20)


@pytest.mark.parametrize('mode', ['free_via', 'blind_via', 'arc', 'locked'])
def test_unsupported_or_protected_generated_member_refuses_whole_unit(tmp_path, mode):
    from kicad_route import copper_ownership as c, native_components
    run, attempt, result, before, after = owned_run(tmp_path)
    old = {x['uuid'] for x in before['tracks'] + before['vias']}
    target = next(x['uuid'] for x in after['tracks' if mode in ('arc', 'locked') else 'vias'] if x['uuid'] not in old)
    path = run / 'candidate/board.kicad_pcb'
    script = '''import pcbnew,sys
b=pcbnew.LoadBoard(sys.argv[1]);t=next(t for t in b.GetTracks() if t.m_Uuid.AsString()==sys.argv[2]);mode=sys.argv[3]
if mode=='locked':t.SetLocked(True)
elif mode=='free_via':t.SetIsFree(True)
elif mode=='blind_via':t.SetViaType(pcbnew.VIATYPE_BLIND)
else:
 a=pcbnew.PCB_ARC(b);a.SetStart(t.GetStart());a.SetEnd(t.GetEnd());a.SetMid(pcbnew.VECTOR2I(t.GetStart().x+100000,t.GetStart().y+100000));a.SetWidth(t.GetWidth());a.SetLayer(t.GetLayer());a.SetNetCode(t.GetNetCode());b.Remove(t);b.Add(a)
b.Save(sys.argv[1])
'''
    proc = subprocess.run(['/usr/bin/python3', '-c', script, str(path), target, mode], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    facts = native_facts(path, run / 'validate/candidate-facts.json')
    native_components.extract(path, run / 'validate/candidate-terminal-components.json')
    for key in ('candidate_board_sha256', 'validated_board_sha256'):
        result['validation'][key] = facts['sha256']
    result['evidence']['candidate_board_sha256'] = facts['sha256']
    for name in ('validate/candidate-facts.json', 'validate/candidate-terminal-components.json'):
        result['evidence']['records'][name] = common.sha256(run / name)
    seal(run, attempt, result)
    original = path.read_bytes()
    with pytest.raises(c.OwnershipError, match='unsupported|locked|ineligible'):
        c.prepare_disposable(run, ['/SIG_B'], tmp_path / 'prepared', 20)
    assert path.read_bytes() == original
    assert not (tmp_path / 'prepared').exists()


def test_internally_consistent_stale_reference_partition_is_not_authority(tmp_path):
    from kicad_route import copper_ownership as c, native_components
    run, attempt, result, before, after = owned_run(tmp_path)
    path = run / 'validate/reference-terminal-components.json'
    stale = json.loads(path.read_text())
    for key in ('board_sha256_before', 'board_sha256_after'):
        stale[key] = '0' * 64
    stale['identity']['board_sha256'] = '0' * 64
    assert native_components.coverage(stale)
    common.write_json(path, stale)
    result['evidence']['records']['validate/reference-terminal-components.json'] = common.sha256(path)
    with pytest.raises(c.OwnershipError, match='qualified baseline'):
        seal(run, attempt, result)
    assert not (tmp_path / 'prepared').exists()


def test_refilled_reference_stage_retained_without_external_cache_dependency(tmp_path):
    from kicad_route import copper_ownership as c, baselinecache, native_components
    run, attempt, result, before, after = owned_run(tmp_path)
    generation = run / 'baseline/generation-fixture'
    terminal_path = run / 'validate/reference-terminal-components.json'
    refilled = json.loads(terminal_path.read_text())
    for key in ('board_sha256_before', 'board_sha256_after'):
        refilled[key] = 'f' * 64
    refilled['identity']['board_sha256'] = 'f' * 64
    common.write_json(terminal_path, refilled)
    common.write_json(generation / native_components.FACTS_NAME, refilled)
    info = json.loads((generation / 'baseline-info.json').read_text())
    info['board_sha256_graded'] = 'f' * 64
    info['refill']['board_sha256_after'] = 'f' * 64
    common.write_json(generation / 'baseline-info.json', info)
    generation_receipt = json.loads((generation / 'generation.json').read_text())
    generation_receipt['files'] = {n: common.sha256(generation / n) for n in baselinecache.FILES}
    common.write_json(generation / 'generation.json', generation_receipt)
    result['evidence']['records']['validate/reference-terminal-components.json'] = common.sha256(terminal_path)
    seal(run, attempt, result)
    shutil.rmtree(generation)
    receipt = c.prepare_disposable(run, ['/SIG_B'], tmp_path / 'prepared', 20)
    assert receipt['status'] == 'prepared'
    assert len(receipt['removed_uuids']) == 2


@pytest.mark.parametrize('stage', ['after_native', 'publication'])
def test_cancellation_discards_mutated_scratch_and_partial_publication(tmp_path, monkeypatch, stage):
    from kicad_route import copper_ownership as c
    run, attempt, result, _, _ = owned_run(tmp_path)
    seal(run, attempt, result)
    def run_hashes():
        return {p.relative_to(run).as_posix(): common.sha256(p)
                for p in run.rglob('*') if p.is_file()}
    original = run_hashes()
    dest = tmp_path / 'prepared'
    sentinel = tmp_path / 'unrelated.txt'
    sentinel.write_text('keep')
    reached = []
    if stage == 'after_native':
        real_run = subprocess.run
        def cancel_after_native(command, **kwargs):
            completed = real_run(command, **kwargs)
            if 'remove_owned.py' in str(command[1]):
                assert completed.returncode == 0, completed.stderr
                receipt = json.loads(Path(command[-1]).read_text())
                assert receipt['status'] == 'prepared'
                assert receipt['output_board_sha256'] != receipt['input_board_sha256']
                reached.append(stage)
                raise KeyboardInterrupt('cancel after native mutation')
            return completed
        monkeypatch.setattr(c.subprocess, 'run', cancel_after_native)
    else:
        real_move = shutil.move
        def cancel_during_publication(source, target):
            assert Path(source).name != 'receipt.json'
            real_move(source, target)
            assert dest.is_dir() and Path(target).exists()
            assert not (dest / 'receipt.json').exists()
            reached.append(stage)
            raise KeyboardInterrupt('cancel during publication')
        monkeypatch.setattr(c.shutil, 'move', cancel_during_publication)
    with pytest.raises(KeyboardInterrupt, match='cancel'):
        c.prepare_disposable(run, ['/SIG_B'], dest, 20)
    assert reached == [stage]
    assert run_hashes() == original
    assert not dest.exists()
    assert not list(tmp_path.glob('.copper-ownership-*'))
    assert sentinel.read_text() == 'keep'
