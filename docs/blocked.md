# Blocked-by receipts

When an attempt leaves in-scope connections open (`routing.status` INCOMPLETE),
`unrouted_nets` says *what* failed but not *what is in the way*. The blocked-by
receipts fill that gap: for every open connection they list the obstacles that lie
across the obvious paths between its endpoints, per copper layer, and rank the
obstacles across all open connections. Code: `src/kicad_route/blocked.py`.

It is diagnosis only. It reads the candidate's native DRC report and the board facts
that validation already extracted, writes one JSON file, and never changes a board or
a rule.

## Outputs

| Where | What |
|---|---|
| `validate/blocked-by.json` (or `revalidate*/<stamp>/blocked-by.json`) | full report: every open connection, per-layer blockers, rankings per category |
| `result.json` → `routing.blocked_by` | summary: the top 10 blockers, and per net its best layer, hard-blocker count and first few blockers |
| `RESULT.md` | the top-blocker table and one line per net |

They are written only when routing is INCOMPLETE; a complete run has no
`blocked_by` and no `blocked-by.json`. `kicad-route validate` computes them for its
re-grade as well.

```sh
kicad-route blocked <run_dir>                  # (re)compute for an existing run (validate/)
kicad-route blocked <run_dir>/revalidate/<stamp>   # or for a re-grade
kicad-route blocked <run_dir> --top 20
```

`blocked` rewrites `blocked-by.json`, updates `routing.blocked_by` in that
directory's `result.json` (and the run's `RESULT.md`), and prints the summary. It
works on runs made before this feature, as long as `validate/candidate-facts.json`
exists. For a `revalidate-origin` directory, copper origin is judged against the
chain's first input when the `against` path still resolves; otherwise against this
run's input (noted in the summary).

## Method

For each KiCad unconnected item on an in-scope net:

1. **Endpoints**: the two item positions KiCad reports. When one side is a zone,
   KiCad's position is the zone's anchor, not a useful target; the corridor goes
   instead to the nearest same-net pad or via inside the zone outline that is not on
   the other endpoint's copper island (islands are grown by geometric contact of
   same-net tracks, vias and pads; zone fills are not modelled). The endpoint's
   `note` says when this substitution happened.
2. **Corridors**: a straight band and the two L-shaped (Manhattan) bands between the
   endpoints, each `track_width + 2 × clearance` of the net's effective class wide
   (KiCad Default 0.2/0.2 mm when the class has no value). For each copper layer the
   band with the fewest hard blockers is kept (`corridor`, with all three counts under
   `corridors`).
3. **Obstacles** on that layer, never of the connection's own net:
   - hard: tracks and arcs, vias (through vias on every layer, blind/buried on their
     span), pads (rectangles of the pad size, turned by the footprint orientation),
     rule areas that forbid tracks, the board outline;
   - soft: copper zones of other nets (fills give way to tracks) and rule areas that
     forbid only vias.
   Copper carries `locked` (the design's own lock flag) and `origin`: `input` when the
   item is in the attempt's input board (same uuid, or same net, layer and geometry
   within 1 µm), `new` when the backend placed it in this attempt.
4. **Best layer**: the layer with the fewest hard blockers (then fewest soft ones).

**Ranking** (`ranking` in the report): each obstacle is credited with the open
connections it blocks on their best layer (`blocks`) and on the best corridor of any
layer (`blocks_any_layer`). Lists are kept per category, and copper is also summed
per net (`input_copper_by_net`, `new_copper_by_net`). Each list keeps 50 entries.

| Category | Means for the next attempt |
|---|---|
| `keepout` | a design constraint: route around it (another layer, another order), never edit it |
| `input_copper` | the input's own copper. With `--protect existing` (default) it is frozen for the attempt, whatever its lock flag. Freeing unlocked items needs `--protect locked`; moving locked ones needs the owner |
| `new_copper` | the backend's own output: cheap to rip. Route the blocked nets first (`--nets`), on the original input |
| `footprint` | other nets' pads, aggregated per footprint: placement is fixed; change layer or approach |
| `board_edge` | a corridor leaves a concave outline |

`unobstructed_connections` counts connections whose best corridor is free. For those
the problem is elsewhere: congestion along a detour, via rules, or the backend's
search or budget.

## Heuristic, not proof

The straight and L-shaped corridors approximate where a route would go. A real router
detours. An obstacle on the corridor may be easy to go around, and a free corridor
does not mean a route exists (the detour around the next obstacle may be closed, or
the needed vias may not fit). Read the ranking as a pointer for the next attempt:
which nets to route first, which layer to prefer, and whether the blocker is the
backend's own copper, the input's copper or a constraint. It is not a routability
verdict, and only KiCad's DRC grades a candidate.

Known approximations:

- Pads use the footprint orientation only, not a pad's own rotation. Rounded,
  circular and custom pad shapes are treated as their bounding rectangle.
- Zones are their outlines, not their fills. A zone of another net counts as soft
  even where its fill would leave no room.
- Clearances are the connection's net-class clearance. Custom `.kicad_dru` rules,
  class-to-class rules, and the obstacle's own (possibly larger) clearance are not
  applied. Edge clearance uses the same value.
- Rule areas inside footprints are not in the board facts and are not seen.
- A layer change needs a via, and that cost is not modelled. Each layer is judged
  as if the whole connection could run on it.
