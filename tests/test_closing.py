"""The connections a run leaves open get more rounds, and a round is kept only when it gains (closing.py).

The board is the route_demo fixture with a GND pour on F.Cu and nothing routed by the run. The
engine's part, routing the open nets once more, is played by a stand-in that lays a known track,
so the test holds what the closing stage itself does: which nets it asks for, with which
settings and how much time, and that it keeps a round only when KiCad's report has fewer open
connections and no more findings. The engine's own pass is krt_plane_pass, as for the plane
passes of every run.
Shared harness: helpers.py and conftest.py; the board helpers are test_thermal_repair's.
"""
import shutil

from kicad_route import cli as route
from kicad_route import closing
from test_thermal_repair import BOARD, CLEAR, edit, pour_dir, stage  # noqa: F401  (pour_dir is a fixture)

# /SIG_A from J1.3 to R1.1 straight through the fixture's keep-out (x 119 to 131, y 105 to 122)
THROUGH_KEEPOUT = [(104.0, 111.08), (124.09, 111.08), (124.09, 103.0)]


def counts(work, board, output, label):
    report = closing.stitch._graded(route, work, board.parent, output, label, folder="test-drc")
    assert report is not None
    return len(report["unconnected_items"]), len(report["violations"]), closing.open_nets(report)


def test_open_nets_reads_the_nets_kicad_names():
    report = {"unconnected_items": [
        {"items": [{"description": "PTH pad 3 [/SIG_A] of J1"}, {"description": "Pad 1 [/SIG_A] of R1 on F.Cu"}]},
        {"items": [{"description": "Track [Net-(C1-Pad2)] on F.Cu, length 1.2 mm"}, {"description": "Zone [GND] on B.Cu"}]},
        {"items": [{"description": "an item with no net"}]}]}
    assert closing.open_nets(report) == ["/SIG_A", "GND", "Net-(C1-Pad2)"]
    assert closing.open_nets({}) == [] and closing.open_nets(None) == []


def test_a_round_that_closes_a_connection_is_kept(pour_dir, tmp_path):
    work, board, before, output = stage(pour_dir, tmp_path, [])
    open_before, found_before, nets_before = counts(work, board, output, "start")
    assert "/SIG_A" in nets_before and open_before >= 2
    calls = []

    def route_again(name, board_file, nets, seconds, strategy):
        calls.append((name, list(nets), seconds, strategy))
        out = work / f"krt-{name}" / BOARD
        out.parent.mkdir(parents=True)
        if len(calls) == 1:                      # the first round connects /SIG_A; the second gains nothing
            edit(board_file, out, track=CLEAR)
        else:
            shutil.copyfile(board_file, out)
        return {"status": "routed", "output": out, "used_s": 40, "command": None}

    rec = closing.close(work, board, before, output, route_again, 200, route)
    assert rec["status"] == "closed" and (rec["open_before"], rec["open_after"]) == (open_before, open_before - 1), rec
    first, second = rec["rounds"]
    assert first["result"] == "kept" and first["nets"] == nets_before
    assert first["strategy"] == closing.STRATEGIES[0]["name"]
    assert first["drc_counts"] == {"before": [open_before, found_before], "after": [open_before - 1, found_before]}
    assert "not the better" in second["result"] and "/SIG_A" not in second["nets"]
    # each round has its own settings, and a share of the time that is left
    assert [c[0] for c in calls] == ["close-1", "close-2"]
    assert [c[3] for c in calls] == list(closing.STRATEGIES)
    assert closing.STRATEGIES[0]["env"] == {"KICAD_DYNAMIC_ITERATIONS": "1"} and not closing.STRATEGIES[0]["args"]
    assert closing.STRATEGIES[1]["env"] == closing.LIFT_ENV and closing.STRATEGIES[1]["args"] == closing.LIFT_ARGS
    assert calls[0][2] == 100 and calls[1][2] == 160           # half of 200, then all of the 160 left
    assert counts(work, board, output, "end")[0] == open_before - 1
    had = edit(before, tmp_path / "copy-before.kicad_pcb")
    now = edit(output, tmp_path / "copy-output.kicad_pcb")
    assert [t for t in now if t[2]] == [t for t in had if t[2]] and len(now) == len(had) + len(CLEAR) - 1


def test_a_round_that_adds_a_finding_is_not_kept(pour_dir, tmp_path):
    work, board, before, output = stage(pour_dir, tmp_path, [])
    original = output.read_bytes()

    def route_again(name, board_file, nets, seconds, strategy):
        out = work / f"krt-{name}" / BOARD       # the connection is made, through the board's keep-out
        out.parent.mkdir(parents=True)
        edit(board_file, out, track=THROUGH_KEEPOUT)
        return {"status": "routed", "output": out, "used_s": 20, "command": None}

    rec = closing.close(work, board, before, output, route_again, 200, route)
    assert rec["status"] == "kept" and output.read_bytes() == original, rec
    for entry in rec["rounds"]:
        before_counts, after_counts = entry["drc_counts"]["before"], entry["drc_counts"]["after"]
        # one connection made, more than one finding added: the report is the longer
        assert after_counts[0] == before_counts[0] - 1 and sum(after_counts) >= sum(before_counts)
        assert "not the better" in entry["result"]


def test_two_connections_closed_are_no_trade_for_one_finding():
    assert closing.better((5, 10), (4, 10)) and closing.better((5, 10), (3, 9))
    assert not closing.better((5, 10), (3, 11))       # the sum is shorter, and the board now fails
    assert not closing.better((5, 10), (5, 9))        # a round is for connections
    assert not closing.better((5, 10), (6, 2))


def test_only_the_runs_own_nets_are_asked_for(pour_dir, tmp_path):
    work, board, before, output = stage(pour_dir, tmp_path, [])
    asked = []

    def route_again(name, board_file, nets, seconds, strategy):
        asked.append(list(nets))
        return {"status": "failed", "reason": "it wrote no board", "used_s": 5, "command": None}

    rec = closing.close(work, board, before, output, route_again, 200, route, in_scope={"/SIG_A"}.__contains__)
    assert asked == [["/SIG_A"], ["/SIG_A"]] and rec["status"] == "kept"
    assert all("could not be routed again (it wrote no board)" in entry["result"] for entry in rec["rounds"])
    none = closing.close(work, board, before, output, route_again, 200, route, in_scope=lambda net: False)
    assert none["status"] == "none" and none["rounds"] == [] and len(asked) == 2


def test_without_time_nothing_is_started(pour_dir, tmp_path):
    work, board, before, output = stage(pour_dir, tmp_path, [])

    def route_again(*args):
        raise AssertionError("no time for a round")

    rec = closing.close(work, board, before, output, route_again, closing.MIN_S - 1, route)
    assert rec["status"] == "none" and rec["rounds"] == [] and "of the budget left" in rec["reason"]
    assert not (work / "closing").exists()


def test_a_round_is_judged_after_what_it_broke_is_repaired(pour_dir, tmp_path):
    work, board, before, output = stage(pour_dir, tmp_path, [])
    open_before = counts(work, board, output, "start")[0]
    settled = []

    def route_again(name, board_file, nets, seconds, strategy):
        out = work / f"krt-{name}" / BOARD
        out.parent.mkdir(parents=True)
        edit(board_file, out, track=THROUGH_KEEPOUT if name == "close-1" else [])
        return {"status": "routed", "output": out, "used_s": 20, "command": None}

    def settle(name, board_file, seconds):
        """The repair's stand-in: the track through the keep-out is laid clear of it instead."""
        settled.append((name, seconds))
        if name == "close-1":
            edit(board_file, board_file, drop_unlocked="/SIG_A", track=CLEAR)
        return [{"argv": ["repair", name]}], 30

    rec = closing.close(work, board, before, output, route_again, 200, route, settle=settle)
    # the repair is offered what the rounds have left, and what it takes comes out of their time
    assert settled == [("close-1", 180.0), ("close-2", 130.0)]
    assert [entry["budget_s"] for entry in rec["rounds"]] == [100, 150]
    assert [entry["settle_s"] for entry in rec["rounds"]] == [30, 30] and rec["used_s"] == 100.0
    assert rec["status"] == "closed" and rec["open_after"] == open_before - 1, rec
    assert rec["rounds"][0]["result"] == "kept" and {"argv": ["repair", "close-1"]} in rec["commands"]
