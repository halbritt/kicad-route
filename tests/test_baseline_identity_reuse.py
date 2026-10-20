"""Reuse this attempt's native version without weakening baseline cache identity."""
import shutil
from types import SimpleNamespace

import pytest

from helpers import FIXTURE
from kicad_route import baselinecache, campaign, cli, common
from test_evidence_integrity import fake_baseline


class CacheMiss(Exception):
    pass


def test_fresh_facts_identity_matches_probe_and_avoids_redundant_native_import(fake_baseline, monkeypatch):
    get, calls, tools, root, reference = fake_baseline
    _, first = get()
    expected = baselinecache.identity("input", "a.kicad_pcb")
    assert expected["pcbnew"] == "pcbnew"
    monkeypatch.setattr(baselinecache.statemanifest, "pcbnew_build_version",
                        lambda: pytest.fail("fresh native facts already identify pcbnew"))
    assert baselinecache.identity("input", "a.kicad_pcb", pcbnew_build="pcbnew") == expected
    def miss(*args, **kwargs):
        raise CacheMiss
    _, hit = baselinecache.get(reference, "a.kicad_pcb", "input", root, miss,
                               shutil.copytree, pcbnew_build="pcbnew")
    assert hit["cached"] is True and hit["generation"] == first["generation"]
    assert hit["identity"]["pcbnew"] == "pcbnew" and len(calls) == 1


@pytest.mark.parametrize("change", ["native version", "native CLI"])
def test_fresh_version_or_tool_change_still_misses_existing_generation(fake_baseline, monkeypatch, change):
    get, calls, tools, root, reference = fake_baseline
    get()
    before = (reference / "a.kicad_pcb").read_bytes()
    version = "new pcbnew" if change == "native version" else "pcbnew"
    if change == "native CLI":
        tools["sha256"] = "new native CLI"
    monkeypatch.setattr(baselinecache.statemanifest, "pcbnew_build_version",
                        lambda: pytest.fail("supplied native version must be used"))
    def miss(*args, **kwargs):
        raise CacheMiss
    with pytest.raises(CacheMiss):
        baselinecache.get(reference, "a.kicad_pcb", "input", root, miss,
                          shutil.copytree, pcbnew_build=version)
    assert (reference / "a.kicad_pcb").read_bytes() == before


def test_legacy_and_explicit_unknown_versions_keep_the_probe_fallback(fake_baseline, monkeypatch):
    get, calls, tools, root, reference = fake_baseline
    versions = iter(["first import", "second import"])
    monkeypatch.setattr(baselinecache.statemanifest, "pcbnew_build_version", lambda: next(versions))
    assert baselinecache.identity("input", "a.kicad_pcb")["pcbnew"] == "first import"
    assert baselinecache.identity("input", "a.kicad_pcb", pcbnew_build=None)["pcbnew"] == "second import"


def test_baseline_adapter_forwards_captured_version(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "_out", tmp_path)
    captured = []
    def get(*args, **kwargs):
        captured.append(kwargs["pcbnew_build"])
        return {}, {}
    monkeypatch.setattr(baselinecache, "get", get)
    cli.baseline_drc(tmp_path, "board.kicad_pcb", "input", pcbnew_build="native facts version")
    assert captured == ["native facts version"]


def test_run_attempt_forwards_this_reference_facts_version(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "_out", tmp_path / "out")
    monkeypatch.setattr(common, "ensure_kicad_config", lambda: None)
    monkeypatch.setattr(cli.sandbox, "facts", lambda **kwargs: {})
    monkeypatch.setitem(cli.BACKENDS, "krt", {"version": lambda: {}})
    monkeypatch.setattr(cli.evidence, "backend_identity", lambda *args: {})
    monkeypatch.setattr(cli.floor, "preflight", lambda *args, **kwargs: None)
    native_facts = {"kicad_version": "fresh native reference version", "nets": {"/A": {"netclass": "Default"}}}
    monkeypatch.setattr(cli, "board_facts", lambda *args: (native_facts, {"exit_code": 0}))
    captured = []
    def baseline(*args, **kwargs):
        captured.append(kwargs["pcbnew_build"])
        raise CacheMiss
    monkeypatch.setattr(cli, "baseline_drc", baseline)
    with pytest.raises(CacheMiss):
        cli.run_attempt(cli.run_namespace("krt", dir=str(FIXTURE), run_dir=str(tmp_path / "run")))
    assert captured == [native_facts["kicad_version"]]


def test_campaign_cache_warming_forwards_this_reference_facts_version(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "ensure_kicad_config", lambda: None)
    facts = {"kicad_version": "fresh campaign reference version"}
    captured = []
    adapter = SimpleNamespace(snapshot_input=lambda *args: (tmp_path, {}),
        board_facts=lambda *args: (facts, {}), input_fingerprint=lambda manifest: "input",
        select_nets=lambda *args: ([], {}),
        baseline_drc=lambda *args, **kwargs: captured.append(kwargs["pcbnew_build"]))
    command = SimpleNamespace(cli=adapter, cfg={"pcb": "board.kicad_pcb"}, dir=tmp_path,
                              args=SimpleNamespace(nets=None, netclass=None, race=2))
    assert campaign.Campaign.prepare_race(command) == []
    assert captured == [facts["kicad_version"]]
