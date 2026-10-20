# Stitching vias: proposed plane-plan/2

Status: design only for KICADROUTE-66. No parser, backend, schema file or CLI
behavior for version 2 is implemented. agent-ee owns the shared plan contract;
its producer and consumer must agree on a new version before implementation.
`agent-ee.plane-plan/1`, its strict accepted fields and deterministic zone IDs
remain unchanged. Adding stitching fields to version 1 must still be refused.

## Proposed contract

Use an explicit `agent-ee.plane-plan/2` discriminator and retain the existing zone
specifications. Add a required `stitching_vias` array, empty when none are wanted.
For the first version, each entry should specify an existing net, an explicit
`position_mm: [x, y]`, diameter and drill in mm, and a layer pair. Initially allow
through vias only (`F.Cu` to `B.Cu`); blind/buried and microvias need a later
extension that accounts for fabrication limits. Require finite coordinates and dimensions,
positive drill smaller than diameter, bounded entry count, no duplicates and no
unknown fields. Exact bounds and the final field names need agreement with
agent-ee. No net creation, inferred dimensions or hidden grid-placement defaults.

The caller chooses exact locations. Grid, perimeter or pitch-based proposals can
be generated separately and reviewed as an explicit plan. A requested via that
cannot be placed legally must fail; never silently skip, move or resize it.
Explicit locations make placement reproducible.

## Native implementation boundary

Prefer extending the existing pcbnew producer: it already creates deterministic
zones in a network-isolated sandbox and inspects the saved board. Adding an
external `route_planes.py` path would require another adapter and proof that its
heuristics honor exact placement and preserve every unplanned object. Reconsider
that backend only for a separately specified heuristic plan contract.

Assign deterministic via UUIDs from canonical version-2 plan content and entry
index using a dedicated UUID namespace and an explicit object-kind tag. Keep
version-1 identity generation untouched. Define version-2 zone identity separately
so changing a via does not accidentally authorize replacing unrelated zones.
Check for collisions with all input board items. Reapplying a plan replaces only
its precisely matching owned zones/vias; unknown copper remains a refusal.
Reject an existing owned via with a changed net, location, layer pair, dimensions
or lock state rather than silently claiming it. Existing unowned tracks/vias,
footprints, rules, outline, stackup and keepouts remain protected.

After native save and refill, independently reload and verify every requested
via's UUID, net, position, layer pair, diameter and drill. Replace the current
no-new-vias inventory assertion only for an exact version-2 planned-via inventory;
retain the version-1 assertion. Apply native DRC, net-class via geometry,
keepouts, clearances, pour integrity and malformed-evidence refusal. Prove that
each stitching via touches intended same-net filled copper on at least two
specified copper layers; a via with no useful plane connection is not successful
stitching. The final contract should name these intended contact layers explicitly.
Global post-routing connectivity and electrical acceptance remain separate.

## Evidence and rollout gates

Proposed additive result evidence: created/reused via counts, the independently
inspected via records, intended layer contacts and failed plan entries. Specify
and document those fields and update the planes result schema with implementation;
this note does not reserve executable output fields or change exit meanings.
Missing evidence is process failure; a produced but invalid candidate is exit 3.

Require original four-layer native fixtures for successful GND stitching, exact
reapplication, byte-identical repeat production, all requested contacts, malformed
plans, missing nets, impossible geometry, disconnected vias, keepout/clearance
collisions, UUID collisions and tampered saved vias. Include a mixed power/GND
case that must never short, and unchanged version-1 rejection/preservation tests.
Coordinate the agent-ee plan schema and adapter opt-in, then run kicad-route's full
pytest, smoke and determinism gates plus the consumer's schema/adapter tests.

Implementation is deferred because schema ownership, contact semantics,
manufacturing limits and permission to add vias need coordinated changes beyond
the three bounded repairs in this ticket.
