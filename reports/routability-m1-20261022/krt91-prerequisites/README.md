# K91 native policy and configuration prerequisites

The native facts producer now captures saved board and footprint rule areas,
including ownership, zone UUIDs, transformed coordinates, holes and restriction
flags. The experiment policy helper checks actual straight tracks and per-layer
circular via copper against the declared layers, spans and native areas. Missing
coverage, unsupported geometry and malformed policy cannot qualify. Duplicate
JSON keys are rejected. Native DRC and preservation remain separate requirements.

Configuration receipts now bind inherited `KICAD_*` controls and record how many
outer batches and later subruns executed. A capture covering only one resolved
outer batch cannot qualify a run with additional uncaptured batches.

The composed implementation candidate is `040b7fd`, based on `409d06a` and the
reviewed `53a07c0`, `3545da73` and `d5d7f23` changes. The root gate passed all
71 configuration, native-policy and preservation cases, with no skips, in
15.62 seconds. It included the real owned backend emitter using a disposable
generation built from the matching source. Production installation was unchanged.
[The integration record](integration.json), [test output](gate-stdout.txt) and
[independent review](independent-review.json) retain the evidence and scope.

Independent geometry checks matched 34 track-area and three via-area intersections
against a separate GEOS oracle. A saved native board with 44 conforming tracks,
22 vias, a rotated and flipped footprint area, and a hole passed. Missing and
malformed native evidence returned `NOT_RUN`. Unsupported arcs, curved boundaries
and noncircular annuli remain explicit limitations.

Read-only extraction of the corrected-policy target confirmed six footprint areas
and two board areas. It performed no target grading or routing. Its native files
remain in the local fixture; this report includes no project files.

KICADROUTE-91 remains unreleased. The experiment sidecar, strict comparison and
actual agent-ee retained-receipt replay still need qualification. Fresh target
grading and the single authorized routing diagnostic follow those gates. This
checkpoint provides neither a current-policy routing result nor a placement
adoption decision.
