"""Immutable native baseline generations, selected by one atomic pointer."""
import json
import os
import shutil
import tempfile
from pathlib import Path

from . import common, contract, native_components, pour, statemanifest

SCHEMA = 2
FILES = ("baseline-drc.json", "baseline-info.json", pour.FACTS_NAME, native_components.FACTS_NAME)


def identity(fingerprint, board_name, pcbnew_build=None):
    root = Path(__file__).parent
    grading = ("baselinecache.py", "cli.py", "pour.py", "kicad/pour.py", "contract.py")
    return {"schema": SCHEMA, "input": fingerprint, "board": board_name,
            "kicad_cli": statemanifest.kicad_cli_identity(),
            "pcbnew": pcbnew_build if pcbnew_build is not None else statemanifest.pcbnew_build_version(),
            "pour_schema": pour.SCHEMA,
            "native_terminals": {"schema": native_components.SCHEMA,
                                 "producer": native_components.producer_identity(),
                                 "tool": native_components.native_identity()},
            "grading": {p: common.sha256(root / p) for p in grading},
            "result_schemas": {p.name: common.sha256(p) for p in
                               sorted(contract.SCHEMAS_DIR.glob("result-*.json"))}}


def read(cache, expected):
    """Read exactly one complete generation; malformed and legacy entries miss."""
    try:
        pointer = json.loads((cache / "current.json").read_text())
        name = pointer["generation"]
        if not isinstance(name, str) or not name.startswith("generation-") or Path(name).name != name:
            return None
        directory = cache / name
        receipt = json.loads((directory / "generation.json").read_text())
        if receipt["identity"] != expected or receipt["generation"] != name:
            return None
        if not isinstance(receipt["files"], dict) or set(receipt["files"]) != set(FILES):
            return None
        values = {}
        for name in FILES:
            path = directory / name
            if common.sha256(path) != receipt["files"][name]:
                return None
            values[name] = json.loads(path.read_text())
        report, info, facts, terminals = (values[p] for p in FILES)
        if (not isinstance(report, dict) or not all(isinstance(report.get(k), list) for k in
                ("violations", "unconnected_items", "schematic_parity"))
                or not isinstance(info, dict)
                or not native_components.source_identity_valid(info.get("source"))
                or not isinstance(info.get("refill"), dict)
                or info["refill"].get("board_sha256_before") != info["source"]["board_sha256"]
                or not native_components.coverage(terminals)
                or terminals.get("identity", {}).get("pcbnew") != expected["native_terminals"]["tool"]
                or terminals.get("identity", {}).get("producer") != expected["native_terminals"]["producer"]
                or terminals["identity"]["kicad_cli"] != expected["kicad_cli"]
                or terminals["identity"]["rules"] != info["source"]["rules"]
                or terminals.get("identity", {}).get("board_sha256") != info.get("board_sha256_graded")
                or not isinstance(info.get("reasons"), list)
                or not isinstance(facts, dict) or facts.get("schema") != pour.SCHEMA
                or not isinstance(facts.get("zones"), list)
                or not all(isinstance(facts.get(k), dict) for k in ("layers", "polygons", "pads"))):
            return None
        info.update(cached=True, report=str(directory / FILES[0]),
                    identity=expected, generation=receipt["generation"])
        return report, info
    except (OSError, ValueError, KeyError, TypeError):
        return None


def get(reference, board_name, fingerprint, root, drc, copy_project, pcbnew_build=None):
    source = {"board_sha256": common.sha256(Path(reference) / board_name),
              "rules": native_components.rules_identity(Path(reference) / board_name)}
    expected = identity(fingerprint, board_name, pcbnew_build=pcbnew_build)
    cache = Path(root) / common.sha256_of(expected)
    hit = read(cache, expected)
    if hit is not None and hit[1].get("source") == source:
        return hit
    cache.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".building-", dir=cache))
    try:
        project = work / "project" / Path(reference).name
        copy_project(reference, project)
        report, info = drc(project / board_name, work / "drc", "baseline", refill_and_save=True)
        info.update(cached=False, identity=expected, generation=None, source=source)
        if report is None:
            return report, info
        facts, _ = pour.extract(project / board_name, work / pour.FACTS_NAME)
        terminals, _ = native_components.extract(project / board_name, work / native_components.FACTS_NAME,
                                                 info.get("board_sha256_graded"))
        if (facts is None or not native_components.coverage(terminals)
                or terminals["identity"]["pcbnew"] != expected["native_terminals"]["tool"]
                or info.get("refill", {}).get("board_sha256_before") != source["board_sha256"]
                or terminals["identity"]["rules"] != source["rules"]
                or common.sha256(Path(reference) / board_name) != source["board_sha256"]
                or native_components.rules_identity(Path(reference) / board_name) != source["rules"]):
            # A generation without complete native/pour coverage is never reusable.
            # Preserve only the original raw DRC as diagnostic evidence.
            incomplete = Path(tempfile.mkdtemp(prefix="uncached-", dir=cache))
            shutil.copy2(work / "drc" / FILES[0], incomplete / FILES[0])
            info["report"] = str(incomplete / FILES[0])
            return report, info
        generation = Path(tempfile.mkdtemp(prefix="generation-", dir=cache))
        generation.rmdir()
        staged = work / "publish"
        staged.mkdir()
        common.write_json(staged / FILES[0], report)
        common.write_json(staged / FILES[1], info)
        common.write_json(staged / FILES[2], facts)
        common.write_json(staged / FILES[3], terminals)
        receipt = {"identity": expected, "generation": generation.name,
                   "files": {p: common.sha256(staged / p) for p in FILES}}
        common.write_json(staged / "generation.json", receipt)
        staged.rename(generation)
        pointer = work / "current.json"
        common.write_json(pointer, {"generation": generation.name})
        os.replace(pointer, cache / "current.json")
        info.update(report=str(generation / FILES[0]), generation=generation.name)
        return report, info
    finally:
        shutil.rmtree(work)


def terminal_facts(report, expected, reference_board=None):
    """Read the exact recorded generation, never a newer pointer or legacy sidecar."""
    if report is None:
        return None
    directory = Path(report).parent
    try:
        receipt = json.loads((directory / "generation.json").read_text())
        if receipt["identity"] != expected or receipt["generation"] != directory.name:
            return None
        if not isinstance(receipt["files"], dict) or set(receipt["files"]) != set(FILES):
            return None
        if any(common.sha256(directory / name) != checksum for name, checksum in receipt["files"].items()):
            return None
        facts = json.loads((directory / native_components.FACTS_NAME).read_text())
        info = json.loads((directory / "baseline-info.json").read_text())
        if (not isinstance(info, dict) or not native_components.source_identity_valid(info.get("source"))
                or not isinstance(info.get("refill"), dict)
                or info["refill"].get("board_sha256_before") != info["source"]["board_sha256"]):
            return None
        if reference_board is not None and info.get("source") != {
                "board_sha256": common.sha256(reference_board), "rules": native_components.rules_identity(reference_board)}:
            return None
        if (not native_components.coverage(facts)
                or facts["identity"]["producer"] != expected["native_terminals"]["producer"]
                or facts["identity"]["pcbnew"] != expected["native_terminals"]["tool"]
                or facts["identity"]["kicad_cli"] != expected["kicad_cli"]
                or facts["identity"]["rules"] != info["source"]["rules"]
                or facts["identity"]["board_sha256"] != info.get("board_sha256_graded")):
            return None
        return facts
    except (OSError, ValueError, KeyError, TypeError):
        return None
