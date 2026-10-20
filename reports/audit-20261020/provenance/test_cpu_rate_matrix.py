"""Bounded CPU-rate protocol tests; fake driver/resources, no scope/native jobs."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('rate',HERE/'run-cpu-rate-matrix.py');rate=importlib.util.module_from_spec(spec);spec.loader.exec_module(rate)
GROUP='/user.slice/test.scope'

def resources():
    return {'group':GROUP,'cpu_affinity':list(range(12)),'nice':0,'scheduler':os.SCHED_OTHER,
            'ancestors':[{'path':'/fake/test.scope','controls':{k:{'text':v} for k,v in
                [('cpu.max','200000 100000\n'),('cpu.max.burst','0\n'),('cpu.weight','100\n'),('cpu.idle','0\n'),('cpuset.cpus.effective','0-11\n')]},
                'observations':{'cpu.stat':{'text':'usage_usec 12\n'}}}]}

class FakeMonitor:
    def __init__(self,driver,group):pass
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def summary(self):return {'outside_scope':[],'read_errors':[],'observed_births':1,'sampler_calls':1,'members':[]}

class RateTests(unittest.TestCase):
    def protocol(self,root):
        return {'scope_unit':'test.scope','cpu_cap':2,'metadata_output':str(root/'metadata'),
            'boards':[{'name':n,'project':'/synthetic','args':['--budget','180'],'output':str(root/n)}
                for n in ['diff-pair','four-layer','multipoint','route-demo']],
            'reference_cli':'/ref','reference_state':'/refstate','candidate_cli':'/candidate','candidate_state':'/candstate',
            'cli_timeout_s':900,'measurement':'Unchanged fullCLI1.10 medians'}

    def test_calibration_selector_requires_three_passes_and_never_skips_ready2(self):
        good={'status':'PASS','wall_s':6.5,'workers':[{'cpu_s':3.0} for _ in range(4)],'verdict':{'passed':True}}
        rate.validate_calibration({'status':'PASS','selected_cap_cpus':2,'caps':{'2':[good]*3}},2)
        with self.assertRaises(ValueError):rate.validate_calibration({'status':'PASS','selected_cap_cpus':1,'caps':{'2':[good]*3,'1':[dict(good,wall_s=13)]*3}},1)
        failed={'status':'INSUFFICIENT_SERVICE','verdict':{'passed':False}}
        rate.validate_calibration({'status':'PASS','selected_cap_cpus':1,'caps':{'2':[good,good,failed],'1':[dict(good,wall_s=13)]*3}},1)
        with self.assertRaises(ValueError):rate.validate_calibration({'status':'RUNNING','caps':{}},2)

    def test_exact_quota_period_burst_weight_affinity_and_priority_required(self):
        protocol={'scope_unit':'test.scope','cpu_cap':2};rate.validate_scope(resources(),protocol)
        for key,value in [('cpu.max','100000 100000'),('cpu.max','200000 50000'),('cpu.max.burst','1000'),('cpu.weight','200'),('cpu.idle','1')]:
            current=resources();current['ancestors'][0]['controls'][key]['text']=value
            with self.subTest(key=key,value=value),self.assertRaises(ValueError):rate.validate_scope(current,protocol)
        for key,value in [('nice',1),('scheduler',1),('cpu_affinity',[0,1]),('group','/else.scope')]:
            current=resources();current[key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):rate.validate_scope(current,protocol)

    def test_membership_piggyback_preserves_rss_and_checks_each_birth_once(self):
        pid=os.getpid();birth=int(Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[19]);calls=[]
        def original(root,tracked):calls.append(root);tracked[pid]=birth;return 123456
        driver=SimpleNamespace(process_tree_rss=original)
        with patch.object(rate,'membership',return_value=GROUP) as read:
            with rate.MembershipMonitor(driver,GROUP) as monitor:
                self.assertEqual(driver.process_tree_rss(pid,{}),123456)
                self.assertEqual(driver.process_tree_rss(pid,{}),123456)
            self.assertIs(driver.process_tree_rss,original)
            self.assertEqual(read.call_count,1)
        self.assertEqual(monitor.summary()['observed_births'],1)
        self.assertEqual(monitor.summary()['sampler_calls'],2)

    def test_observed_escape_is_a_violation_but_exit_race_is_explicit(self):
        pid=os.getpid();birth=int(Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[19])
        driver=SimpleNamespace(process_tree_rss=lambda root,tracked:(tracked.update({pid:birth}) or 10))
        with patch.object(rate,'membership',return_value='/outside'):
            with rate.MembershipMonitor(driver,GROUP) as monitor:driver.process_tree_rss(pid,{})
        self.assertEqual(len(monitor.summary()['outside_scope']),1)
        driver.process_tree_rss=lambda root,tracked:(tracked.update({999999999:1}) or 10)
        with rate.MembershipMonitor(driver,GROUP) as monitor:driver.process_tree_rss(pid,{})
        self.assertEqual(monitor.summary()['members'][0]['status'],'exited-before-membership-verification')
        self.assertEqual(monitor.summary()['read_errors'],[])

    def test_failed_comparison_keeps_all_four_boards_all48_without_retry(self):
        with tempfile.TemporaryDirectory(dir=HERE) as folder:
            root=Path(folder);calls=[]
            def run(config,side,index):calls.append((config.output.name,side,index));return {'quality':{'process':'FAIL'}}
            driver=SimpleNamespace(run_sample=run,compare=lambda *args:{'passed':False,'failures':['nativeFAIL']})
            self.assertEqual(rate.run_matrix(self.protocol(root),driver,resources,FakeMonitor),1)
            self.assertEqual(len(calls),48);self.assertEqual(len(set(calls)),48)
            matrix=json.loads((root/'metadata/matrix.json').read_text());self.assertEqual(matrix['status'],'FAIL')
            self.assertEqual([r['completed_samples'] for r in matrix['boards']],[12]*4)
            for row in matrix['boards']:
                report=json.loads((Path(row['output'])/'comparison.json').read_text())
                self.assertEqual((len(report['reference']),len(report['candidate']),len(report['warmup'])),(5,5,2))

    def test_post_completion_control_mismatch_keeps_sample_and_prevents_pass(self):
        with tempfile.TemporaryDirectory(dir=HERE) as folder:
            root=Path(folder);count=[0]
            def observe():
                count[0]+=1;current=resources()
                if count[0]==3:current['ancestors'][0]['controls']['cpu.max']['text']='100000 100000'
                return current
            driver=SimpleNamespace(run_sample=lambda *args:{'quality':{}},compare=lambda *args:{'passed':True})
            self.assertEqual(rate.run_matrix(self.protocol(root),driver,observe,FakeMonitor),1)
            matrix=json.loads((root/'metadata/matrix.json').read_text());self.assertEqual(matrix['completed_samples'],48)
            self.assertTrue(matrix['resource_violations']);self.assertFalse(matrix['all_comparisons_passed'])

    def test_driver_error_keeps_previous_completion_and_current_telemetry(self):
        with tempfile.TemporaryDirectory(dir=HERE) as folder:
            root=Path(folder);calls=[0]
            def run(*args):
                calls[0]+=1
                if calls[0]==2:raise RuntimeError('driver cannot produce receipt')
                return {'quality':{}}
            driver=SimpleNamespace(run_sample=run,compare=lambda *args:{'passed':True})
            self.assertEqual(rate.run_matrix(self.protocol(root),driver,resources,FakeMonitor),1)
            matrix=json.loads((root/'metadata/matrix.json').read_text());self.assertEqual(matrix['status'],'EXECUTION_ERROR');self.assertEqual(matrix['completed_samples'],1)
            self.assertIn('after',json.loads((root/'metadata/samples/diff-pair-candidate-0.json').read_text()))
            self.assertEqual(set(json.loads((root/'diff-pair/samples.json').read_text())['warmup']),{'reference'})

if __name__=='__main__':unittest.main()
