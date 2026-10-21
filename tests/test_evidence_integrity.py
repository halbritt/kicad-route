"""Negative evidence cases for corpus, cache publication and execution identity."""
import copy
import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from helpers import ROOT  # noqa: F401 (puts src/ on sys.path)
from kicad_route import baselinecache, cli, common, corpus, determinism, evidence, ledger, native_components, pour


def outcome(**updates):
    rec = {"exit_code": 0, "process": "PASS", "validation": "PASS", "routing": "COMPLETE",
           "unconnected_in_scope": 0, "new_violations": 0, "new_parity": 0,
           "preservation_fail": [], "preservation_checks": ["copper", "pour_integrity"]}
    rec.update(updates)
    return rec


@pytest.mark.parametrize("change,reason", [
    ({"validation": "FAIL"}, "validation PASS -> FAIL"),
    ({"routing": "INCOMPLETE"}, "routing COMPLETE -> INCOMPLETE"),
    ({"new_violations": None}, "missing evidence: new DRC"),
    ({"new_parity": True}, "missing evidence: new parity"),
    ({"preservation_checks": ["copper"]}, "missing preservation evidence"),
    ({"exit_code": 3}, "exit status"),
    ({"validation": "FAIL", "routing": "NOT_RUN", "new_violations": None,
      "new_parity": None, "preservation_checks": []}, "validation PASS -> FAIL"),
])
def test_corpus_rejects_lost_evidence(change, reason):
    regressions, _ = corpus.compare(outcome(), outcome(**change))
    assert any(reason in r for r in regressions)


def test_corpus_unchanged_invalid_and_improving_baselines():
    for rec in (outcome(), outcome(exit_code=2, routing="INCOMPLETE", unconnected_in_scope=2),
                outcome(exit_code=3, validation="FAIL", preservation_fail=["copper"])):
        assert corpus.compare(rec, copy.deepcopy(rec))[0] == []
    reg, imp = corpus.compare(outcome(exit_code=2, routing="INCOMPLETE", unconnected_in_scope=2), outcome())
    assert not reg and "routing now COMPLETE" in imp and "unconnected in scope 2 -> 0" in imp
    assert corpus.compare(outcome(), {"refused": "no backend"})[0] == ["attempt refused: no backend"]


def test_failed_validation_fails_corpus_and_update(tmp_path, monkeypatch):
    common.set_out(tmp_path / "out")
    root = tmp_path / "corpus"
    common.write_json(root / "test" / "entry.json", {"name": "test", "board": "board.kicad_pcb",
        "reference_dir": "reference", "attempt": {"backend": "krt"}, "baseline": outcome()})
    failed = {"exit_code": 3, "process": {"status": "PASS"}, "validation": {"status": "FAIL"},
              "routing": {"status": "NOT_RUN"}, "backend": {"name": "krt"}, "run_dir": str(tmp_path)}
    monkeypatch.setattr(cli, "run_attempt", lambda args: failed)
    assert corpus.check(root=root)["status"] == "FAIL"
    update = tmp_path / "update.json"
    assert corpus.after_update(update, 0, root) == 1
    assert common.load_json(update)["corpus"]["status"] == "FAIL"


@pytest.mark.parametrize("changed", [False, True])
@pytest.mark.parametrize("revalidate", [False, True])
def test_candidate_evidence_identifies_final_refilled_artifact(tmp_path, changed, revalidate):
    run = tmp_path / "run"
    board = run / "candidate" / "board.kicad_pcb"
    board.parent.mkdir(parents=True)
    board.write_bytes(b"backend")
    pre = common.sha256(board)
    if changed:
        board.write_bytes(b"native refill")
    final = common.sha256(board)
    common.write_json(run / "snapshot.json", {"files": {"board.kicad_pcb": "input"}})
    common.write_json(run / "attempt.json", {"board": "board.kicad_pcb"})
    grade = run / ("revalidate/one" if revalidate else "validate")
    grade.mkdir(parents=True)
    validation = {"delivered_board_sha256": pre, "validated_board_sha256": final,
                  "candidate_board_sha256": final}
    block = evidence.block(run, {"board": "board.kicad_pcb"}, grade, validation, pre,
                           records_base=grade if revalidate else run)
    for name, checksum in block["records"].items():
        assert common.sha256((grade if revalidate else run) / name) == checksum
    assert block["candidate_board_sha256"] == common.sha256(board) == final
    assert block["pre_refill_board_sha256"] == block["backend_output_board_sha256"] == pre
    assert (pre != final) == changed
    assert determinism.layer_record({"evidence": block})["candidate_bytes"] == final


@pytest.fixture
def fake_baseline(tmp_path, monkeypatch):
    reference = tmp_path / "reference"
    reference.mkdir()
    (reference / "a.kicad_pcb").write_text("untouched a")
    (reference / "b.kicad_pcb").write_text("untouched b")
    tools = {"version": "1", "path": "/usr/bin/kicad-cli", "sha256": "a" * 64}
    monkeypatch.setattr(baselinecache.statemanifest, "kicad_cli_identity", lambda: dict(tools))
    monkeypatch.setattr(baselinecache.statemanifest, "pcbnew_build_version", lambda: "pcbnew")
    native = {"version": "pcbnew", **{key: {"path": key, "sha256": "a" * 64}
                                      for key in ("module", "binding", "python")}}
    monkeypatch.setattr(native_components, "native_identity", lambda: native)
    calls = []
    def terminals(board, path, expected_sha256=None):
        from component_fixtures import component_receipt
        facts = component_receipt({}, board_sha256=common.sha256(board))
        facts["native_identity"] = facts["identity"]["pcbnew"] = native
        facts["identity"]["rules"] = native_components.rules_identity(board)
        facts["identity"]["kicad_cli"] = dict(tools)
        common.write_json(path, facts)
        return facts, {}
    monkeypatch.setattr(native_components, "extract", terminals)
    def drc(board, directory, name, **kwargs):
        calls.append(str(board))
        report = {"violations": [], "unconnected_items": [], "schematic_parity": [], "board": board.name}
        common.write_json(directory / "baseline-drc.json", report)
        return report, {"reasons": [], "report": str(directory / "baseline-drc.json"),
                        "board_sha256_graded": common.sha256(board),
                        "refill": {"board_sha256_before": common.sha256(board)}}
    def extract(board, path):
        facts = {"schema": pour.SCHEMA, "board": board.name, "zones": [],
                 "layers": {}, "polygons": {}, "pads": {}}
        common.write_json(path, facts)
        return facts, {}
    monkeypatch.setattr(pour, "extract", extract)
    root = tmp_path / "cache"
    def get(board="a.kicad_pcb"):
        return baselinecache.get(reference, board, "input", root, drc, shutil.copytree)
    return get, calls, tools, root, reference


def test_baseline_identity_invalidates_board_tools_and_schema(fake_baseline, monkeypatch):
    get, calls, tools, root, reference = fake_baseline
    _, first = get()
    assert not first["cached"] and first["generation"]
    _, second = get()
    assert second["cached"] and len(calls) == 1 and second["generation"] == first["generation"]
    assert not get("b.kicad_pcb")[1]["cached"]
    tools["sha256"] = "e" * 64
    assert not get()[1]["cached"]
    monkeypatch.setattr(baselinecache, "SCHEMA", baselinecache.SCHEMA + 1)
    assert not get()[1]["cached"]
    assert (reference / "a.kicad_pcb").read_text() == "untouched a"


@pytest.mark.parametrize("name", [*baselinecache.FILES, "generation.json", "current.json"])
@pytest.mark.parametrize("damage", ["missing", "malformed"])
def test_baseline_damaged_entries_recompute(fake_baseline, name, damage):
    get, calls, _, root, _ = fake_baseline
    _, first = get()
    directory = Path(first["report"]).parent
    path = directory.parent / name if name == "current.json" else directory / name
    if damage == "missing":
        path.unlink()
    else:
        path.write_text("{")
    assert not get()[1]["cached"] and len(calls) == 2


@pytest.mark.parametrize("fail_at", [1, 2, 3, 4, 5, 6])
def test_interrupted_publication_is_never_a_hit(fake_baseline, monkeypatch, fail_at):
    get, calls, _, root, _ = fake_baseline
    original = common.write_json
    writes = 0
    def interrupted(path, data):
        nonlocal writes
        if Path(path).parent.name == "publish" or Path(path).name == "current.json":
            writes += 1
            if writes == fail_at:
                raise OSError("interrupted cache write")
        return original(path, data)
    monkeypatch.setattr(common, "write_json", interrupted)
    with pytest.raises(OSError, match="interrupted"):
        get()
    monkeypatch.setattr(common, "write_json", original)
    assert not get()[1]["cached"] and len(calls) == 2
    assert get()[1]["cached"]


def test_concurrent_cache_publications_are_complete(fake_baseline):
    get, calls, _, root, _ = fake_baseline
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: get(), range(12)))
    for report, info in results:
        generation = Path(info["report"]).parent
        receipt = common.load_json(generation / "generation.json")
        assert receipt["identity"] == info["identity"]
        assert report["board"] == common.load_json(generation / pour.FACTS_NAME)["board"]
        for name, checksum in receipt["files"].items():
            assert common.sha256(generation / name) == checksum
    assert get()[1]["cached"]


def test_content_execution_identity_and_legacy_rows(tmp_path, monkeypatch):
    monkeypatch.delenv("KICAD_ROUTE_LEDGER", raising=False)
    path = tmp_path / "ledger"
    version = {"commit": "docs1", "version": "1", "execution_identity": {
        "source_sha256": "source", "extension_module_sha256": "extension", "venv_packages_sha256": "deps"}}
    settings = {"protect": "human", "protect_requested": "human", "protect_only_sha256": "human keys"}
    gate = ledger.check(path, "input", "krt", version, settings)
    result = {"backend": {"name": "krt", **version}, "settings": settings,
              "input": {"fingerprint": "input", "project": "p"}, "run_dir": str(tmp_path / "run"),
              "process": {"status": "FAIL"}, "routing": {"status": "NOT_RUN"}, "exit_code": 1}
    ledger.append(path, gate, result)
    assert ledger.from_result(result)[0] == gate["fingerprint"]
    assert ledger.check(path, "input", "krt", dict(version, commit="docs2"), settings)["refuse"]
    assert ledger.check(path, "input", "krt", version, settings, force=True)["forced"]
    for key in version["execution_identity"]:
        changed = copy.deepcopy(version)
        changed["execution_identity"][key] += "-changed"
        assert not ledger.check(path, "input", "krt", changed, settings)["refuse"]
    old = {"version": "1", "commit": "old"}
    assert ledger.fingerprint("input", "krt", old, settings) == ledger.fingerprint("input", "krt", old, settings)
    assert not ledger.check(path, "input", "krt", old, settings)["refuse"]


def test_legacy_determinism_uses_validation_final_hash():
    result = {"validation": {"candidate_board_sha256": "final"},
              "evidence": {"candidate_board_sha256": "before refill"}}
    assert determinism.layer_record(result)["candidate_bytes"] == "final"
    assert determinism.layer_record({"evidence": result["evidence"]})["candidate_bytes"] is None


def test_native_refill_and_human_ledger_reconstruction(tmp_path):
    from helpers import cli as command, FIXTURE, OUT
    run = tmp_path / "run"
    routed = command("--json", "run", "--backend", "krt", "--dir", str(FIXTURE),
                     "--run-dir", str(run), "--nets", "/SIG_A*", "--protect", "human",
                     "--default-clearance", "0.4", "--budget", "120")
    assert routed.returncode == 0, routed.stdout + routed.stderr
    result = json.loads(routed.stdout)
    assert ledger.from_result(result)[0] == result["ledger"]["fingerprint"]
    board = Path(result["candidate"]["board"])
    board = board if board.is_absolute() else OUT / board
    assert result["evidence"]["candidate_board_sha256"] == common.sha256(board)
    board.write_bytes(board.read_bytes() + b"\n\n")
    pre = common.sha256(board)
    validated = command("--json", "validate", str(run))
    assert validated.returncode == 0, validated.stdout + validated.stderr
    regraded = json.loads(validated.stdout)
    assert regraded["validation"]["delivered_board_sha256"] == pre
    assert pre != common.sha256(board), "native refill must save changed bytes for this regression"
    assert regraded["evidence"]["candidate_board_sha256"] == common.sha256(board)
    assert regraded["evidence"]["pre_refill_board_sha256"] == pre
    assert regraded["evidence"]["candidate_board_sha256"] == regraded["validation"]["candidate_board_sha256"]


def test_execution_identity_tracks_installed_bytes_without_git(tmp_path, monkeypatch):
    from kicad_route import engine
    source = tmp_path / "owned"
    source.mkdir()
    (source / "VERSION").write_text("1")
    (source / "UPSTREAM.json").write_text("{}")
    implementation = source / "engine.py"
    implementation.write_text("first implementation")
    monkeypatch.setattr(engine, "ROOT", source)
    monkeypatch.setattr(common, "git", lambda *args: None)
    site = tmp_path / ".venv-krt" / "lib/python3.12/site-packages"
    site.mkdir(parents=True)
    extension = site / "grid_router.so"
    extension.write_bytes(b"first extension")
    metadata = site / "numpy.dist-info" / "METADATA"
    metadata.parent.mkdir()
    metadata.write_text("Name: numpy\nVersion: 1\n")
    def identity():
        return evidence.backend_identity("krt", engine.identity(), tmp_path)
    first = identity()
    assert first["commit"] is None
    implementation.write_text("second implementation")
    second = identity()
    assert second["source_sha256"] != first["source_sha256"]
    extension.write_bytes(b"second extension")
    third = identity()
    assert third["extension_module_sha256"] != second["extension_module_sha256"]
    metadata.write_text("Name: numpy\nVersion: 2\n")
    assert identity()["venv_packages_sha256"] != third["venv_packages_sha256"]


@pytest.mark.parametrize('generation', ['.', '..', '../generation-other', '/generation-other'])
def test_baseline_pointer_cannot_select_outside_a_generation(fake_baseline, generation):
    get, calls, _, _, _ = fake_baseline
    _, first = get()
    pointer = Path(first['report']).parent.parent / 'current.json'
    common.write_json(pointer, {'generation': generation})
    assert not get()[1]['cached'] and len(calls) == 2


def test_native_terminal_producer_and_binary_changes_invalidate_generation(fake_baseline, monkeypatch):
    get, calls, _, _, _ = fake_baseline
    get()
    identity = native_components.native_identity()
    changed = json.loads(json.dumps(identity))
    changed["module"]["sha256"] = "f" * 64
    monkeypatch.setattr(native_components, "native_identity", lambda: changed)
    assert not get()[1]["cached"]
    original = native_components.producer_identity()
    altered = {**original, "kicad/native_components.py": "a" * 64}
    monkeypatch.setattr(native_components, "producer_identity", lambda: altered)
    assert not get()[1]["cached"] and len(calls) == 3


@pytest.mark.parametrize("name", ["baseline-info.json", native_components.FACTS_NAME])
@pytest.mark.parametrize("payload", [None, []])
def test_nonobject_sealed_baseline_artifacts_are_cache_misses(fake_baseline, name, payload):
    get, _, _, _, _ = fake_baseline
    _, first = get()
    directory = Path(first["report"]).parent
    common.write_json(directory / name, payload)
    receipt = common.load_json(directory / "generation.json")
    receipt["files"][name] = common.sha256(directory / name)
    common.write_json(directory / "generation.json", receipt)
    assert baselinecache.read(directory.parent, first["identity"]) is None
    assert baselinecache.terminal_facts(first["report"], first["identity"]) is None


def test_sealed_baseline_without_source_binding_is_unknown(fake_baseline):
    get, _, _, _, _ = fake_baseline
    _, first = get()
    directory = Path(first["report"]).parent
    info = common.load_json(directory / "baseline-info.json")
    info["source"] = None
    common.write_json(directory / "baseline-info.json", info)
    receipt = common.load_json(directory / "generation.json")
    receipt["files"]["baseline-info.json"] = common.sha256(directory / "baseline-info.json")
    common.write_json(directory / "generation.json", receipt)
    assert baselinecache.read(directory.parent, first["identity"]) is None
    assert baselinecache.terminal_facts(first["report"], first["identity"]) is None
