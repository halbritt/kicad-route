"""Guards on the test suite itself, and the zone comparison the spoke-pressure
test depends on (no KiCad, no routing). Shared harness: helpers.py."""
import re
from pathlib import Path

from helpers import ROOT  # noqa: F401  (puts src/ on sys.path)
from kicad_route import cli as route

TESTS = Path(__file__).resolve().parent
SET_ASSERT = re.compile(r"\b(returncode|exit_code)\b[^\n#]*\bin\s*[(\[{]")
JUSTIFIED = "exit code not pinned:"


def test_exit_codes_are_pinned():
    """An assertion like `returncode in (2, 3)` cannot tell valid-but-incomplete (2)
    from failed validation (3). Each scenario pins its code; one that genuinely
    cannot must say why in a comment on the line above, starting with JUSTIFIED."""
    loose = []
    for p in sorted(TESTS.glob("*.py")):
        lines = p.read_text().splitlines()
        for i, line in enumerate(lines):
            code = line.split("#", 1)[0]
            if "assert" in code and SET_ASSERT.search(code) and not (i and JUSTIFIED in lines[i - 1]):
                loose.append(f"{p.name}:{i + 1}: {line.strip()}")
    loose = [x for x in loose if not x.startswith(Path(__file__).name)]
    assert not loose, "\n".join(loose)


def zone(name, layer, filled, priority=0):
    return {"name": name, "layers": [layer], "outline": [[0, 0], [10, 0], [10, 10]], "net": "GND",
            "priority": priority, "clearance": 300000, "min_thickness": 250000, "pad_connection": 1,
            "thermal_gap": 500000, "thermal_spoke": 500000, "fill_mode": 0, "island_removal": 0,
            "locked": False, "filled": filled}


def facts_order(zones):   # the order facts.py writes: by the whole record, `filled` included
    import json
    return {"zones": sorted(zones, key=lambda r: json.dumps(r, sort_keys=True))}


def test_zone_definitions_ignore_fill_state_and_its_order():
    """The F.Cu-pour variant's GND_F is never filled in the input; refilled in the
    candidate it sorts the other way. That is not a zone change (it failed the
    `zones` check on every fzone run before), but a real definition change is."""
    ref = facts_order([zone("GND_B", "B.Cu", True), zone("GND_F", "F.Cu", False)])
    cand = facts_order([zone("GND_B", "B.Cu", True), zone("GND_F", "F.Cu", True)])
    assert [z["name"] for z in ref["zones"]] != [z["name"] for z in cand["zones"]]
    assert route.zone_definitions(ref) == route.zone_definitions(cand)
    changed = facts_order([zone("GND_B", "B.Cu", True), zone("GND_F", "F.Cu", True, priority=1)])
    assert route.zone_definitions(ref) != route.zone_definitions(changed)
    assert route.zone_definitions(ref) != route.zone_definitions(facts_order([zone("GND_B", "B.Cu", True)]))
