# Rust routing engine migration

The replacement is a routing engine in Rust, not another search accelerator for
the Python engine. Rust owns copper state, geometry and legality, search, net
ordering, speculative edits and restore, and the exact output edit list. Existing
`src/kicad_route/router` code stays intact as a reference and current backend.
The new engine does not become the default or update the installed runtime as a
side effect of building or testing it.

## Modules and invariants

One typed copper store owns item identity, net membership and geometry. Input
copper is immutable in the first slice. Generated copper and its derived search
indexes are updated together; failure must not expose half an edit. Checkpoints
may be constant time, but rollback work is proportional to the mutations and
index maintenance. We make no unmeasured latency promise.

Geometry checks swept segments against real shapes and board edges, rather than
checking only destination cells. Search state must retain every attribute that
changes future feasibility or cost. A lower-cost prefix is not sufficient to
dominate a prefix with different via exclusions or approach constraints. Tiny
independent exact-search oracles cover the supported state model. Exhaustion,
work-budget exhaustion, unsupported input and invalid input remain distinct.

The board-level controller chooses a deterministic net order, stages routes,
validates each proposal and produces a complete edit list. It will grow into
multi-terminal and multilayer orchestration here, not by calling the old Python
controller. Global routing improvements follow a sound, measured foundation.

## Native compatibility interface

`tools/rust_engine_kicad.py` uses installed `pcbnew` only to decode native KiCad
objects and encode Rust's explicit edit list. It does not select paths, victims,
IDs, geometry or routing costs. This is justified by KiCad's native Python
bindings; a Rust KiCad-format reader can replace it later without moving engine
policy. Native KiCad DRC and connectivity remain external acceptance checks.

The first adapter accepts standalone rectangular two-layer boards with front
copper SMD circular/axis-aligned rectangular pads and straight existing tracks.
Routing uses F.Cu only. Project/custom-rule files, zones, vias, unsupported pad
shapes/rotations, copper artwork and other layers are rejected. Explicit uniform
rules must meet native default floors. Project/rule import and other features
are missing capabilities, not ignored inputs. Pad centres initially must align
to the requested routing grid; Rust checks this. Pad offsets, pad/footprint
clearance overrides, net ties, footprint zones/keepouts, copper fields and
footprint board cuts are also rejected on import and output application until
the interchange represents them.

The version1 exchange uses integer nanometres, explicit net and item IDs, bounds,
rules, fixed copper and a total search-work budget. Rust assigns the added item
UUIDs. The adapter checks source identity, requires a complete plan and applies
the exact geometry and IDs to a new file. The first slice cannot delete input
copper. This first supported subset is not complete board-tool compatibility.

## Implementation sequence and acceptance

1. **Executable foundation slice.** Rust copper store, continuous geometry,
   supported-state search, deterministic multi-net controller, failed-edit
   restore and explicit output semantics. Native import/export, independent
   search-oracle tests, and a real KiCad fixture routed around fixed copper.
   Acceptance requires unchanged input/protected copper, native readback,
   expected terminal connectivity and no introduced native DRC errors. Adapter
   tests alone are not a routing result.
2. **Real project and geometry coverage.** Effective board/net/pair/layer rules,
   local overrides, arbitrary pad geometry and artwork, multilayer/via spans,
   keepouts and zones. Every new capability gets a native fixture and explicit
   unsupported-input behavior until implemented. Preserve locked items and
   per-net terminal partitions rather than relying only on aggregate counts.
3. **Complete orchestration.** Multipoint trees, differential pairs, length and
   delay constraints, plane connectivity and victim recovery under one Rust
   state authority. Include cancellation and failure injection at publication
   points. No automatic migration of the legacy Python state graph.
4. **Measure and improve throughput.** Attribute whole-path CPU, memory and
   native-tool costs on fixed revisions and inputs before choosing spatial
   indexes, cache layouts or parallel work. No GPU or language-speedup claim
   without measurement. Respect existing benchmark admission and holds.
5. **Global-routing quality and migration.** Compare alternative ordering,
   congestion negotiation and coarse capacity planning only after the preceding
   contracts hold. Demonstrate complete-board results over representative cases
   before retiring the old backend. Deployment/default changes require their
   own authorization; this implementation does not grant it.

## Ownership and current evidence

Agent-1484 (existing Codex-3 session) owns `rust-engine/`, including CLI and Rust
tests. Agent-1497 owns this migration, native adapter and integration tests, and
acceptance. Fable agent-1503 and Carbon agent-1502 produced independent first
reviews; their addenda and supervisor synthesis remain part of this work.
Review claims are checked against source: timing counters that cover selected
passes are not whole-engine profiles; rejected proposals do not prove failed
rollback; a proposed last-via-only state is insufficient for an all-prior-via
constraint. Carbon's corrected retained command places the foreign segment between the three
samples and demonstrates a 24 micrometre missed clearance intrusion. The first
report's midpoint coordinate was wrong. Both reports and their corrections
are linked from the [supervisor synthesis](https://artifact.harm.org/tubrxk/synthesis.html).

The first executable slice passes 18 Rust tests, including an independent
Dijkstra oracle over 256 small obstacle worlds, generated-net rollback and
explicit failure outcomes. Fifteen native tests cover extraction, exact
identity/geometry readback, unchanged input, unsupported geometry/rules and
failed/stale-plan refusal. One test runs the actual Rust CLI on a native
two-terminal board with protected foreign copper. KiCad 10.0.8 reports one
unconnected pair before routing and zero after; the same pre-existing dangling
track warning remains, with no new DRC violations. This is a supported-subset
fixture, not a whole-board capability or performance claim. Independent code
acceptance remains pending. No production backend changed.

Run the combined focused gate with `mise run test:rust-engine`. It uses the
existing host process boundary, locked offline Cargo dependencies and the native
test fixture. It does not rebuild or invoke the installed legacy routing engine.
