"""Explicit terminal populations for tests whose subject is grading, not native extraction."""
from kicad_route import common, native_components


def component_receipt(nets, *, board_sha256="a" * 64, inventory=None):
    if inventory is None:
        inventory = {uid: {"net": net, "number": uid, "footprint": f"fp:{net}"}
                     for net, groups in nets.items() for group in groups for uid in group}
    native = {"version": "test-native", **{key: {"path": key, "sha256": "b" * 64}
                                          for key in ("module", "binding", "python")}}
    jumpers = {p["footprint"]: {"duplicate_numbers": False, "groups": []} for p in inventory.values()}
    return {"schema": native_components.SCHEMA, "status": common.PASS, "reasons": [],
            "inventory": inventory, "inventory_sha256": common.sha256_of(inventory),
            "nets": nets, "jumpers": jumpers, "jumpers_sha256": common.sha256_of(jumpers), "native_identity": native,
            "board_sha256_before": board_sha256, "board_sha256_after": board_sha256,
            "identity": {"board_sha256": board_sha256, "producer": native_components.producer_identity(),
                         "rules": {"test.kicad_pro": "c" * 64}, "pcbnew": native,
                         "kicad_cli": {"sha256": "d" * 64}}}
