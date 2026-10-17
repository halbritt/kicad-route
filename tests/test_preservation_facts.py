"""Preservation checks and the board facts they read (cli.preservation, kicad/facts.py).

Synthetic facts dicts exercise preservation() directly; boards edited with pcbnew
(copies of the route_demo fixture) exercise facts.py: space-indented (setup),
microvias and their net-class sizes, reversed arcs and copper text.
KICADROUTE-11, 12, 18, 23, 40, 41.
"""
import copy
import json
import re
import shutil

import pytest

from helpers import FIXTURE
from kicad_route import cli as route
from kicad_route.kicad import facts as facts_py

PASS, FAIL = route.PASS, route.FAIL
MM = 1_000_000
NET = {"netclass": "Default", "track_width": 200_000, "clearance": 200_000, "via_diameter": 600_000,
       "via_drill": 300_000, "uvia_diameter": 300_000, "uvia_drill": 100_000}
SETUP = ["setup", ["pad_to_mask_clearance", "0"]]


def base_facts(**over):
    f = {"outline": [], "copper_layer_count": 4, "enabled_layers": ["B.Cu", "F.Cu", "In1.Cu", "In2.Cu"],
         "copper_layers": [{"name": n, "type": 0} for n in ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")],
         "stackup": None, "setup": copy.deepcopy(SETUP), "footprints": {}, "zones": [], "rule_areas": [],
         "copper_graphics": [], "tracks": [], "vias": [], "nets": {"N1": dict(NET)}}
    f.update(copy.deepcopy(over))
    return f


def seg(x0, x1, locked=True, uuid="t", **kw):
    return {"kind": "segment", "layer": "F.Cu", "net": "N1", "width": 200_000, "start": [x0, 0], "end": [x1, 0],
            "locked": locked, "uuid": uuid, **kw}


def via(x, locked=True, uuid="v", via_type="through", diameter=600_000, drill=300_000, layers=("F.Cu", "B.Cu")):
    return {"position": [x, 0], "diameter": diameter, "drill": drill, "via_type": via_type, "layers": list(layers),
            "net": "N1", "locked": locked, "uuid": uuid}


def arc(start, mid, end, uuid="a", locked=True):
    return {"kind": "arc", "layer": "F.Cu", "net": "N1", "width": 200_000, "start": start, "mid": mid, "end": end,
            "locked": locked, "uuid": uuid}


def run_checks(tmp_path, ref, cand, protect="existing", scope=("N1",)):
    for side in ("ref", "cand"):
        (tmp_path / side).mkdir(exist_ok=True)
    checks, _ = route.preservation(ref, cand, tmp_path / "ref", tmp_path / "cand", "board.kicad_pcb", protect,
                                   set(scope))
    return {c["check"]: c for c in checks}


def test_identical_facts_pass(tmp_path):
    f = base_facts(tracks=[seg(0, MM)], vias=[via(MM)])
    checks = run_checks(tmp_path, f, copy.deepcopy(f))
    assert all(c["status"] == PASS for c in checks.values()), {k: c["detail"] for k, c in checks.items()}


# --------------------------------------------------------- KICADROUTE-11: lock flags

@pytest.mark.parametrize("protect", ["existing", "locked"])
def test_dropped_lock_flags_fail_protected_copper(tmp_path, protect):
    """Geometry intact but every lock flag dropped (probe_preservation_lost_locks_ignored)."""
    ref = base_facts(tracks=[seg(0, MM, uuid=f"t{i}") for i in range(5)],
                     vias=[via(MM, uuid=f"v{i}") for i in range(2)])
    cand = copy.deepcopy(ref)
    for t in cand["tracks"] + cand["vias"]:
        t["locked"] = False
    c = run_checks(tmp_path, ref, cand, protect)["protected_copper"]
    assert c["status"] == FAIL
    assert "7 lock flags dropped" in c["detail"] and "max deviation 0 nm" in c["detail"]
    assert len(c["differences"]) == 7 and all(d["lock_flag"] == "dropped" for d in c["differences"])


def test_lock_moved_to_other_item_still_fails(tmp_path):
    """Unlocking one locked track and locking another keeps the lock count equal; the
    dropped flag must still be seen (the old count-difference check missed it)."""
    ref = base_facts(tracks=[seg(0, MM, locked=True, uuid="a"), seg(2 * MM, 3 * MM, locked=False, uuid="b")])
    cand = base_facts(tracks=[seg(0, MM, locked=False, uuid="a"), seg(2 * MM, 3 * MM, locked=True, uuid="b")])
    c = run_checks(tmp_path, ref, cand, "locked")["protected_copper"]
    assert c["status"] == FAIL and "1 lock flags dropped" in c["detail"]
    assert c["differences"][0]["reference_uuid"] == "a"


def test_identical_copper_pairs_by_lock_state(tmp_path):
    """Two coincident vias, one locked: pairing must not cross the lock flags."""
    ref = base_facts(vias=[via(MM, locked=True, uuid="L"), via(MM, locked=False, uuid="U")])
    cand = base_facts(vias=[via(MM, locked=False, uuid="U2"), via(MM, locked=True, uuid="L2")])
    assert run_checks(tmp_path, ref, cand)["protected_copper"]["status"] == PASS


def test_removed_locked_via_is_missing(tmp_path):
    ref = base_facts(vias=[via(MM), via(2 * MM, uuid="v2")])
    cand = base_facts(vias=[via(MM)])
    c = run_checks(tmp_path, ref, cand)["protected_copper"]
    assert c["status"] == FAIL and "1 missing or altered" in c["detail"] and "lock flags" not in c["detail"]


def test_locks_kept_pass(tmp_path):
    ref = base_facts(tracks=[seg(0, MM)], vias=[via(MM)])
    cand = base_facts(tracks=[seg(0, MM, uuid="new")], vias=[via(MM, uuid="new2")])
    c = run_checks(tmp_path, ref, cand)["protected_copper"]
    assert c["status"] == PASS and "lock flags" not in c["detail"]


# ------------------------------------------------- KICADROUTE-12: (setup) fails closed

@pytest.mark.parametrize("unread", ["reference", "candidate", "both"])
def test_unreadable_setup_fails_closed(tmp_path, unread):
    ref, cand = base_facts(), base_facts()
    if unread in ("reference", "both"):
        ref.update(setup=None, stackup=None)
    if unread in ("candidate", "both"):
        cand.update(setup=None, stackup=None)
    checks = run_checks(tmp_path, ref, cand)
    for name in ("stackup", "board_setup"):
        assert checks[name]["status"] == FAIL, name
        assert "could not be read" in checks[name]["detail"]


def test_stackup_absent_on_both_sides_passes(tmp_path):
    checks = run_checks(tmp_path, base_facts(stackup=None), base_facts(stackup=None))
    assert checks["stackup"]["status"] == PASS and checks["board_setup"]["status"] == PASS


def test_setup_change_fails(tmp_path):
    cand = base_facts(setup=["setup", ["pad_to_mask_clearance", "0.05"]])
    assert run_checks(tmp_path, base_facts(), cand)["board_setup"]["status"] == FAIL


def test_setup_block_ignores_indentation_and_strings():
    text = (FIXTURE / "route_demo.kicad_pcb").read_text()
    tabbed = facts_py.setup_block(text)
    assert tabbed and tabbed[0] == "setup" and facts_py.find(tabbed, "stackup")
    spaced = re.sub(r"(?m)^\t+", lambda m: "  " * len(m.group(0)), text)
    assert "\n\t(setup" not in spaced
    assert facts_py.setup_block(spaced) == tabbed
    assert facts_py.setup_block(text.replace("\n", " ")) == tabbed
    # a "(setup" inside a string or nested deeper is not the board's setup block
    decoy = '(kicad_pcb (title_block (comment 1 "x\\n\\t(setup (fake 1))")) (group (setup (fake 2))))'
    assert facts_py.setup_block(decoy) is None
    assert facts_py.setup_block("(kicad_pcb (setup (a 1)") is None  # unbalanced


# ------------------------------------------------------- KICADROUTE-18: microvias

@pytest.mark.parametrize("vtype,dia,drill,ok", [
    ("micro", 300_000, 100_000, True),    # at the class microvia size
    ("micro", 250_000, 100_000, False),   # below the microvia diameter
    ("micro", 300_000, 80_000, False),    # below the microvia drill
    ("through", 300_000, 100_000, False),  # a through via at microvia size
    ("blind", 600_000, 300_000, True),    # blind/buried use the via size
    ("buried", 500_000, 300_000, False),
])
def test_new_via_checked_against_its_type(tmp_path, vtype, dia, drill, ok):
    layers = ("F.Cu", "In1.Cu") if vtype in ("micro", "blind") else ("In1.Cu", "In2.Cu") \
        if vtype == "buried" else ("F.Cu", "B.Cu")
    cand = base_facts(vias=[via(MM, locked=False, uuid="n", via_type=vtype, diameter=dia, drill=drill,
                                layers=layers)])
    c = run_checks(tmp_path, base_facts(), cand)["new_copper_netclass_geometry"]
    assert c["status"] == (PASS if ok else FAIL), c
    if not ok:
        want = [0.3, 0.1] if vtype == "micro" else [0.6, 0.3]
        assert c["differences"][0]["required_mm"] == want and c["differences"][0]["via_type"] == vtype


def test_microvia_without_class_size_and_partial_sizes(tmp_path):
    """No microvia size in the facts (an older facts.json): no microvia minimum to apply;
    a class with only a drill must not crash the report."""
    nets = {"N1": {k: v for k, v in NET.items() if not k.startswith("uvia")}}
    cand = base_facts(nets=nets, vias=[via(MM, locked=False, via_type="micro", diameter=250_000, drill=100_000,
                                           layers=("F.Cu", "In1.Cu"))])
    assert run_checks(tmp_path, base_facts(nets=nets), cand)["new_copper_netclass_geometry"]["status"] == PASS
    nets = {"N1": dict(NET, via_diameter=None)}
    cand = base_facts(nets=nets, vias=[via(MM, locked=False, drill=200_000)])
    c = run_checks(tmp_path, base_facts(nets=nets), cand)["new_copper_netclass_geometry"]
    assert c["status"] == FAIL and c["differences"][0]["required_mm"] == [0, 0.3]


# ---------------------------------------------------------- KICADROUTE-23: arcs

def test_reversed_arc_is_the_same_copper(tmp_path):
    a = arc([0, 0], [500_000, 500_000], [MM, 0])
    b = arc([MM, 0], [500_000, 500_000], [0, 0], uuid="rev")
    assert route.same_copper(a, b, route.GEOMETRY_TOLERANCE_NM)
    assert route.same_copper(a, b, 0)
    pairs, missing, extra = route.match_copper([a], [b])
    assert len(pairs) == 1 and not missing and not extra
    c = run_checks(tmp_path, base_facts(tracks=[a]), base_facts(tracks=[b]))["protected_copper"]
    assert c["status"] == PASS and "max deviation 0 nm" in c["detail"]


def test_arc_through_another_mid_is_not_the_same():
    a = arc([0, 0], [500_000, 500_000], [MM, 0])
    b = arc([MM, 0], [500_000, -500_000], [0, 0])
    assert not route.same_copper(a, b, route.GEOMETRY_TOLERANCE_NM)


# ------------------------------------------------ KICADROUTE-40: footprint diffs

def fp(**over):
    rec = {"fpid": "R:R_0603", "value": "1k", "position": [MM, MM], "orientation": 0.0, "layer": "F.Cu",
           "locked": False, "path": "/abc", "pads": {"1": {"position": [MM, MM], "net": "N1", "size": [1, 1],
                                                         "drill": [0, 0], "attribute": 1, "layers": ["F.Cu"]}}}
    rec.update(over)
    return rec


def test_footprint_differences_name_the_changed_attributes(tmp_path):
    ref = base_facts(footprints={"R1": fp(), "R2": fp(), "R3": fp(), "R4": fp()})
    pads = copy.deepcopy(fp()["pads"])
    pads["1"]["size"] = [2, 2]
    cand = base_facts(footprints={"R1": fp(locked=True), "R2": fp(layer="B.Cu"), "R3": fp(pads=pads),
                                  "R5": fp()})
    c = run_checks(tmp_path, ref, cand)["footprints"]
    assert c["status"] == FAIL
    d = {x["footprint"]: x for x in c["differences"]}
    assert d["R1"]["changed"] == ["locked"]
    assert d["R1"]["reference"]["locked"] is False and d["R1"]["candidate"]["locked"] is True
    assert d["R1"]["reference"]["position"] == d["R1"]["candidate"]["position"]  # placement still shown
    assert d["R2"]["changed"] == ["layer"] and d["R2"]["candidate"]["layer"] == "B.Cu"
    assert d["R3"]["changed"] == ["pads"] and d["R3"]["pads_changed"] == 1
    assert d["R3"]["pads"][0] == {"pad": "1", "changed": ["size"], "reference": {"size": [1, 1]},
                                  "candidate": {"size": [2, 2]}}
    assert d["R4"]["changed"] == ["present"] and d["R4"]["candidate"] is None
    assert d["R5"]["changed"] == ["present"] and d["R5"]["reference"] is None


def test_footprint_value_change_is_not_a_difference(tmp_path):
    ref = base_facts(footprints={"R1": fp()})
    cand = base_facts(footprints={"R1": fp(value="2k")})
    assert run_checks(tmp_path, ref, cand)["footprints"]["status"] == PASS


# ------------------------------------------ KICADROUTE-41: copper text (synthetic)

def test_copper_graphics_differences_listed(tmp_path):
    text = {"class": "PCB_TEXT", "layer": "F.Cu", "position": [MM, MM], "text": "REV A"}
    ref = base_facts(copper_graphics=[text])
    cand = base_facts(copper_graphics=[dict(text, text="REV B")])
    c = run_checks(tmp_path, ref, cand)["copper_graphics"]
    assert c["status"] == FAIL
    assert {(d["only_in"], d["text"]) for d in c["differences"]} == {("reference", "REV A"),
                                                                    ("candidate", "REV B")}


# ------------------------------------------------- facts.py on pcbnew-built boards

def facts_of(board_path, tmp_path, name):
    f, rec = route.board_facts(board_path, tmp_path / f"{name}.json")
    assert f is not None, rec
    return f


@pytest.fixture(scope="module")
def hdi_dir(tmp_path_factory):
    """The fixture made 4-layer with an arc, a microvia, a copper text and its own
    microvia sizes for the Power class."""
    import pcbnew
    d = tmp_path_factory.mktemp("hdi") / "route_demo"
    shutil.copytree(FIXTURE, d, ignore=shutil.ignore_patterns("generate.py", "__pycache__"))
    pro = d / "route_demo.kicad_pro"
    data = json.loads(pro.read_text())
    power = next(c for c in data["net_settings"]["classes"] if c["name"] == "Power")
    power.update(microvia_diameter=0.25, microvia_drill=0.12)
    pro.write_text(json.dumps(data, indent=2))
    board = pcbnew.LoadBoard(str(d / "route_demo.kicad_pcb"))
    board.SetCopperLayerCount(4)
    layers = board.GetEnabledLayers()
    layers.AddLayer(pcbnew.In1_Cu)
    layers.AddLayer(pcbnew.In2_Cu)
    board.SetEnabledLayers(layers)
    sig = board.FindNet("/SIG_A")
    a = pcbnew.PCB_ARC(board)
    a.SetLayer(pcbnew.F_Cu)
    a.SetNet(sig)
    a.SetWidth(pcbnew.FromMM(0.2))
    a.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(112), pcbnew.FromMM(102)))
    a.SetMid(pcbnew.VECTOR2I(pcbnew.FromMM(111), pcbnew.FromMM(101)))
    a.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(110), pcbnew.FromMM(102)))   # end before start in x
    board.Add(a)
    v = pcbnew.PCB_VIA(board)
    v.SetViaType(pcbnew.VIATYPE_MICROVIA)
    v.SetLayerPair(pcbnew.F_Cu, pcbnew.In1_Cu)
    v.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(115), pcbnew.FromMM(102)))
    v.SetWidth(pcbnew.F_Cu, pcbnew.FromMM(0.25))
    v.SetDrill(pcbnew.FromMM(0.12))
    v.SetNet(board.FindNet("GND"))
    board.Add(v)
    t = pcbnew.PCB_TEXT(board)
    t.SetLayer(pcbnew.F_Cu)
    t.SetText("REV A")
    t.SetTextPos(pcbnew.VECTOR2I(pcbnew.FromMM(120), pcbnew.FromMM(125)))
    board.Add(t)
    board.Save(str(d / "route_demo.kicad_pcb"))
    return d


def variant(src, tmp_path, name, edit=None, text_edit=None):
    import pcbnew
    d = tmp_path / "variants" / name / src.name
    shutil.copytree(src, d)
    path = d / "route_demo.kicad_pcb"
    if edit:
        board = pcbnew.LoadBoard(str(path))
        edit(board, pcbnew)
        board.Save(str(path))
    if text_edit:
        path.write_text(text_edit(path.read_text()))
    return path


def test_facts_of_hdi_board(hdi_dir, tmp_path):
    f = facts_of(hdi_dir / "route_demo.kicad_pcb", tmp_path, "hdi")
    assert f["setup"] is not None and f["stackup"] is not None
    arcs = [t for t in f["tracks"] if t["kind"] == "arc"]
    assert len(arcs) == 1 and arcs[0]["start"] <= arcs[0]["end"]     # canonical endpoint order
    assert arcs[0]["start"] == [110 * MM, 102 * MM] and arcs[0]["mid"] == [111 * MM, 101 * MM]
    micro = [v for v in f["vias"] if v["via_type"] == "micro"]
    assert len(micro) == 1 and micro[0]["layers"] == ["F.Cu", "In1.Cu"]
    assert (f["nets"]["GND"]["uvia_diameter"], f["nets"]["GND"]["uvia_drill"]) == (250_000, 120_000)
    assert (f["nets"]["/SIG_A"]["uvia_diameter"], f["nets"]["/SIG_A"]["uvia_drill"]) == (300_000, 100_000)
    text = [g for g in f["copper_graphics"] if g.get("text") == "REV A"]
    assert len(text) == 1 and text[0]["class"] == "PCB_TEXT" and text[0]["layer"] == "F.Cu"
    assert text[0]["text_position"] == [120 * MM, 125 * MM] and len(text[0]["bbox"]) == 4


def test_microvia_at_class_size_passes_netclass_geometry(hdi_dir, tmp_path):
    """The microvia (0.25/0.12, the Power class microvia size) is new copper on GND; the
    class's through-via size (0.8/0.4) must not apply to it."""
    ref = facts_of(variant(hdi_dir, tmp_path, "ref", edit=lambda b, p: [
        b.Remove(v) for v in list(b.GetTracks()) if isinstance(v, p.PCB_VIA) and v.GetViaType() == p.VIATYPE_MICROVIA]),
        tmp_path, "ref")
    cand = facts_of(hdi_dir / "route_demo.kicad_pcb", tmp_path, "cand")
    c = run_checks(tmp_path, ref, cand, scope=cand["nets"])
    assert c["new_copper_netclass_geometry"]["status"] == PASS, c["new_copper_netclass_geometry"]
    assert "1 new vias" in c["new_copper_netclass_geometry"]["detail"]


def test_reversed_arc_board_keeps_protected_copper(hdi_dir, tmp_path):
    def reverse(board, pcbnew):
        a = next(t for t in board.GetTracks() if isinstance(t, pcbnew.PCB_ARC))
        s, e = pcbnew.VECTOR2I(a.GetStart()), pcbnew.VECTOR2I(a.GetEnd())  # copies, not references
        a.SetStart(e)
        a.SetEnd(s)
    ref = facts_of(hdi_dir / "route_demo.kicad_pcb", tmp_path, "ref")
    cand = facts_of(variant(hdi_dir, tmp_path, "rev", edit=reverse), tmp_path, "rev")
    strip = lambda ts: [route.strip(t, "uuid") for t in ts]  # noqa: E731
    assert strip(ref["tracks"]) == strip(cand["tracks"])          # same facts either way round
    c = run_checks(tmp_path, ref, cand, scope=ref["nets"])
    assert c["protected_copper"]["status"] == PASS and "max deviation 0 nm" in c["protected_copper"]["detail"]


@pytest.mark.parametrize("change", ["content", "position"])
def test_copper_text_change_fails_copper_graphics(hdi_dir, tmp_path, change):
    def edit(board, pcbnew):
        t = next(d for d in board.GetDrawings() if isinstance(d, pcbnew.PCB_TEXT) and d.GetText() == "REV A")
        if change == "content":
            t.SetText("REV B")
        else:
            t.SetTextPos(pcbnew.VECTOR2I(pcbnew.FromMM(121), pcbnew.FromMM(125)))
    ref = facts_of(hdi_dir / "route_demo.kicad_pcb", tmp_path, "ref")
    cand = facts_of(variant(hdi_dir, tmp_path, change, edit=edit), tmp_path, change)
    c = run_checks(tmp_path, ref, cand, scope=ref["nets"])
    assert c["copper_graphics"]["status"] == FAIL
    assert {d["only_in"] for d in c["copper_graphics"]["differences"]} == {"reference", "candidate"}
    assert c["protected_copper"]["status"] == PASS


def spaces(text):
    return re.sub(r"(?m)^\t+", lambda m: "  " * len(m.group(0)), text)


def test_space_indented_board_setup_is_read(hdi_dir, tmp_path):
    """probe_setup_block_indentation: a space-indented board has a readable (setup) equal to
    the tab-indented one, and a relaxed setup value on it fails board_setup."""
    ref = facts_of(hdi_dir / "route_demo.kicad_pcb", tmp_path, "ref")
    same = facts_of(variant(hdi_dir, tmp_path, "spaces", text_edit=spaces), tmp_path, "spaces")
    assert same["setup"] is not None and same["setup"] == ref["setup"] and same["stackup"] == ref["stackup"]
    checks = run_checks(tmp_path, ref, same, scope=ref["nets"])
    assert checks["board_setup"]["status"] == PASS and checks["stackup"]["status"] == PASS

    def relax(text):
        out = spaces(text).replace("(pad_to_mask_clearance 0)", "(pad_to_mask_clearance 0.2)")
        return out.replace("(thickness 1.51)", "(thickness 1.2)")
    changed = facts_of(variant(hdi_dir, tmp_path, "relaxed", text_edit=relax), tmp_path, "relaxed")
    checks = run_checks(tmp_path, ref, changed, scope=ref["nets"])
    assert checks["board_setup"]["status"] == FAIL and checks["stackup"]["status"] == FAIL
