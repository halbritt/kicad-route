"""State manifest (KICADROUTE-3), evidence and tool_identity blocks (KICADROUTE-4), and
the KRT repeat check with backend.reproducible / measurement (KICADROUTE-7).

The unit tests build a synthetic STATE in a temporary directory; the integration test
routes the fixture twice with KRT (ledger off) and compares the candidates by copper
geometry, as `kicad-route determinism` does."""
import copy
import json
import os
import stat
import zipfile
from pathlib import Path

import pytest

from helpers import FIXTURE, OUT, cli, record  # noqa: F401
from kicad_route import cli as route
from kicad_route import common, determinism, engine, evidence, ledger, statemanifest


# ------------------------------------------------------------ copper signature

def track(x1, y1, x2, y2, net="/A", layer="F.Cu", uuid="u1"):
    a, b = sorted([[x1, y1], [x2, y2]])
    return {"kind": "segment", "layer": layer, "width": 250000, "net": net, "locked": False,
            "uuid": uuid, "start": a, "end": b}


def via(x, y, uuid="v1"):
    return {"position": [x, y], "diameter": 600000, "drill": 300000, "via_type": "through",
            "layers": ["F.Cu", "B.Cu"], "net": "/A", "locked": False, "uuid": uuid}


def test_copper_signature_ignores_uuids_and_order_but_not_geometry():
    a = {"tracks": [track(0, 0, 10, 0, uuid="a"), track(10, 0, 10, 10, uuid="b")], "vias": [via(10, 10, "c")]}
    b = {"tracks": [track(10, 0, 10, 10, uuid="x"), track(0, 0, 10, 0, uuid="y")], "vias": [via(10, 10, "z")]}
    reversed_seg = {"tracks": [dict(track(0, 0, 10, 0), start=[10, 0], end=[0, 0]), track(10, 0, 10, 10)],
                    "vias": [via(10, 10)]}
    moved = copy.deepcopy(a)
    moved["tracks"][0]["end"] = [10, 1]  # 1 nm
    sig = evidence.copper_signature(a)
    assert sig == evidence.copper_signature(b) == evidence.copper_signature(reversed_seg)
    assert sig["tracks"] == 2 and sig["vias"] == 1
    assert evidence.copper_signature(moved)["sha256"] != sig["sha256"]
    other_net = copy.deepcopy(a)
    other_net["vias"][0]["net"] = "/B"
    assert evidence.copper_signature(other_net)["sha256"] != sig["sha256"]
    assert evidence.copper_signature(None) is None


# ------------------------------------------------------------- state manifest

def fake_state(root):
    """A STATE with KRT (VERSION, module, requirements, venv) and a Freerouting jar."""
    krt = root / "vendor" / "krt"
    (krt / "rust_router").mkdir(parents=True)
    (krt / "VERSION").write_text("9.9.9\n")
    (krt / "UPSTREAM.json").write_text('{"fork_commit":"synthetic-test-source"}')
    (krt / "requirements.txt").write_text("numpy>=1\n")
    site = root / ".venv-krt" / "lib" / "python3.12" / "site-packages"
    (site / "numpy-2.5.4.dist-info").mkdir(parents=True)
    (site / "grid_router.so").write_bytes(b"\x7fELF module v1")
    (site / "numpy-2.5.4.dist-info" / "METADATA").write_text("Metadata-Version: 2.4\nName: numpy\nVersion: 2.5.4\n\nbody\n")
    (root / ".venv-krt" / "pyvenv.cfg").write_text("home = /usr/bin\nversion_info = 3.12.3\n")
    cur = root / "vendor" / "freerouting" / "v9.9.9"
    cur.mkdir(parents=True)
    jar = cur / "freerouting-9.9.9.jar"
    with zipfile.ZipFile(jar, "w") as z:
        z.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\nCreated-By: 21.0.1\nBuild-Revision: abc123\n")
    (cur / "release.json").write_text(json.dumps({
        "tag": "v9.9.9", "asset": jar.name, "sha256": common.sha256(jar), "published_at": "2026-01-01T00:00:00Z",
        "build_revision": "abc123", "url": "https://example.invalid/freerouting-9.9.9.jar"}))
    (root / "vendor" / "freerouting" / "current").symlink_to("v9.9.9")
    log = root / "build-router.log"
    log.write_text("Downloading grid_router-linux-x86_64.so (12 bytes) -> x\ngrid_router v9.9.9 ready.\n"
                   "Installed prebuilt binary from release v9.9.9.\n")
    return log


@pytest.fixture
def state(tmp_path, monkeypatch):
    root = tmp_path / "state"
    root.mkdir()
    log = fake_state(root)
    monkeypatch.setattr(engine, "ROOT", root / "vendor" / "krt")
    path, m = statemanifest.refresh("test", log, state=root)
    return root, m


def drift_check(root):
    return next(c for c in statemanifest.doctor_checks(root) if c[0] == "state manifest matches the files on disk")


def test_manifest_records_the_install_and_validates(state):
    root, m = state
    assert statemanifest.manifest_path(root).exists()
    assert statemanifest.schema_problems(statemanifest.read(root)) == []
    assert m["schema"] == "kicad-route/state/1" and m["pinned"] is False
    assert m["krt"]["version"] == "9.9.9"
    assert m["krt"]["extension_module"] == {"path": ".venv-krt/lib/python3.12/site-packages/grid_router.so",
                                            "sha256": common.sha256(root / ".venv-krt/lib/python3.12/site-packages/grid_router.so")}
    assert m["krt"]["prebuilt"]["from_source"] is True
    assert m["krt"]["venv"]["packages"] == ["numpy==2.5.4"] and m["krt"]["venv"]["python_version"] == "3.12.3"
    assert m["freerouting"]["sha256_matches_release"] is True and m["freerouting"]["java_required"] == 21
    assert m["freerouting"]["build_revision"] == "abc123"
    assert m["kicad"]["kicad_cli"]["version"] == common.kicad_version()
    assert statemanifest.verify(m, root) == []
    checks = {c[0]: c for c in statemanifest.doctor_checks(root)}
    assert checks["state manifest"][1] == common.PASS
    assert checks["state manifest matches the files on disk"][1:4:2] == (common.PASS, True)
    record("evidence_manifest_unit_manifest", {"manifest": m, "checks": [list(c) for c in checks.values()]})


@pytest.mark.parametrize("change", ["jar", "module", "venv", "source"])
def test_drift_is_a_required_doctor_fail(state, change):
    root, _ = state
    if change == "jar":
        jar = root / "vendor/freerouting/current/freerouting-9.9.9.jar"
        jar.write_bytes(jar.read_bytes() + b"tampered")
        item = "freerouting.jar.sha256"
    elif change == "module":
        (root / ".venv-krt/lib/python3.12/site-packages/grid_router.so").write_bytes(b"\x7fELF module v2")
        item = "krt.extension_module.sha256"
    elif change == "source":
        (engine.ROOT / "VERSION").write_text("changed source\n")
        item = "krt.source_sha256"
    else:
        site = root / ".venv-krt/lib/python3.12/site-packages/scipy-1.0.dist-info"
        site.mkdir()
        (site / "METADATA").write_text("Name: scipy\nVersion: 1.0\n")
        item = "krt.venv.packages_sha256"
    name, status, detail, required = drift_check(root)
    assert status == common.FAIL and required is True and item in detail
    assert [d["item"] for d in statemanifest.verify(statemanifest.read(root), root)] == [item]


def test_reading_the_manifest_needs_no_write_access(state):
    root, _ = state
    before = sorted(str(p) for p in root.rglob("*"))
    dirs = [root, *[p for p in root.rglob("*") if p.is_dir() and not p.is_symlink()]]
    for d in dirs:
        d.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        checks = statemanifest.doctor_checks(root)
        assert drift_check(root)[1] == common.PASS
        assert statemanifest.reproducibility("krt", root)["reproducible"] is None
        assert checks[0][1] == common.PASS
    finally:
        for d in dirs:
            d.chmod(stat.S_IRWXU)
    assert sorted(str(p) for p in root.rglob("*")) == before


def test_owned_manifest_does_not_require_wrapper_git_metadata(state, tmp_path, monkeypatch):
    root, manifest = state
    unversioned = tmp_path / "unversioned-tool"
    unversioned.mkdir()
    monkeypatch.setattr(common, "TOOL_ROOT", unversioned)
    assert statemanifest.verify(manifest, root) == []


def test_missing_manifest_is_reported_not_required(tmp_path):
    (name, status, detail, required), = statemanifest.doctor_checks(tmp_path)
    assert status == common.NOT_RUN and required is False and "state-manifest" in detail


def test_prebuilt_asset_from_build_router_log():
    assert statemanifest.prebuilt_from_log(None) is None
    src = statemanifest.prebuilt_from_log("Building from source with cargo build --release\n")
    assert src["url"] is None and src["from_source"] is True


# --------------------------------------------------- reproducible / measurement

def test_freerouting_results_are_attempts_not_measurements(state):
    root, _ = state
    r = statemanifest.reproducibility("freerouting", root)
    assert r["reproducible"] is False and r["measurement"] is False


def test_krt_reproducible_follows_the_record_for_the_build_on_disk(state):
    root, m = state
    assert statemanifest.reproducibility("krt", root)["reproducible"] is None
    rec = {"backend": "krt", "build_id": statemanifest.current_build_id(root, "krt"), "reproducible": True,
           "verified_utc": "2026-10-17T00:00:00+00:00"}
    statemanifest.record_determinism(rec, root)
    r = statemanifest.reproducibility("krt", root)
    assert r["reproducible"] is True and r["measurement"] is True
    # a re-written manifest keeps the record while the build is unchanged
    statemanifest.refresh("test", None, state=root)
    assert statemanifest.read(root)["determinism"]["krt"]["reproducible"] is True
    # another extension module is another build: the record no longer applies
    (root / ".venv-krt/lib/python3.12/site-packages/grid_router.so").write_bytes(b"\x7fELF module v2")
    r = statemanifest.reproducibility("krt", root)
    assert r["reproducible"] is None and r["measurement"] is False and "another builtin build" in r["basis"]
    statemanifest.refresh("test", None, state=root)
    assert "krt" not in statemanifest.read(root)["determinism"]


def layers(**over):
    base = {"run_dir": "r", "exit_code": 0, "outcome": {"process": "PASS", "routing": "COMPLETE",
                                                        "validation": "PASS", "unconnected_in_scope": 0},
            "wall_clock_stop": False, "backend_output_bytes": "b1", "candidate_bytes": "c1",
            "copper_geometry": "g1", "copper_items": {"tracks": 1, "vias": 0}}
    return {**base, **over}


def test_determinism_compare_judges_geometry_not_bytes():
    same = determinism.compare([layers(), layers(candidate_bytes="c2", backend_output_bytes="b2")])
    assert same["reproducible"] is True and same["same"]["candidate_bytes"] is False and same["bytes_note"]
    assert same["differs_at"] == "candidate_bytes"
    diff = determinism.compare([layers(), layers(copper_geometry="g2")])
    assert diff["reproducible"] is False and diff["differs_at"] == "copper_geometry"
    assert determinism.compare([layers(), layers(wall_clock_stop=True)])["reproducible"] is False
    assert determinism.compare([layers(), layers(copper_geometry=None)])["reproducible"] is False
    assert determinism.compare([layers()])["reproducible"] is False


def test_ledger_says_a_freerouting_repeat_may_differ(tmp_path, monkeypatch):
    monkeypatch.delenv("KICAD_ROUTE_LEDGER", raising=False)
    path = tmp_path / "ledger.jsonl"
    settings = {"budget_s": 10}
    for backend, phrase in (("freerouting", "may differ"), ("krt", "changes nothing")):
        version = {"version": "1"}
        fp = ledger.fingerprint("in", backend, version, settings)
        with open(path, "a") as f:
            f.write(json.dumps({"fingerprint": fp, "outcome": "incomplete", "run_dir": f"/r/{backend}",
                                "exit_code": 2}) + "\n")
        gate = ledger.check(path, "in", backend, version, settings)
        assert gate["refuse"] and phrase in gate["message"] and "--force" in gate["message"]


# ------------------------------------------------------- the fixture, twice

def test_krt_repeat_is_deterministic_and_results_carry_evidence():
    """`kicad-route determinism`: two KRT runs on the fixture (ledger off) give the same
    copper geometry; both results carry evidence and tool_identity that match the files."""
    r = cli("determinism", "--backend", "krt", "--runs", "2", "--budget", "180", timeout=1800)
    path = next(l.split("] ", 1)[1] for l in r.stdout.splitlines()
                if l.startswith("[determinism] ") and l.rstrip().endswith("determinism.json"))
    rec = json.loads((OUT / path).read_text())
    results = [json.loads((OUT / run["run_dir"] / "result.json").read_text()) for run in rec["runs"]]
    record("evidence_manifest_determinism", {k: rec[k] for k in ("reproducible", "reasons", "same", "differs_at",
                                                                 "runs", "build")})
    assert r.returncode == 0 and rec["reproducible"] is True, (rec["reasons"], r.stdout[-2000:])
    assert rec["same"]["copper_geometry"] and rec["same"]["outcome"]

    a, b = results
    for res in results:
        run_dir = OUT / res["run_dir"]
        ev = res["evidence"]
        ref = run_dir / "reference" / "route_demo"
        assert ev["reference_files"] == {str(p.relative_to(ref)): common.sha256(p)
                                         for p in sorted(ref.rglob("*")) if p.is_file() and not p.is_symlink()}
        assert ev["input_board_sha256"] == common.sha256(FIXTURE / "route_demo.kicad_pcb")
        assert set(ev["rules_files"]) == {"route_demo.kicad_pro", "route_demo.kicad_dru"}
        assert ev["candidate_board_sha256"] == common.sha256(OUT / res["candidate"]["board"]) \
            == res["validation"]["candidate_board_sha256"]
        for name, digest in ev["records"].items():
            assert common.sha256(run_dir / name) == digest, name
        facts = json.loads((run_dir / "validate" / "candidate-facts.json").read_text())
        assert ev["candidate_copper_signature"] == evidence.copper_signature(facts)
        ti = res["tool_identity"]
        assert ti["kicad_cli"]["version"] == common.kicad_version()
        assert ti["backend"]["extension_module_sha256"] and ti["backend"]["commit"] == res["backend"]["commit"]
        assert ti["state_manifest_sha256"] == statemanifest.file_sha256() or ti["state_manifest_sha256"] is None
        # backend.reproducible says what the state manifest says for this build
        want = statemanifest.reproducibility("krt")
        assert res["backend"]["reproducible"] == want["reproducible"]
        assert res["measurement"] == want["measurement"]
    # same input, same build: identical input hashes and tool identity
    for key in ("reference_files", "input_board_sha256", "rules_files", "rule_floor_sha256", "input_fingerprint"):
        assert a["evidence"][key] == b["evidence"][key], key
    assert a["tool_identity"] == b["tool_identity"]
    assert a["evidence"]["candidate_copper_signature"] == b["evidence"]["candidate_copper_signature"]

    # a forced difference (one track moved by 1 nm) is caught
    facts = json.loads((OUT / a["run_dir"] / "validate" / "candidate-facts.json").read_text())
    moved = copy.deepcopy(facts)
    moved["tracks"][0]["end"] = [moved["tracks"][0]["end"][0] + 1, moved["tracks"][0]["end"][1]]
    forced = determinism.layer_record(a)
    forced["copper_geometry"] = evidence.copper_signature(moved)["sha256"]
    verdict = determinism.compare([determinism.layer_record(a), forced])
    assert verdict["reproducible"] is False and verdict["differs_at"] == "copper_geometry"
