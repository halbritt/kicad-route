"""Decoupling-cell audit (`kicad-route decoupling`): read-only.

A bypass capacitor works when the IC's supply current loop runs pin -> capacitor ->
plane. A pin with its own plane via (or its own pour contact) bypasses the capacitor:
the plane, not the capacitor, feeds the pin first. kicad/decoupling.py (pcbnew) finds
the cells -- two-pin capacitors bridging a power net and a ground-like net within a
radius of an IC's power/ground pin pair -- and classifies each side's copper from
KiCad's own connectivity, on the board with its zones refilled in memory.

- `kicad-route decoupling --board B.kicad_pcb`: OUT/decoupling/<board>/decoupling.json
  and decoupling.md.
- `kicad-route decoupling RUN_DIR`: the run's reference and candidate boards, and the
  cells whose topology the candidate changed: RUN_DIR/decoupling.json and .md.

Never modifies a board: the script never saves, and the board's SHA-256 is checked
before and after (a change fails the command). Advisory only: the result is not
part of a run's verdict. Exit 0 report written, 1 analysis failed. docs/decoupling.md.
"""
import json
import sys
from pathlib import Path

from . import common

SCRIPT = Path(__file__).resolve().parent / "kicad" / "decoupling.py"
TOPOLOGIES = ("respects", "bypassed", "incomplete")


def log(msg):
    print(msg, flush=True)


def analyze(board, out_json, opts, log_path):
    """Run the pcbnew analysis of `board` -> (report or None, command record, reason)."""
    board = Path(board).resolve()
    if not board.is_file():
        return None, None, f"{common.rel(board)}: no such board"
    before = common.sha256(board)
    rec = common.run_cmd([common.PCBNEW_PYTHON, SCRIPT, board, out_json, json.dumps(opts)],
                         board.parent, log_path, timeout=900)
    if common.sha256(board) != before:  # fail closed: the analyzer must never write the board
        return None, rec, f"{common.rel(board)} changed during analysis"
    report = common.load_json(out_json) if rec["exit_code"] == 0 else None
    if report is None:
        return None, rec, f"analysis failed (see {rec['log']})"
    report["board"] = common.rel(board)
    return report, rec, None


def cell_key(cell):
    return f"{cell['ic']}:{cell['power_pin']['pad']}/{cell['ground_pin']['pad']}:{cell['capacitor']['ref']}"


def _side_text(s):
    t = s["topology"]
    if t == "bypassed" and s.get("direct_entries"):
        e = s["direct_entries"][0]
        where = e.get("position_mm") or e.get("start_mm")
        what = e.get("pad") or e["kind"]
        t += f" ({what} at {where[0]:g},{where[1]:g})"
    return t


def markdown(report, title):
    lines = [f"# {title}", "",
             f"Board `{report['board']}`; radius {report['options']['radius_mm']:g} mm; "
             f"power nets {', '.join(report['power_nets']) or '-'}; "
             f"ground nets {', '.join(report['ground_nets']) or '-'}.", "",
             "Cells: " + (", ".join(f"{report['counts'].get(t, 0)} {t}" for t in TOPOLOGIES)) +
             f"; {len(report['unpaired'])} capacitors unpaired.", ""]
    if report["cells"]:
        lines += ["| IC | power pin | ground pin | capacitor | power side | ground side | cell |",
                  "|---|---|---|---|---|---|---|"]
        for c in report["cells"]:
            cap = c["capacitor"]
            amb = " (ambiguous)" if c["ambiguous"] else ""
            lines.append(f"| {c['ic']}{amb} | {c['power_pin']['pad']} {c['power']['net']} "
                         f"| {c['ground_pin']['pad']} {c['ground']['net']} "
                         f"| {cap['ref']} {cap['value']} ({c['power']['distance_mm']:g} / "
                         f"{c['ground']['distance_mm']:g} mm) "
                         f"| {_side_text(c['power'])} | {_side_text(c['ground'])} | **{c['topology']}** |")
    else:
        lines.append("No decoupling cells found.")
    if report["unpaired"]:
        lines += ["", "Unpaired capacitors: " + ", ".join(f"{u['ref']} ({u['power_net']}, {u['ground_net']})"
                                                          for u in report["unpaired"])]
    return "\n".join(lines) + "\n"


def changes(ref, cand):
    """Cells whose topology differs between the reference and the candidate."""
    r = {cell_key(c): c for c in ref["cells"]}
    k = {cell_key(c): c for c in cand["cells"]}
    out = []
    for key in sorted(r.keys() | k.keys()):
        a, b = r.get(key), k.get(key)
        ta, tb = (a or {}).get("topology"), (b or {}).get("topology")
        sides = {s: [(a or {}).get(s, {}).get("topology"), (b or {}).get(s, {}).get("topology")]
                 for s in ("power", "ground")}
        if ta != tb or any(x != y for x, y in sides.values()):
            out.append({"cell": key, "reference": ta, "candidate": tb, "sides": sides})
    return out


def _options(args):
    opts = {"radius_mm": args.radius}
    for k in ("ic", "cap", "power", "ground"):
        if getattr(args, k):
            opts[k] = getattr(args, k)
    return opts


def board_command(board, opts):
    board = Path(board).resolve()
    out = common.out_dir() / "decoupling" / board.stem
    out.mkdir(parents=True, exist_ok=True)
    report, rec, reason = analyze(board, out / "decoupling.json", opts, out / "decoupling.log")
    if report is None:
        log(f"[decoupling] {reason}")
        return 1
    report["command"] = common.public(rec)
    common.write_json(out / "decoupling.json", report)
    (out / "decoupling.md").write_text(markdown(report, f"Decoupling cells: {board.name}"))
    _summary(report)
    log(f"[decoupling] {common.rel(out / 'decoupling.json')}  {common.rel(out / 'decoupling.md')}")
    return 0


def run_command(run_dir, opts):
    run_dir = Path(run_dir).resolve()
    attempt = common.load_json(run_dir / "attempt.json")
    if attempt is None:
        log(f"[decoupling] {run_dir}: not a routing run (attempt.json missing)")
        return 1
    ddir = run_dir / "decoupling"
    ddir.mkdir(exist_ok=True)
    reports = {}
    for role, sub in (("reference", attempt["reference_dir"]), ("candidate", attempt["candidate_dir"])):
        board = run_dir / sub / attempt["board"]
        report, rec, reason = analyze(board, ddir / f"{role}.json", opts, ddir / f"{role}.log")
        if report is None:
            log(f"[decoupling] {role}: {reason}")
            return 1
        report["command"] = common.public(rec)
        reports[role] = report
    changed = changes(reports["reference"], reports["candidate"])
    result = {"kind": "decoupling-run", "run_dir": common.rel(run_dir), **reports, "changed": changed}
    common.write_json(run_dir / "decoupling.json", result)
    md = markdown(reports["candidate"], f"Decoupling cells: candidate of {run_dir.name}")
    md += "\n## Changed by the candidate\n\n" + ("\n".join(
        f"- `{c['cell']}`: {c['reference']} -> {c['candidate']} (power {c['sides']['power'][0]} -> "
        f"{c['sides']['power'][1]}, ground {c['sides']['ground'][0]} -> {c['sides']['ground'][1]})"
        for c in changed) or "None.") + "\n"
    (run_dir / "decoupling.md").write_text(md)
    _summary(reports["candidate"], "candidate ")
    log(f"[decoupling] cells changed by the candidate: {len(changed)}")
    log(f"[decoupling] {common.rel(run_dir / 'decoupling.json')}  {common.rel(run_dir / 'decoupling.md')}")
    return 0


def _summary(report, prefix=""):
    log(f"[decoupling] {prefix}{len(report['cells'])} cells: " +
        ", ".join(f"{report['counts'].get(t, 0)} {t}" for t in TOPOLOGIES) +
        f"; {len(report['unpaired'])} of {report['capacitors']} power/ground capacitors unpaired")


def add_parser(sub):
    dp = sub.add_parser("decoupling", help="read-only audit: does each bypass capacitor sit between its IC pins "
                                           "and the plane (docs/decoupling.md)")
    dp.add_argument("run_dir", nargs="?", help="routing run: audit its reference and candidate "
                                               "(writes RUN_DIR/decoupling.json and .md)")
    dp.add_argument("--board", help="instead of a run: audit this board (OUT/decoupling/<board>/)")
    dp.add_argument("--radius", type=float, default=5.0, metavar="MM",
                    help="max pad-centre distance from a capacitor pad to its IC pin (default 5)")
    dp.add_argument("--ic", nargs="+", metavar="PATTERN", help="IC reference patterns (default U* IC*)")
    dp.add_argument("--cap", nargs="+", metavar="PATTERN", help="capacitor reference patterns (default C*)")
    dp.add_argument("--power", nargs="+", metavar="PATTERN",
                    help="power net name patterns (replace the built-in power-net rule)")
    dp.add_argument("--ground", nargs="+", metavar="PATTERN",
                    help="ground net name patterns (replace the built-in ground-net rule)")
    return dp


def cmd(args):
    if args.radius <= 0:
        sys.exit("kicad-route decoupling: --radius must be positive")
    if args.board and args.run_dir:
        sys.exit("kicad-route decoupling: pass RUN_DIR or --board, not both")
    if args.board:
        return board_command(args.board, _options(args))
    if not args.run_dir:
        sys.exit("kicad-route decoupling: pass RUN_DIR or --board")
    return run_command(args.run_dir, _options(args))
