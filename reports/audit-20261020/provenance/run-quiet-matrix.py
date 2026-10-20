#!/usr/bin/env python3
"""Prespecified quiet-host admission around the frozen comparison driver.

This ignored qualification harness never edits production code. Admission happens
before GNU time/public CLI launch. Completed samples are never retried or dropped.
"""
import argparse
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
CPU_SOURCE = '/proc/stat'
PSI_SOURCES = {'cpu': '/proc/pressure/cpu', 'io': '/proc/pressure/io'}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.writing')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def read_resources():
    snapshot = {'observed_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                'monotonic_s': time.monotonic(), 'actual_cpu_affinity':sorted(os.sched_getaffinity(0)), 'sources': {CPU_SOURCE: None, **dict.fromkeys(PSI_SOURCES.values())}}
    for path in snapshot['sources']:
        try:
            snapshot['sources'][path] = Path(path).read_text()
        except OSError as error:
            snapshot.setdefault('read_errors', {})[path] = str(error)
    return snapshot


def parse_cpu(text, cpus):
    rows = {}
    for line in (text or '').splitlines():
        fields = line.split()
        if fields and fields[0] in {f'cpu{cpu}' for cpu in cpus}:
            if len(fields) < 9:
                raise ValueError('CPU row has fewer than eight accounting fields')
            if int(fields[0][3:]) in rows:
                raise ValueError('Duplicate selected CPU row')
            ticks = tuple(int(value) for value in fields[1:9])
            if any(value < 0 for value in ticks):
                raise ValueError('Negative CPU counter')
            rows[int(fields[0][3:])] = ticks
    if set(rows) != set(cpus):
        raise ValueError('Missing selected CPU row')
    return rows


def parse_psi(text, kind):
    matches = [line.split() for line in (text or '').splitlines() if line.split() and line.split()[0] == kind]
    if len(matches) != 1:
        raise ValueError(f'Missing/duplicate PSI {kind} row')
    pairs = [part.split('=', 1) for part in matches[0][1:]]
    if any(len(pair) != 2 for pair in pairs) or len({pair[0] for pair in pairs}) != len(pairs):
        raise ValueError('Malformed/duplicate PSI field')
    fields = dict(pairs)
    values = {key: float(fields[key]) for key in ('avg10', 'avg60', 'avg300')}
    total = int(fields['total'])
    if total < 0 or any(not math.isfinite(value) or not 0 <= value <= 100 for value in values.values()):
        raise ValueError('Invalid PSI value')
    return {**values, 'total': total}


def eligibility(previous, current, policy, cpus):
    """Idle uses only idle ticks; iowait is in total but never in idle.

    First eight /proc/stat fields count user,nice,system,idle,iowait,irq,softirq,
    steal. guest and guest_nice are already included in user/nice and omitted.
    """
    result = {'eligible': False, 'reasons': []}
    try:
        before = parse_cpu(previous['sources'][CPU_SOURCE], cpus)
        after = parse_cpu(current['sources'][CPU_SOURCE], cpus)
        differences = {cpu: tuple(b-a for a, b in zip(before[cpu], after[cpu])) for cpu in cpus}
        if any(any(value < 0 for value in ticks) for ticks in differences.values()):
            raise ValueError('CPU counters reset')
        if any(sum(ticks) <= 0 for ticks in differences.values()):
            raise ValueError('Nonpositive selected CPU interval')
        total = sum(sum(ticks) for ticks in differences.values())
        result['cpu_idle_fraction'] = sum(ticks[3] for ticks in differences.values()) / total
        result['cpu_interval_ticks'] = {str(cpu): list(ticks) for cpu, ticks in differences.items()}
        for name, kind in [('cpu', 'some'), ('io', 'full')]:
            old = parse_psi(previous['sources'][PSI_SOURCES[name]], kind)
            new = parse_psi(current['sources'][PSI_SOURCES[name]], kind)
            if new['total'] < old['total']:
                raise ValueError(f'{name} PSI counter reset')
            result[name+'_psi'] = new
        if result['cpu_idle_fraction'] < policy['minimum_idle_fraction']:
            result['reasons'].append('CPU idle below threshold')
        if result['cpu_psi']['avg10'] > policy['maximum_cpu_some_avg10_percent']:
            result['reasons'].append('CPU some avg10 above threshold')
        if result['io_psi']['avg10'] > policy['maximum_io_full_avg10_percent']:
            result['reasons'].append('IO full avg10 above threshold')
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        result['reasons'].append('Unusable telemetry: '+str(error))
    result['eligible'] = not result['reasons']
    return result


class AdmissionExpired(Exception):
    pass


class Admission:
    def __init__(self, policy, cpus, emit, read=read_resources, clock=time.monotonic, sleep=time.sleep):
        self.policy, self.cpus, self.emit = policy, cpus, emit
        self.read, self.clock, self.sleep = read, clock, sleep
        self.used_s = 0.0

    def admit(self, label):
        # Every invocation starts with a fresh baseline and zero consecutive passes.
        start, consecutive = self.clock(), 0
        limit = self.policy['global_wait_budget_s']
        previous = self.read()
        previous_at = self.clock()
        try:
            while True:
                remaining = limit-self.used_s-(self.clock()-start)
                if remaining < self.policy['interval_s']:
                    if remaining > 0:
                        self.sleep(remaining)
                    self.emit({'label':label, 'event':'admission-expired', 'wait_used_s':self.used_s+self.clock()-start})
                    raise AdmissionExpired('Global admission waiting budget exhausted')
                self.sleep(self.policy['interval_s'])
                current = self.read()
                observed_at = self.clock()
                interval = observed_at-previous_at
                assessment = eligibility(previous, current, self.policy, self.cpus)
                if interval < self.policy['interval_s']:
                    assessment['eligible'] = False
                    assessment['reasons'].append('Telemetry interval shorter than policy')
                consecutive = consecutive+1 if assessment['eligible'] else 0
                event = {'label':label, 'event':'resource-interval', 'actual_interval_s':interval,
                         'before':previous, 'after':current, **assessment,
                         'consecutive_eligible_intervals':consecutive,
                         'wait_used_s':self.used_s+observed_at-start}
                self.emit(event)
                if self.used_s+self.clock()-start > limit:
                    self.emit({'label':label, 'event':'admission-expired', 'wait_used_s':self.used_s+self.clock()-start})
                    raise AdmissionExpired('Global admission waiting budget exhausted')
                if consecutive >= self.policy['consecutive_intervals']:
                    return current
                previous, previous_at = current, observed_at
        finally:
            self.used_s += self.clock()-start


def run_matrix(protocol, driver, admission, read=read_resources, clock=time.monotonic):
    root = Path(protocol['metadata_output'])
    root.mkdir(parents=True, exist_ok=False)
    resource_dir = root/'samples'
    resource_dir.mkdir()
    write_json(root/'protocol.json', protocol)
    matrix = {'schema':'kicad-route/quiet-admission-matrix/1', 'status':'RUNNING',
              'protocol_sha256':sha(root/'protocol.json'), 'boards':[], 'completed_samples':0,
              'admission_wait_s':0.0, 'all_comparisons_passed':False}

    def finish(status, error=None):
        matrix['status'], matrix['admission_wait_s'] = status, admission.used_s
        matrix['all_comparisons_passed'] = status == 'PASS'
        if error is not None:
            matrix['error'] = str(error)
        write_json(root/'matrix.json', matrix)

    finish('RUNNING')
    try:
        for board in protocol['boards']:
            output = Path(board['output'])
            output.mkdir(parents=True, exist_ok=False)
            config = SimpleNamespace(output=output, project=Path(board['project']), arg=board['args'],
                reference_cli=Path(protocol['reference_cli']), reference_state=Path(protocol['reference_state']),
                candidate_cli=Path(protocol['candidate_cli']), candidate_state=Path(protocol['candidate_state']),
                timeout=protocol['cli_timeout_s'])
            report = {'schema':'kicad-route/engine-comparison/2', 'project':str(config.project),
                      'args':config.arg, 'reference':[], 'candidate':[], 'warmup':{},
                      'mode':'migration-equivalence', 'cpu_affinity':sorted(os.sched_getaffinity(0)),
                      'measurement':protocol['measurement'], 'quiet_admission_protocol':str(root/'protocol.json')}
            row = {'name':board['name'], 'output':str(output), 'status':'RUNNING', 'completed_samples':0}
            matrix['boards'].append(row)
            write_json(output/'samples.json', report)
            for index in range(protocol['measured_repetitions']+1):
                for side in (('reference','candidate') if index % 2 == 0 else ('candidate','reference')):
                    label = f"{board['name']}/{side}-{index}"
                    print(label+': waiting for prespecified admission', flush=True)
                    before = admission.admit(label)
                    telemetry = {'label':label, 'before_launch':before, 'admission_wait_total_s':admission.used_s,
                                 'actual_cpu_affinity_before':sorted(os.sched_getaffinity(0))}
                    observation = resource_dir/(board['name']+'-'+side+'-'+str(index)+'.json')
                    write_json(observation, telemetry)
                    print(label+': running '+('warmup' if index == 0 else 'measured'), flush=True)
                    started = clock()
                    try:
                        sample = driver.run_sample(config, side, index)
                    finally:
                        telemetry['driver_call_wall_s'] = clock()-started
                        telemetry['actual_cpu_affinity_after'] = sorted(os.sched_getaffinity(0))
                        try:
                            telemetry['after_completion'] = read()
                        except Exception as error:
                            telemetry['after_observation_error'] = str(error)
                        # After-pressure never affects admission or sample outcome.
                        write_json(observation, telemetry)
                    if index == 0:
                        report['warmup'][side] = sample
                    else:
                        report[side].append(sample)
                    row['completed_samples'] += 1
                    matrix['completed_samples'] += 1
                    write_json(output/'samples.json', report)
                    finish('RUNNING')
            report['verdict'] = driver.compare(report['reference'], report['candidate'], report['warmup'], report['mode'])
            write_json(output/'comparison.json', report)
            row['status'] = 'PASS' if report['verdict']['passed'] else 'FAIL'
            row['verdict'] = report['verdict']
            print(board['name']+': '+row['status'], flush=True)
            finish('RUNNING')
        passed = (len(matrix['boards']) == 4 and matrix['completed_samples'] == 48
                  and all(row['status'] == 'PASS' and row['completed_samples'] == 12 for row in matrix['boards']))
        finish('PASS' if passed else 'FAIL')
        return 0 if passed else 1
    except AdmissionExpired as error:
        finish('INCONCLUSIVE_ADMISSION_EXPIRED', error)
        return 2
    except KeyboardInterrupt as error:
        finish('INTERRUPTED', error)
        return 130
    except Exception as error:
        # Driver exception means no coherent sample can be obtained. Ordinary
        # native FAIL/INCOMPLETE receipts return normally and all boards continue.
        finish('EXECUTION_ERROR', error)
        return 1


def git_head(root):
    return subprocess.check_output(['git','-C',str(root),'rev-parse','HEAD'],text=True).strip()


def preflight(protocol):
    if protocol['measured_repetitions'] != 5 or [b['name'] for b in protocol['boards']] != ['diff-pair','four-layer','multipoint','route-demo']:
        raise ValueError('Preregistered four-board/five-repetition profile changed')
    if sorted(os.sched_getaffinity(0)) != protocol['cpu_affinity']:
        raise ValueError('Actual CPU affinity differs from preregistered0–11')
    for item in protocol['hash_prerequisites']:
        if sha(item['path']) != item['sha256']:
            raise ValueError('Frozen prerequisite hash changed: '+item['path'])
    for item in protocol['source_heads']:
        if git_head(item['path']) != item['head']:
            raise ValueError('Frozen source HEAD changed: '+item['path'])
        dirty = subprocess.check_output(['git','-C',item['path'],'status','--porcelain','--untracked-files=no'],text=True).strip()
        if dirty:
            raise ValueError('Frozen tracked source changed: '+item['path'])
    paths = [Path(protocol['metadata_output']), *(Path(b['output']) for b in protocol['boards'])]
    if any(path.exists() for path in paths):
        raise ValueError('Immutable quiet-admission output already exists')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--protocol', type=Path, default=HERE/'quiet-admission-protocol.json')
    args = parser.parse_args()
    protocol = json.loads(args.protocol.read_text())
    preflight(protocol)
    spec = importlib.util.spec_from_file_location('frozen_compare_router', protocol['driver'])
    driver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(driver)
    log_path = Path(protocol['metadata_output']).with_name('quiet-admission-resource-intervals.jsonl')
    # Refuse overwriting earlier telemetry, independently of result directories.
    with log_path.open('x') as log:
        def emit(event):
            log.write(json.dumps(event, sort_keys=True)+'\n')
            log.flush()
        admission = Admission(protocol['admission'], protocol['cpu_affinity'], emit)
        return run_matrix(protocol, driver, admission)


if __name__ == '__main__':
    raise SystemExit(main())
