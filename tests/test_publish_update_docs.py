"""Unit tests (no routing, no network, no backend update) for: publish copying every
structured receipt and never a board (KICADROUTE-27), the KRT unsupported-rule log
parser (39), scripts/install.sh link handling (45), and the update helpers' pure
parts: GitHub token headers (46), jar asset selection (47), asset digest checks
(48), and the KRT venv interpreter (49). Also the exit-code table across the docs (38)."""
import os
import re
import subprocess
import sys

import pytest

from helpers import ROOT
from kicad_route import cli as route


# ------------------------------------------------------------ 27: publish

def _run_dir(tmp_path):
    run = tmp_path / "proj" / "label" / "20261017T000000.000000Z"
    files = {
        "result.json": "{}", "RESULT.md": "# result", "attempt.json": "{}", "snapshot.json": "{}",
        "spokes.json": '{"kind": "spokes"}', "provenance.json": "{}",
        "validate/preservation.json": "[]", "validate/new-findings.json": "{}",
        "validate/candidate-drc.json": "{}", "validate/candidate-facts.json": "{}",
        "validate/candidate-pour.json": "{}", "validate/candidate-refill.json": "{}",
        "validate/pour-integrity.json": "{}", "validate/blocked-by.json": "{}",
        "facts/reference-facts.json": "{}", "facts/human-keys.json": "{}",
        "spokes/reference-plan.json": "{}",
        "revalidate-origin/20261017T000001.000000Z/result.json": '{"old": 1}',
        "revalidate-origin/20261017T000002.000000Z/result.json": '{"new": 1}',
        "revalidate-origin/20261017T000002.000000Z/blocked-by.json": "{}",
        "revalidate/20261017T000003.000000Z/pour-integrity.json": "{}",
        # never published: boards, project files, logs, the backend's work tree
        "candidate/proj/proj.kicad_pcb": "(kicad_pcb)", "candidate/proj/proj.kicad_pro": "{}",
        "reference/proj/proj.kicad_pcb": "(kicad_pcb)", "validate/candidate-drc.log": "log",
        "facts/reference-facts.log": "log", "spokes/plan.log": "log",
        "work/krt-out/summary.json": "{}", "work/input/proj/proj.kicad_pcb": "(kicad_pcb)",
        "revalidate/20261017T000003.000000Z/candidate-drc.log": "log",
    }
    for name, text in files.items():
        (run / name).parent.mkdir(parents=True, exist_ok=True)
        (run / name).write_text(text)
    return run


def test_publish_copies_every_receipt_and_never_a_board(tmp_path):
    run = _run_dir(tmp_path)
    dest = tmp_path / "dest"
    route.publish_run(run, dest)
    got = sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file())
    assert got == sorted([
        "RESULT.md", "attempt.json", "provenance.json", "result.json", "snapshot.json", "spokes.json",
        "blocked-by.json", "candidate-drc.json", "candidate-facts.json", "candidate-pour.json",
        "candidate-refill.json", "new-findings.json", "pour-integrity.json", "preservation.json",
        "facts/human-keys.json", "facts/reference-facts.json", "spokes/reference-plan.json",
        "against-origin/blocked-by.json", "against-origin/result.json",
        "revalidate/pour-integrity.json"])
    assert (dest / "against-origin" / "result.json").read_text() == '{"new": 1}'  # the latest stamp
    assert not any(p.suffix in (".kicad_pcb", ".kicad_pro", ".log") for p in dest.rglob("*"))


def test_publish_never_lets_validate_overwrite_a_root_receipt(tmp_path):
    run = _run_dir(tmp_path)
    (run / "validate" / "result.json").write_text('{"validate": 1}')
    dest = tmp_path / "dest"
    route.publish_run(run, dest)
    assert (dest / "result.json").read_text() == "{}"
    assert (dest / "validate" / "result.json").read_text() == '{"validate": 1}'


def test_publish_skips_symlinked_receipts(tmp_path):
    run = _run_dir(tmp_path)
    secret = tmp_path / "elsewhere.json"
    secret.write_text('{"secret": 1}')
    (run / "validate" / "linked.json").symlink_to(secret)
    dest = tmp_path / "dest"
    route.publish_run(run, dest)
    assert not (dest / "linked.json").exists()


# ------------------------------------------------- 39: KRT skipped-rule parser

KRT_LOG = """\
Loading board...
  .kicad_dru: rule 'power_to_signal_clearance': clearance 0.4mm has a non-layer scope (netclass/area/... condition) -- skipped, KiCad will still enforce it
  .kicad_dru: rule 'power_to_signal_clearance': clearance 0.4mm has a non-layer scope (netclass/area/... condition) -- skipped, KiCad will still enforce it
  .kicad_dru: rule 'quiet': severity ignore, skipped
  .kicad_dru rule 'hv_gap': NOT honoured (condition: unsupported function); KiCad will still enforce it
  .kicad_dru rule 'len': parsed, but length has no consumer in this tool yet
  rule 'odd': unknown constraint 'foo' -- kept in the table, not evaluated
  .kicad_dru: rule 'trk': track-to-track clearance 0.3mm for class 'Power' -- handled by the track channel
Per-layer clearance rules (.kicad_dru, #498): F.Cu:0.2
  .kicad_dru: fanout clearance floored 0.1 -> 0.15
  (design_rules power reconcile skipped: no power nets)
Routing 12 nets: skipped 0
"""


def test_krt_unsupported_rules_matches_every_not_applied_wording():
    got = route.krt_unsupported_rules(KRT_LOG)
    names = [re.search(r"rule '([^']+)'", g)[1] for g in got]
    assert sorted(names) == ["hv_gap", "len", "odd", "power_to_signal_clearance", "quiet"]
    # the long-standing wording comes through verbatim (tests/test_routing.py relies on it)
    assert any("power_to_signal_clearance" in g and "skipped" in g for g in got)
    assert got == sorted(set(got))


def test_krt_unsupported_rules_tolerates_wording_drift():
    for line in ["  .kicad_dru: rule 'r1' skipping (not modelled)",
                 "  .KICAD_DRU: Rule \"r1\": not applied by the router",
                 "  .kicad_dru: rule `r1` unsupported condition",
                 "  .kicad_dru: rule 'r1' -- skipped"]:
        assert route.krt_unsupported_rules(line) == [line.strip()], line
    assert route.krt_unsupported_rules("") == []


# ---------------------------------------------------------- 45: install.sh

def _install(home):
    env = {"HOME": str(home), "PATH": os.environ["PATH"], "CODEX_HOME": str(home / ".codex")}
    return subprocess.run(["bash", str(ROOT / "scripts" / "install.sh"), "--no-update"],
                          capture_output=True, text=True, env=env, timeout=60)


LINKS = (".local/bin/kicad-route", ".agents/skills/kicad-route", ".codex/skills/kicad-route",
         ".claude/skills/kicad-route")


def test_install_creates_then_keeps_links(tmp_path):
    r = _install(tmp_path)
    assert r.returncode == 0, r.stderr
    for rel in LINKS:
        assert (tmp_path / rel).is_symlink()
    assert os.path.realpath(tmp_path / LINKS[0]) == os.path.realpath(ROOT / "bin" / "kicad-route")
    r = _install(tmp_path)
    assert r.returncode == 0 and "Repointing" not in r.stdout, r.stdout + r.stderr


def test_install_repoints_stale_and_dangling_links(tmp_path):
    old = tmp_path / "old-checkout" / "skills" / "kicad-route"
    old.mkdir(parents=True)
    (old / "keep.txt").write_text("old")
    (tmp_path / ".claude" / "skills").mkdir(parents=True)
    (tmp_path / ".claude" / "skills" / "kicad-route").symlink_to(old)  # stale, to a directory
    (tmp_path / ".local" / "bin").mkdir(parents=True)
    (tmp_path / ".local" / "bin" / "kicad-route").symlink_to(tmp_path / "gone")  # dangling
    r = _install(tmp_path)
    assert r.returncode == 0, r.stderr
    assert os.path.realpath(tmp_path / ".claude/skills/kicad-route") == os.path.realpath(ROOT / "skills" / "kicad-route")
    assert os.path.realpath(tmp_path / ".local/bin/kicad-route") == os.path.realpath(ROOT / "bin" / "kicad-route")
    assert sorted(p.name for p in old.iterdir()) == ["keep.txt"]  # ln -n: no link created inside the old target


@pytest.mark.parametrize("kind", ["file", "dir"])
def test_install_refuses_real_files_and_directories(tmp_path, kind):
    dest = tmp_path / ".agents" / "skills" / "kicad-route"
    dest.parent.mkdir(parents=True)
    if kind == "file":
        dest.write_text("mine")
    else:
        dest.mkdir()
        (dest / "SKILL.md").write_text("mine")
    r = _install(tmp_path)
    assert r.returncode == 1 and "Refusing to replace existing path" in r.stderr
    assert not dest.is_symlink()
    assert (dest.read_text() if kind == "file" else (dest / "SKILL.md").read_text()) == "mine"


# ------------------------------------------------------ 46: GitHub token header

def test_github_headers_without_token():
    h = route.github_headers({})
    assert "Authorization" not in h and h["User-Agent"] == "kicad-route"


def test_github_headers_prefers_github_token_then_gh_token():
    assert route.github_headers({"GITHUB_TOKEN": "a", "GH_TOKEN": "b"})["Authorization"] == "Bearer a"
    assert route.github_headers({"GH_TOKEN": "b"})["Authorization"] == "Bearer b"
    assert route.github_headers({"GITHUB_TOKEN": "", "GH_TOKEN": " b \n"})["Authorization"] == "Bearer b"
    assert "Authorization" not in route.github_headers({"GITHUB_TOKEN": "  "})


# ------------------------------------------------------ 47: jar asset selection

def _assets(*names):
    return [{"name": n, "browser_download_url": f"https://example.invalid/{n}"} for n in names]


def test_jar_asset_accepts_stable_and_prerelease_names():
    for name in ["freerouting-2.5.0.jar", "freerouting-2.5.0-alpha.jar", "freerouting-3.0.0-rc1.jar",
                 "freerouting-3.0.0-beta.2.jar", "freerouting-2.1.0-SNAPSHOT.jar", "freerouting-3.0.0-m1.jar"]:
        assert route.freerouting_jar_asset(_assets(name))["name"] == name, name


def test_jar_asset_rejects_non_executable_jars_and_other_assets():
    assert route.freerouting_jar_asset(_assets(
        "freerouting-2.5.0-sources.jar", "freerouting-2.5.0-javadoc.jar", "freerouting-2.5.0-linux-x64.zip",
        "freerouting-cli-2.5.0-checksums.txt", "freerouting-2.5.0-macos.jar", "freerouting.jar")) is None


def test_jar_asset_prefers_stable_over_prerelease():
    got = route.freerouting_jar_asset(_assets("freerouting-2.6.0-rc1.jar", "freerouting-2.5.0-sources.jar",
                                              "freerouting-2.6.0.jar", "freerouting-2.6.0-alpha.jar"))
    assert got["name"] == "freerouting-2.6.0.jar"
    got = route.freerouting_jar_asset(_assets("freerouting-2.6.0-rc1.jar", "freerouting-2.6.0-rc2.jar"))
    assert got["name"] == "freerouting-2.6.0-rc1.jar"  # release order kept among pre-releases


def test_jar_asset_on_the_live_v2_5_0_shape():
    names = ["freerouting-2.5.0-linux-x64.zip", "freerouting-2.5.0-macos-arm64.dmg", "freerouting-2.5.0-macos-x64.dmg",
             "freerouting-2.5.0-windows-x64.msi", "freerouting-2.5.0-x86_64.AppImage",
             "freerouting-2.5.0-x86_64.AppImage.zsync", "freerouting-2.5.0.jar", "freerouting-cli-2.5.0-checksums.txt",
             "freerouting-cli-2.5.0-linux-x64.tar.gz"]
    assert route.freerouting_jar_asset(_assets(*names))["name"] == "freerouting-2.5.0.jar"


# ------------------------------------------------------ 48: asset digest check

HEX = "f6f51bb02245e8e717f9359bd260cc9c5c0b1bc0acc8b7cb2cd5b8ffeb5de3c7"


def test_asset_digest_verifies_github_sha256():
    assert route.check_asset_digest({"name": "j.jar", "digest": f"sha256:{HEX}"}, HEX) is True
    assert route.check_asset_digest({"name": "j.jar", "digest": f"sha256:{HEX.upper()}"}, HEX) is True


def test_asset_digest_mismatch_raises():
    with pytest.raises(RuntimeError, match="does not match"):
        route.check_asset_digest({"name": "j.jar", "digest": f"sha256:{'0' * 64}"}, HEX)


def test_asset_digest_unknown_algorithm_raises():
    with pytest.raises(RuntimeError, match="unrecognised"):
        route.check_asset_digest({"name": "j.jar", "digest": f"md5:{'0' * 32}"}, HEX)
    with pytest.raises(RuntimeError, match="unrecognised"):
        route.check_asset_digest({"name": "j.jar", "digest": HEX}, HEX)


def test_asset_digest_absent_is_reported_unverified():
    assert route.check_asset_digest({"name": "j.jar"}, HEX) is False
    assert route.check_asset_digest({"name": "j.jar", "digest": None}, HEX) is False


# ------------------------------------------------------ 49: KRT venv interpreter

def test_krt_venv_python_defaults_to_the_running_interpreter():
    assert route.krt_venv_python({}) == sys.executable
    assert route.krt_venv_python({"KICAD_ROUTE_KRT_PYTHON": "/opt/py/bin/python3.13"}) == "/opt/py/bin/python3.13"


# ------------------------------------------------------ 38: exit codes in the docs

# each doc's exit-code statement, and what every code must say there (as cli.py's docstring)
EXIT_DOCS = {"README.md": "with exit codes", "docs/routing.md": "Exit status:", "skills/kicad-route/SKILL.md": "Exit codes:"}
EXIT_MEANINGS = {"0": r"complete and valid", "1": r"process\s+failure", "2": r"valid but\s+incomplete",
                 "3": r"fails\s+validation", "4": r"refused\s+before\s+routing"}


@pytest.mark.parametrize("doc", sorted(EXIT_DOCS))
def test_docs_document_every_exit_code(doc):
    text = (ROOT / doc).read_text()
    start = text.index(EXIT_DOCS[doc])
    para = text[start:text.find("\n\n", start)]
    for code, meaning in EXIT_MEANINGS.items():
        assert re.search(rf"(?<![\d.]){code}\W{{1,3}}{meaning}", para), f"{doc}: exit code {code} ({meaning}) missing"
    assert "canary" in para and "ledger" in para, f"{doc}: exit 4's two causes not named"
