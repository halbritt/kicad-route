"""Immutable native baseline generations, selected by one atomic pointer."""
import json
import os
import shutil
import tempfile
from pathlib import Path

from . import common, contract, pour, statemanifest

SCHEMA = 1
FILES = ("baseline-drc.json", "baseline-info.json", pour.FACTS_NAME)


def identity(fingerprint, board_name):
    root = Path(__file__).parent
    grading = ("baselinecache.py", "cli.py", "pour.py", "kicad/pour.py", "contract.py")
    return {"schema": SCHEMA, "input": fingerprint, "board": board_name,
            "kicad_cli": statemanifest.kicad_cli_identity(),
            "pcbnew": statemanifest.pcbnew_build_version(), "pour_schema": pour.SCHEMA,
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
        if set(receipt["files"]) != set(FILES):
            return None
        values = {}
        for name in FILES:
            path = directory / name
            if common.sha256(path) != receipt["files"][name]:
                return None
            values[name] = json.loads(path.read_text())
        report, info, facts = (values[p] for p in FILES)
        if (not isinstance(report, dict) or not all(isinstance(report.get(k), list) for k in
                ("violations", "unconnected_items", "schematic_parity"))
                or not isinstance(info, dict) or not isinstance(info.get("reasons"), list)
                or not isinstance(facts, dict) or facts.get("schema") != pour.SCHEMA
                or not isinstance(facts.get("zones"), list)
                or not all(isinstance(facts.get(k), dict) for k in ("layers", "polygons", "pads"))):
            return None
        info.update(cached=True, report=str(directory / FILES[0]),
                    identity=expected, generation=receipt["generation"])
        return report, info
    except (OSError, ValueError, KeyError, TypeError):
        return None


def get(reference, board_name, fingerprint, root, drc, copy_project):
    expected = identity(fingerprint, board_name)
    cache = Path(root) / common.sha256_of(expected)
    hit = read(cache, expected)
    if hit is not None:
        return hit
    cache.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".building-", dir=cache))
    try:
        project = work / "project" / Path(reference).name
        copy_project(reference, project)
        report, info = drc(project / board_name, work / "drc", "baseline", refill_and_save=True)
        info.update(cached=False, identity=expected, generation=None)
        if report is None:
            return report, info
        facts, _ = pour.extract(project / board_name, work / pour.FACTS_NAME)
        if facts is None:
            # A DRC-only entry is never a reusable baseline. Keep the DRC for
            # this run; validation will independently fail if pour extraction fails.
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
