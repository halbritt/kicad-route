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
| `OUT/blocked/<board>/<stamp>/blocked-by.json` | `kicad-route blocked --board`: the full report for a placed board (see [Board mode](#board-mode)) |

They are written only when routing is INCOMPLETE; a complete run has no
`blocked_by` and no `blocked-by.json`. `kicad-route validate` computes them for its
re-grade as well. `kicad-route publish` (and `smoke --publish`) copies
`validate/blocked-by.json` with the rest of a run's evidence.

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

## Board mode

```sh
kicad-route blocked --board B.kicad_pcb           # summary, report under OUT/blocked/<board>/<stamp>/
kicad-route blocked --board B.kicad_pcb --json    # the full report on stdout (logs on stderr)
```

Receipts for a placed board that has not been routed (or is partly routed), with no
backend run, so a placement proposal can be scored before routing it. The board and
its same-stem `.kicad_pro` / `.kicad_dru` (and library tables) are copied; the copy is
refilled and graded by `kicad-cli pcb drc`, its facts are extracted, and every KiCad
unconnected item is analysed (`nets_considered` is every net). The source files are
never written; if the source board changes while it is analysed, the command fails
(exit 1) instead of reporting on a board that no longer exists. All copper is the
input's own (`origin: input`), so there is no `new_copper`, and `source` is `board`
with `boards.candidate_sha256` null. Exit 0 when the report was written, 1 when DRC or
the facts could not be produced.

Measured on fome-mc's board (492 footprints, 4 copper layers; KICADROUTE-9, rung 4),
on a shared host at load average ~130:

| Board | Open items | Footprints in the report | Wall time | Report |
|---|---|---|---|---|
| as committed (routed) | 0 | 0 | 34.6 min (3.8 min CPU; the zone refill and DRC of the routed board) | 12 KB |
| every track and via removed (placed, unrouted) | 499 | 343 | 3.2 min (refill ~100 s, DRC ~50 s, facts ~15 s, corridors ~25 s) | 2.1 MB |

Per-layer blocker lists are cut to 25 entries (the counts stay exact) and every
ranking list to 50, so the report grows with the number of open connections (about
4 KB each), not with the board's density.

## Schema (`kicad-route/blocked/2`)

`schemas/blocked-by.schema.json` (JSON Schema 2020-12) is the contract for
`blocked-by.json`; its `$defs/summary` is `result.json` → `routing.blocked_by`. A
consumer (agent-ee's placement checks) should check `schema` and treat the file as a
hashed input. Fields beyond what the receipts above describe:

| Field | Meaning |
|---|---|
| `schema`, `version` | `kicad-route/blocked/2`, `2`. Version 1 reports (before KICADROUTE-9) had only `version: 1`, endpoints without footprint/pad/layers, and no `footprints`, `boards` or `nets_considered` |
| `source` | `run` (a routing attempt's candidate) or `board` (`blocked --board`) |
| `heuristic`, `method` | always `true`, and the corridor method in one sentence: the data says it is not a proof |
| `boards` | sha256 of `input_sha256` (the attempt's input snapshot, or the `--board` file), `candidate_sha256` (the delivered candidate; null in board mode) and `graded_sha256` (the board KiCad's DRC read, after zone refill) |
| `nets_considered` | nets whose unconnected items were analysed: the run's scope (validation), its open in-scope nets (`blocked RUN_DIR`), or every net (board mode) |
| `connections[].index` | the connection's position in `connections`; `footprints` refers to connections by it |
| `connections[].endpoints[]` | `kind`, `net`, `footprint` and `pad` (for a pad endpoint: the same-net pad at the reported position, the footprint the DRC text names when pads are stacked; null otherwise, and a pad that is not found carries a `note` instead of a guess), `layers` (the item's copper layers), `position_mm` (where the corridors end), `reported_position_mm` (KiCad's position), and for a zone endpoint `target` (the pad or via the corridors go to) |
| `footprints.<ref>` | every footprint that blocks or terminates an open connection: `position_mm`, `orientation_deg`, `layer` and `side` (`front`/`back`), `locked`, `value`; `blocks` (connections its pads block on their best layer), `blocks_any_layer`, `terminates` (connections with an endpoint or zone target on its pads), `pads_blocking`, `nets_blocked`, `nets_terminated` |
| `layers.<L>.hard` / `.soft` | at most 25 entries each; `hard_count` / `soft_count` are exact |

Coordinates are KiCad board millimetres (y grows downwards); orientations are degrees
counter-clockwise as KiCad shows them.

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
