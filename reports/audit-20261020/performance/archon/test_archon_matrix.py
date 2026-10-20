"""Bounded Archon protocol tests; fake driver/resources, no scope/native jobs."""
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
spec=importlib.util.spec_from_file_location('rate',HERE/'run-archon-matrix.py');rate=importlib.util.module_from_spec(spec);spec.loader.exec_module(rate)
GROUP='/user.slice/test.scope'

def resources():
    return {'group':GROUP,'cpu_affinity':list(range(16)),'nice':0,'scheduler':os.SCHED_OTHER,
            'ancestors':[{'path':'/fake/test.scope','controls':{k:{'text':v} for k,v in
                [('cpu.max','max 100000\n'),('cpu.max.burst','0\n'),('cpu.weight','100\n'),('cpu.idle','0\n'),('cpuset.cpus.effective','0-15\n')]},
                'observations':{'cpu.stat':{'text':'usage_usec 12\n'}}}]}

class FakeMonitor:
    def __init__(self,driver,group):pass
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def summary(self):return {'outside_scope':[],'read_errors':[],'observed_births':1,'sampler_calls':1,'members':[]}

class RateTests(unittest.TestCase):
    def protocol(self,root):
        return {'cpu_affinity':list(range(16)),'metadata_output':str(root/'metadata'),
            'boards':[{'name':n,'project':'/synthetic','args':['--budget','180'],'output':str(root/n)}
                for n in ['diff-pair','four-layer','multipoint','route-demo']],
            'reference_cli':'/ref','reference_state':'/refstate','candidate_cli':'/candidate','candidate_state':'/candstate',
            'cli_timeout_s':900,'measurement':'Unchanged fullCLI1.10 medians'}

    def test_all_available_affinity_normal_priority_and_no_new_quota_required(self):
        protocol={'cpu_affinity':list(range(16))};rate.validate_scope(resources(),protocol)
        for key,value in [('cpu.max','100000 100000'),('cpu.max','bad'),('cpu.idle','1')]:
            current=resources();current['ancestors'][0]['controls'][key]['text']=value
            with self.subTest(key=key),self.assertRaises(ValueError):rate.validate_scope(current,protocol)
        for key,value in [('nice',1),('scheduler',1),('cpu_affinity',[0,1]),('group','/../outside')]:
            current=resources();current[key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):rate.validate_scope(current,protocol)
        protocol['allowed_existing_policy_quotas']={'/fake/test.scope':'100000 100000'}
        current=resources();current['ancestors'][0]['controls']['cpu.max']['text']='100000 100000'
        rate.validate_scope(current,protocol)

    def test_root_namespace_rejects_parent_escape_but_accepts_canonical_descendants(self):
        pid=os.getpid();birth=int(Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[19])
        for group,allowed in [('/',True),('/child',True),('/..',False),('/../other',False),('/child/../other',False),('//child',False)]:
            driver=SimpleNamespace(process_tree_rss=lambda root,tracked:(tracked.update({pid:birth}) or 10))
            with patch.object(rate,'membership',return_value=group):
                with rate.MembershipMonitor(driver,'/') as monitor:driver.process_tree_rss(pid,{})
            self.assertEqual(not bool(monitor.summary()['outside_scope']),allowed,group)

    def test_host_attestation_checks_actual_container_and_hidden_quotas(self):
        protocol={'container_id':'id','container_hostname':'box','container_image_id':'sha256:image','cpu_affinity':list(range(16))}
        value={'host':'archon','actual_hostname':'archon','container_id':'id','container_hostname':'box','image_id':'sha256:image',
            'init_affinity':list(range(16)),'init_nice':0,'init_scheduler':os.SCHED_OTHER,'init_pid':1,'raw_init_cgroup':'0::/scope',
            'ancestors':[{'controls':{'cpu.max':'max 100000'}}],
            'docker_resource_config':dict(NanoCpus=0,CpuShares=0,CpuPeriod=0,CpuQuota=0,CpusetCpus='')}
        rate.validate_host_attestation(value,protocol)
        for key,changed in [('image_id','wrong'),('init_affinity',[0])]:
            current=copy.deepcopy(value);current[key]=changed
            with self.assertRaises(ValueError):rate.validate_host_attestation(current,protocol)
        value['ancestors'][0]['controls']['cpu.max']='100000 100000'
        with self.assertRaises(ValueError):rate.validate_host_attestation(value,protocol)

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
