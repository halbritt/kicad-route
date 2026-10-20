# Planned planes

`planes` creates or repairs exactly the copper zones in an
`agent-ee.plane-plan/1` input, using KiCad's native Python bindings. This is the
plan-input backend; it does not call KRT's heuristic `route_planes.py`.

```sh
kicad-route planes --board project/board.kicad_pcb --plan plane-plan.json \
  --run-dir build/planes-attempt --json
```

The run directory must be new and outside the input project. The source board,
same-name `.kicad_pro` (required), optional `.kicad_dru` and `.kicad_prl`, and plan
are captured before production. The source files stay untouched. Production,
refill, DRC and fact extraction run in bwrap with a private network namespace,
a read-only host filesystem, private temporary storage and writable attempt work.
Missing bwrap, KiCad, evidence or native output fails the process.
`doctor --json --offline` probes namespace support as `bwrap (planes)` and reports
`capabilities.planes.bwrap_ready`; see [sandbox.md](sandbox.md).

## Plan contract

The schema matches agent-ee's `plane-plan/1` producer, including deterministic
UUIDs and `agent-ee-plane/<uuid>` zone names. The root has exactly `schema` and
`zones`; `zones` contains 1–64 entries. Unknown fields and duplicate JSON keys are
refused. There are no implicit defaults or net creation. For example:

```json
{
  "schema": "agent-ee.plane-plan/1",
  "zones": [
    {
      "net": "GND",
      "layer": "In1.Cu",
      "outline": "board",
      "clearance_mm": 0.25,
      "min_width_mm": 0.2,
      "thermal": {"connection": "thermal", "gap_mm": 0.3, "spoke_width_mm": 0.3},
      "priority": 0,
      "island_removal": "always"
    }
  ]
}
```

Each net must already exist; each named copper layer must be enabled. `outline`
is `"board"` (one closed native outline, with its cutouts) or
`{"polygon_mm": [[x, y], ...]}` with 3–4096 distinct vertices, implicit closure,
finite coordinates within ±10000 mm, positive area and no self intersection.
Clearance, minimum width, thermal gap and spoke width are finite numbers in
[0.001, 100] mm. Priority is an integer in [0, 100]. Thermal connection is
`solid`, `thermal` or `tht_thermal`; island removal is `always` or `never`.

A pre-existing copper zone must have this exact plan's deterministic UUID,
name, net and single layer. Other copper zones, including footprint-owned ones,
are refused. Rule areas are retained. Reapplying the same plan replaces its zones
and refills them without duplicates. Changing the plan changes its UUIDs, so an
older plan's zones are refused. Stitching vias are outside `plane-plan/1`;
unknown via fields are refused and all existing tracks/vias must remain identical.

## Evidence and exits

`result.json` has schema `kicad-route/planes/1`, kind `planes`. It contains captured
input hashes, sandbox command/exit, process and validation statuses, the candidate
path, and a `planes` section:

- `zones_created`: the count added beyond the existing planned inventory.
- `zones`: saved UUID, net, layer, native filled area in mm² and polygon count.
- `pour_integrity`: the existing preservation check, full refilled `before` and
  `after` facts, and the comparison `delta`.

`work/` retains native logs, refill/DRC reports and facts. The saved zone inventory,
settings and outline are checked against the plan after native refill. Every
planned zone must have positive native filled copper. Existing preservation
checks apply, with exact-plan zone inspection replacing zone equality. No trace
routing behavior or verdict changes. Captured project sidecars are restored
before grading and delivery. `validation.restored_project_files` is a sorted,
deduplicated list of candidate sidecar basenames actually changed by restoration
(`.kicad_pro`, `.kicad_dru`, `.kicad_prl`). It includes generated files removed
because they were absent from the input, and files restored after either native
apply or grading. Unchanged copies and baseline-only restorations are omitted.
The list is empty if no completed worker report is available (`NOT_RUN`). This
is evidence of native side effects; restoring captured sidecars does not itself
fail plane validation. `project_files` still checks the restored bytes.

New native DRC errors fail validation; planned `always` zones must also have no
isolated-copper findings, including warnings and exclusions. Both DRC reports
must declare full error/warning/exclusion coverage and well-formed violation
metadata and item UUIDs. Error findings and isolated-copper findings when any
`always` zone is planned require nonempty item attribution. Incomplete coverage,
unattributed findings or malformed records fail production (exit 1, no published
candidate), rather than being filtered into a pass. An attributed isolated-copper
finding on an `always` zone fails validation (exit 3); one attributed only to a
`never` zone does not trigger this additional check.

Exit 0 means production and its preservation/DRC-delta checks succeeded; exit 3
means a candidate was produced but failed validation. Both retain
`candidate/<board>.kicad_pcb`. Exit 1 means sandbox/native failure or missing
evidence, with `validation: NOT_RUN`, no published candidate and no pour pass.
Exit 4 is an input/usage/state refusal through the existing refusal interface.
`--json` prints one result or refusal record on stdout.

Plane production does not certify electrical acceptance. A bare board can have
unconnected pads and no schematic; full routing, schematic parity, final DRC and
fresh pour-integrity checks remain the caller's responsibility. In particular,
a preservation pass from a board without zones cannot prove the new planes feed
every intended pad.

`tests/test_planes.py` builds an original four-layer board and reloads the saved
In1 GND and In2 power zones independently. It covers exact settings, positive fill,
reapplication, locked track/via and keepout preservation, unplanned zones/nets,
disabled layers, invalid/unfillable polygons,
strict plan parsing, missing tools, source immutability and sandbox isolation.

## Stitching-via follow-up

Stitching is a separate proposed contract; see the
[plane-plan/2 design note](plane-plan-2-design.md). This implementation continues
to accept only `agent-ee.plane-plan/1` and adds no vias.
