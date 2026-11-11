# Areas a board closes to new copper without a rule area

KiCad's DRC rejects new copper in some places that no rule area marks. A routing
backend that does not model such a place routes straight through it, and the candidate
then fails native DRC. On every `run`, kicad-route finds these places on the work copy
of the board, hands them to the backend as temporary keep-out rule areas, and removes
the areas from the backend's output before grading. No option switches this on or off.

```sh
kicad-route run --backend builtin        # work/closed-plan.json lists what was closed, and why
```

## Solder-mask apertures that are not pad openings

A shape or text drawn on `F.Mask` or `B.Mask` opens a window in the solder mask: a
logo, a marking, an opening over a pour for a shield contact. KiCad 10's
`solder_mask_bridge` check (`pcbnew/drc/drc_test_provider_solder_mask.cpp`) treats
every item that is on a mask layer and on no copper layer as an aperture without a net
of its own:

- The first net whose copper lies under the aperture becomes its net. Copper of any
  other net under it is a bridge. The fill of a pour counts as copper of its net.
- Copper is under the aperture when it comes within the board's
  solder-mask-to-copper clearance, plus a shape's own solder mask expansion, of the
  aperture, on the outer copper layer of that side. A via counts on both sides.
- Apertures of a footprint that allows solder mask bridges are not checked.

So a track routed under a logo that sits over a ground pour is one error for the
track, and usually more: KiCad reports each pair of items the aperture joins. On one
real two-layer board, 28 such apertures gave 30 to 41 new `solder_mask_bridge` errors
per routed candidate. The count changes from run to run because KiCad tests the items
in parallel and reports against whichever net it met first.

### What is closed

`src/kicad_route/kicad/closed.py plan` (pcbnew) never saves the board. Every aperture
gives one window on the outer copper layer of its side, the aperture grown by

    grow = expansion + max(mask-to-copper clearance, untented-via term)

- `expansion` is the shape's solder mask expansion: its own value, else the board's.
  KiCad adds none to text.
- The untented-via term is `minimum web width + 2 × board mask expansion`. It applies
  on a side whose vias the board does not tent, because KiCad then tests a new via as
  an aperture of its own against the minimum web width.
- A shape keeps its exact outline, holes included, so the inside of a ring stays open.
  Text, text boxes, dimensions, tables and barcodes are closed as the convex hull of
  their outline.

Windows of one layer that touch are merged. A rule area shuts out every net, also the
one a window was drawn for, so copper the input already has on that layer must stay
reachable:

| Input copper on the window's layer | What happens |
|---|---|
| a pad with a net, a track or a via that lies wholly under the window | the window is **left open** and listed with that copper: it can only be reached through the window |
| any other such copper within its berth of the window | the berth is cut out of the window; the copper is reached over its own part outside it |
| a pad without a net | nothing: it needs no track |

The berth is the copper grown by its net class's clearance plus track width. What is
left of each window is closed to tracks and to vias.

A ring is closed like any other window. Copper inside it can then only be reached
from another layer. That is also all KiCad accepts when the ring shows a pour.

### Limits

- A rule area cannot tell nets apart. KiCad lets the one net an aperture already
  shows add copper under it; the closed window keeps that net out too. A pour is not
  affected.
- A window that is left open is not protected. A track of another net routed through
  it, away from the copper it was left open for, is a bridge. `work/closed-plan.json`
  lists these windows under `mask.windows` with `enclosed`.
- `solder_mask_expansion` constraints in a `.kicad_dru` are not evaluated. The plan
  uses the shape's and the board's values and records a warning.
- The built-in router keeps new copper its clearance away from every rule area, not
  just out of it. Around a window that is more than KiCad asks for, and in a tight
  place it can cost a connection that fits.

## Copper without a net

A shape or text drawn on a copper layer without a net is foreign copper to every net:
the tab of a land pattern, a solder-jumper bridge, a printed antenna, a logo, a board
marking. KiCad reports new copper that touches it as `shorting_items`, or as
`clearance` with nothing left (which of the two depends on the order of the items'
UUIDs), and new copper beside it as `clearance`. A backend may not model such copper.
The built-in router does, but it gives a footprint's own copper to the net of the pad
it touches, and then routes that net across it and drops vias on it.

### What is closed

Each such shape is closed on its layer at its exact outline, text as the convex hull
of its outline. Hidden text is not copper. A graphic that has a net is not listed: a
backend reads it as it reads a track. The backend keeps its own clearance from a rule
area, as it does from copper, so the outline is not grown.

A shape can lie over a pad of the input. Then the pad cannot be connected at all
without touching the shape, and KiCad already reports the pad against the shape on
the unrouted board. The pad must stay reachable, and new copper should touch the
shape as little as that takes:

| Input copper that overlaps the shape | Tracks | Vias |
|---|---|---|
| none | closed on the whole shape | closed |
| a pad with a net, a track or a via whose berth reaches past the shape | closed outside the berths, **open inside them** | closed on the whole shape |
| copper whose berth lies wholly on the shape | **open** on the whole shape: there is no way to it except across the shape | closed on the whole shape |

The berth is the same as for a mask window: the copper grown by its net class's
clearance plus track width. Copper that is only beside a shape gets no berth; it is
reached without touching the shape.

### Limits

- A pad under copper without a net cannot be connected without a finding. The best a
  run can do is one: the track that enters the pad. Such a board cannot be routed
  both completely and without a new DRC error, whatever the backend; the footprint
  needs its tab drawn as part of the pad.
- With the tab closed, the pad can only be entered through its berth. When other
  nets take that room first, the built-in router has left the pad unconnected where
  it used to cross the tab.
- A shape that is left open to tracks is protected from other nets only by the
  backend's own model of it.
- Copper zones without a net are not covered.

## How a run uses it

In `run_attempt`, `closed.around_route` wraps the backend call, outside the
`--barrier-from` and `--reserve-spokes` hooks:

1. **Plan** on the work input board (`work/input/<project>/<board>`; the reference
   copy is never touched). A board without anything to close is routed as it is.
2. **Reserve:** each planned area becomes a single-layer rule area named
   `kicad-route:closed:<source>:<layer>:<n>` (`mask` or `copper`) that forbids
   tracks, vias or both. It allows pads, footprints and zone fills. A board that
   already has such areas is refused.
3. **Route** with the backend as usual. Both backends read rule areas.
4. **Strip** every `kicad-route:closed:` rule area from the backend's output board
   before it is copied into the candidate. When nothing was removed, the strip leaves
   the file untouched.

The candidate is then graded exactly as before. If an area is left over, the `keepouts`
preservation check fails, because it compares the rule areas with the input's. Closing
an area only takes routing room away from the backend. It never changes a rule, a
severity, a width or a clearance, and grading always uses the input's own project
files.

Recorded:

- `process.closed_areas`: the counts (`mask.aperture_count`, `window_count`, `closed`,
  `left_open`, `ignored`; `copper.shape_count`, `closed`, `berths_open`, `left_open`),
  the areas reserved and stripped, warnings and the commands that ran.
- Work files: `work/closed-plan.json` (every aperture, window, copper shape and area)
  and `closed-{plan,reserve,strip}.log`.

A failed plan or reservation is a process failure (exit 1, no routing). A failed strip
is added to `process.reasons` and fails `keepouts`.
