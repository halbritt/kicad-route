"""Net names through the Specctra bridge and --nets (KICADROUTE-15, 16, 17).

KiCad's DSN writer never escapes: a class member list holds each net name raw,
quoted only when it needs quotes, while (net ...) definitions write `"` as `''`.
The class reader (specctra.read_class) must partition classes by exact name;
select_nets must let a name that starts with '!' be selected (`\\!RESET`) and
must not turn `--nets '!RESET'` into "every net but RESET" silently.
Shared harness: helpers.py and conftest.py.
"""
import fnmatch
import itertools
import json
import re
import shutil
import subprocess

import pytest

from helpers import FIXTURE, OUT, cli, record
from kicad_route import canary, campaign, common, netnames
from kicad_route import cli as route
from kicad_route.kicad import specctra


def network(classes, nets):
    """A KiCad-style DSN: (net ...) definitions as KiCad writes them, then `classes`."""
    defs = "\n".join(f"    (net {specctra.dsn_net_token(n)}\n      (pins P{i}-1 P{i}-2)\n    )"
                     for i, n in enumerate(nets))
    return ("(pcb t.dsn\n  (parser\n    (string_quote \")\n    (space_in_quoted_tokens on)\n  )\n"
            f"  (network\n{defs}\n{classes}\n  )\n  (wiring\n  )\n)\n")


def kicad_class(name, members, width=200):
    """A class block exactly as KiCad 10 writes it (members raw, quoted when needed)."""
    return (f"    (class {name} {' '.join(specctra.kicad_token(m) for m in members)}\n"
            "      (circuit\n        (use_via \"Via[0-1]_600:300_um\")\n      )\n"
            f"      (rule\n        (width {width})\n        (clearance 200)\n      )\n    )")


def members(dsn):
    return {c.name: [m[0] for m in c.members] for c in specctra.read_network(dsn).classes}


# Net names whose quotes KiCad writes raw in class lists (KiCad 10 export of the fixture,
# renamed with kicad/rename_nets.py, gives exactly this class line).
QUOTE_NETS = ['"foo"', '"q r"', 'a"b', "foo", 'foo "(bar)"', 'x y"z']
QUOTE_CLASS_LINE = '(class kicad_default "foo" ""q r"" a"b foo "foo "(bar)"" "x y"z"'


# ------------------------------------------------------- KICADROUTE-15

def test_quoted_and_unquoted_names_do_not_collide():
    """The probe's DSN: skipping `foo` must leave the net `"foo"` routable, once."""
    dsn = """(pcb test.dsn
  (parser (string_quote ") (space_in_quoted_tokens on))
  (network
    (net ''foo'' (pins P1-1 P1-2))
    (net foo (pins P2-1 P2-2))
    (class kicad_default "foo" foo (circuit (use_via "Via1")) (rule (width 200)))
  )
)"""
    out, ignored = specctra.split_classes(dsn, {"foo"})
    assert ignored == ["kicad_default__skip"]
    assert members(out) == {"kicad_default": ['"foo"'], "kicad_default__skip": ["foo"]}
    assert "foo foo" not in out
    assert '(class kicad_default "foo"\n' in out and '(class "kicad_default__skip" foo\n' in out

    out, ignored = specctra.split_classes(dsn, {'"foo"'})
    assert members(out) == {"kicad_default": ["foo"], "kicad_default__skip": ["''foo''"]}
    assert "(class kicad_default foo\n" in out and "(class \"kicad_default__skip\" ''foo''\n" in out


@pytest.mark.parametrize("skip", [{"foo"}, {'"foo"'}, {'a"b'}, {'foo "(bar)"', 'x y"z'}, {'"q r"', "foo"},
                                  set(QUOTE_NETS)])
def test_split_partitions_by_exact_name(skip):
    dsn = network(kicad_class("kicad_default", QUOTE_NETS), QUOTE_NETS)
    assert QUOTE_CLASS_LINE + "\n" in dsn
    out, ignored = specctra.split_classes(dsn, skip)
    got = members(out)
    kept = [n for n in QUOTE_NETS if n not in skip]
    # kept members keep KiCad's own token, in order
    if kept:
        assert f"(class kicad_default {' '.join(specctra.kicad_token(n) for n in kept)}\n" in out
        assert got["kicad_default"] == kept
    else:
        assert "kicad_default" not in got
    # moved members are written as their definitions, which the reader maps back to the same nets
    moved = [specctra.dsn_net_token(n) for n in QUOTE_NETS if n in skip]
    assert f"(class \"kicad_default__skip\" {' '.join(moved)}\n" in out
    assert [n.replace("''", '"') for n in got["kicad_default__skip"]] == [n for n in QUOTE_NETS if n in skip]
    assert ignored == ["kicad_default__skip"]
    assert out[specctra.read_network(out).end:].lstrip().startswith("(wiring")  # network closes in place


def test_split_untouched_class_and_power_class():
    nets = QUOTE_NETS + ["-VIN(1)", "GND 'x' (y)"]
    dsn = network(kicad_class("kicad_default", QUOTE_NETS) + "\n" + kicad_class("Power", nets[-2:], 500), nets)
    out, ignored = specctra.split_classes(dsn, {"-VIN(1)"})
    assert ignored == ["Power__skip"]
    assert QUOTE_CLASS_LINE + "\n" in out          # a class without skipped nets is byte-identical
    assert members(out)["Power"] == ["GND 'x' (y)"]
    assert members(out)["Power__skip"] == ["-VIN(1)"]
    assert "(width 500)" in out[out.index('(class "Power__skip"'):]


def test_wrapped_member_lines():
    """KiCad wraps long class lists between names (newline + indentation)."""
    nets = ["a" * 40, "b" * 47, "c" * 25 + " c", "d", "e" * 95, 'q"(1)"']
    dsn = network("    (class kicad_default " + " ".join(specctra.kicad_token(n) for n in nets[:2])
                  + "\n      " + " ".join(specctra.kicad_token(n) for n in nets[2:]) + "\n      (circuit\n"
                  "        (use_via V)\n      )\n      (rule\n        (width 200)\n      )\n    )", nets)
    assert members(dsn) == {"kicad_default": nets}
    out, _ = specctra.split_classes(dsn, {nets[2], nets[5]})
    assert members(out)["kicad_default"] == [nets[0], nets[1], nets[3], nets[4]]


def test_long_member_list_is_linear():
    """A big Default class on one line (worst case; KiCad wraps at ~72 columns)."""
    import time
    nets = [f"Net-(U{i}-Pad{i % 7})" if i % 3 == 0 else (f'q"{i}" x' if i % 5 == 0 else f"/S{i}")
            for i in range(3000)]
    dsn = network(kicad_class("kicad_default", nets), nets)
    t = time.monotonic()
    out, _ = specctra.split_classes(dsn, set(nets[::2]))
    assert time.monotonic() - t < 10
    assert members(out) == {"kicad_default": nets[1::2],
                            "kicad_default__skip": [n.replace('"', "''") for n in nets[::2]]}


def test_ambiguous_member_list_is_refused():
    """`"a b" c"` is one name `a b" c` or two (`a b`, `c"`); with all three defined there
    is no telling, and the bridge must refuse rather than guess (fail closed)."""
    dsn = network('    (class kicad_default "a b" c"\n      (circuit (use_via V))\n      (rule (width 200)))',
                  ['a b', 'c"', 'a b" c'])
    with pytest.raises(ValueError, match="ambiguous"):
        specctra.split_classes(dsn, {"a b"})
    # with only one reading defined, it is that reading
    dsn = network('    (class kicad_default "a b" c"\n      (circuit (use_via V))\n      (rule (width 200)))',
                  ['a b" c'])
    assert members(dsn) == {"kicad_default": ['a b" c']}


def test_kicad_quoting_rule():
    """Quoted iff empty, leading `#`, whitespace or one of (){}% (KiCad 10 exports)."""
    for name in ["", "#hash", "a b", "a\tb", "x(1)", "{x", "x}", "x%", "GND 'x' (y)"]:
        assert specctra.needs_quote(name), name
    for name in ["a#b", "a'b", "a,b", "a;b", "a\\b", "$y", "|", "~", "-x", "ü", '"', '""', "!RESET", 'a"b']:
        assert not specctra.needs_quote(name), name
    assert specctra.dsn_net_token('foo "(bar)"') == "\"foo ''(bar)''\""
    assert specctra.dsn_net_token('"q"') == "''q''"


# ------------------------------------------------------- KICADROUTE-16

def test_find_open_skips_parentheses_inside_quoted_names():
    block = '(class kicad_default "foo "(bar)"" /VIN (circuit (use_via "Via1")) (rule (width 200)))'
    assert specctra.find_open(block, 1) == block.find("(circuit") == 40
    assert specctra.find_open('(class X "Net-(BT1-+)" a\n  (circuit (use_via V)))') == 27
    assert specctra.find_open('(class X "Net-(BT1-+)" a)') == -1


def test_internal_quotes_and_parentheses_split_cleanly():
    nets = ['foo "(bar)"', "/VIN", 'x y"z']
    dsn = network(kicad_class("kicad_default", nets), nets)
    assert '(class kicad_default "foo "(bar)"" /VIN "x y"z"\n' in dsn
    out, ignored = specctra.split_classes(dsn, {'foo "(bar)"'})
    assert '(class kicad_default /VIN "x y"z"\n      (circuit' in out
    assert '(class "kicad_default__skip" "foo \'\'(bar)\'\'"\n      (circuit' in out
    assert members(out) == {"kicad_default": ["/VIN", 'x y"z'], "kicad_default__skip": ["foo ''(bar)''"]}
    # the rest of the export pipeline reads the split DSN too
    out2, raised = specctra.raise_default_clearance(out, 300)
    assert [r["where"] for r in raised] == ["class kicad_default"]
    out3, applied = specctra.add_class_clearances(out2, [("r", "Default", "*all", 0.3)])
    assert applied[0]["pairs"] == 4 and members(out3) == members(out)


def test_kicad_export_round_trip(tmp_path):
    """KiCad's own DSN export of the fixture with quote-bearing names, split by
    specctra.py --skip-nets: classes partition exactly (string oracle, no parser)."""
    proj = tmp_path / FIXTURE.name
    shutil.copytree(FIXTURE, proj, ignore=shutil.ignore_patterns("generate.py", "__pycache__"))
    board = proj / "route_demo.kicad_pcb"
    names = {"/SIG_A": 'foo "(bar)"', "/SIG_A_OUT": '"foo"', "/SIG_B": "foo", "/SIG_B_OUT": 'a"b',
             "/VIN": 'x y"z', "GND": '"q r"'}
    (tmp_path / "map.json").write_text(json.dumps(names))
    scripts = route.KICAD_SCRIPTS
    r = subprocess.run([str(common.PCBNEW_PYTHON), str(scripts / "rename_nets.py"), str(board),
                        str(tmp_path / "map.json")], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr[-2000:]
    raw = tmp_path / "raw.dsn"
    r = subprocess.run([str(common.PCBNEW_PYTHON), str(scripts / "specctra.py"), "export", str(board), str(raw)],
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr[-2000:]
    text = raw.read_text()
    line = next(l.strip() for l in text.splitlines() if l.strip().startswith("(class kicad_default"))
    # KiCad's member order is its own: find it by brute force over the spellings
    orders = [o for o in itertools.permutations(names.values())
              if line == "(class kicad_default " + " ".join(specctra.kicad_token(n) for n in o)]
    assert len(orders) == 1, line
    expect_order = orders[0]
    skip = ['foo "(bar)"', '"foo"', 'a"b']
    (tmp_path / "skip.json").write_text(json.dumps(skip))
    scoped = tmp_path / "scoped.dsn"
    r = subprocess.run([str(common.PCBNEW_PYTHON), str(scripts / "specctra.py"), "export", str(board), str(scoped),
                        "--skip-nets", str(tmp_path / "skip.json")], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr[-2000:]
    bridge = route.tagged_json(r.stdout, "BRIDGE_JSON")
    assert bridge["ignored_classes"] == ["kicad_default__skip"]
    out = scoped.read_text()
    kept = [n for n in expect_order if n not in skip]
    moved = [specctra.dsn_net_token(n) for n in expect_order if n in skip]
    assert "(class kicad_default " + " ".join(specctra.kicad_token(n) for n in kept) + "\n" in out
    assert '(class "kicad_default__skip" ' + " ".join(moved) + "\n" in out
    for tok in moved:  # each moved member is spelled exactly as its (net ...) definition
        assert re.search(r"\(net " + re.escape(tok) + r"\n", out), tok
    record("specctra_nets_export", {"raw_class_line": line, "skip": skip, "kept": kept, "moved": moved})


def test_unreadable_dsn_fails_the_export(tmp_path):
    """Fail closed: a class list the reader cannot read stops the export (exit != 0)
    instead of handing Freerouting a guessed partition."""
    dsn = network('    (class kicad_default "a b" c"\n      (circuit (use_via V))\n      (rule (width 200)))',
                  ['a b', 'c"', 'a b" c'])
    with pytest.raises(ValueError):
        specctra.rewrite_dsn(dsn, {"--skip-nets": str(_write(tmp_path / "s.json", ["a b"]))}, {})


def _write(path, data):
    path.write_text(json.dumps(data))
    return path


def test_canary_freerouting(tmp_path):
    """End to end: KiCad export, class split and a short Freerouting round trip on
    the awkward-name canary board."""
    res = canary.run_canary("freerouting", dest=tmp_path / "canary")
    record("specctra_nets_canary_freerouting", res)
    assert res["status"] == common.PASS, res["reasons"]


# ------------------------------------------------------- KICADROUTE-17

ACTIVE_LOW = {"nets": {"!RESET": {"netclass": "Default"}, "SPI_CLK": {"netclass": "Default"},
                       "VCC": {"netclass": "Power"}, "GND": {"netclass": "Power"}}}


def test_escaped_bang_selects_the_literal_net():
    assert route.select_nets(ACTIVE_LOW, [r"\!RESET"], None)[0] == ["!RESET"]
    assert route.select_nets(ACTIVE_LOW, [r"\!RESET", "SPI_CLK"], None)[0] == ["!RESET", "SPI_CLK"]
    assert route.select_nets(ACTIVE_LOW, [r"\!RE*"], None)[0] == ["!RESET"]
    # and excludes it
    assert route.select_nets(ACTIVE_LOW, [r"!\!RESET"], None)[0] == ["GND", "SPI_CLK", "VCC"]
    assert route.select_nets(ACTIVE_LOW, ["*", r"!\!RESET"], None)[0] == ["GND", "SPI_CLK", "VCC"]


def test_bare_bang_on_a_literal_net_is_refused():
    """`--nets '!RESET'` would route every net but the (absent) RESET: refused, both
    alone and next to other patterns, with the escape in the message."""
    for pats in (["!RESET"], ["!RESET", "SPI_CLK"]):
        with pytest.raises(route.NetSelectionError) as e:
            route.select_nets(ACTIVE_LOW, pats, None)
        assert r"\!RESET" in str(e.value) and r"!\!RESET" in str(e.value)


def test_real_exclusions_are_unchanged():
    assert route.select_nets(ACTIVE_LOW, ["!GND"], None)[0] == ["!RESET", "SPI_CLK", "VCC"]
    assert route.select_nets(ACTIVE_LOW, ["*", "!GND"], None)[0] == ["!RESET", "SPI_CLK", "VCC"]
    assert route.select_nets(ACTIVE_LOW, ["!G*"], ["Power"])[0] == ["VCC"]
    # an exclusion that does exclude something is honoured even when `!X` is also a net
    both = {"nets": {"RESET": {"netclass": "Default"}, "!RESET": {"netclass": "Default"},
                     "A": {"netclass": "Default"}}}
    assert route.select_nets(both, ["!RESET"], None)[0] == ["!RESET", "A"]
    assert route.select_nets(ACTIVE_LOW, ["!NOPE"], None)[0] == sorted(ACTIVE_LOW["nets"])


def test_select_nets_reads_netnames_and_campaign_spellings():
    """netnames.exact() (KRT) and campaign.net_pattern() both select exactly one name."""
    names = ["!RESET", "!x*", "-12V", "a*b", "a[0]", "/s/!n", "plain"]
    facts = {"nets": {n: {"netclass": "Default"} for n in names}}
    for n in names:
        assert route.select_nets(facts, [netnames.exact(n)], None)[0] == [n], n
        assert route.select_nets(facts, [campaign.net_pattern(n)], None)[0] == [n], n
        assert fnmatch.fnmatchcase(n, route.parse_net_pattern(netnames.exact(n))[1])


def test_run_refuses_bare_bang_scope(tmp_path):
    """Through the CLI: exit 4 (refused before routing), no run directory left."""
    proj = tmp_path / FIXTURE.name
    shutil.copytree(FIXTURE, proj, ignore=shutil.ignore_patterns("generate.py", "__pycache__"))
    (tmp_path / "map.json").write_text(json.dumps({"/VIN": "!RESET"}))
    r = subprocess.run([str(common.PCBNEW_PYTHON), str(route.KICAD_SCRIPTS / "rename_nets.py"),
                        str(proj / "route_demo.kicad_pcb"), str(tmp_path / "map.json")],
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr[-2000:]
    r = cli("run", "--backend", "krt", "--dir", str(proj), "--label", "bang-refused", "--nets", "!RESET",
            timeout=600)
    assert r.returncode == 4, (r.stdout[-2000:], r.stderr[-2000:])
    assert r"\!RESET" in r.stdout + r.stderr
    assert not list((OUT / "runs" / FIXTURE.name / "bang-refused").glob("*/result.json"))
