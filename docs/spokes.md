# Thermal-spoke corridors: `--reserve-spokes` and `kicad-route spokes`

A pad joined to a same-net pour by thermal relief reaches the pour only through its
spokes. A new track of another net routed close to the pad pushes the pour back by its
clearance. A spoke can then end in nothing, and KiCad drops it. When fewer than the
zone's `min_resolved_spokes` remain, DRC reports `starved_thermal`, but only after
routing. On a real 4-layer board, new F.Cu signal tracks routed 0.1–0.35 mm from GND
pads in an F.Cu GND pour left 24 pads with one spoke (the rule asks for 2). For 22 of
them that spoke was the only ground path.

kicad-route can keep new copper out of the spokes' way while it routes, and it can say
afterwards which new track starved which pad.

```sh
kicad-route run --backend builtin --reserve-spokes            # N = the board's min_resolved_spokes (normally 2)
kicad-route run --backend freerouting --reserve-spokes 1  # one corridor per pad: less blocked area
kicad-route spokes <run_dir>                              # spoke-cut attribution -> <run_dir>/spokes.json
kicad-route spokes <run_dir> --validation <run_dir>/revalidate/<stamp>   # after a hand edit + validate
kicad-route spokes --board my.kicad_pcb [--per-pad N]     # preview: OUT/spokes/<board>/plan.json
kicad-route spokes --board my.kicad_pcb --verify          # + check the corridors against KiCad's filler
```

## Corridors

`src/kicad_route/kicad/spokes.py plan` (pcbnew) refills the zones in memory and never
saves the board. It considers every pad that meets all of these conditions:

- it is on a copper layer of a same-net zone, and the zone outline contains the pad;
- its zone connection resolves to thermal relief. The pad's setting comes first, then
  the footprint's, then the zone's. THT-thermal means plated through-hole pads only.

For each such pad and layer it builds the four spokes the way KiCad's zone filler lays
them out:

- **Direction:** pad orientation + the pad's spoke angle + k·90°. The stock spoke
  angle is 45° for round pads and 90° for all other shapes.
- **Spoke end:** the pad's extent along that direction, plus the relief gap. This is
  the rotated bounding box KiCad builds the spokes in.
- **Gap and spoke width:** the pad's overrides, else the zone's values. The spoke
  width is clamped to the pad's smaller side. KiCad builds no spokes when the spoke
  width is below the zone's min thickness.

Each spoke gets one corridor, a rectangle that starts at the pad edge:

| | |
|---|---|
| length (from the pad's extent) | `gap + min_thickness + clearance + max_error` |
| half-width | `spoke_width / 2 + clearance` |

This is the area that must stay free of other copper so that, after the pour is
pushed back by `clearance`, it still covers the spoke end with `min_thickness` of
copper. `clearance` is the largest value any other net can claim against the pour:
the zone's own clearance, every net class clearance, the board minimum, and every
`clearance` minimum in the project's `.kicad_dru`. A custom rule can only make the
corridor larger, never smaller. On the fixture this is the 0.4 mm Power-to-signal
rule.

Each corridor is classified:

- **live:** the refilled pour covers KiCad's own spoke test point, the spoke end.
  Spokes facing a neighbouring pin usually have no pour there.
- **eligible:** live, and it overlaps no existing track, via or other pad on its
  layer. A rule area over existing copper would block that copper's own net, so these
  corridors are never reserved.
- **selected:** the best `N` eligible corridors per pad, ranked by the most pour
  beyond the gap (sampled up to twice the corridor length), so corridors that point
  into the pour come first. `N` defaults to the board's `min_resolved_spokes`. The
  plan records pads that have fewer than `N` eligible corridors.

## `run --reserve-spokes [N]`

The option takes 0–4; with no value it uses the board's `min_resolved_spokes`. In
`run_attempt`, `spokes.around_route` wraps the backend call:

1. **Plan and reserve** on the work input board
   (`work/input/<project>/<board>`; the reference copy is never touched). Each
   selected corridor becomes a single-layer rule area named
   `kicad-route:spoke:<ref>:<pad>:<layer>:<k>`. The area allows no tracks and no vias;
   pads, footprints and zone fills are allowed. A board that already has such areas
   is refused.
2. **Route** with the backend as usual. The built-in router reads rule areas natively, per layer
   (`add_rule_area_keepout_obstacles`). For Freerouting, KiCad's DSN export writes
   each area as a single-layer Specctra `(keepout …)`; the tests check this.
3. **Strip** every `kicad-route:spoke:` rule area from the backend's output board
   before it is copied into the candidate. When nothing was removed, the strip leaves
   the file untouched. Reserve followed by strip is byte-identical on the fixture.

The candidate is then graded exactly as without the option. If a corridor is left
over, the `keepouts` preservation check fails, because it compares the rule areas
with the input's. Reserving only takes routing room away from the backend. It never
changes a rule, a severity, a width or a clearance, and grading always uses the
input's own project files.

Recorded:

- `settings.reserve_spokes`: `null` = off, `0` = the board's value, or N.
- `process.spoke_reservation`: per pad, live corridors, reserved and stripped counts,
  clearance, warnings and the commands that ran.
- Work files: `work/spokes-plan.json`, `spokes-{plan,reserve,strip}.log`.

### Targeting a native thermal finding

`run --reserve-spoke-pad UUID@F.Cu` reserves corridors only for that exact native
pad and copper layer. Repeat the option for several pads. It implies
`--reserve-spokes` with the board's normal count unless an explicit count is also
given. Native DRC and pour-integrity requirements are unchanged. An absent or
ambiguous pad/layer identity fails before routing; it never becomes an empty
reservation or falls back to all pads.

Use the pad UUID from the native `starved_thermal` finding and its layer, or the
matching entry in `kicad-route spokes RUN_DIR`. This permits a bounded retry to
protect the demonstrated weak connection without reserving unnecessary corridors
around every other pad. It does not establish that the retry will route, and the
final saved board still needs every normal validation check. The original full
plan stays in `work/spokes-plan-all.json`; the selected plan, attempt settings
and process result record the exact targeted identities. Campaign and corpus
replay forward the option through their existing routing-argument handling.

A failed plan or reservation is a process failure (exit 1, no routing). A failed
strip is added to `process.reasons` and fails `keepouts`.

## `kicad-route spokes RUN_DIR`

Plans corridors on the run's reference board (`RUN_DIR/spokes/reference-plan.json`).
Every live corridor, selected or not, is then intersected with the candidate's new
copper, meaning tracks, arcs and vias in `validate/candidate-facts.json` that match
nothing in `facts/reference-facts.json`. The candidate DRC's `starved_thermal` pads
are matched to the plan by pad UUID, or by position and layer when the UUID differs.
`RUN_DIR/spokes.json` contains:

- `corridors_live`, `corridors_crossed`, `pads_with_crossed_corridors`, `crossings`:
  every corridor that new copper entered, with the item's UUID, net and layer.
- `starved_pads`: each starved pad, with `new` (true when the finding is not in the
  input's baseline), `crossed_corridors`, `cutting_items` (UUIDs) and `attributed`.
- `reserved` (runs made with `--reserve-spokes`): the reserved corridors that new
  copper still entered. This should be empty; an entry means the backend ignored a
  keepout.

Exit 0 when there is no new `starved_thermal`; 3 when there is (attributed or not).
Use `--validation DIR` to read a re-validation, for example after editing the
candidate by hand.

## Checking the model: `spokes --board B --verify`

The corridor dimensions are a model. `--verify` tests the model on the board itself,
with KiCad's own zone filler, without saving the board
(`src/kicad_route/kicad/spokes.py verify`; the geometry is the pure function
`corridor_geometry()`). For each selected corridor it puts foreign-net tracks against
the corridor's edge from outside, refills the pad's zone in memory, and reads the fill:

| mode | adversary |
|---|---|
| `end` | one track across the far end: the router passing beyond the spoke end |
| `sides` | one track along each long side: the router passing beside the spoke |

The clearance the filler applies depends on the net pair and on `.kicad_dru`
conditions, so one net of every net class is tried rather than one guessed worst
net. A check holds when all three of these are true:

- **kept:** KiCad kept the spoke. The fill covers the spoke axis in the middle of the
  relief gap, where only spoke copper can be.
- **anchor:** the pour beyond the gap still covers a strip `min_thickness` long and
  `spoke_width` wide, to 98 %.
- **body:** the fill piece that holds the spoke end (with the pad cut out, so spokes
  do not join through the pad) keeps at least half the area it had without the
  adversary.

A corridor holds when its spoke is kept without adversaries and every check holds.
Results go to `OUT/spokes/<board>/verify.json`; exit 3 when a corridor does not hold.
That means the reservation cannot be trusted on that board, for example because of a
custom pad shape or a custom thermal rule. Each check is one zone refill, so on a
large board the check takes a long time. The script takes `--limit N` (first N pads).

Measured on the fixture with an F.Cu GND pour (`tests/test_adhd_spoke_keepout.py`;
19 live corridors, `/SIG_A` under the 0.4 mm Power-to-signal rule, `/VIN` at 0.3 mm):

- At the corridor edge, every corridor holds for both classes and both modes.
  kicad-cli DRC with `--refill-zones` agrees: with tracks against the end of all
  three live corridors of C1.2, there is no `starved_thermal` finding.
- **The length is exact.** A `/SIG_A` track 10 µm inside the far end makes KiCad drop
  the spoke. 50 µm inside two of C1.2's corridors gives DRC `starved_thermal`
  (actual 1, minimum 2). A `/VIN` track at the same place does not drop the spoke,
  which is why every class is tried.
- **The half-width is conservative.** From the sides KiCad keeps the spoke even 50 µm
  inside. Only the spoke-wide anchor gets narrower.
- **A trench is not prevented.** Tracks that hug the end and both sides together
  (`--modes u`; all outside the corridor) leave the spoke in place, but on a pocket
  of about 0.25 mm² that is cut off from the pour. DRC counts the spoke, so there is
  no `starved_thermal` finding. The result still fails grading: DRC reports the cut-off
  pour piece as unconnected, and `pour_integrity` reports the island. A corridor
  alone cannot stop a router from enclosing it.

## Limits

- **Over-reservation.** Each corridor is about `spoke_width + 2·clearance` wide (1.3 mm
  on the fixture). Both backends also hold their own clearance from a rule-area edge.
  Measured on the F.Cu-pour test variant, the closest new copper was 0.41 mm (historical KRT run) and
  0.57 mm (Freerouting) from a reserved corridor. The effective blocked area is
  therefore about one clearance larger again. On dense boards this can make nets
  unroutable or push them to other layers. Use `--reserve-spokes 1`, or route the
  crowded area first without reservation, and check with `kicad-route spokes`. If a
  pad needs no protection, nothing is lost by leaving it unreserved: KiCad's DRC
  still grades the result.
- **A model, not KiCad's filler.** Corridors follow the layout described above, which
  is exact for rectangle, rounded-rectangle, oval and circle pads at the stock spoke
  angles. `--verify` checks the model on a given board with KiCad's filler. The model does not evaluate:
  - spoke templates of custom-shape pads (corridors use the pad's extent, and the
    plan notes it);
  - spoke angles that are not multiples of 45° on non-round pads (KiCad builds those
    from a rotated bounding box; the corridor follows the same support-function
    reach, but it is not verified);
  - via thermals.
- **Custom thermal rules are not evaluated.** `thermal_relief_gap`,
  `thermal_spoke_width`, `zone_connection` and `min_resolved_spokes` constraints in
  `.kicad_dru` are reported under `warnings`; corridors use the pad and zone values.
- **Zones and connections.** Only zones whose outline contains the pad centre are
  considered. A pad in several overlapping same-net zones on one layer gets the
  corridors of the highest-priority zone. Hatched fills are treated like solid fills.
- **Spoke counts come from the input fill.** Liveness is judged on the input's
  refilled pour. A pad that is already starved in the input stays starved. New copper
  can also starve a pad far from its corridors by splitting the pour into an island;
  attribution reports such pads as `attributed: false`.
- **Same-net copper is blocked too.** A corridor blocks every net, including the pad's
  own. A backend that wants to run a GND track out of a reserved side must use
  another side. Pads stay reachable from their unreserved sides.
