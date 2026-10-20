"""Pour integrity: compare the copper pours of a refilled reference board with the
refilled candidate (docs/pour.md).

KiCad's DRC passes boards whose pours got electrically worse: new tracks that cut
a ground pour into islands, starve a pad's thermal spokes (only a warning, and
`min_resolved_spokes` may be lowered), or leave a zone-connected pad off the main
pour. The facts come from kicad/pour.py, run on the boards KiCad refilled for
grading; this module turns the reference/candidate pair into the
`pour_integrity` preservation check. It never relaxes anything: it only adds
failures on top of the native grading.

FAIL when, against the reference:
- a zone-connected pad that touched the fill no longer touches it
  (`lost_fill_contact`), or the zone no longer reaches it at all
  (`lost_pad_reach`: the candidate has no record for a pad the reference's zone
  reached and fed);
- a thermal pad whose spokes (fill spokes plus tracks from the pad into the fill,
  as KiCad counts them) were at or above the minimum is now below it (`spokes_below_minimum`); the
  minimum is the project's `min_resolved_spokes`, raised (never lowered) by
  `--pour-min-spokes`;
- a pad on the main pour of its net is now on a fill island cut off from it
  (`isolated_island`);
- with `--pour-area-loss PCT`, a net/layer lost more than PCT % of its fill area
  (`area_loss`). Without it, area loss is reported only.
"""
from pathlib import Path

from . import common

KICAD_SCRIPT = Path(__file__).resolve().parent / "kicad" / "pour.py"
FACTS_NAME = "baseline-pour.json"
CHECK = "pour_integrity"
SCHEMA = 1  # kicad/pour.py's SCHEMA


def extract(board, out):
    """Pour facts of a refilled board (kicad/pour.py with KiCad's Python). Returns (facts, record)."""
    out = Path(out)
    rec = common.run_cmd([common.PCBNEW_PYTHON, KICAD_SCRIPT, board, out], Path(board).parent,
                         out.with_suffix(".log"), timeout=1800)
    facts = common.load_json(out) if rec["exit_code"] == 0 else None
    if facts is not None and facts.get("schema") != SCHEMA:
        facts = None
    return facts, common.public(rec)


def reference_facts(baseline_report, ref_dir, board_name, work, drc):
    """Pour facts of the refilled input, kept beside the cached baseline DRC report.
    A baseline cached before this check existed has none: refill a copy of the
    reference exactly as baseline_drc does (drc = cli.kicad_drc) and cache it."""
    cached = Path(baseline_report).parent / FACTS_NAME if baseline_report else None
    if cached is not None:
        facts = common.load_json(cached)
        if facts is not None and facts.get("schema") == SCHEMA:
            return facts, {"source": str(cached), "cached": True}
    import shutil
    work = Path(work)
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(ref_dir, work / Path(ref_dir).name, symlinks=True)
    board = work / Path(ref_dir).name / board_name
    report, info = drc(board, work / "drc", "baseline-pour", refill_and_save=True)
    if report is None:
        return None, {"source": None, "reasons": info["reasons"]}
    facts, rec = extract(board, work / FACTS_NAME)
    if (facts is not None and cached is not None and cached.parent.is_dir()
            and not (cached.parent / "generation.json").exists()):
        common.write_json(cached, facts)
    shutil.rmtree(work / Path(ref_dir).name, ignore_errors=True)
    return facts, {"source": str(cached) if cached else str(work / FACTS_NAME), "cached": False,
                   "log": rec.get("log")}


def _effective_spokes(p):
    """Spokes as KiCad's starved-thermal test counts them: fill spokes plus tracks into the fill."""
    return float("inf") if p["solid"] else p["spokes"] + p["track_spokes"]


def compare(ref, cand, area_loss_pct=None, min_spokes=None):
    """Return (check, delta): check is a preservation-check record, delta the full report."""
    board_min = int(ref.get("min_resolved_spokes") or 0)
    minimum = max(board_min, int(min_spokes or 0))
    failures, info = [], []
    def off_main(facts, lk):
        return round(sum(p["area_mm2"] for p in facts["polygons"].values()
                         if f"{p['net']}@{p['layer']}" == lk and not p["in_main"]), 4)

    layers = []
    for lk in sorted(set(ref["layers"]) | set(cand["layers"])):
        r, c = ref["layers"].get(lk), cand["layers"].get(lk)
        ra, ca = (r or {}).get("area_mm2", 0.0), (c or {}).get("area_mm2", 0.0)
        pct = round(100.0 * (ca - ra) / ra, 3) if ra else None
        rec = {"net_layer": lk, "area_mm2": [ra, ca], "area_delta_pct": pct,
               "polygons": [(r or {}).get("polygons", 0), (c or {}).get("polygons", 0)],
               "islands_off_main": [(r or {}).get("islands_off_main", 0), (c or {}).get("islands_off_main", 0)],
               "area_off_main_mm2": [off_main(ref, lk), off_main(cand, lk)]}
        layers.append(rec)
        if area_loss_pct is not None and pct is not None and -pct > area_loss_pct:
            failures.append({"kind": "area_loss", "net_layer": lk, "area_mm2": [ra, ca], "delta_pct": pct,
                             "limit_pct": area_loss_pct})

    pads = []
    for key in sorted(set(ref["pads"]) | set(cand["pads"])):
        r, c = ref["pads"].get(key), cand["pads"].get(key)
        if r is None or c is None:
            # The zone reached a connected pad on the reference and does not reach it on the
            # candidate (the zone outline, its net, the pad or its net changed): that pad lost
            # its pour, as surely as lost_fill_contact. A pad reached only on the candidate,
            # or one the reference reached without fill contact, is reported.
            if r is not None and r["connection"] != "none" and r["touches_fill"]:
                failures.append({"kind": "lost_pad_reach", "pad": key, "connection": r["connection"],
                                 "position_mm": r["position_mm"], "touches_fill": [True, False],
                                 "spokes": [r["spokes"], 0], "solid": [r["solid"], False],
                                 "track_spokes": [r["track_spokes"], 0], "in_main": [r["in_main"], False],
                                 "zone": r.get("zone")})
            else:
                info.append({"kind": "pad_reach_changed", "pad": key, "reference": r is not None,
                             "candidate": c is not None})
            continue
        if r["connection"] == "none":
            continue
        rec = {"pad": key, "connection": r["connection"], "position_mm": c["position_mm"],
               "touches_fill": [r["touches_fill"], c["touches_fill"]], "spokes": [r["spokes"], c["spokes"]],
               "solid": [r["solid"], c["solid"]], "track_spokes": [r["track_spokes"], c["track_spokes"]],
               "in_main": [r["in_main"], c["in_main"]], "polygons": c["polygons"],
               "other_path": c["tracks_attached"] > 0 or c["via_spokes"] > 0}
        pads.append(rec)
        if r["touches_fill"] and not c["touches_fill"]:
            failures.append({"kind": "lost_fill_contact", **rec})
            continue
        if r["in_main"] and not c["in_main"]:
            failures.append({"kind": "isolated_island", **rec,
                             "island": [{"id": pid, **{k: cand["polygons"][pid][k] for k in
                                                       ("area_mm2", "bbox_mm", "pads", "vias", "tracks")}}
                                        for pid in c["polygons"] if pid in cand["polygons"]]})
            continue
        es_r, es_c = _effective_spokes(r), _effective_spokes(c)
        if r["connection"] == "thermal" and r["touches_fill"] and es_r >= minimum > es_c:
            failures.append({"kind": "spokes_below_minimum", **rec, "minimum": minimum})
        elif es_c < es_r or (r["solid"] and not c["solid"]):
            info.append({"kind": "spokes_reduced", **rec})

    # Candidate fill islands cut off from the main pour without pads: floating copper,
    # reported (pads on such islands are failures above). The reference's count is in the delta.
    ref_off = sum(v["islands_off_main"] for v in ref["layers"].values())
    for pid, p in sorted(cand["polygons"].items()):
        if not p["in_main"] and not p["pads"]:
            info.append({"kind": "floating_island", "id": pid, "area_mm2": p["area_mm2"], "bbox_mm": p["bbox_mm"],
                         "vias": p["vias"], "tracks": p["tracks"]})

    def area_text(rec):
        pct = rec["area_delta_pct"]
        off = rec["area_off_main_mm2"][1]
        return (f"{rec['net_layer']} {rec['area_mm2'][1]:.1f} mm2 ({'+' if pct and pct > 0 else ''}"
                f"{pct if pct is not None else 'new'} %), {rec['polygons'][1]} polygons"
                + (f", {off:.1f} mm2 off the main pour" if off else ""))

    kinds = common.count_by(failures, "kind")
    detail = (f"{len(ref['zones'])} copper zones, {len(pads)} zone-connected pads compared on the refilled boards "
              f"(min spokes {minimum}" + (f", project {board_min}" if minimum != board_min else "") + ")"
              + (f"; area loss limit {area_loss_pct} %" if area_loss_pct is not None else "")
              + ("; " + "; ".join(area_text(r) for r in layers[:6]) if layers else "")
              + ("; " + ", ".join(f"{n} {k}" for k, n in kinds.items()) if failures else ""))
    if not ref["zones"] and not cand["zones"]:
        detail = "no copper zones"
    status = common.FAIL if failures else common.PASS
    check = {"check": CHECK, "status": status, "detail": detail, "differences": (failures + info)[:40]}
    delta = {"check": CHECK, "status": status, "min_spokes": minimum, "project_min_resolved_spokes": board_min,
             "area_loss_limit_pct": area_loss_pct, "reference_islands_off_main": ref_off,
             "layers": layers, "failures": failures, "informational": info, "pads": pads}
    return check, delta


def missing_check(reason):
    """Fail closed: a pour comparison that could not run is not a pass."""
    return {"check": CHECK, "status": common.FAIL, "detail": f"pour facts unavailable: {reason}", "differences": []}
