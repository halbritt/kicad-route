"""Session fixtures shared by every test module: the source fixture must stay
untouched, and variants of it (blocked, unlocked) are built once per session."""
import shutil

import pytest

from helpers import FIXTURE, ROOT, record, tree_hash


@pytest.fixture(scope="session", autouse=True)
def source_untouched():
    """The authoritative fixture project must be byte-identical after every attempt."""
    before = tree_hash(FIXTURE)
    yield
    after = tree_hash(FIXTURE)
    record("source_untouched", {"fixture": str(FIXTURE.relative_to(ROOT)), "before": before, "after": after})
    assert before == after, "a routing run modified the source project"


@pytest.fixture(scope="session", autouse=True)
def fresh_ledger():
    """Each session starts with an empty run ledger, so an attempt a previous
    session recorded as failed is not refused (exit 4) when its test repeats it."""
    from helpers import OUT
    (OUT / "ledger.jsonl").unlink(missing_ok=True)
    yield


@pytest.fixture(scope="session")
def blocked_dir(tmp_path_factory):
    """The fixture with a keepout wall that cuts J2 off from every other part."""
    import pcbnew
    d = tmp_path_factory.mktemp("blocked") / "route_demo"
    shutil.copytree(FIXTURE, d, ignore=shutil.ignore_patterns("generate.py", "__pycache__"))
    board = pcbnew.LoadBoard(str(d / "route_demo.kicad_pcb"))
    wall = pcbnew.ZONE(board)
    wall.SetIsRuleArea(True)
    layers = pcbnew.LSET()
    layers.AddLayer(pcbnew.F_Cu)
    layers.AddLayer(pcbnew.B_Cu)
    wall.SetLayerSet(layers)
    wall.SetDoNotAllowTracks(True)
    wall.SetDoNotAllowVias(True)
    wall.SetDoNotAllowZoneFills(True)
    wall.SetDoNotAllowPads(False)
    wall.SetDoNotAllowFootprints(False)
    wall.SetZoneName("wall")
    o = wall.Outline()
    o.NewOutline()
    for x, y in [(138, 99), (140, 99), (140, 131), (138, 131)]:
        o.Append(pcbnew.FromMM(x), pcbnew.FromMM(y))
    board.Add(wall)
    board.Save(str(d / "route_demo.kicad_pcb"))
    return d


@pytest.fixture(scope="session")
def unlocked_dir(tmp_path_factory):
    """The fixture with the pre-routed /SIG_B left UNLOCKED: ordinary existing copper."""
    import pcbnew
    d = tmp_path_factory.mktemp("unlocked") / "route_demo"
    shutil.copytree(FIXTURE, d, ignore=shutil.ignore_patterns("generate.py", "__pycache__"))
    board = pcbnew.LoadBoard(str(d / "route_demo.kicad_pcb"))
    for t in board.GetTracks():
        t.SetLocked(False)
    board.Save(str(d / "route_demo.kicad_pcb"))
    return d
