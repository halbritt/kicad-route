"""The blocked-by receipts schema (schemas/blocked-by.schema.json, docs/blocked.md).

KICADROUTE-9: blocked-by.json is placement evidence for agent-ee, so its shape is a
versioned contract. Every committed routing.blocked_by summary validates against the
schema's summary form; synthetic reports pin endpoint resolution (footprint and pad),
the footprint aggregates and the board hashes; a KRT run on the keepout-wall fixture
and `kicad-route blocked --board` on the unrouted fixture produce schema-valid
reports, and the board mode changes no file.
"""
import hashlib
import json

import jsonschema
import pytest

from helpers import FIXTURE, OUT, ROOT, cli, record, run, tree_hash
from kicad_route import blocked
from kicad_route import cli as route

SCHEMA = json.loads((ROOT / blocked.SCHEMA_FILE).read_text())
REPORTS = ROOT / "reports" / "fixture"
MM = blocked.NM


def report_errors(doc):
    return [f"{list(e.absolute_path)}: {e.message}"
            for e in jsonschema.Draft202012Validator(SCHEMA).iter_errors(doc)]


def summary_errors(doc):
    sub = {"$schema": SCHEMA["$schema"], "$defs": SCHEMA["$defs"], "$ref": "#/$defs/summary"}
    return [f"{list(e.absolute_path)}: {e.message}" for e in jsonschema.Draft202012Validator(sub).iter_errors(doc)]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def find_key(node, key):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key:
                yield v
            yield from find_key(v, key)
    elif isinstance(node, list):
        for v in node:
            yield from find_key(v, key)


# --------------------------------------------------------------- the schema

def test_schema_is_a_valid_versioned_json_schema():
    jsonschema.Draft202012Validator.check_schema(SCHEMA)
    assert SCHEMA["$id"] == blocked.SCHEMA_ID == f"kicad-route/blocked/{blocked.REPORT_VERSION}"
    report = SCHEMA["$defs"]["report"]["properties"]
    assert report["schema"]["const"] == blocked.SCHEMA_ID and report["version"]["const"] == blocked.REPORT_VERSION
    assert report["heuristic"]["const"] is True
    hard = SCHEMA["$defs"]["layer_receipt"]["properties"]["hard"]
    assert hard["maxItems"] == blocked.LAYER_LIST_LIMIT


def test_committed_summaries_match_the_schema():
    """Every routing.blocked_by in the committed fixture evidence (version 1 summaries,
    before this schema) is valid; so is any committed full blocked-by.json."""
    found = 0
    for path in sorted(REPORTS.rglob("*.json")):
        doc = json.loads(path.read_text())
        for s in find_key(doc, "blocked_by"):
            found += 1
            assert summary_errors(s) == [], path
        if path.name == "blocked-by.json":
            assert report_errors(doc) == [], path
    assert found >= 3  # blocked_krt, blocked_freerouting, blocked_receipts at least


def test_summary_schema_rejects_drift():
    good = json.loads((REPORTS / "blocked_krt.json").read_text())["routing"]["blocked_by"]
    assert summary_errors({**good, "top": [{"id": "x"}]})
    assert summary_errors({**good, "schema": blocked.SCHEMA_ID})                   # schema without version
    assert summary_errors({k: v for k, v in good.items() if k != "nets"})
    assert summary_errors({"error": "KeyError: x"}) == []


# ------------------------------------------------------- synthetic reports

LAYERS = [{"name": "F.Cu", "type": 0}, {"name": "B.Cu", "type": 0}]
NETS = {n: {"netclass": "Default", "track_width": 200_000, "clearance": 200_000} for n in ("A", "B", "C", "GND")}


def pad(x, y, net, layers=("F.Cu",)):
    return {"position": [x * MM, y * MM], "net": net, "size": [MM, MM], "drill": [0, 0], "attribute": 1,
            "layers": list(layers)}


def fp(x, y, pads, layer="F.Cu", orientation=0.0):
    return {"value": "v", "position": [x * MM, y * MM], "orientation": orientation, "layer": layer,
            "locked": False, "pads": pads}


def board(**over):
    facts = {"copper_layers": LAYERS, "nets": NETS, "tracks": [], "vias": [], "zones": [], "rule_areas": [],
             "footprints": {}, "outline": [{"shape": "Rect", "layer": "Edge.Cuts", "width": 100_000,
                                            "start": [-5 * MM, -10 * MM], "end": [35 * MM, 10 * MM]}]}
    facts.update(over)
    return facts


def item(desc, x, y, uuid="u"):
    return {"description": desc, "pos": {"x": x, "y": y}, "uuid": uuid}


def drc(*pairs):
    return {"unconnected_items": [{"type": "unconnected_items", "items": list(p)} for p in pairs]}


def test_endpoints_name_footprint_pad_and_layers():
    """A pad endpoint is resolved to its footprint and pad number from the board facts by
    position and net; stacked same-net pads are told apart by the footprint the DRC text
    names. A track endpoint carries its layer. U3's pads block both connections."""
    facts = board(
        footprints={"U1": fp(0, 0, {"1": pad(0, 0, "A", ("F.Cu", "B.Cu"))}),
                    "U2": fp(20, 0, {"1": pad(20, 0, "A")}),
                    "U9": fp(20, 0, {"7": pad(20, 0, "A")}, layer="B.Cu", orientation=90.0),  # stacked under U2.1
                    "U3": fp(10, 2, {"1": pad(10, 0, "C", ("F.Cu", "B.Cu")), "2": pad(10, 4, "C", ("F.Cu", "B.Cu"))})},
        tracks=[{"kind": "segment", "uuid": "t-b", "net": "B", "layer": "B.Cu", "locked": False, "width": 200_000,
                 "start": [0, 4 * MM], "end": [MM, 4 * MM]}])
    rep = blocked.analyze(drc((item("PTH pad 1 [A] of U1", 0, 0), item("Pad 7 [A] of U9 on B.Cu", 20, 0)),
                              (item("Track [B] on B.Cu, length 1 mm", 1, 4, "t-b"), item("Pad 1 [B] of U5", 20, 4))),
                          facts, facts, scope={"A", "B"},
                          boards={"input_sha256": "a" * 64, "candidate_sha256": "b" * 64, "graded_sha256": "b" * 64})
    assert report_errors(rep) == []
    a, b = rep["connections"]
    assert [(e["footprint"], e["pad"], e["layers"]) for e in a["endpoints"]] == [
        ("U1", "1", ["F.Cu", "B.Cu"]), ("U9", "7", ["F.Cu"])]
    track_end, missing = b["endpoints"]
    assert track_end["kind"] == "track" and track_end["layers"] == ["B.Cu"] and track_end["footprint"] is None
    assert missing["footprint"] is None and "not found" in missing["note"]           # never guessed
    assert rep["boards"]["input_sha256"] == "a" * 64 and rep["nets_considered"] == ["A", "B"]
    assert rep["source"] == "run" and rep["heuristic"] is True and rep["schema"] == blocked.SCHEMA_ID

    u3 = rep["footprints"]["U3"]
    assert u3["blocks_any_layer"] == [0, 1] and u3["terminates"] == [] and u3["pads_blocking"] == ["1", "2"]
    assert u3["position_mm"] == [10.0, 2.0] and u3["side"] == "front" and u3["nets_blocked"] == ["A", "B"]
    u9 = rep["footprints"]["U9"]
    assert u9["terminates"] == [0] and u9["side"] == "back" and u9["orientation_deg"] == 90.0
    assert "U2" not in rep["footprints"]                     # neither blocks nor terminates
    s = blocked.summary(rep, "validate/blocked-by.json")
    assert summary_errors(s) == [] and s["version"] == blocked.REPORT_VERSION


def test_zone_endpoint_target_names_the_pad():
    facts = board(
        zones=[{"name": "GND_B", "net": "GND", "layers": ["F.Cu", "B.Cu"], "priority": 0,
                "outline": [[-4 * MM, -9 * MM], [34 * MM, -9 * MM], [34 * MM, 9 * MM], [-4 * MM, 9 * MM]]}],
        footprints={"J1": fp(30, 0, {"2": pad(30, 0, "GND", ("F.Cu", "B.Cu"))}),
                    "U6": fp(5, 1, {"1": pad(5, 1, "GND")})})
    rep = blocked.analyze(drc((item("PTH pad 2 [GND] of J1", 30, 0), item("Zone 'GND_B' [GND] on B.Cu, priority 0", -4, -9))),
                          facts, facts)
    assert report_errors(rep) == []
    zone = rep["connections"][0]["endpoints"][1]
    assert zone["kind"] == "zone" and zone["layers"] == ["F.Cu", "B.Cu"] and zone["footprint"] is None
    assert zone["reported_position_mm"] == [-4.0, -9.0] and zone["position_mm"] == [5.0, 1.0]
    assert zone["target"] == {"kind": "pad", "footprint": "U6", "pad": "1", "position_mm": [5.0, 1.0]}
    assert rep["footprints"]["U6"]["terminates"] == [0] and rep["footprints"]["J1"]["terminates"] == [0]
    assert rep["nets_considered"] == ["A", "B", "C", "GND"]  # no scope: every net


def test_layer_lists_are_bounded_but_counts_exact():
    """A dense board does not blow up the report: each layer lists at most
    LAYER_LIST_LIMIT blockers, and hard_count stays the full number."""
    n = blocked.LAYER_LIST_LIMIT + 10
    pads = {str(i): pad(1 + i * 0.5, 0, "C", ("F.Cu", "B.Cu")) for i in range(n)}
    facts = board(footprints={"U1": fp(0, 0, {"1": pad(-3, 0, "A")}), "U2": fp(30, 0, {"1": pad(30, 0, "A")}),
                              "RN": fp(10, 0, pads)})
    rep = blocked.analyze(drc((item("Pad 1 [A] of U1 on F.Cu", -3, 0), item("Pad 1 [A] of U2 on F.Cu", 30, 0))),
                          facts, facts)
    assert report_errors(rep) == []
    lay = rep["connections"][0]["layers"]["F.Cu"]
    assert lay["hard_count"] == n and len(lay["hard"]) == blocked.LAYER_LIST_LIMIT
    assert len(rep["footprints"]["RN"]["pads_blocking"]) == n


def test_publish_carries_blocked_by(tmp_path):
    run_dir = tmp_path / "run"
    (run_dir / "validate").mkdir(parents=True)
    (run_dir / "result.json").write_text("{}")
    (run_dir / "validate" / "blocked-by.json").write_text('{"schema": "x"}')
    route.publish_run(run_dir, tmp_path / "pub")
    assert (tmp_path / "pub" / "blocked-by.json").read_text() == '{"schema": "x"}'


# --------------------------------------------------------------- integration

def test_wall_run_report_is_schema_valid(blocked_dir):
    """The keepout-wall run (budget 600 s: the result does not depend on it, and a shared
    host can starve KRT of the 180 s the other tests use): blocked-by.json validates, every pad endpoint names its
    footprint and pad, the board hashes are the run's own files, and J2 (cut off by
    the wall) terminates every open connection. Recomputing gives the same answer."""
    r, run_dir, result = run("run", "--backend", "krt", "--budget", "600", "--dir", str(blocked_dir),
                             "--backend-env", "KICAD_NET_RESCUE=0", "--label", "blocked-schema-krt")
    assert result["routing"]["status"] == route.INCOMPLETE, result["routing"]
    path = run_dir / "validate" / "blocked-by.json"
    rep = json.loads(path.read_text())
    assert report_errors(rep) == []
    assert summary_errors(result["routing"]["blocked_by"]) == []
    attempt = json.loads((run_dir / "attempt.json").read_text())
    assert rep["boards"] == {
        "input_sha256": sha(run_dir / attempt["reference_dir"] / attempt["board"]),
        "candidate_sha256": sha(run_dir / attempt["candidate_dir"] / attempt["board"]),
        "graded_sha256": result["validation"]["validated_board_sha256"]}
    # validation analyses the run's scope; a recompute the open nets of it (the same items)
    assert set(result["routing"]["unrouted_nets"]) <= set(rep["nets_considered"]) <= set(attempt["scope"]["nets"])
    pads = [e for c in rep["connections"] for e in c["endpoints"] if e["kind"] == "pad"]
    assert pads and all(e["footprint"] and e["pad"] and e["layers"] for e in pads), pads
    assert rep["footprints"]["J2"]["terminates"] == list(range(rep["connections_analyzed"]))
    record("blocked_schema", {"run_dir": result["run_dir"], "connections": rep["connections_analyzed"],
                              "boards": rep["boards"], "footprints": sorted(rep["footprints"]),
                              "bytes": path.stat().st_size})

    assert cli("blocked", str(run_dir)).returncode == 0
    again = json.loads(path.read_text())
    assert report_errors(again) == [] and again["boards"] == rep["boards"]
    assert again["connections"] == rep["connections"] and again["footprints"] == rep["footprints"]
    assert again["nets_considered"] == result["routing"]["unrouted_nets"]


def test_board_mode_scores_an_unrouted_board_without_writing_it():
    """`blocked --board` on the unrouted fixture: no backend run, every one of the 6
    unconnected items of the input is listed with footprint and pad endpoints and the
    footprints they terminate on; the source project is untouched."""
    board = FIXTURE / "route_demo.kicad_pcb"
    before = tree_hash(FIXTURE)
    r = cli("blocked", "--board", str(board), "--json", timeout=600)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    rep = json.loads(r.stdout)
    assert tree_hash(FIXTURE) == before
    assert report_errors(rep) == []
    assert rep["source"] == "board" and rep["boards"]["input_sha256"] == sha(board)
    assert rep["boards"]["candidate_sha256"] is None
    assert rep["connections_analyzed"] == 6
    ends = [e for c in rep["connections"] for e in c["endpoints"]]
    assert len(ends) == 12 and all(e["kind"] == "pad" and e["footprint"] and e["pad"] for e in ends), ends
    assert {e["footprint"] for e in ends} == {ref for ref, f in rep["footprints"].items() if f["terminates"]}
    record("blocked_board_mode", {"connections": rep["connections_analyzed"], "runtime_s": rep["runtime_s"],
                                  "footprints": sorted(rep["footprints"]), "boards": rep["boards"]})

    human = cli("blocked", "--board", str(board), timeout=600)
    assert human.returncode == 0 and "6 unconnected items" in human.stdout
    assert tree_hash(FIXTURE) == before


def test_board_mode_refuses_bad_arguments(tmp_path):
    assert cli("blocked").returncode != 0
    assert cli("blocked", "--board", str(tmp_path / "nope.kicad_pcb")).returncode != 0
    assert cli("blocked", str(tmp_path), "--board", str(FIXTURE / "route_demo.kicad_pcb")).returncode != 0
