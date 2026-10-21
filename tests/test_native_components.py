"""Native terminal equivalences, independent of KiCad's capped DRC finding lists."""
import json
import shutil
from pathlib import Path

import pcbnew
import pytest

from helpers import FIXTURE
from kicad_route import cli, common, native_components

BOARD = "terminals.kicad_pcb"


def project(path, count=1, joined=True, duplicate_numbers=False, jumper=False):
    path.mkdir()
    board = pcbnew.BOARD()
    for index in range(count):
        net = pcbnew.NETINFO_ITEM(board, f"N{index:04}")
        board.Add(net)
        fp = pcbnew.FOOTPRINT(board)
        fp.SetReference(f"J{index + 1}")
        fp.SetDuplicatePadNumbersAreJumpers(jumper)
        board.Add(fp)
        x, y = 10 + (index % 25) * 12, 10 + (index // 25) * 5
        terminal_pads = []
        for pad_index in range(3 if count == 1 else 2):
            pad = pcbnew.PAD(fp)
            pad.SetNumber("1" if duplicate_numbers else str(pad_index + 1))
            pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
            pad.SetShape(pcbnew.PAD_SHAPE_RECT)
            pad.SetSize(pcbnew.VECTOR2I(600_000, 600_000))
            layers = pcbnew.LSET()
            layers.AddLayer(pcbnew.F_Cu)
            pad.SetLayerSet(layers)
            pad.SetPosition(pcbnew.VECTOR2I(int((x + pad_index * 3) * 1e6), int(y * 1e6)))
            pad.SetNet(net)
            fp.Add(pad)
            terminal_pads.append(pad)
        if joined:
            track = pcbnew.PCB_TRACK(board)
            track.SetStart(terminal_pads[0].GetPosition())
            track.SetEnd(terminal_pads[1].GetPosition())
            track.SetLayer(pcbnew.F_Cu)
            track.SetWidth(300_000)
            track.SetNet(net)
            board.Add(track)
    pcbnew.SaveBoard(str(path / BOARD), board, aSkipSettings=True)
    shutil.copy2(FIXTURE / "route_demo.kicad_pro", path / "terminals.kicad_pro")
    return board


def swapped_candidate(reference, candidate):
    shutil.copytree(reference, candidate)
    board = pcbnew.LoadBoard(str(candidate / BOARD))
    pads = sorted(next(iter(board.GetFootprints())).Pads(), key=lambda pad: int(pad.GetNumber()))
    track = next(iter(board.GetTracks()))
    track.SetStart(pads[1].GetPosition())
    track.SetEnd(pads[2].GetPosition())
    pcbnew.SaveBoard(str(candidate / BOARD), board, aSkipSettings=True)


def test_partial_net_cannot_swap_joined_terminals_at_equal_open_count(tmp_path, monkeypatch):
    reference, candidate = tmp_path / "reference", tmp_path / "candidate"
    project(reference)
    swapped_candidate(reference, candidate)
    monkeypatch.setattr(common, "_out", tmp_path / "out")
    common.ensure_kicad_config()
    base, info = cli.baseline_drc(reference, BOARD, "partial-equivalence")
    after, _ = cli.kicad_drc(candidate / BOARD, tmp_path / "after", "candidate", refill_and_save=True)
    facts, _ = cli.board_facts(reference / BOARD, tmp_path / "facts.json")
    assert len(base["unconnected_items"]) == len(after["unconnected_items"]) == 1
    before_facts = cli.baselinecache.terminal_facts(info["report"], info["identity"])
    after_facts, _ = native_components.extract(candidate / BOARD, tmp_path / "after-components.json")
    def numbers(facts):
        return sorted(sorted(facts["inventory"][uid]["number"] for uid in group) for group in facts["nets"]["N0000"])
    assert numbers(before_facts) == [["1", "2"], ["3"]]
    assert numbers(after_facts) == [["1"], ["2", "3"]]
    check = cli.provenance.net_completion(before_facts, after_facts)
    assert check["status"] == common.FAIL
    assert check["nets_lost"] == []
    assert check["split_components"][0]["net"] == "N0000"


@pytest.mark.parametrize("name", ['0', 'quote" slash\\ line\nΩ', '/BUS[0]'])
def test_native_named_terminal_inventory_handles_quoted_atoms(tmp_path, name):
    directory = tmp_path / "named"
    board = project(directory, joined=False)
    net = next(n for n in board.GetNetsByName().values() if n.GetNetname() == "N0000")
    net.SetNetname(name)
    pcbnew.SaveBoard(str(directory / BOARD), board, aSkipSettings=True)
    components, _ = native_components.extract(directory / BOARD, tmp_path / "components.json")
    assert components["status"] == common.PASS, components["reasons"]
    assert set(components["nets"]) == {name}
    assert len(components["nets"][name]) == 3


@pytest.mark.parametrize("jumper", [False, True])
def test_same_number_pads_follow_native_jumper_semantics(tmp_path, jumper):
    directory = tmp_path / "same-numbers"
    project(directory, joined=False, duplicate_numbers=True, jumper=jumper)
    components, _ = native_components.extract(directory / BOARD, tmp_path / "components.json")
    assert components["status"] == common.PASS, components["reasons"]
    assert len(components["inventory"]) == 3
    assert len(components["nets"]["N0000"]) == (1 if jumper else 3)
    if jumper:
        before = json.loads(json.dumps(components))
        before["jumpers"][next(iter(before["jumpers"]))]["duplicate_numbers"] = False
        assert cli.provenance.net_completion(before, components)["status"] == common.NOT_RUN


def test_changed_jumper_group_cannot_replace_a_physical_connection(tmp_path):
    directory = tmp_path / "jumpers"
    project(directory, joined=False)
    before, _ = native_components.extract(directory / BOARD, tmp_path / "before.json")
    path = directory / BOARD
    text = path.read_text().replace('(duplicate_pad_numbers_are_jumpers no)',
                                    '(duplicate_pad_numbers_are_jumpers no)\n(jumper_pad_groups ("1" "2"))')
    path.write_text(text)
    after, _ = native_components.extract(path, tmp_path / "after.json")
    assert before["status"] == after["status"] == common.PASS
    assert len(before["nets"]["N0000"]) == 3 and len(after["nets"]["N0000"]) == 2
    assert cli.provenance.net_completion(before, after)["status"] == common.NOT_RUN


def test_both_capped_native_reports_cannot_hide_a_complete_origin_loss(tmp_path, monkeypatch):
    reference, candidate = tmp_path / "reference", tmp_path / "candidate"
    board = project(reference, count=511, joined=False)
    fp = next(fp for fp in board.GetFootprints() if fp.GetReference() == "J511")
    pads = list(fp.Pads())
    track = pcbnew.PCB_TRACK(board)
    track.SetStart(pads[0].GetPosition())
    track.SetEnd(pads[1].GetPosition())
    track.SetLayer(pcbnew.F_Cu)
    track.SetWidth(300_000)
    track.SetNet(pads[0].GetNet())
    board.Add(track)
    pcbnew.SaveBoard(str(reference / BOARD), board, aSkipSettings=True)
    shutil.copytree(reference, candidate)
    board = pcbnew.LoadBoard(str(candidate / BOARD))
    board.Remove(next(iter(board.GetTracks())))
    pcbnew.SaveBoard(str(candidate / BOARD), board, aSkipSettings=True)
    monkeypatch.setattr(common, "_out", tmp_path / "out")
    common.ensure_kicad_config()
    before, info = cli.baseline_drc(reference, BOARD, "over-499")
    after, _ = cli.kicad_drc(candidate / BOARD, tmp_path / "after", "candidate", refill_and_save=True)
    reference_components = cli.baselinecache.terminal_facts(info["report"], info["identity"])
    candidate_components, _ = native_components.extract(candidate / BOARD, tmp_path / "candidate-components.json")
    assert len(before["unconnected_items"]) == len(after["unconnected_items"]) == 499
    assert sum(len(groups) - 1 for groups in reference_components["nets"].values()) == 510
    assert sum(len(groups) - 1 for groups in candidate_components["nets"].values()) == 511
    check = cli.provenance.net_completion(reference_components, candidate_components)
    assert check["status"] == common.FAIL
    assert check["nets_connected_in_input"] == 1 and check["nets_lost"] == ["N0510"]


@pytest.mark.parametrize("damage", ["missing", "partial", "stale_board", "stale_native", "missing_jumpers", "root_list"])
def test_incomplete_or_stale_terminal_coverage_is_unknown(damage):
    from component_fixtures import component_receipt
    before = component_receipt({"N": [["a", "b"]]})
    after = component_receipt({"N": [["a", "b"]]})
    if damage == "missing":
        after = None
    elif damage == "partial":
        after["nets"]["N"] = [["a"]]
    elif damage == "stale_board":
        after["identity"]["board_sha256"] = "f" * 64
    elif damage == "stale_native":
        after["identity"]["pcbnew"] = None
    elif damage == "missing_jumpers":
        before["jumpers"] = after["jumpers"] = {}
    else:
        after = ["corrupt"]
    assert cli.provenance.net_completion(before, after)["status"] == common.NOT_RUN


@pytest.mark.parametrize("damage", ["native_version_only", "missing_module_hash", "native_string", "cli_path_only",
                                   "cli_string", "rules_list", "short_board_hash", "numeric_board_hash",
                                   "footprint_list", "number_list"])
def test_matching_malformed_identities_cannot_establish_connectivity(damage):
    from component_fixtures import component_receipt
    before = component_receipt({"N": [["a", "b"]]})
    after = component_receipt({"N": [["a", "b"]]})
    for receipt in (before, after):
        if damage == "native_version_only":
            receipt["native_identity"] = receipt["identity"]["pcbnew"] = {"version": "10.0.7"}
        elif damage == "missing_module_hash":
            del receipt["native_identity"]["module"]["sha256"]
        elif damage == "native_string":
            receipt["native_identity"] = receipt["identity"]["pcbnew"] = "not-a-native-identity"
        elif damage == "cli_path_only":
            receipt["identity"]["kicad_cli"] = {"path": "/usr/bin/kicad-cli"}
        elif damage == "cli_string":
            receipt["identity"]["kicad_cli"] = "not-a-cli-identity"
        elif damage == "rules_list":
            receipt["identity"]["rules"] = ["not-rules"]
        elif damage in ("short_board_hash", "numeric_board_hash"):
            value = "x" if damage == "short_board_hash" else 7
            receipt["identity"]["board_sha256"] = value
            receipt["board_sha256_before"] = receipt["board_sha256_after"] = value
        else:
            receipt["inventory"]["a"]["footprint" if damage == "footprint_list" else "number"] = []
            receipt["inventory_sha256"] = common.sha256_of(receipt["inventory"])
    assert cli.provenance.net_completion(before, after)["status"] == common.NOT_RUN


@pytest.mark.parametrize("source_reasons", [None, 7])
def test_malformed_native_child_reasons_return_unknown(tmp_path, monkeypatch, source_reasons):
    from component_fixtures import component_receipt
    board, output = tmp_path / BOARD, tmp_path / "components.json"
    board.write_text("unchanged input")
    def malformed_child(argv, cwd, log_path, timeout):
        facts = component_receipt({"N": [["a", "b"]]}, board_sha256=common.sha256(board))
        facts.pop("identity")
        facts["reasons"] = source_reasons
        common.write_json(Path(argv[-1]), facts)
        return {"exit_code": 0}
    monkeypatch.setattr(common, "run_cmd", malformed_child)
    facts, _ = native_components.extract(board, output)
    assert facts["status"] == common.NOT_RUN
    assert "malformed native terminal reasons" in facts["reasons"]
    assert board.read_text() == "unchanged input"
