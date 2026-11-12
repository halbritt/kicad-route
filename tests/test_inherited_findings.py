"""A finding the input's own footprint causes is reported, not counted against the routed board.

Found on a designer's two-layer board: a SOT-89 footprint draws its tab as a copper polygon
without a net, over pad 2. KiCad reports the pad against the polygon on the unrouted input.
Every track that reaches pad 2 lies in the polygon as well, so a complete, otherwise clean
routing came back with two new clearance errors that no route to that pad can avoid.
"""
from kicad_route import cli as route

POLYGON = {"description": "Polygon [<no net>] of U2 on F.Cu", "uuid": "poly"}
PAD = {"description": "Pad 2 [Net-(C1-Pad1)] of U2 on F.Cu", "uuid": "pad2"}
KNOWN = {"Net-(C1-Pad1)", "/OTHER", "GND"}


def clearance(*items, actual="0.0000"):
    return {"type": "clearance", "severity": "error", "items": list(items),
            "description": f"Clearance violation (clearance 0.1270 mm; actual {actual} mm)"}


def track(net, uuid):
    return {"description": f"Track [{net}] on F.Cu, length 5.2326 mm", "uuid": uuid}


BASE = {"violations": [clearance(PAD, POLYGON)]}


def test_the_inputs_own_report_names_the_netless_copper_and_the_net_of_its_pad():
    assert route.input_joined_copper(BASE, KNOWN) == {"poly": {"Net-(C1-Pad1)"}}


def test_a_track_of_the_pads_net_in_that_copper_is_set_aside_and_every_other_finding_stays():
    same_net = clearance(track("Net-(C1-Pad1)", "t1"), POLYGON)
    short = {"type": "shorting_items", "severity": "error", "items": [track("Net-(C1-Pad1)", "t2"), POLYGON],
             "description": "Items shorting two nets (nets Net-(C1-Pad1) and <no net>)"}
    other_net = clearance(track("/OTHER", "t3"), POLYGON)
    near_miss = clearance(track("Net-(C1-Pad1)", "t4"), POLYGON, actual="0.0500")
    elsewhere = clearance(track("Net-(C1-Pad1)", "t5"), track("GND", "t6"))
    kept, inherited = route.split_inherited([same_net, short, other_net, near_miss, elsewhere],
                                            route.input_joined_copper(BASE, KNOWN), KNOWN)
    assert inherited == [same_net, short]
    assert kept == [other_net, near_miss, elsewhere]


def test_netless_copper_that_only_comes_near_a_pad_in_the_input_explains_nothing():
    base = {"violations": [clearance(PAD, POLYGON, actual="0.0500")]}
    assert route.input_joined_copper(base, KNOWN) == {}
    finding = clearance(track("Net-(C1-Pad1)", "t1"), POLYGON)
    assert route.split_inherited([finding], {}, KNOWN) == ([finding], [])


def test_netless_copper_between_two_nets_in_the_input_is_set_aside_for_neither_alone():
    other_pad = {"description": "Pad 3 [GND] of U2 on F.Cu", "uuid": "pad3"}
    joined = route.input_joined_copper({"violations": [clearance(PAD, POLYGON), clearance(other_pad, POLYGON)]}, KNOWN)
    assert joined == {"poly": {"Net-(C1-Pad1)", "GND"}}
    # each net was in contact with it in the input, so each one's own new copper there is inherited
    kept, inherited = route.split_inherited([clearance(track("GND", "t1"), POLYGON),
                                             clearance(track("/OTHER", "t2"), POLYGON)], joined, KNOWN)
    assert len(inherited) == 1 and len(kept) == 1
