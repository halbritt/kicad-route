"""Net names holding `"` through the Specctra bridge to Freerouting (KICADROUTE-56).

KiCad's DSN exporter writes `"` as `''` in (net ...) definitions but raw in
(plane ...) entries, class member lists and the (net ...) of fixed wires and
vias. Freerouting 2.5.0 cannot read that DSN (`Shape.read_area_scope: could not
read shape`, then a NullPointerException on planeInfo.area). The bridge respells
those references as the definitions and leaves every other byte alone; a name it
cannot write one way is refused (exit 4) before Freerouting starts.
Shared harness: helpers.py and conftest.py.
"""
import json
import shutil
import subprocess

import pytest

from helpers import FIXTURE, OUT, cli, record, run, summary
from kicad_route import canary, common
from kicad_route import cli as route
from kicad_route.kicad import specctra

SCRIPTS = route.KICAD_SCRIPTS
# the zone's net, an in-scope signal that needs copper, and a net with locked wires/vias
# whose name KiCad quotes (space): its raw class/wire token is `"W "C""`
QUOTED = {"GND": 'GND"A', "/SIG_A": 'SIG"B', "/SIG_B": 'W "C"'}
# as tests/test_routing.py's smoke-freerouting attempt (exit 0 on the plain fixture)
FREEROUTING_CLEAN = ("--clearance-margin", "0.01", "--passes", "100", "--backend-arg=--router.fanout.enabled=true")


def make_board(dest, names, power=None):
    """A copy of the fixture with `names` renamed through KiCad (the fixture is never
    edited); `power` replaces the Power class patterns so renamed nets stay in it."""
    proj = dest / FIXTURE.name
    shutil.copytree(FIXTURE, proj, ignore=shutil.ignore_patterns("generate.py", "__pycache__"))
    board = proj / "route_demo.kicad_pcb"
    (dest / "rename.json").write_text(json.dumps(names))
    r = subprocess.run([str(common.PCBNEW_PYTHON), str(SCRIPTS / "rename_nets.py"), str(board),
                        str(dest / "rename.json")], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    if power is not None:
        pro = proj / "route_demo.kicad_pro"
        data = json.loads(pro.read_text())
        data["net_settings"]["netclass_patterns"] = [{"netclass": "Power", "pattern": n} for n in power]
        pro.write_text(json.dumps(data, indent=2))
    return proj, board


def bridge(*args):
    r = subprocess.run([str(common.PCBNEW_PYTHON), str(SCRIPTS / "specctra.py"), *map(str, args)],
                       capture_output=True, text=True, timeout=300)
    return r, route.tagged_json(r.stdout, "BRIDGE_JSON"), route.tagged_json(r.stdout, "BRIDGE_REFUSAL")


@pytest.fixture(scope="module")
def quoted_board(tmp_path_factory):
    return make_board(tmp_path_factory.mktemp("quoted"), QUOTED, power=["/VIN", 'GND"A'])


# ------------------------------------------------------------ pure Python

def dsn(planes, classes, wiring, nets):
    defs = "\n".join(f"    (net {specctra.dsn_net_token(n)}\n      (pins P{i}-1 P{i}-2)\n    )"
                     for i, n in enumerate(nets))
    return ("(pcb t.dsn\n  (parser\n    (string_quote \")\n    (space_in_quoted_tokens on)\n  )\n"
            f"  (structure\n{planes}\n  )\n  (network\n{defs}\n{classes}\n  )\n  (wiring\n{wiring}\n  )\n)\n")


def test_references_are_respelled_and_nothing_else():
    gnd, xyz, plain, q, wc = nets = ['GND"A', 'x y"(z)', "/plain", '"q"', 'W "C"']
    tok = [specctra.kicad_token(n) for n in nets]   # KiCad's raw spelling
    raw = dsn(f"    (plane {tok[0]} (polygon B.Cu 0  1 2  3 4))\n"
              f"    (plane {tok[1]} (polygon F.Cu 0  1 2  3 4))\n"
              "    (plane /plain (polygon F.Cu 0  5 6  7 8))",
              "    (class kicad_default " + " ".join(tok) + "\n      (circuit\n        (use_via V)\n"
              "      )\n      (rule\n        (width 200)\n        (clearance 200)\n      )\n    )",
              f"    (wire (path F.Cu 200  1 2  3 4)(net {tok[4]})(type fix))\n"
              f"    (via \"Via[0-1]_600:300_um\"  1 2 (net {tok[3]})(type fix))\n"
              "    (wire (path F.Cu 200  1 2  3 4)(net /plain)(type fix))", nets)
    out, report = specctra.definition_spelling(raw)
    expect = raw
    for old, new in [("(plane GND\"A ", "(plane GND''A "),
                     ("(plane \"x y\"(z)\" ", "(plane \"x y''(z)\" "),
                     ('(class kicad_default GND"A "x y"(z)" /plain "q" "W "C""\n',
                      "(class kicad_default GND''A \"x y''(z)\" /plain ''q'' \"W ''C''\"\n"),
                     ('(net "W "C"")(type', "(net \"W ''C''\")(type"),
                     ('(net "q")(type', "(net ''q'')(type")]:
        assert expect.count(old) == 1, old
        expect = expect.replace(old, new)
    assert out == expect                 # every other byte as KiCad wrote it
    assert {r["net"]: (r["planes"], r["classes"], r["wiring"]) for r in report} == {
        gnd: (1, 1, 0), xyz: (1, 1, 0), q: (0, 1, 1), wc: (0, 1, 1)}
    assert plain not in {r["net"] for r in report}
    assert specctra.definition_spelling(out) == (out, [])   # idempotent
    assert not canary.reference_problems(out)
    assert canary.reference_problems(raw)  # the canary's oracle sees KiCad's raw spelling


def test_split_then_respell(tmp_path):
    """--skip-nets reads kept members as KiCad wrote them, then everything is respelled."""
    nets = ['GND"A', 'SIG"B', "/S"]
    raw = dsn("    (plane GND\"A (polygon B.Cu 0  1 2  3 4))",
              "    (class kicad_default GND\"A SIG\"B /S\n      (circuit (use_via V))\n      (rule (width 200)))",
              "", nets)
    (tmp_path / "skip.json").write_text(json.dumps(nets[2:]))
    out = specctra.rewrite_dsn(raw, {"--skip-nets": str(tmp_path / "skip.json")}, info := {})
    assert "(class kicad_default GND''A SIG''B\n" in out and '(class "kicad_default__skip" /S\n' in out
    assert "(plane GND''A (polygon" in out and not canary.reference_problems(out)
    assert info["ignored_classes"] == ["kicad_default__skip"]
    assert [r["net"] for r in info["net_names_respelled"]] == ['GND"A', 'SIG"B']


def test_names_that_cannot_be_made_consistent_raise():
    # two nets KiCad writes the same: Freerouting would merge them
    with pytest.raises(specctra.NetNameError, match="more than one net"):
        specctra.definition_spelling(dsn("", "", "", ['a"b', "a''b"]))
    # a plane whose raw name reads two ways, both defined nets: `a b` and `a b" (polygon F.Cu`
    two = dsn('    (plane "a b" (polygon F.Cu" (polygon F.Cu 0  1 2))', "", "", ["a b", 'a b" (polygon F.Cu'])
    with pytest.raises(specctra.NetNameError, match="reads as"):
        specctra.definition_spelling(two)
    # a raw `"` name with no reading KiCad could have written
    with pytest.raises(specctra.NetNameError, match="unreadable"):
        specctra.definition_spelling(dsn("", "", '    (wire (path F.Cu 200  1 2)(net a"b c)(type fix))', ['a"b']))
    # the class reader's ambiguity is the same refusal (exit 4, not exit 1)
    amb = dsn("", '    (class kicad_default "a b" c"\n      (circuit (use_via V))\n      (rule (width 200)))',
              "", ['a b', 'c"', 'a b" c'])
    with pytest.raises(specctra.NetNameError, match="ambiguous"):
        specctra.definition_spelling(amb)


# ------------------------------------------------------- KiCad 10 export

def test_kicad_export_is_respelled(quoted_board, tmp_path):
    """KiCad's own DSN of a board with a zone on GND"A, an in-scope SIG"B and locked
    copper on `W "C"`: the bridge's export equals KiCad's with exactly the plane,
    class and wiring references respelled."""
    _, board = quoted_board
    path = tmp_path / "board.dsn"   # one path, so the (pcb "<path>") header matches
    r, info, refusal = bridge("check", board, path)
    assert r.returncode == 0 and refusal is None, r.stdout[-2000:] + r.stderr[-2000:]
    raw = path.read_text()
    assert '(plane GND"A (polygon' in raw and "(net GND''A\n" in raw     # KiCad's inconsistency
    r, info, refusal = bridge("export", board, path)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    out = path.read_text()
    line = next(l.strip() for l in raw.splitlines() if l.strip().startswith("(class kicad_default"))
    assert line == '(class kicad_default /SIG_A_OUT /SIG_B_OUT SIG"B "W "C""', line
    expect = raw.replace('(plane GND"A (polygon', "(plane GND''A (polygon")
    expect = expect.replace(line, "(class kicad_default /SIG_A_OUT /SIG_B_OUT SIG''B \"W ''C''\"")
    assert raw.count('(net "W "C"")') == 6           # the locked wires and vias of the fixture's /SIG_B
    expect = expect.replace('(net "W "C"")', "(net \"W ''C''\")")
    assert raw.count('(class Power /VIN GND"A\n') == 1
    expect = expect.replace('(class Power /VIN GND"A\n', "(class Power /VIN GND''A\n")
    assert out == expect
    assert not canary.reference_problems(out) and canary.reference_problems(raw)
    assert {x["net"]: (x["planes"], x["classes"], x["wiring"]) for x in info["net_names_respelled"]} == {
        'GND"A': (1, 1, 0), 'SIG"B': (0, 1, 0), 'W "C"': (0, 1, 6)}
    record("dsn_quoted_nets_export", {"raw_class_line": line, "respelled": info["net_names_respelled"]})


def test_freerouting_round_trip(quoted_board):
    """Freerouting reads the respelled DSN (no NPE), routes SIG"B, and KiCad's SES
    import maps the session's `''` names back to the board's own nets."""
    proj, _ = quoted_board
    r, run_dir, result = run("run", "--backend", "freerouting", "--dir", str(proj), "--budget", "180",
                             *FREEROUTING_CLEAN, "--label", "dsn-quoted-freerouting", timeout=900)
    record("dsn_quoted_nets_freerouting", summary(result))
    log = (run_dir / "work" / "freerouting.log").read_text(errors="replace")
    assert "NullPointerException" not in log and "could not read shape" not in log
    assert result["process"]["status"] == route.PASS, result["process"]["reasons"]
    respelled = {x["net"] for x in result["process"]["bridge"]["export"]["net_names_respelled"]}
    assert respelled == set(QUOTED.values())
    cand = json.loads((run_dir / "validate" / "candidate-facts.json").read_text())
    ref = json.loads((run_dir / "facts" / "reference-facts.json").read_text())
    assert set(cand["nets"]) == set(ref["nets"]) and set(QUOTED.values()) <= set(cand["nets"])
    assert any(t["net"] == 'SIG"B' for t in cand["tracks"]), "no copper on SIG\"B"
    assert r.returncode == 0 and result["exit_code"] == 0, result["validation"]["reasons"]


# ---------------------------------------------------------------- refusals

def refused(proj, label):
    r = cli("run", "--json", "--backend", "freerouting", "--dir", str(proj), "--budget", "60", "--label", label,
            timeout=600)
    assert r.returncode == 4, (r.stdout[-2000:], r.stderr[-2000:])
    rec = json.loads(r.stdout)
    assert rec["kind"] == "refusal" and rec["reason_class"] == "input" and rec["exit_code"] == 4
    assert (OUT / rec["record"]).exists()                      # the refusal record
    assert not (OUT / rec["discarded_run_dir"]).exists()       # nothing left: no backend ran
    assert not list((OUT / "runs" / FIXTURE.name / label).rglob("freerouting.log"))
    return rec


def test_colliding_names_are_refused(tmp_path):
    """`a"b` and `a''b` are both `a''b` in the DSN: refused from the facts, naming both."""
    proj, _ = make_board(tmp_path, {"/SIG_A": 'a"b', "/SIG_A_OUT": "a''b"})
    rec = refused(proj, "dsn-quoted-collision")
    assert "'a\"b'" in rec["message"] and "\"a''b\"" in rec["message"], rec["message"]
    record("dsn_quoted_nets_collision_refusal", rec)


def test_unreadable_names_are_refused(tmp_path):
    """KiCad writes `a b`, `c"` and `a b" c` in one class line that reads two ways:
    the bridge's check refuses before Freerouting starts (was an exit 1)."""
    proj, _ = make_board(tmp_path, {"/SIG_A": "a b", "/SIG_A_OUT": 'c"', "/SIG_B": 'a b" c'})
    rec = refused(proj, "dsn-quoted-ambiguous")
    assert "Specctra bridge" in rec["message"] and "ambiguous" in rec["message"], rec["message"]
    record("dsn_quoted_nets_ambiguous_refusal", rec)
