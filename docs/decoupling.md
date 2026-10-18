# Decoupling cells: `kicad-route decoupling`

A bypass capacitor does its job when the IC's supply current runs pin → capacitor →
plane. If the IC pin has its own via to the plane, or sits in a pour of its net, the
plane feeds the pin first and the capacitor hangs off to one side: the loop the
capacitor was placed to shorten is not the loop the current takes. An autorouter
that drops a power via next to every IC pin produces exactly that.

`kicad-route decoupling` is a read-only audit. It finds the decoupling cells of a
board and reports, per cell, whether the copper respects the pin → capacitor → plane
order. It never modifies a board, and its result is not part of a run's verdict.
This is the first step of KICADROUTE-54 (pre-routed, locked bypass cells); nothing
routes or locks cells yet.

```sh
kicad-route decoupling --board my.kicad_pcb          # OUT/decoupling/<board>/decoupling.json + .md
kicad-route decoupling <run_dir>                     # reference and candidate: <run_dir>/decoupling.json + .md
kicad-route decoupling --board my.kicad_pcb --radius 3 --ic 'U*' 'IC*' --cap 'C*'
kicad-route decoupling --board my.kicad_pcb --power '+3V3' 'VDD*' --ground 'GND' 'AGND'
```

Exit status: 0 the report was written; 1 the analysis ran and failed (the pcbnew
script failed, or the board changed while analysed): `decoupling.json` then holds the
failure record (`error`) and no `.md` is left beside it; 4 refused before any analysis
(no such board, KiCad could not load it, not a routing run, a run without its
reference or candidate board): a refusal record in `OUT/refusals/` (docs/interface.md).
`--json` prints the record (`kicad-route/decoupling/1` for `--board`,
`kicad-route/decoupling-run/1` for a run) or the refusal on stdout. The board's SHA-256
is checked before and after the analysis; a change fails the command.

## Cells

The analysis runs in `src/kicad_route/kicad/decoupling.py` under KiCad's `pcbnew`.
It loads the board, refills its zones in memory (the planes DRC would grade), and
never saves.

- **Ground-like net:** the last path component of the name matches `GND*`, `AGND`,
  `DGND`, `PGND`, `SGND`, `VSS*`, `0V`, `EARTH`, `CHASSIS`, `PE`
  (case-insensitive).
- **Power net:** not ground-like, and either the name looks like a supply (`+…`,
  `-…`, `3V3`, `1V8_CORE`, `VCC*`, `VDD*`, `VEE*`, `VBAT*`, `VBUS*`, `VREF*`, `VIN`,
  `VIN_12V`, `AVDD*`, `PWR*`, …; not `VINP`, `VOUT`, `VSYNC`) or one of its pads
  has the schematic pin type `power_in` or `power_out`.
- `--power` / `--ground` (fnmatch patterns, full name or last path component)
  replace these name rules.
- **Capacitor:** reference matches `--cap` (default `C*`), exactly two pads with a
  net, one on a power net and one on a ground-like net.
- **IC:** reference matches `--ic` (default `U*`, `IC*`).
- **Pairing:** an IC is a candidate for a capacitor when it has a pad on the
  capacitor's power net within `--radius` (default 5 mm, pad centre to pad centre)
  of the capacitor's power pad, and a pad on its ground net within the radius of the
  capacitor's ground pad. The nearest such pins of each IC form its pin pair; the
  candidate with the smallest summed distance becomes the cell. The other candidates
  are listed under `alternatives`, and the cell is `ambiguous`. Capacitors on the
  same pin pair are listed under `shared_with`. Capacitors with no candidate are
  listed under `unpaired`.

## Topology

For each net, the analysis builds a graph of its copper from KiCad's own
connectivity: pads, tracks, arcs and vias are nodes, and edges come from
`CONNECTIVITY_DATA.GetConnectedTracks` / `GetConnectedPads` (items that touch).
Every same-net filled zone is one PLANE node (rule areas and teardrops excluded). An
item touches PLANE when the zone fill on one of its copper layers collides with the
item's shape there. When two tracks or vias meet inside a pad, KiCad connects them
directly; the analysis drops that edge so the junction runs through the pad. A
track that ends at a capacitor pad's centre therefore reaches the next track only
through the capacitor pad.

Each side of a cell (power, ground) is classified for its IC pin and capacitor pad:

| topology | meaning |
|---|---|
| `respects` | the pin reaches the capacitor pad without the plane, reaches the plane only through the capacitor pad, and the capacitor pad reaches the plane without the pin |
| `bypassed` | the pin reaches the plane without passing through the capacitor pad. `direct_entries` lists the items that give it that path, nearest first (a via, a T-stub via, the pin's own pour contact) |
| `cap_not_on_plane` | the pin reaches the capacitor pad, but the capacitor pad has no path to the plane except through the pin (or the net has no plane) |
| `incomplete` | the pin has copper, but it reaches neither the capacitor pad nor the plane |
| `unrouted` | the pin touches no track, via or zone |

The cell is `respects` when both sides respect, `bypassed` when either side is
bypassed, else `incomplete`. `capacitor_entries` (on a `respects` side) lists the
capacitor's own plane entries; a via in the capacitor pad counts.

## A run

`kicad-route decoupling <run_dir>` audits the run's reference and candidate boards
and lists in `changed` every cell whose topology differs (keyed
`IC:power_pin/ground_pin:capacitor`, e.g. `U1:U1.7/U1.8:C1`). A cell that goes
`respects` → `bypassed` means the backend gave an IC pin its own path to the plane.

## Limits

- **Every same-net zone counts as plane.** A small copper pour used as a wide track
  between a pin and its capacitor makes both touch "the plane", so the cell reads
  `bypassed`.
- **Every via that touches a same-net zone is a plane entry.** On a board with the
  capacitor on the opposite side, the pin's via to that side passes through the
  inner plane of its net, so the cell reads `bypassed`. This is electrically true:
  the plane is reached before the capacitor.
- **Pairing is a distance heuristic.** It is pad centre to pad centre and per
  capacitor. On dense multi-rail parts (MCUs, PMICs, BGAs), check `ambiguous`,
  `alternatives` and `shared_with` before acting on a cell.
- **Name rules decide the nets.** A rail with an unusual name and no `power_in` pins
  is not a power net. Pass `--power` for it.
- **Not part of grading.** The audit does not change any run's verdict or exit
  code. A future grading check (loop area, inductance) would be a separate,
  tightening-only preservation check.
