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
policy. Applying a plan independently validates the saved native board before
publishing it: zero native open connections, no new native DRC violations and
added widths at least the native minimum. A missing or failed DRC command also
refuses publication. Validation failures have a `native_validation_failed`
CLI result on stderr and exit 2. Expected non-native failures also exit 2 with
JSON on the final stderr line: `invalid` for malformed/stale requests,
`unsupported` for unrepresented native features, `output_exists` for an existing
destination, and `io_error` for input/output failures. Argument-parser usage
errors and unexpected programming errors are not converted to routing outcomes.
Duplicate JSON fields, malformed track fields and zero-length additions are
rejected before publication. Exported JSON also uses staged create-if-absent
publication, so a failed write cannot leave a partial request at its destination.
Publication atomically creates a new destination;
it does not overwrite a concurrent writer's output.
Export and application each read the standalone source once into a private
snapshot. Its bytes determine the source hash, parsed geometry and DRC baseline;
later edits to the original are preserved and cannot change that operation's
baseline. Native DRC runs only on private copies and includes `--all-track-errors`,
so its preferences files do not appear beside the source. Process termination can
leave private temporary files; publication is atomic in visibility, not fsynced
for crash durability.

The first adapter accepts standalone rectangular two-layer boards with front
copper SMD circular/axis-aligned rectangular pads and straight existing tracks.
Routing uses F.Cu only. Project/custom-rule files, zones, vias, unsupported pad
shapes/rotations, copper artwork and other layers are rejected. Explicit uniform
rules must meet native default floors. Project/rule import and other features
are missing capabilities, not ignored inputs. Pad centres initially must align
to the requested routing grid; Rust checks this. Pad offsets, pad/footprint
clearance overrides, net ties, footprint zones/keepouts, copper fields and
footprint board cuts are also rejected on import and output application until
the interchange represents them. Nondefault board solder-mask rules and local
pad/footprint mask margins are also outside this first native contract.
Board and footprint Margin drawings are rejected on both import and application:
KiCad treats them as clearance boundaries, unlike ordinary documentation graphics.

The version1 input uses integer nanometres, explicit net and item IDs, bounds,
rules, fixed copper, an expansion budget and optional resource `limits`. Rust
owns the limits and their defaults; native export passes `--limits FILE` through
without selecting routing policy. Results are separately versioned at **3** and
require `request_sha256` plus unsigned 64-bit `expanded_states` and `work_used`
counters. Native apply rejects version1 and version2 results; regenerate them.
Work, admission, journal and nesting failures publish
empty edits and cannot be applied as a native board. The Rust README records the
charged operations, separately bounded initialization and exclusions; the work
counter is not elapsed time or a process-memory measurement.
Rust assigns the added item
UUIDs. The adapter checks source identity, requires a complete plan and applies
the exact geometry and IDs to a new file. The first slice cannot delete input
copper. This first supported subset is not complete board-tool compatibility.

Native apply requires `--request FILE`, retaining the exact document supplied to
the engine. Its SHA-256 must equal the complete result's non-null lowercase hex
`request_sha256`, in addition to the existing source-board match. The Python API
requires keyword-only `expected_request: bytes`; there is no optional or legacy
bypass. The CLI reads the expected request once, at most 16 MiB. It compares raw
bytes without JSON normalization: whitespace, key order, explicit defaults,
rules, limits and expansion budgets all affect identity. Thus a failed run B
cannot cause a retained result A for different rules to be applied as B, even
when both use the same board. Keep the request paired with its result:

```sh
rust-engine/target/debug/kicad-route-engine request.json result.json
# Continue only after checking the engine exit status.
/usr/bin/python3 tools/rust_engine_kicad.py apply source.kicad_pcb result.json output.kicad_pcb --request request.json
```

Run these commands from the repository root. Identity checks consistency,
not authenticated provenance or a fresh invocation nonce.
Identical request bytes have identical identity, so a caller still must inspect
process exit status. A fabricated result is not authenticated by its own hash.
Typed Rust `run(Input)` has no original wire bytes: it hashes the full compact
serde serialization with explicit defaults. For original wire identity use
`run_json` and retain those bytes. The Rust README describes typed serialization,
oversized/partial input, and hashing costs outside charged routing work.

An unwinding routing-executor panic produces an edit-free version3
`internal_error` result and exit 7 when publication succeeds. Its counters are
zero sentinels for unavailable measurements, as stated in its reason. Library
transactions still restore state and propagate the original panic. Failed
publication or argument/path refusal exits **8** and reports that no fresh result
was established; an older file may remain. Exit **2** means a fresh invalid or
unsupported result was published. A failed input read can publish invalid with
null request identity and exit 2; a publication failure instead returns 8.
Malformed but fully read bounded input retains its raw-byte identity. Oversized
and failed/partial reads have null identity rather than a misleading prefix hash.
Callers must check exit status before consuming a result.
Abort, SIGKILL, OOM, panic-abort builds and process death remain outside this
unwind contract. Direct route, reroute and batch failures describe the fixed
copper/grid/seed domain rather than asserting global infeasibility.

The native export/apply entrypoints limit each source board, plan, expected request,
rules/limits JSON and consumed DRC report to 16 MiB. Reads request at most the limit plus one
byte; source boards are checked before KiCad decoding. The internal snapshot
helpers expect an already admitted private snapshot. Export also refuses to publish
JSON exceeding the Rust input limit. Oversized inputs are `invalid`; oversized
DRC reports are `native_validation_failed`. JSON decoder and export encoder
recursion errors are typed; supported depths depend on the host implementation.
Export uses compact JSON to avoid quadratic indentation growth with nesting.
These byte limits do not measure or bound parsed/native allocations, report
generation, in-memory API arguments, CPU or whole-operation wall time. DRC keeps
its existing timeout; full-process cancellation and memory accounting remain open.

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
reviews and explicitly accepted continuing independent oversight; their addenda
and supervisor synthesis remain part of this work. Fable primarily reviews search,
state, transactions and orchestration. Carbon primarily reviews geometry, native
rules/outcomes and measured performance. Each records its own view before seeing
the other review. Agent-1497 adjudicates findings and exact-revision acceptance.
Review claims are checked against source: timing counters that cover selected
passes are not whole-engine profiles; rejected proposals do not prove failed
rollback; a proposed last-via-only state is insufficient for an all-prior-via
constraint. Carbon's corrected retained command places the foreign segment between the three
samples and demonstrates a 24 micrometre missed clearance intrusion. The first
report's midpoint coordinate was wrong. Both reports and their corrections
are linked from the [supervisor synthesis](https://artifact.harm.org/tubrxk/synthesis.html).

At `f931aad`, the combined gate passed 23 Rust and 19 native tests, including an
independent Dijkstra oracle over 256 small single-net obstacle worlds and the
actual Rust CLI routing a native two-terminal fixture around protected copper.
KiCad 10.0.8 reports one open before and zero after, with no introduced DRC
violation. The original axis-aligned tangency false-completion regression is
fixed; clearance and contact remain separate predicates. This is bounded fixture
evidence, not complete capability or performance acceptance.

Continuing review found a diagonal sub-nanometre contact disagreement, subsequently
reproduced with the actual Rust binary: 21 of 400 near-tangent pairs returned
complete despite a native open. `06ea2ca` requires a full nanometre of overlap
after flooring track radii, without changing clearance. In those 400 cases and
800 further contact samples, no false completion remained; conservative extra
routing remains possible. This sampling is not a universal KiCad rounding proof.
The native regression suite retains an actual diagonal counterexample and checks
that routing closes its open while preserving all incumbent copper.

Carbon's nested-checkpoint objection concerns a future extension: the current
crate-private mutation API and call graph cannot nest transactions, as documented
at `d7e488f`. The journal does not support nested commits. One-nanometre tracks
remain conservatively disconnected by the contact predicate; this can cause
extra routing or budget exhaustion and is not a manufacturability claim.
Both dispositions await independent review.

The combined gate after these corrections and the retained diagonal regression
passes 26 Rust and 24 native tests. Four native Margin controls prove that board
and footprint Margin drawings cause edge-clearance errors and are refused;
identical Dwgs.User drawings remain accepted. First-slice acceptance remains
open pending independent exact-revision review. Both continuing reviewers accepted
the correction scope at `3a763b7` but identified native validation inside the
operation as the next foundation gate. The integrated candidate now implements
that gate; its review remains pending. No production backend changed.

Run the combined focused gate with `mise run test:rust-engine`. It uses the
existing host process boundary, locked offline Cargo dependencies and the native
test fixture. It does not rebuild or invoke the installed legacy routing engine.
