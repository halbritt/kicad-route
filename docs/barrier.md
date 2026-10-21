# Collision barriers: `run --barrier-from` and `kicad-route barrier`

A candidate can fail DRC because new copper ran too close to something: a track
0.3748 mm from a GND pad under a 0.4 mm rule, or two new tracks crossing. kicad-route
can route the same input again with a small keepout, a *barrier*, at the spot where
the earlier route ran. The backend then has to go around that spot. The barrier is
removed before grading.

This is the first step toward iterative rewind with barriers on DRC collisions
(KICADROUTE-53). It rewinds by one run: the earlier run's input is the state before its
routing decision. It places one barrier per collision. It does not stack barriers
across several rounds, and it does not rewind per net.

```sh
kicad-route run --backend builtin --nets /SIG_A                       # exit 3: new clearance violation
kicad-route run --backend builtin --nets /SIG_A --barrier-from <run>  # same input, barrier where it collided
kicad-route run ... --barrier-from <run> --barrier-size 0.1       # larger square (default 0.05 mm)
kicad-route barrier <run>                                         # preview: <run>/barrier-preview/
kicad-route barrier <run> --size 0.1                              # preview with the square `--barrier-size` would use
```

## Where a barrier goes

`barrier.plan` reads the earlier run's `validate/new-findings.json`, the findings the
routing introduced. When the run had no baseline, it reads
`validate/candidate-drc.json` and records a note. It keeps findings of these types:

`clearance`, `tracks_crossing`, `shorting_items`, `hole_clearance`, `copper_edge_clearance`

A finding is used only when one of its items is **new copper**: a track, arc or via of
the candidate that matches nothing in the reference. This is the same geometric match
that `kicad-route spokes` uses on `facts/reference-facts.json` and
`validate/candidate-facts.json`. The plan skips findings between existing items and
findings with only one item, and records why.

`src/kicad_route/kicad/barrier.py locate` (pcbnew) loads the candidate board and finds
the items by UUID. For every copper layer the new item shares with the other items,
it finds the point on the new item's centreline closest to their copper. For a via,
that point is the via's centre. When the new item overlaps the other copper, the
point is the middle of the overlap. The barrier goes at the closest point over all
shared layers, on that layer. For `copper_edge_clearance`, the other item is an
Edge.Cuts shape, so the barrier goes on the new item's own layer.

On the fixture, the built-in router's `/SIG_A` diagonal passes C1.2 with 0.3748 mm of clearance. Its
barrier is at (111.7125, 104.9875) on F.Cu, 0.4748 mm from the pad: the clearance plus
half the 0.2 mm track width.

## `run --barrier-from RUN_DIR`

The plan is made before the ledger gate, into `<new run>/barrier/`. The barrier points
are part of the attempt: `settings.barrier` holds the source run's input fingerprint,
the size and every point (x, y in nm, layer). A rewind is therefore a different
attempt from the plain run, and repeating the same rewind is refused like any other
repeat (exit 4). If RUN_DIR routed a different input, the barriers keep their board
coordinates and a note says so.

In `run_attempt`, `barrier.around_route` wraps the backend call. It also wraps
`--reserve-spokes`, so the two can be combined:

1. **Inject** into the work input board (`work/input/<project>/<board>`; the reference
   copy is never touched). Each barrier is a square, `--barrier-size` on a side,
   centred on the point. It is a single-layer rule area named
   `kicad-route:barrier:<x>:<y>` (mm) that allows no tracks and no vias; pads,
   footprints and zone fills are allowed. A barrier that touches existing copper on
   its layer (a track, via or pad of any net) is skipped, because it would block that
   copper's own net. A board that already has barrier areas is refused.
2. **Route** with the backend as usual. The built-in router reads rule areas per layer. For
   Freerouting, KiCad's DSN export writes each area as a single-layer Specctra
   `(keepout …)`; the tests check this.
3. **Strip** every `kicad-route:barrier:` rule area from the backend's output board
   before it is copied into the candidate. When nothing was removed, the strip leaves
   the file untouched. Inject followed by strip is byte-identical on the fixture.

The candidate is then graded exactly as without the option. If a barrier is left
over, the `keepouts` preservation check fails, because it compares the rule areas
with the input's. A barrier only takes routing room away from the backend. It never
changes a rule, a severity, a width or a clearance, and grading always uses the
input's own project files.

After grading, `barrier.outcome` reports for each injected barrier:

- `entered_by_new_copper`: new tracks or vias that still touch its square. This should
  be empty; an entry means the backend ignored the keepout.
- `collision_recurred`: whether a new finding of the same type names the same other
  items (by UUID) again.

Recorded:

- `settings.barrier`: `null` when off.
- `process.barrier`: the plan (each barrier with its finding, the new item, the
  distance), `skipped`, `notes`, `injected`, `stripped`, `remaining`, `outcome` and
  the commands that ran.
- Files: `barrier/barrier-{request,located,plan}.json`, `barrier-locate.log`;
  `work/barrier-plan.json`, `barrier-{inject,strip}.log`.

A run with nothing to barrier (no collision made by new copper, or every barrier on
existing copper) is a process failure (exit 1). The backend does not run. A failed
strip is added to `process.reasons` and fails `keepouts`.

## `kicad-route barrier RUN_DIR`

This plans the same barriers and injects them into a copy of the run's reference
project, `RUN_DIR/barrier-preview/<project>/`, without routing. Open that copy in KiCad to see
where the barriers would go. The plan is `RUN_DIR/barrier-preview/barrier-plan.json`
(record `kicad-route/barrier-preview/1`, also printed by `--json`); each preview
replaces the whole `barrier-preview/` directory. Exit status:

- 0: at least one barrier was injected.
- 1: the analysis ran and failed: locating the collisions failed, or every barrier lies
  on existing copper. The plan file is the record (`reasons`).
- 4: refused before the preview, with a refusal record (docs/interface.md): `input`
  when RUN_DIR is not a graded run, has nothing to barrier (no copper collision made
  by new copper, none locatable) or its candidate cannot be loaded by KiCad; `state`
  when the barriers cannot be injected into the preview copy.

## Limits

- **Search chaos.** A barrier forces a detour at one point. It does not make the
  detour local: a rip-up-and-reroute router can change the topology elsewhere and
  make new collisions or leave nets open. Grade the rewind like any run; it is not
  better because it had a barrier. On the fixture, one built-in router rewind on `/SIG_A` moved
  the diagonal by 0.5 mm and passed.
- **The effective size is larger than the square.** Both backends keep their own
  clearance from a rule-area edge, so a 0.05 mm square blocks about one clearance
  plus half a track width around the point.
- **One round.** A rewind that collides somewhere else needs another
  `--barrier-from` on the new run. That run's barriers replace the earlier ones; they
  do not add to them.
- **Not for thermal spokes.** `starved_thermal` has no new item in the finding. Use
  `--reserve-spokes` (docs/spokes.md).
- **Same-net copper is blocked too.** A barrier blocks every net, including the new
  item's own net, at that spot.
