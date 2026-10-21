# Pour integrity (`pour_integrity`)

KiCad's DRC passes boards whose copper pours got electrically worse. New signal
tracks can cut a ground pour into islands. KiCad then removes the islands or
leaves them, depending on the zone's island settings. Tracks can also starve a
pad's thermal spokes, which DRC reports only as a warning, and only below
`min_resolved_spokes`, which a project may have lowered. A pad whose only ground
path was the pour can end up on one spoke or none. On a real board this left 24
pads with one spoke, and 22 of them had no other ground path.

`pour_integrity` is a preservation check added on top of the native grading. It
compares the pours of the refilled input with the pours of the refilled candidate.
It never relaxes a rule. It can only add failures.

## Where the facts come from

`src/kicad_route/kicad/pour.py` runs with KiCad's Python (`pcbnew`). It reads a
board whose zones KiCad has just refilled, so the fill polygons are the ones KiCad
grades:

- **Reference:** `baseline_drc` refills a copy of the untouched input
  (`kicad-cli pcb drc --refill-zones --save-board`) for the baseline DRC. The pour
  facts are extracted from that same refilled board before the copy is deleted.
  They are cached beside the baseline report as
  `OUT/.cache/baseline/<fingerprint>/baseline-pour.json`. A baseline cached before
  this check existed has no pour facts. In that case `validate` refills a copy of
  the run's reference the same way and adds the facts to the cache.
- **Candidate:** `validate` extracts the facts from the candidate after its
  refill-and-save, into `validate/candidate-pour.json`.

What `pour.py` records:

| Fact | How |
|---|---|
| Per zone net and layer | filled area (mm²), number of fill polygons, number of polygons cut off from the main pour |
| Per fill polygon | area, bounding box, and the pads, vias and tracks of its net that touch it |
| Connectivity | union-find over the net's fill polygons, pads, vias and tracks (geometric contact; track ends against pads, vias and other tracks). The component with the most fill area is the net's **main pour** |
| Per pad the zone reaches | effective connection setting (pad, then footprint, then zone; `tht_thermal` resolved by pad type), thermal gap, whether the pad touches the fill, **spokes present**, solid or not, whether the pad is on the main pour, tracks attached (on the zone layer and in total), track spokes, vias in the pad that reach a fill |

Spokes are measured, not read from settings, and counted the way KiCad's
starved-thermal test counts them:

- **Fill spokes:** the fill is intersected with a thin band along the pad edge
  (20 µm either side). Each separate piece is one spoke; KiCad counts pad-outline
  crossings divided by 2. If the fill closes all the way around the pad (the piece
  keeps a hole), the connection is solid.
- **Track spokes:** a track of the pad's net on that layer with one end in the pad
  and the other end in the fill. A stub that ends inside the pad does not count.
- **Vias in the pad** that reach a fill are recorded (`via_spokes`, and
  `other_path` in the comparison) but not counted, because KiCad does not count
  them either.

The minimum spoke count is the project's `min_resolved_spokes` (KiCad default 2),
read from the board's design settings.

Calibration on a real 4-layer board (fome-mc, 386 zone-reached pads, 250 fill
polygons): with `min_resolved_spokes` restored to 2, KiCad reports 24
`starved_thermal` pads. This count finds the same 24, plus one pad (C110.2, with a
via in the pad) that KiCad does not report. 21 of them have no track and no via:
the pour is their only ground path. Extraction takes about 5 s on that board.
Quadtree tiling of the fill polygons keeps it fast; without tiling it took 160 s.

## Verdict

`src/kicad_route/pour.py` compares the two sets of facts.

**FAIL** when, compared with the reference:

| Kind | Condition |
|---|---|
| `lost_fill_contact` | a pad that touched the fill no longer touches it |
| `lost_pad_reach` | the zone no longer reaches a pad it reached and fed on the reference (the zone outline, its net, the pad or the pad's net changed), so the candidate has no record for that pad at all |
| `isolated_island` | a pad that was on the main pour of its net is now cut off from it: its fill island has no via, track or other polygon that reaches the main pour. The island (area, bounding box, pads, vias, tracks) is listed |
| `spokes_below_minimum` | a thermal pad's spokes (fill spokes plus track spokes, as above) were at or above the minimum and are now below it. Solid connections count as unlimited |
| `area_loss` | only with `--pour-area-loss PCT`: a net/layer lost more than PCT % of its fill area |

**Reported, not failed:** area change per net/layer, polygon counts, area off the
main pour, spoke counts that dropped but stay at or above the minimum
(`spokes_reduced`), candidate islands without pads (`floating_island`), and pads
the zone reaches only on the candidate, or reached on the reference without fill
contact (`pad_reach_changed`). New routing may reduce pour area legitimately.

If pour facts cannot be extracted for either board, the check fails (it does not
pass silently).

## Options (tighten only)

Both `run` and `validate` take these options. `run` records them in the attempt's
`settings`, and `validate` uses its own values instead when they are given:

- `--pour-area-loss PCT` fails a net/layer that loses more than PCT % of its fill
  area.
- `--pour-min-spokes N` raises the spoke minimum to `max(min_resolved_spokes, N)`.
  It never lowers it. It applies only to pads whose spoke count the candidate
  reduced.

## Outputs

- `validate/preservation.json`: the `pour_integrity` record. Its `detail` gives
  the area per net/layer and the failure counts. `differences` holds the first 40
  failures and informational items.
- `validate/pour-integrity.json`: the full comparison: `layers`, `pads` (reference
  and candidate values side by side), `failures`, `informational`, `min_spokes`,
  `area_loss_limit_pct`, and where the reference facts came from.
- `validate/candidate-pour.json` and `baseline-pour.json`: the raw facts.

## Evidence (`tests/test_pour.py`)

Each test starts from a clean built-in router candidate of the fixture routed with
`--nets '!GND'`. J1.2 and J2.2 then reach GND only through the B.Cu pour, with four
45° spokes each. The tests edit copies of that candidate with pcbnew and run
`validate` again:

- The round trip passes: both pads stay on the main pour with all their spokes.
- A signal track across the whole pour at x = 142 mm splits it in two, and
  `isolated_island` fails for J2.2: its island has 183.7 mm², pad J2.2 and no vias.
  The total area falls by only 2.5 %, because KiCad keeps the island (it has a pad).
- Foreign stubs across three of J1.2's spoke paths give `spokes_below_minimum`
  (spokes 4 → 1, minimum 2). Stubs across all four give `lost_fill_contact`.
- A track near the bottom edge cuts off a pad-free strip, and KiCad removes it
  (−7.5 % area). The check passes by default. With `--pour-area-loss 1`, `area_loss`
  fails.

## Limitations

- `min_resolved_spokes`, the zone connection and the thermal gap come from the
  project and the board (pad, footprint, zone). Custom `.kicad_dru` rules with
  `min_resolved_spokes`, `zone_connection` or `thermal_relief_gap` constraints are
  not evaluated. Spokes are still measured from the real fill, but the minimum and
  the connection type use the board values.
- A via in the pad does not count as a spoke (as in KiCad), but KiCad's count can
  differ for such pads (one pad on the calibration board).
- The facts carry a `schema` number. Cached baseline facts with another schema are
  recomputed.
- Connectivity is geometric: contact of shapes and track ends. It does not use
  KiCad's connectivity engine. Pads that touch each other directly (not through
  tracks or fill) are not joined.
- Pads are matched by `<ref>.<number>@<layer>`. Candidates that move footprints
  already fail `footprints`.
- `audit` does not run this check yet.

## Explicit plane production

[`kicad-route planes`](planes.md) also uses this comparison. Its `result.json`
records `planes.pour_integrity.before`, `.after`, `.check` and `.delta` from
the native refilled input and planned candidate. Missing facts fail production.
A before/after preservation pass is not proof of every new plane connection.
