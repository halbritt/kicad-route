"""Source-bound native terminal evidence on exact saved/refilled board geometry."""
import json
import subprocess
from pathlib import Path

from . import common, statemanifest

SCHEMA = 1
FACTS_NAME = "baseline-terminals.json"
KICAD_SCRIPT = Path(__file__).parent / "kicad" / "native_components.py"


def native_identity():
    try:
        proc = subprocess.run([common.PCBNEW_PYTHON, KICAD_SCRIPT, "--identity"],
                              capture_output=True, text=True, timeout=120, env=common.tool_env())
    except (OSError, subprocess.TimeoutExpired):
        return None
    try:
        return json.loads(proc.stdout) if proc.returncode == 0 else None
    except ValueError:
        return None


def producer_identity():
    root = Path(__file__).parent
    return {name: common.sha256(root / name) for name in
            ("native_components.py", "kicad/native_components.py", "kicad/facts.py")}


def rules_identity(board):
    board = Path(board)
    return {board.with_suffix(ext).name: common.sha256(board.with_suffix(ext))
            if board.with_suffix(ext).is_file() else None for ext in (".kicad_pro", ".kicad_dru")}


def coverage(facts):
    """Validate the complete native partition against the independently parsed inventory."""
    if not isinstance(facts, dict) or facts.get("schema") != SCHEMA or facts.get("status") != common.PASS:
        return False
    inventory, nets = facts.get("inventory"), facts.get("nets")
    if "identity" in facts and not isinstance(facts["identity"], dict):
        return False
    if not isinstance(inventory, dict) or not isinstance(nets, dict) or not isinstance(facts.get("jumpers"), dict):
        return False
    jumpers = facts["jumpers"]
    if any(not isinstance(pad, dict) or pad.get("footprint") not in jumpers for pad in inventory.values()):
        return False
    for setting in jumpers.values():
        if not isinstance(setting, dict) or type(setting.get("duplicate_numbers")) is not bool:
            return False
        groups = setting.get("groups")
        if (not isinstance(groups, list) or any(not isinstance(g, list) or not g
                or any(not isinstance(p, str) for p in g) or g != sorted(set(g)) for g in groups)):
            return False
    if facts.get("jumpers_sha256") != common.sha256_of(jumpers):
        return False
    if facts.get("inventory_sha256") != common.sha256_of(inventory):
        return False
    expected = {}
    for uid, pad in inventory.items():
        if not isinstance(uid, str) or not isinstance(pad, dict) or not isinstance(pad.get("net"), str):
            return False
        if pad["net"]:
            expected.setdefault(pad["net"], set()).add(uid)
    if set(nets) != set(expected):
        return False
    for name, groups in nets.items():
        if not isinstance(groups, list):
            return False
        seen = set()
        for group in groups:
            if not isinstance(group, list) or not group or any(not isinstance(uid, str) for uid in group):
                return False
            group_set = set(group)
            if len(group_set) != len(group) or seen & group_set:
                return False
            seen.update(group_set)
        if seen != expected[name]:
            return False
    return True


def extract(board, out, expected_sha256=None):
    """Native process boundary; missing, changed or unsupported coverage is NOT_RUN."""
    board, out = Path(board).resolve(), Path(out).resolve()
    identity = {"board_sha256": common.sha256(board), "rules": rules_identity(board),
                "producer": producer_identity(), "kicad_cli": statemanifest.kicad_cli_identity()}
    rec = common.run_cmd([common.PCBNEW_PYTHON, KICAD_SCRIPT, board, out], board.parent,
                         out.with_suffix(".log"), timeout=1800)
    facts = common.load_json(out) if rec["exit_code"] == 0 else None
    if not isinstance(facts, dict):
        facts = None
    reasons = list((facts or {}).get("reasons", []))
    if not coverage(facts):
        reasons.append("missing or incomplete native terminal coverage")
    if (common.sha256(board) != identity["board_sha256"]
            or rules_identity(board) != identity["rules"] or producer_identity() != identity["producer"]
            or statemanifest.kicad_cli_identity() != identity["kicad_cli"]
            or identity["board_sha256"] != (facts or {}).get("board_sha256_before")
            or identity["board_sha256"] != (facts or {}).get("board_sha256_after")
            or (expected_sha256 is not None and identity["board_sha256"] != expected_sha256)):
        reasons.append("native terminal evidence changed or differs from the graded board")
    facts = facts or {"schema": SCHEMA}
    facts.update(status=common.NOT_RUN if reasons else common.PASS, reasons=reasons,
                 identity={**identity, "pcbnew": facts.get("native_identity")})
    common.write_json(out, facts)
    return facts, common.public(rec)


def compatible(reference, candidate):
    """Both partitions cover identical terminal/net populations under the same authority."""
    if not coverage(reference) or not coverage(candidate):
        return False
    for facts in (reference, candidate):
        identity = facts.get("identity", {})
        if (identity.get("producer") != producer_identity()
                or not identity.get("board_sha256")
                or identity["board_sha256"] != facts.get("board_sha256_before")
                or identity["board_sha256"] != facts.get("board_sha256_after")
                or identity.get("pcbnew") != facts.get("native_identity")):
            return False
    if reference["inventory"] != candidate["inventory"] or reference["jumpers"] != candidate["jumpers"]:
        return False
    for key in ("rules", "producer", "pcbnew", "kicad_cli"):
        if not reference.get("identity", {}).get(key) or reference["identity"][key] != candidate.get("identity", {}).get(key):
            return False
    return True
