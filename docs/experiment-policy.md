# Native experiment policy qualification

`kicad_route.experiment_policy.load(path)` reads exactly these caller-owned fields:

```json
{"schema":"routability-policy/1","experiment_class":"current-policy",
 "allowed_trace_layers":["F.Cu","B.Cu"],
 "allowed_via_spans":[["F.Cu","B.Cu"]],
 "rule_areas_sha256":"<64 lowercase hex digits>"}
```

`historical-all-layer` is also a supported experiment class. It does not bypass
native rule areas. Layer names must refer to the actual physical copper stack;
via endpoints use top-to-bottom stack order. An empty via-span list prohibits all
actual vias. The area digest is `common.sha256_of(facts["rule_areas"])`, including
ownership, saved UUIDs, native coordinates, holes, layers and all restriction flags.

`validate(policy, facts)` returns `status`, `reasons`, `policy_sha256`,
`rule_areas_sha256` and `coverage`. `PASS` requires complete native facts and
conforming actual copper. `FAIL` identifies a policy mismatch or prohibited actual
copper. `NOT_RUN` means the supplied evidence cannot support qualification, even
when another known violation was found. No missing inventory defaults to empty.

The facts producer enumerates BOARD and FOOTPRINT rule areas separately. Its
coverage fields are `rule_areas_coverage: "board-and-footprint"`,
`rule_areas_status: "PASS" | "NOT_RUN"` and `rule_areas_reasons`. Each area has
`uuid`, `owner: {kind, uuid, reference}`, `polygons: [{outline, holes}]`, layers
and native flags. The legacy flattened `outline` remains for existing readers;
qualification uses the full polygons. Ordinary board copper-zone facts retain
their previous meaning. Footprint copper zones are not added to that inventory.
Native footprint area coordinates already include placement, rotation and side;
the producer does not transform them again. BOARD object UUIDs regenerate on load
and are not serialized identities, so board ownership uses null UUID/reference.
Zone and footprint UUIDs remain the native saved identities.

Qualification checks actual straight-track layers, actual via endpoint spans,
track-width capsules and native per-layer via annulus discs against prohibitions
on every intersected physical layer. Via facts include `layer_diameters` and
`annulus_geometry: "circular-layer-discs"`; unsupported noncircular shapes cannot
qualify. Hole interiors are clear; boundaries and
tangencies are conservatively prohibited. A tracks-only area allows vias. Areas
with neither flag set do not prohibit track or via copper. Every native polygon
outline/hole is retained; the native saved board, rather than unsaved geometry
that KiCad's writer may discard, is the qualification input.

Curved boundaries, arc tracks, malformed rings, missing native coverage and
unknown layer/span/annulus data return `NOT_RUN`. The qualifier supports linear
simple rings and disjoint holes strictly inside their outline. It does not invent
new restrictions. Pads, footprint placement, zone-fill restrictions, native DRC
and preservation retain their existing independent gates. A declaration hash alone
is insufficient: callers must bind facts to the exact saved/refilled board,
current producer and project rules, and separately qualify effective routing
configuration against the declaration.
