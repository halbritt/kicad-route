"""Bounded audit-verifier checks; no native execution or real waiting."""
from pathlib import Path
import copy
import hashlib
import importlib.util
import json
import tempfile

Q = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("acceptance_finalizer", Q / "finalize-acceptance.py")
f = importlib.util.module_from_spec(spec)
spec.loader.exec_module(f)


def snapshot(t, clock, cpu=0, io=0, idle=80):
    return {"observed_utc": "synthetic", "monotonic_s": clock, "actual_cpu_affinity": list(range(12)),
            "sources": {"/proc/stat": "\n".join(f"cpu{i} {t*(100-idle)} 0 0 {t*idle} 0 0 0 0 999999 0" for i in range(12)),
                        "/proc/pressure/cpu": f"some avg10={cpu} avg60=0 avg300=0 total={t}\n",
                        "/proc/pressure/io": f"full avg10={io} avg60=0 avg300=0 total={t}\n"}}


def main():
    results = []
    first, last = snapshot(0, 0), snapshot(1, 5, cpu=5, io=1)
    cases = [("exact thresholds", first, last, 5, True),
             ("idle below80", first, snapshot(1, 5, idle=79), 5, False),
             ("CPU pressure above5", first, snapshot(1, 5, cpu=5.0001), 5, False),
             ("IO pressure above1", first, snapshot(1, 5, io=1.0001), 5, False),
             ("short interval", first, last, 4.999, False)]
    for name, source, replacement in [
        ("missing selected CPU", "/proc/stat", "cpu0 1 0 0 4 0 0 0 0"),
        ("duplicate selected CPU", "/proc/stat", last["sources"]["/proc/stat"] + "\ncpu0 1 0 0 4 0 0 0 0"),
        ("NaN pressure", "/proc/pressure/cpu", "some avg10=nan avg60=0 avg300=0 total=1"),
        ("duplicate pressure field", "/proc/pressure/cpu", "some avg10=0 avg10=9 avg60=0 avg300=0 total=1"),
        ("missing pressure", "/proc/pressure/io", None)]:
        bad = copy.deepcopy(last)
        bad["sources"][source] = replacement
        cases.append((name, first, bad, 5, False))
    cases.append(("counter reset", snapshot(2, 0), snapshot(1, 5), 5, False))
    for name, before, after, interval, expected in cases:
        assert f.admission_assessment(before, after, interval, list(range(12)))["eligible"] is expected, name
        results.append("PASS parser: " + name)
    with tempfile.TemporaryDirectory(dir=Q, prefix="finalizer-check-") as folder:
        root = Path(folder)
        (root / "samples").mkdir()
        protocol = {"metadata_output": str(root), "resource_interval_log": str(root / "journal.jsonl"),
                    "boards": [{"name": name} for name in ("diff-pair", "four-layer", "multipoint", "route-demo")]}
        clock = tick = wait = 0
        events = []
        for board in protocol["boards"]:
            for index in range(6):
                for side in (("reference", "candidate") if index % 2 == 0 else ("candidate", "reference")):
                    label = f"{board['name']}/{side}-{index}"
                    previous = snapshot(tick, clock)
                    for streak in (1, 2, 3):
                        clock, tick, wait = clock + 5, tick + 1, wait + 5
                        current = snapshot(tick, clock)
                        events.append({"event": "resource-interval", "label": label, "before": previous,
                                       "after": current, "actual_interval_s": 5, "eligible": True,
                                       "consecutive_eligible_intervals": streak, "wait_used_s": wait})
                        previous = current
                    lastmeta = root / "samples" / (label.replace("/", "-") + ".json")
                    clock, tick = clock + 10, tick + 1
                    lastmeta.write_text(json.dumps({"label": label, "before_launch": previous,
                        "admission_wait_total_s": wait, "actual_cpu_affinity_before": list(range(12)),
                        "actual_cpu_affinity_after": list(range(12)), "driver_call_wall_s": 10,
                        "after_completion": snapshot(tick, clock)}))
        journal = Path(protocol["resource_interval_log"])
        journal.write_text("".join(json.dumps(event) + "\n" for event in events))
        terminal = root / "matrix.json"
        terminal.write_text(json.dumps({"admission_wait_s": 720}))
        proof = f.verify_admission_journal(protocol)
        assert proof["admitted_samples"] == proof["completed_sample_records"] == 48
        assert proof["interval_events"] == 144
        results.append("PASS population:48 admissions/144 intervals/720s wait, native time excluded")

        def rejected(action, restore, label, check=lambda: f.verify_admission_journal(protocol), expected_message=None):
            action()
            try:
                check()
            except RuntimeError as error:
                if expected_message is not None:
                    assert expected_message in str(error), str(error)
                results.append("PASS rejection: " + label)
            else:
                raise AssertionError("not rejected: " + label)
            finally:
                restore()

        pristine = journal.read_text()
        bad = copy.deepcopy(events)
        bad[0]["after"]["sources"]["/proc/pressure/cpu"] = "some avg10=6 avg60=0 avg300=0 total=1"
        rejected(lambda: journal.write_text("".join(json.dumps(e) + "\n" for e in bad)),
                 lambda: journal.write_text(pristine), "forged eligible flag")
        saved = lastmeta.read_text()
        rejected(lastmeta.unlink, lambda: lastmeta.write_text(saved), "dropped admission record")
        rejected(lambda: journal.write_text(pristine + json.dumps(events[-1]) + "\n"),
                 lambda: journal.write_text(pristine), "reused terminal admission event")
        rejected(lambda: terminal.write_text(json.dumps({"admission_wait_s": 719})),
                 lambda: terminal.write_text(json.dumps({"admission_wait_s": 720})), "misbound terminal waiting budget")
        bad = json.loads(json.dumps(events))
        for event in bad[:3]:
            event["before"]["monotonic_s"] *= 1000
            event["after"]["monotonic_s"] *= 1000
        firstmeta = root / "samples/diff-pair-reference-0.json"
        first_saved = firstmeta.read_text()
        first_changed = json.loads(first_saved)
        first_changed["before_launch"] = bad[2]["after"]
        first_changed["after_completion"]["monotonic_s"] = bad[2]["after"]["monotonic_s"] + 10
        def corrupt_span():
            journal.write_text("".join(json.dumps(e) + "\n" for e in bad))
            firstmeta.write_text(json.dumps(first_changed))
        def restore_span():
            journal.write_text(pristine)
            firstmeta.write_text(first_saved)
        rejected(corrupt_span, restore_span, "raw snapshot span exceeds claimed budget",
                 expected_message="raw snapshot elapsed waiting time")
        sampledir = root / "candidate-0"
        (sampledir / "run").mkdir(parents=True)
        sample = {"result": str(sampledir / "run/result.json"), "wall_s": 10.0, "user_s": 8.0,
                  "system_s": 2.0, "max_rss_kib": 100}
        (sampledir / "time.json").write_text(json.dumps({key: sample[key] for key in ("wall_s", "user_s", "system_s", "max_rss_kib")}))
        f.verify_raw_timing(sample)
        rejected(lambda: sample.update(wall_s=9.0), lambda: sample.update(wall_s=10.0),
                 "summary differs from raw GNU time", check=lambda: f.verify_raw_timing(sample))
    old_profile, old_read = f.quiet_profile, f.read
    f.quiet_profile = lambda: {"metadata_output": "/synthetic-nonterminal-matrix"}
    f.read = lambda path: {"status": "RUNNING"}
    try:
        try:
            f.verify_final_matrix("18111", 0)
        except RuntimeError as error:
            assert "complete terminal PASS" in str(error), error
            results.append("PASS rejection: nonterminal RUNNING matrix cannot finalize")
        else:
            raise AssertionError("nonterminal matrix finalized")
    finally:
        f.quiet_profile, f.read = old_profile, old_read
    assert len(results) == 19
    digest = hashlib.sha256((Q / "finalize-acceptance.py").read_bytes()).hexdigest()
    print("Finalizer SHA256: " + digest)
    print("\n".join(results))
    print("19 bounded verifier checks PASS; synthetic data only, no native execution or real waiting.")


if __name__ == "__main__":
    main()
