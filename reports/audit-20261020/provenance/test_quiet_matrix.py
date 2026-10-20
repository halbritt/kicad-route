"""Bounded protocol checks: synthetic counters, fake time and fake native driver."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('quiet_matrix', HERE/'run-quiet-matrix.py')
quiet = importlib.util.module_from_spec(spec)
spec.loader.exec_module(quiet)
POLICY = {'interval_s':5, 'consecutive_intervals':3, 'global_wait_budget_s':1800,
          'minimum_idle_fraction':0.8, 'maximum_cpu_some_avg10_percent':5,
          'maximum_io_full_avg10_percent':1}


class FakeClock:
    def __init__(self): self.value = 0
    def __call__(self): return self.value
    def sleep(self, seconds): self.value += seconds


def snapshot(ticks, cpu_psi=0, io_psi=0, idle=90, wait=0, guest=0):
    # 100 ticks per selected CPU per interval; aggregate line intentionally noisy.
    text = f'cpu 900000 0 0 1 0 0 0 0 0 0\ncpu0 {ticks*(100-idle-wait)} 0 0 {ticks*idle} {ticks*wait} 0 0 0 {guest} 0\n'
    return {'sources':{'/proc/stat':text,
        '/proc/pressure/cpu':f'some avg10={cpu_psi} avg60=0 avg300=0 total={ticks}\n',
        '/proc/pressure/io':f'full avg10={io_psi} avg60=0 avg300=0 total={ticks}\n'}}


class Reader:
    def __init__(self, changes=None): self.index = -1; self.changes = changes or {}
    def __call__(self):
        self.index += 1
        return snapshot(self.index, **self.changes.get(self.index, {}))


class AdmissionTests(unittest.TestCase):
    def admission(self, reader=None, policy=None):
        clock, events = FakeClock(), []
        gate = quiet.Admission(policy or POLICY, [0], events.append, reader or Reader(), clock, clock.sleep)
        return gate, clock, events

    def test_each_sample_requires_fresh_three_complete_intervals(self):
        gate, clock, events = self.admission()
        gate.admit('first'); gate.admit('second')
        self.assertEqual(clock(), 30)
        self.assertEqual([e['consecutive_eligible_intervals'] for e in events], [1,2,3,1,2,3])
        self.assertEqual(gate.used_s, 30)

    def test_any_failed_interval_resets_consecutive_admission(self):
        gate, clock, events = self.admission(Reader({2:{'cpu_psi':6}}))
        gate.admit('reset')
        self.assertEqual(clock(), 25)
        self.assertEqual([e['consecutive_eligible_intervals'] for e in events], [1,0,1,2,3])

    def test_global_budget_excludes_native_elapsed_and_is_never_reset(self):
        gate, clock, events = self.admission(policy=dict(POLICY, global_wait_budget_s=20))
        gate.admit('first')
        clock.sleep(1000)  # simulated CLI elapsed, outside admission
        with self.assertRaises(quiet.AdmissionExpired): gate.admit('second')
        self.assertEqual(gate.used_s, 20)
        self.assertEqual(clock(), 1020)
        self.assertEqual(events[-1]['event'], 'admission-expired')

    def test_missing_malformed_nan_and_reset_telemetry_never_admit(self):
        invalid = [snapshot(1), snapshot(1), snapshot(1), snapshot(1), snapshot(1)]
        invalid[0]['sources']['/proc/stat'] = 'cpu0 broken\n'
        invalid[1]['sources']['/proc/pressure/cpu'] = None
        invalid[2]['sources']['/proc/pressure/io'] = 'full avg10=nan avg60=0 avg300=0 total=1\n'
        invalid[3] = snapshot(0)  # counters reset against a positive baseline
        invalid[4]['sources']['/proc/pressure/cpu'] = 'some avg10=0 avg60=0 avg300=0 total=-1\n'
        for current in invalid:
            with self.subTest(current=current):
                self.assertFalse(quiet.eligibility(snapshot(2), current, POLICY, [0])['eligible'])

    def test_idle_excludes_iowait_and_guest_double_count(self):
        previous = snapshot(0)
        low = quiet.eligibility(previous, snapshot(1, idle=70, wait=25, guest=1000000), POLICY, [0])
        self.assertAlmostEqual(low['cpu_idle_fraction'], 0.7)
        self.assertFalse(low['eligible'])
        boundary = quiet.eligibility(previous, snapshot(1, idle=80, wait=15, guest=1000000, cpu_psi=5, io_psi=1), POLICY, [0])
        self.assertTrue(boundary['eligible'])
        self.assertAlmostEqual(boundary['cpu_idle_fraction'], 0.8)

    def test_duplicate_selected_cpu_rows_and_psi_fields_refuse(self):
        current=snapshot(1)
        current['sources']['/proc/stat'] += 'cpu0 1 2 3 4 5 6 7 8\n'
        self.assertFalse(quiet.eligibility(snapshot(0),current,POLICY,[0])['eligible'])
        current=snapshot(1)
        current['sources']['/proc/pressure/cpu'] = 'some avg10=0 avg10=9 avg60=0 avg300=0 total=1\n'
        self.assertFalse(quiet.eligibility(snapshot(0),current,POLICY,[0])['eligible'])

    def test_counters_must_advance_on_every_selected_cpu(self):
        self.assertFalse(quiet.eligibility(snapshot(1), snapshot(1), POLICY, [0])['eligible'])
        self.assertFalse(quiet.eligibility(snapshot(0), snapshot(1), POLICY, [0,1])['eligible'])


class MatrixTests(unittest.TestCase):
    def protocol(self, root):
        return {'metadata_output':str(root/'metadata'), 'boards':[
            {'name':name,'output':str(root/name),'project':'/synthetic','args':['--budget','180']}
            for name in ['diffpair','fourlayer','multipoint','route']],
            'reference_cli':'/reference','reference_state':'/refstate',
            'candidate_cli':'/candidate','candidate_state':'/candstate',
            'cli_timeout_s':900,'measurement':'Full CLI wall; all1.10 gates',
            'measured_repetitions':5}

    def test_gate_failure_preserves_all_four_boards_every_sample_without_retry(self):
        with tempfile.TemporaryDirectory(dir=HERE) as folder:
            root=Path(folder); calls=[]; clock=FakeClock(); events=[]
            gate=quiet.Admission(POLICY,[0],events.append,Reader(),clock,clock.sleep)
            def run(config,side,index):
                calls.append((config.output.name,side,index));clock.sleep(100)  # native time
                return {'quality':{'process':'FAIL'},'wall_s':100}
            driver=SimpleNamespace(run_sample=run,compare=lambda *args:{'passed':False,'failures':['native FAIL']})
            status=quiet.run_matrix(self.protocol(root),driver,gate,read=lambda: {'after':'noisy'},clock=clock)
            self.assertEqual(status,1)
            self.assertEqual(len(calls),48)
            self.assertEqual(len(set(calls)),48)
            self.assertEqual(gate.used_s,48*15)
            self.assertEqual(len(events),48*3)
            matrix=json.loads((root/'metadata/matrix.json').read_text())
            self.assertEqual(matrix['status'],'FAIL')
            self.assertEqual([b['status'] for b in matrix['boards']],['FAIL']*4)
            for board in matrix['boards']:
                report=json.loads((Path(board['output'])/'comparison.json').read_text())
                self.assertEqual(len(report['reference']),5)
                self.assertEqual(len(report['candidate']),5)
                self.assertEqual(set(report['warmup']),{'reference','candidate'})
            self.assertEqual(calls[:4],[('diffpair','reference',0),('diffpair','candidate',0),('diffpair','candidate',1),('diffpair','reference',1)])

    def test_expiry_is_inconclusive_and_does_not_launch_or_discard(self):
        with tempfile.TemporaryDirectory(dir=HERE) as folder:
            root=Path(folder);clock=FakeClock();gate=quiet.Admission(dict(POLICY,global_wait_budget_s=20),[0],lambda event:None,Reader(),clock,clock.sleep);calls=[]
            def run(config,side,index):calls.append((side,index));return {'quality':{},'wall_s':1}
            driver=SimpleNamespace(run_sample=run,compare=lambda *args:{'passed':True})
            self.assertEqual(quiet.run_matrix(self.protocol(root),driver,gate,read=lambda:{},clock=clock),2)
            self.assertEqual(calls,[('reference',0)])
            matrix=json.loads((root/'metadata/matrix.json').read_text())
            self.assertEqual(matrix['status'],'INCONCLUSIVE_ADMISSION_EXPIRED')
            self.assertFalse(matrix['all_comparisons_passed'])
            self.assertEqual(matrix['completed_samples'],1)
            report=json.loads((root/'diffpair/samples.json').read_text())
            self.assertEqual(set(report['warmup']),{'reference'})

    def test_empty_or_partial_board_profile_cannot_vacuously_pass(self):
        with tempfile.TemporaryDirectory(dir=HERE) as folder:
            root=Path(folder);clock=FakeClock();gate=quiet.Admission(POLICY,[0],lambda event:None,Reader(),clock,clock.sleep)
            protocol=self.protocol(root);protocol['boards']=[]
            driver=SimpleNamespace(run_sample=lambda *args:None,compare=lambda *args:{'passed':True})
            self.assertEqual(quiet.run_matrix(protocol,driver,gate,read=lambda:{},clock=clock),1)
            self.assertFalse(json.loads((root/'metadata/matrix.json').read_text())['all_comparisons_passed'])

    def test_post_completion_telemetry_failure_does_not_drop_sample(self):
        with tempfile.TemporaryDirectory(dir=HERE) as folder:
            root=Path(folder);clock=FakeClock();gate=quiet.Admission(POLICY,[0],lambda event:None,Reader(),clock,clock.sleep)
            protocol=self.protocol(root)
            driver=SimpleNamespace(run_sample=lambda *args:{'quality':{}},compare=lambda *args:{'passed':True})
            def broken_read():raise OSError('telemetry missing after CLI')
            self.assertEqual(quiet.run_matrix(protocol,driver,gate,read=broken_read,clock=clock),0)
            self.assertEqual(json.loads((root/'metadata/matrix.json').read_text())['completed_samples'],48)


if __name__=='__main__':unittest.main()
