"""Repeat check: route one input twice with the same backend build and compare.

`kicad-route determinism` (and `update`, after smoke) routes the input (default:
the bundled fixture) N times with the ledger off and compares three layers:

    backend_output_bytes   sha256 of the board the backend handed back
    candidate_bytes        sha256 of the delivered candidate (after KiCad's refill and save)
    copper_geometry        evidence.copper_signature of the candidate's facts

plus the outcome (process, routing, validation, unconnected count). The run is
reproducible when every run produced a candidate and copper geometry and outcome
agree. Byte differences alone are expected: KiCad writes fresh UUIDs and KRT's item
order varies, so they are reported, not failed. A run that stopped at its budget
or stall limit stopped on the wall clock, so it cannot prove anything: the check
reports reproducible false with that reason.

The record goes to OUT/determinism/<stamp>/determinism.json; with --record it is
also stored in STATE/manifest.json under determinism.<backend>, which is what
backend.reproducible in result.json reads.
"""
import os
from pathlib import Path

from . import common
from . import contract
from . import statemanifest

SCHEMA = "kicad-route/determinism/1"
LAYERS = ("outcome", "copper_geometry", "candidate_bytes", "backend_output_bytes")


def wall_clock_stops(process):
    return sorted({flag for command in [process, *(process.get("commands") or [])]
                   for flag in ("budget_reached", "stalled", "killed") if command.get(flag)})


def layer_record(result):
    """The comparable layers of one finished run's result.json."""
    ev = result.get("evidence") or {}
    sig = ev.get("candidate_copper_signature") or {}
    proc = result.get("process") or {}
    # Legacy evidence may name pre-refill bytes. Validation has carried the
    # final candidate digest since /1; prefer it when reconstructing old runs.
    final_hash = (result.get("validation") or {}).get("candidate_board_sha256")
    if final_hash is None and ev.get("candidate_hash_basis") == "post-refill":
        final_hash = ev.get("candidate_board_sha256")
    return {
        "run_dir": result.get("run_dir"), "exit_code": result.get("exit_code"),
        "outcome": {"process": proc.get("status"), "routing": (result.get("routing") or {}).get("status"),
                    "validation": (result.get("validation") or {}).get("status"),
                    "unconnected_in_scope": (result.get("routing") or {}).get("unconnected_in_scope")},
        "wall_clock_stop": bool(wall_clock_stops(proc)),
        "backend_output_bytes": ev.get("backend_output_board_sha256"),
        "candidate_bytes": final_hash,
        "copper_geometry": sig.get("sha256"),
        "copper_items": {"tracks": sig.get("tracks"), "vias": sig.get("vias")} if sig else None,
    }


def compare(records):
    """Verdict over the layer records of repeated runs."""
    same = {layer: len({repr(r[layer]) for r in records}) == 1 for layer in LAYERS}
    reasons = []
    if len(records) < 2:
        reasons.append("fewer than two runs")
    if any(r["copper_geometry"] is None for r in records):
        reasons.append("a run produced no gradable candidate")
    if any(r["wall_clock_stop"] for r in records):
        reasons.append("a run stopped at its budget or stall limit (wall clock): raise --budget")
    if not same["outcome"]:
        reasons.append("outcomes differ")
    if not same["copper_geometry"]:
        reasons.append("copper geometry differs")
    differs_at = next((layer for layer in LAYERS if not same[layer]), None)
    return {"reproducible": not reasons, "reasons": reasons, "same": same, "differs_at": differs_at,
            "bytes_note": None if same["candidate_bytes"] else
            "candidate bytes differ while geometry is compared: KiCad UUIDs and item order"}


def check(cli, backend="krt", runs=2, budget=180, dir=None, board=None, backend_env=None,
          default_clearance=None, record=False):
    """Route the input `runs` times (ledger off, in this process) and compare."""
    common.ensure_kicad_config()
    stamp = common.now_utc().strftime("%Y%m%dT%H%M%S.%fZ")
    results = []
    saved = os.environ.get("KICAD_ROUTE_LEDGER")
    os.environ["KICAD_ROUTE_LEDGER"] = "off"  # the repeats are the point
    try:
        for i in range(runs):
            a = cli.run_namespace(backend, dir=str(dir or cli.FIXTURE), board=board, budget=budget,
                                  label=f"determinism-{backend}-{i + 1}", backend_env=backend_env or [],
                                  default_clearance=default_clearance)
            results.append(cli.run_attempt(a))
    finally:
        if saved is None:
            os.environ.pop("KICAD_ROUTE_LEDGER", None)
        else:
            os.environ["KICAD_ROUTE_LEDGER"] = saved
    layers = [layer_record(r) for r in results]
    verdict = compare(layers)
    executed = [statemanifest.execution_build_id(r.get("tool_identity"), backend) for r in results]
    same_build = bool(executed) and None not in executed and len(set(executed)) == 1
    verdict["same"]["execution_build"] = same_build
    if not same_build:
        verdict["reproducible"] = False
        verdict["reasons"].append("missing execution build identity" if None in executed or not executed
                                  else "executed builds differ between repeats")
        verdict["differs_at"] = verdict["differs_at"] or "execution_build"
    first = results[0] if results else {}
    rec = {"schema": SCHEMA, "backend": backend, "verified_utc": common.now_utc().isoformat(),
           "build_id": executed[0] if same_build else None,
           "build": (first.get("tool_identity") or {}).get("backend"),
           "kicad_cli": ((first.get("tool_identity") or {}).get("kicad_cli") or {}).get("version"),
           "input": {"dir": common.rel(Path(dir or cli.FIXTURE)),
                     "fingerprint": (first.get("input") or {}).get("fingerprint")},
           "settings": {"runs": runs, "budget_s": budget, "backend_env": backend_env or [],
                        "default_clearance_mm": default_clearance},
           **verdict, "runs": layers}
    out = common.out_dir() / "determinism" / stamp
    common.write_json(out / "determinism.json", rec)
    rec_path = out / "determinism.json"
    if record:
        try:
            statemanifest.record_determinism(rec)
        except (OSError, RuntimeError) as error:
            rec["record_error"] = str(error)
        else:
            rec["recorded_in"] = str(statemanifest.manifest_path())
        common.write_json(rec_path, rec)
    return rec, rec_path


def cmd(args, cli):
    env = list(args.backend_env or [])
    if any("=" not in e for e in env):
        contract.refuse("usage", "--backend-env takes KEY=VALUE")
    rec, path = check(cli, args.backend, args.runs, args.budget, args.dir, args.board, env,
                      args.default_clearance, args.record)
    for r in rec["runs"]:
        print(f"[determinism] {r['run_dir']}: exit {r['exit_code']}, geometry {str(r['copper_geometry'])[:12]}, "
              f"candidate {str(r['candidate_bytes'])[:12]}, backend output {str(r['backend_output_bytes'])[:12]}",
              flush=True)
    print(f"[determinism] same: " + ", ".join(f"{k} {'yes' if v else 'no'}" for k, v in rec["same"].items()),
          flush=True)
    print(f"[determinism] {args.backend}: {'REPRODUCIBLE' if rec['reproducible'] else 'NOT REPRODUCIBLE'}"
          + (f" ({'; '.join(rec['reasons'])})" if rec["reasons"] else ""), flush=True)
    if rec.get("record_error"):
        print(f"[determinism] not recorded: {rec['record_error']}", flush=True)
    elif rec.get("recorded_in"):
        print(f"[determinism] recorded in {rec['recorded_in']}", flush=True)
    if args.publish:
        dest = Path(args.publish)
        dest.mkdir(parents=True, exist_ok=True)
        name = f"{args.backend}-{(rec.get('build') or {}).get('version') or 'unknown'}.json"
        common.write_json(dest / name, rec)
        print(f"[determinism] published {common.rel(dest / name)}", flush=True)
    print(f"[determinism] {common.rel(path)}", flush=True)
    if rec.get("record_error"):
        return 1
    return 0 if rec["reproducible"] else 3


def add_parser(sub):
    p = sub.add_parser("determinism", help="route one input repeatedly with one backend build and compare "
                                           "the candidates (copper geometry, bytes, outcome)")
    p.add_argument("--backend", default="krt", choices=["krt", "freerouting"])
    p.add_argument("--runs", type=int, default=2, help="number of runs (default 2)")
    p.add_argument("--budget", type=int, default=180, help="wall-clock seconds per run (default 180)")
    p.add_argument("--dir", help="project directory (default: the bundled fixture)")
    p.add_argument("--board", help="board file name inside --dir")
    p.add_argument("--default-clearance", type=float, metavar="MM", help="as for run")
    p.add_argument("--backend-env", action="append", metavar="KEY=VALUE", help="as for run (e.g. a forced difference)")
    p.add_argument("--record", action="store_true",
                   help="store the record in STATE/manifest.json (what backend.reproducible reads)")
    p.add_argument("--publish", metavar="DIR", help="also copy the record to DIR/<backend>-<version>.json")
    return p
