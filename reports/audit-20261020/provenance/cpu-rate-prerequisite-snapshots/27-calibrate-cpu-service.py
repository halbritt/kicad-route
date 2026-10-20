#!/usr/bin/env python3
"""Prepare-only CPU-service selector. Root invokes --run after quiet18111 ends.

No router runs, installs, remote execution, sibling cgroup writes, priority or
weight changes. Every load trial owns one normal transient user scope.
"""
import argparse
import datetime as dt
import hashlib
import json
import math
import multiprocessing as mp
from multiprocessing.connection import wait
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
CGROOT = Path("/sys/fs/cgroup")
CPUS = list(range(12))
CAPS = (2, 1)
TRIALS = 3
WORKERS = 4
CPU_SECONDS = 3.0
MINIMUM_SERVICE_FRACTION = 0.90
PERIOD_SECONDS = 0.1
ACCOUNTING_ALLOWANCE_SECONDS = 0.02


def write(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".writing")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def stats(path):
    return {line.split()[0]: int(line.split()[1]) for line in path.read_text().splitlines()}


def membership(pid="self"):
    return next(line.split("::", 1)[1] for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines()
                if line.startswith("0::"))


def controls():
    relative = membership()
    group = CGROOT / relative.lstrip("/")
    ancestry = []
    for path in (group, *group.parents):
        if path != CGROOT and not path.is_relative_to(CGROOT):
            break
        record = {"path": str(path)}
        for name in ("cpu.max", "cpu.max.burst", "cpu.weight", "cpu.pressure", "cpuset.cpus.effective"):
            record[name] = (path / name).read_text().strip() if (path / name).exists() else None
        if (path / "cpu.stat").exists():
            record["cpu.stat"] = stats(path / "cpu.stat")
        ancestry.append(record)
        if path == CGROOT:
            break
    return {"cgroup": relative, "affinity": sorted(os.sched_getaffinity(0)),
            "nice": os.getpriority(os.PRIO_PROCESS, 0), "scheduler": os.sched_getscheduler(0),
            "ancestry": ancestry, "host_cpu_pressure": Path("/proc/pressure/cpu").read_text(),
            "host_io_pressure": Path("/proc/pressure/io").read_text(),
            "loadavg": Path("/proc/loadavg").read_text().strip()}


def validate_controls(snapshot, cap):
    own = snapshot["ancestry"][0]
    expected = f"{cap * 100000} 100000"
    if (snapshot["affinity"] != CPUS or snapshot["nice"] != 0 or snapshot["scheduler"] != os.SCHED_OTHER or
            not snapshot["cgroup"].split("/")[-1].startswith("kicadroute-agent497-calibration-") or
            own.get("cpu.max") != expected or own.get("cpu.max.burst") != "0" or own.get("cpu.weight") != "100"):
        raise RuntimeError("actual owned scope quota/period/burst/weight/affinity/nice/scheduler differs from plan")
    for ancestor in snapshot["ancestry"][1:]:
        if ancestor.get("cpu.max") is not None:
            quota, period = ancestor["cpu.max"].split()
            if quota != "max" and int(quota) / int(period) < cap:
                raise RuntimeError("ancestor quota is lower than the candidate service cap")


def verdict(cap, wall, workers):
    if cap not in CAPS or type(wall) not in (int, float) or not math.isfinite(wall) or wall <= 0 or len(workers) != WORKERS or any(
            type(worker.get("cpu_s")) not in (int, float) or not math.isfinite(worker["cpu_s"]) or
            worker["cpu_s"] < CPU_SECONDS or worker.get("affinity") != CPUS or
            worker.get("nice") != 0 or worker.get("scheduler") != os.SCHED_OTHER for worker in workers):
        raise ValueError("invalid work or control evidence")
    delivered = sum(worker["cpu_s"] for worker in workers)
    minimum_wall = delivered / cap - PERIOD_SECONDS
    if wall + 0.000001 < minimum_wall:
        raise ValueError("measured work is too fast for the declared cap and one-period startup allowance")
    fraction = delivered / (cap * wall)
    return {"passed": fraction >= MINIMUM_SERVICE_FRACTION,
            "worker_cpu_s": delivered, "ideal_wall_s": delivered / cap,
            "measured_wall_s": wall, "minimum_cap_consistent_wall_s": minimum_wall,
            "one_period_startup_allowance_s": PERIOD_SECONDS, "service_fraction": fraction,
            "service_deficit_fraction": max(0.0, 1 - fraction), "limit": 0.10}


def validate_workers(workers, group, finished=False):
    if len(workers) != WORKERS or len({worker["pid"] for worker in workers}) != WORKERS:
        raise RuntimeError("worker membership evidence is incomplete")
    for identity in workers:
        if (identity.get("cgroup") != group or identity.get("affinity") != CPUS or
                identity.get("nice") != 0 or identity.get("scheduler") != os.SCHED_OTHER):
            raise RuntimeError("worker escaped owned scope or planned controls")
        if finished and (identity.get("cgroup_after") != group or identity.get("affinity_after") != CPUS or
                         identity.get("nice_after") != 0 or identity.get("scheduler_after") != os.SCHED_OTHER):
            raise RuntimeError("worker controls changed during calibration")


def validate_accounting(delivered, cgroup_cpu, cap, wall):
    if (not math.isfinite(cgroup_cpu) or cgroup_cpu < delivered - ACCOUNTING_ALLOWANCE_SECONDS or
            cgroup_cpu > cap * (wall + PERIOD_SECONDS) + ACCOUNTING_ALLOWANCE_SECONDS):
        raise RuntimeError("owned cgroup CPU accounting disagrees with delivered work or declared cap")


def worker(connection):
    try:
        identity = {"pid": os.getpid(), "affinity": sorted(os.sched_getaffinity(0)),
                    "nice": os.getpriority(os.PRIO_PROCESS, 0), "scheduler": os.sched_getscheduler(0),
                    "cgroup": membership()}
        connection.send({"ready": True, **identity})
        connection.recv()
        start = time.process_time()
        checksum = 1
        while time.process_time() - start < CPU_SECONDS:
            for _ in range(4096):
                checksum = (checksum * 1664525 + 1013904223) & 0xffffffff
        connection.send({**identity, "cpu_s": time.process_time() - start, "checksum": checksum,
                         "affinity_after": sorted(os.sched_getaffinity(0)), "cgroup_after": membership(),
                         "nice_after": os.getpriority(os.PRIO_PROCESS, 0),
                         "scheduler_after": os.sched_getscheduler(0)})
    finally:
        connection.close()


def trial(cap, output):
    before = controls()
    validate_controls(before, cap)  # Bare invocation cannot launch load without the planned own scope.
    processes, pipes = [], []
    receipt = {"cap_cpus": cap, "status": "RUNNING", "before": before, "workers": []}
    write(output, receipt)
    try:
        context = mp.get_context("fork")
        for _ in range(WORKERS):
            parent, child = context.Pipe()
            process = context.Process(target=worker, args=(child,))
            process.start()
            child.close()
            processes.append(process)
            pipes.append(parent)
        ready = []
        for pipe in pipes:
            if not pipe.poll(30):
                raise TimeoutError("owned worker did not reach the start barrier")
            ready.append(pipe.recv())
        receipt["ready"] = ready
        validate_workers(ready, before["cgroup"])
        receipt["parent_observed_worker_membership"] = {str(item["pid"]): membership(item["pid"]) for item in ready}
        if any(group != before["cgroup"] for group in receipt["parent_observed_worker_membership"].values()):
            raise RuntimeError("parent observed a worker outside the owned scope")
        work_before = controls()
        validate_controls(work_before, cap)
        started = time.monotonic()
        for pipe in pipes:
            pipe.send("start")
        pending = set(pipes)
        while pending:
            remaining = 90 - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError("owned trial work exceeded fixed90-second wall limit")
            available = wait(pending, timeout=remaining)
            if not available:
                raise TimeoutError("owned trial workers did not complete")
            for pipe in available:
                receipt["workers"].append(pipe.recv())
                pending.remove(pipe)
        elapsed = time.monotonic() - started
        after = controls()
        validate_controls(after, cap)
        validate_workers(receipt["workers"], before["cgroup"], finished=True)
        if {worker["pid"] for worker in ready} != {worker["pid"] for worker in receipt["workers"]}:
            raise RuntimeError("completed workers differ from the start-barrier workers")
        receipt.update({"after": after, "work_before": work_before,
                        "wall_s": elapsed, "verdict": verdict(cap, elapsed, receipt["workers"]),
                        "cgroup_work_cpu_usec": after["ancestry"][0]["cpu.stat"]["usage_usec"] -
                                                work_before["ancestry"][0]["cpu.stat"]["usage_usec"]})
        validate_accounting(receipt["verdict"]["worker_cpu_s"], receipt["cgroup_work_cpu_usec"] / 1000000, cap, elapsed)
        receipt["accounting_allowance_s"] = ACCOUNTING_ALLOWANCE_SECONDS
        receipt["status"] = "PASS" if receipt["verdict"]["passed"] else "INSUFFICIENT_SERVICE"
    except BaseException as error:
        receipt.update(status="ERROR", error=f"{type(error).__name__}: {error}")
    finally:
        for process in processes:
            process.join(timeout=1)
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)
            if process.is_alive():
                process.kill()
                process.join()
        for pipe in pipes:
            pipe.close()
        receipt["finished_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
        write(output, receipt)
    return 0 if receipt["status"] in ("PASS", "INSUFFICIENT_SERVICE") else 1


def select(receipts):
    for cap in CAPS:
        if str(cap) not in receipts:
            if cap == CAPS[0]:
                raise ValueError("highest candidate cap was not calibrated first")
            return None, "TRY_LOWER_CAP"
        trials = receipts.get(str(cap), [])
        if len(trials) != TRIALS:
            raise ValueError("candidate cap lacks exactly three trials")
        if any(receipt["status"] == "ERROR" for receipt in trials):
            return None, "CALIBRATION_ERROR"
        if all(receipt["status"] == "PASS" and receipt["verdict"]["passed"] is True for receipt in trials):
            return cap, "PASS"
    return None, "INSUFFICIENT_SERVICE"


def run(output):
    terminal = json.loads((HERE / "quiet-admission-metadata/matrix.json").read_text())
    if terminal["status"] == "RUNNING":
        raise RuntimeError("quiet study must be terminal before calibration")
    output.mkdir(parents=True, exist_ok=False)
    receipts = {}
    result = {"status": "RUNNING", "selector": "2CPU first;1CPU only after2CPU readiness failure",
              "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "caps": receipts}
    write(output / "selection.json", result)
    for cap in CAPS:
        receipts[str(cap)] = []
        for index in range(1, TRIALS + 1):
            directory = output / f"{cap}cpu-trial{index}"
            directory.mkdir()
            unit = f"kicadroute-agent497-calibration-{cap}cpu-{index}-{uuid.uuid4().hex[:8]}"
            argv = ["systemd-run", "--user", "--scope", "--collect", f"--unit={unit}",
                    f"--property=CPUQuota={cap * 100}%", "--property=CPUQuotaPeriodSec=100ms",
                    sys.executable, str(Path(__file__).resolve()), "--trial", str(cap),
                    "--output", str((directory / "trial.json").resolve())]
            write(directory / "launcher-argv.json", argv)
            with (directory / "launcher.log").open("w") as stream:
                launch = subprocess.run(argv, stdout=stream, stderr=subprocess.STDOUT)
            path = directory / "trial.json"
            receipt = json.loads(path.read_text()) if path.exists() else {"status": "ERROR", "error": "scope produced no receipt"}
            receipt["launcher_exit_code"] = launch.returncode
            if launch.returncode:
                receipt["status"] = "ERROR"
            receipts[str(cap)].append(receipt)
            write(output / "selection.json", result)
        selected, status = select(receipts)
        if status == "PASS" or status == "CALIBRATION_ERROR":
            break
    result.update(status=status, selected_cap_cpus=selected, finished_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
                  claim="measured delivered CPU service under a fixed cap; no isolation or future-service guarantee")
    write(output / "selection.json", result)
    print(json.dumps({"status": status, "selected_cap_cpus": selected, "receipt": str(output / "selection.json")}, indent=2))
    return 0 if status == "PASS" else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--run", action="store_true")
    modes.add_argument("--trial", type=int, choices=CAPS)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    return trial(args.trial, args.output) if args.trial else run(args.output)


if __name__ == "__main__":
    raise SystemExit(main())
