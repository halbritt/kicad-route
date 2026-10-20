"""Read-only portable evidence and unchanged native comparison gates.

An index must be captured by the trusted transfer step and supplied with its
observed SHA256. Original remote JSON bytes/absolute locators stay unchanged.
No remote process, container, build, routing, or filesystem mutation occurs here.
"""
import hashlib
import types
import json
import math
from pathlib import Path, PurePosixPath


class EvidenceError(RuntimeError):
    pass


def require(value, message):
    if not value:
        raise EvidenceError(message)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class EvidenceStore:
    def __init__(self, index_path, index_sha256, required_host="archon"):
        self.index_path = Path(index_path).resolve()
        require(digest(self.index_path) == index_sha256, "retained inventory SHA differs from observed transfer")
        self.index = json.loads(self.index_path.read_text())
        require(self.index["host"] == required_host, "retained evidence is not from requested final host")
        self.root = self.index_path.parent
        self.files = {}
        retained = {}
        for row in self.index["files"]:
            source = PurePosixPath(row["source"])
            target = PurePosixPath(row["retained"])
            require(source.is_absolute() and ".." not in source.parts and str(source) == row["source"],
                    "noncanonical original evidence locator")
            require(not target.is_absolute() and target.parts and ".." not in target.parts and
                    str(target) == row["retained"], "unsafe retained evidence locator")
            require(row["source"] not in self.files, "duplicate original evidence identity")
            require(row["retained"] not in retained or retained[row["retained"]] == row["sha256"],
                    "retained alias claims conflicting bytes")
            path = self.root.joinpath(*target.parts)
            require(path.resolve().is_relative_to(self.root) and path.is_file() and not path.is_symlink(),
                    "missing/escaped retained evidence file")
            require(digest(path) == row["sha256"], "retained evidence bytes differ from transfer inventory")
            self.files[row["source"]] = dict(row, local_path=path)
            retained[row["retained"]] = row["sha256"]
        require(self.files, "retained evidence inventory is empty")

    def resolve(self, original):
        # Never fall back to an existing local file bearing the remote name.
        row = self.files.get(str(original))
        require(row is not None, "original evidence locator absent from inventory: " + str(original))
        path = row["local_path"]
        require(path.resolve().is_relative_to(self.root) and not path.is_symlink() and
                digest(path) == row["sha256"], "retained evidence changed after inventory validation")
        return path

    def read(self, original):
        return json.loads(self.resolve(original).read_text())

    def artifact(self, original):
        self.resolve(original)
        row = self.files[str(original)]
        return {key: row[key] for key in ("source", "retained", "sha256")}

    def descendants(self, original_directory, pattern=None):
        directory = PurePosixPath(original_directory)
        require(directory.is_absolute() and ".." not in directory.parts, "unsafe original evidence directory")
        result = set()
        for source in self.files:
            path = PurePosixPath(source)
            if path.is_relative_to(directory) and path != directory:
                relative = path.relative_to(directory)
                if pattern is None or relative.match(pattern):
                    result.add(source)
        return result

    def load_comparison(self, original_driver, expected_sha256):
        path = self.resolve(original_driver)
        require(digest(path) == expected_sha256, "frozen adjudicator bytes differ from preregistration")
        # The frozen comparison script is stdlib-only and guards execution with
        # __main__; importing it defines pure quality/compare functions only.
        module = types.ModuleType("frozen_retained_comparison")
        module.__file__ = str(path)
        exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
        return module


def validate_declared_profile(host, affinity, repetitions, warmups, board_names, gate_limit):
    require(host == "archon", "active final performance host must be user-selected archon")
    require(isinstance(affinity, list) and affinity and affinity == sorted(set(affinity)) and
            all(type(cpu) is int and cpu >= 0 for cpu in affinity), "invalid declared host CPU affinity")
    require(repetitions == 5 and warmups == 1 and gate_limit == 1.10 and
            board_names == ["diff-pair", "four-layer", "multipoint", "route-demo"],
            "fixed48-sample/four-board/unchanged1.10 profile changed")


def verify_raw_timing(store, sample):
    original = str(PurePosixPath(sample["result"]).parent.parent / "time.json")
    raw = store.read(original)
    for field in ("wall_s", "user_s", "system_s", "max_rss_kib"):
        value = raw.get(field)
        require(type(value) in (int, float) and math.isfinite(value) and value >= 0 and
                sample.get(field) == value, "sample differs from actual raw GNU time: " + field)
    require(raw["wall_s"] > 0 and raw["max_rss_kib"] > 0, "nonpositive raw wall/RSS")
    return store.artifact(original)


def verify_board_gates(store, board, comparison, declared_affinity):
    """Profile-independent native/wall/RSS gates; never execute the runner."""
    output = PurePosixPath(board["output"])
    report_path = str(output / "comparison.json")
    report, raw = store.read(report_path), store.read(str(output / "samples.json"))
    require(report["schema"] == "kicad-route/engine-comparison/2" and
            report["mode"] == "migration-equivalence" and report["project"] == board["project"] and
            report["args"] == board["args"] and report["cpu_affinity"] == declared_affinity,
            "comparison board/settings/declared CPU identity differs")
    require(all(report.get(key) == raw.get(key) for key in ("reference", "candidate", "warmup")),
            "comparison summaries differ from retained raw samples")
    require(len(raw["reference"]) == len(raw["candidate"]) == 5 and
            set(raw["warmup"]) == {"reference", "candidate"}, "not exactly5+5+2 raw native samples")
    samples = raw["reference"] + raw["candidate"] + list(raw["warmup"].values())
    expected = {str(output / f"{side}-{index}/run/result.json")
                for side in ("reference", "candidate") for index in range(6)}
    require({sample["result"] for sample in samples} == expected and
            store.descendants(str(output), "*/run/result.json") == expected,
            "missing/extra/dropped native result population")
    for sample in samples:
        result = store.read(sample["result"])
        quality = comparison.quality(result)
        require(sample["exit_code"] == result["exit_code"] == 0 and quality == sample["quality"] and
                quality["routing"] == "COMPLETE" and quality["unconnected"] == 0 and quality["open_nets"] == [],
                "native raw quality/result differs or is incomplete")
        verify_raw_timing(store, sample)
    verdict = comparison.compare(raw["reference"], raw["candidate"], raw["warmup"], report["mode"])
    require(verdict == report["verdict"] and verdict["passed"] is True and
            verdict["copper_geometry_equal"] is True and set(verdict["metrics"]) ==
            {"wall_s", "max_rss_kib", "process_tree_peak_rss_kib"} and
            all(metric["limit"] == 1.10 for metric in verdict["metrics"].values()),
            "unchanged fullCLI wall/native/copper/dualRSS gates do not pass")
    return {"board": board["name"], "report": store.artifact(report_path),
            "raw_samples": store.artifact(str(output / "samples.json")), "verdict": verdict,
            "native_attempts": 12, "raw_native_and_GNU_time_recomputed": True,
            "additional_required_profile_gates": "Frozen host/container/tool/build/input identity, terminal48sample authority and per-attempt resource/membership verification remain mandatory"}
