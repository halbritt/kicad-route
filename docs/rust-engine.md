# Rust routing engine migration

The replacement is a greenfield routing engine in Rust. KRT is a behavioral
reference and a source of test cases, not an implementation to wrap, fork or
continuously synchronize. Upstream sync convenience does not constrain the new
design. Rust owns copper state, geometry and legality, search, net
ordering, speculative edits and restore, and the exact output edit list. Existing
`src/kicad_route/router` code stays intact as a reference and current backend.
The standalone Rust command is `kicad-route-engine route-native`. In agent-ee,
the builtin backend selects this command when the checkout supports it. The
separate legacy kicad-route CLI retains its existing backend selection.

Heath's explicit direction is `owner-greenfield` in the requirements catalog
(Cairn `d159855b-5bc5-4632-b43d-5ab2d70ea942/v1`). Replicate useful KRT capabilities
with representative boards and explicit acceptance tests; do not reproduce known
bugs or treat matching KRT output as correctness. The [parity matrix](rust-parity.md)
separates demonstrated fixture behavior from missing capabilities. Retain the
existing KRT backend separately during migration. Record any source reuse and
honor its attribution and license obligations.

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

Raw requests express `effective_rules.via_cost` in grid steps. Fractional
layer weights use one million ticks per step. A via cost that overflows when
scaled, or an A* priority that exceeds the unsigned 64-bit range, returns
`invalid` with an empty edit plan.

The multi-net search divides its expansion budget among joins and repair
attempts. Replaying a route requires its original limits. A completed run's
`expanded_states` records how much work that run spent on expansions.
Supplying that count as a new limit can change the route or exhaust the budget.

The Rust board-level controller joins connected components on each net, chooses
a deterministic net order, and can rip up generated copper to reroute competing
nets. When two single-ended nets with at least four pads use the same footprint
and copper layer, the controller can lay clear branches on both nets before
routing either whole net. Those branches use the board's search budget and
remain available for rip-up. During an initial two-pad search, a generated net
that blocks through-via sites and track moves on every inner layer can be
tried for early rip-up. The trial charges the board's budget and restores the
previous copper and congestion costs if either net cannot reconnect.
During local repair, a multi-pad net can keep branches it has already connected.
If the repair displaces another multi-pad net, the controller can reuse a whole
branch that still joins two pads and passes clearance checks against the changed
copper. When a short branch ends on the centerline of a generated track and
its far end also overlaps that track, the router splits the track at the
junction. Both pieces keep the trunk's width and straight-line geometry.
The router handles either insertion order and restores the original trunk
and its identity if the transaction rolls back. The controller stages the
resulting edits and publishes the board after native checks.

## Native compatibility interface

A successful standalone `route-native` operation publishes the routed PCB
with its original native rules. It installs the admitted `.kicad_pro` and
`.kicad_dru`, when present, under the output basename before it links the
PCB. Existing byte-identical copies are reused. A conflicting or extra
companion causes `publication_refused` (exit 8) before the board is linked.
A matching partial set is completed. A source without either file produces
only a PCB. The request and operation-report paths must be distinct from
both boards' companion paths.

Native DRC uses the source and output basenames in separate private
directories. Each PCB has the admitted custom rules and project settings.
The bridge clears saved DRC exclusions in both private project copies so it
can compare the underlying findings. For an input symlink, the canonical
board supplies the source basename and companions. If the input has no
project, the operation uses KiCad's defaults and omits `request.project_path`
from its result. For project library checks when reopening the PCB, supply
the original library tables and the libraries they reference.

The PCB's atomic link is the commit point. Readers may briefly see the rule
files first. The Linux publisher locks the destination directory for the
short commit section. If interrupted before it links the PCB, the staging
owner removes the companion links created by this operation. It preserves
existing files. If the host stops or both the publisher and its cleanup helper
are killed, private staging files or matching companions can remain. A retry
can complete a matching partial set. A rule-file change detected after the PCB link returns
`ambiguous_termination` (exit 8) and preserves the board. Keep the
destination project stable while opening or regrading the result.

KiCad text variables such as `${PROJECTNAME}` can render different copper or
solder-mask strokes when a PCB is staged or reopened under a different name.
For board and footprint text graphics on these layers the adapter accepts
literal text and a standalone `${LAYER}` with a literal layer name. On a board
without assembly variants, a footprint text graphic can also show its literal
`${REFERENCE}` or `${VALUE}`. Other variable expressions in this
artwork are reported as unsupported before routing or applying edits.
Silkscreen and fabrication text can use project variables, including the PCB
filename and project name. The adapter refuses text whose rendering depends
on the PCB's absolute path, which changes in private staging.
KiCad also expands variables in `.kicad_dru` before parsing rule conditions
or removing comments. A multiline project variable in a comment can introduce
a rule. The adapter therefore reports variables anywhere in this file as
unsupported.

`tools/rust_engine_kicad.py` uses installed `pcbnew` only to decode native KiCad
objects and encode Rust's explicit edit list. It does not select paths, victims,
IDs, geometry or routing costs. This is justified by KiCad's native Python
bindings; a Rust KiCad-format reader can replace it later without moving engine
policy. Applying a plan independently validates the saved native board before
publishing it. New tracks and vias must belong to the nets selected by the
retained request, and any reported pair or bus must stay inside that selection.
For a partial route, each selected net must have all its terminal pads
connected and zero native DRC opens. KiCad can choose different pads or
tracks to describe an existing open. On each unselected net, validation
checks the open count and verifies that every previously connected group of
pads, tracks and vias remains connected. Full-board routing requires zero
open connections. Both paths require no new native DRC violations and added
widths at least the native minimum. A missing or failed DRC command also
refuses publication. Validation failures have a `native_validation_failed`
CLI result on stderr and exit 2. Expected non-native failures also exit 2 with
JSON on the final stderr line: `invalid` for malformed/stale requests,
`unsupported` for unrepresented native features, `output_exists` for an existing
destination, and `io_error` for input/output failures. Argument-parser usage
errors and unexpected programming errors are not converted to routing outcomes.
When a project has custom rules, validation also runs KiCad on a private
copy with a temporary track and a rule that forbids that track. KiCad can drop
a rules file that fails to compile while returning success. Publication
requires the DRC finding for the temporary track. The source and candidate
receive their ordinary DRC checks under the original rule text.

Duplicate JSON fields, malformed track fields and zero-length additions are
rejected before publication. Exported JSON also uses staged create-if-absent
publication, so a failed write cannot leave a partial request at its destination.
Publication atomically creates a new destination;
it does not overwrite a concurrent writer's output.
Export and application each read the standalone source once into a private
snapshot. Its bytes determine the source hash, parsed geometry and DRC baseline;
later edits to the original are preserved and cannot change that operation's
baseline. Native DRC runs only on private copies and includes `--all-track-errors`,
so KiCad creates its preferences files beside those copies. The direct Python
adapter uses an atomic link for visibility and can leave private temporary
files after abrupt process death.

The standalone Linux `route-native` command also supervises the native child
process group and its private staging directory. A killed native child causes
exit 7 with `child_process_error` and the terminating signal. In a 64 MiB
memory-limited run, the kernel killed the export child. The router removed
its staging files and preserved the original board. The destination remained
absent. If the router itself dies during a native phase, a surviving
helper terminates its descendants and removes the staging directory. If the host kills both the router and every helper, the caller or its
temporary filesystem owns any remaining staging files.

The Rust CLI links the completed board before it writes the optional operation
report. A Linux regression interrupts the parent after the board directory has
been synchronized and at the operation-report link. KiCad reopens the same board
bytes as the uninterrupted run, and the source and rule files remain intact.
A caller that finds a board after an interrupted operation can regrade it using
the original project. If directory synchronization fails after a board link,
the CLI returns exit 8 and reports the visible board's digest. The same exit
applies if the saved request or operation report was linked but its directory
could not be synchronized. In the latter case the report can still describe
success. The caller must check the process exit and its final diagnostic.
The regression requires Linux `strace` with syscall fault
injection and permission to trace its own child process.

The native adapter accepts boards with two to 32 copper layers and reads the
project and custom-rule files beside the source board. An empty project or one
with no declared format version uses the Default netclass that KiCad loads from
the board and project. Its track width, clearance and through-via dimensions
must be resolved. Any board-wide minima and custom rules still apply. An
explicit project format version must be in the supported range of 1 through 3.

For a net assigned to several classes, Rust resolves each routing dimension
from the first class that defines it. KiCad net settings through version 3 use
the class list order. Later versions sort by ascending numeric priority and
then by class name. Version-0 projects using legacy overbar notation in class
assignments require migration in KiCad. Missing track and via dimensions inherit from Default.
Custom rules can select a constituent class and raise its dimensions. In
comparisons on item type or net name, KiCad matches the left value against the
case-insensitive pattern on the right. A custom minimum or maximum drill
diameter uses the native `hole_size` constraint.

Before publication KiCad reloads the candidate and the adapter checks the width of
each new track and the diameter and drill of each new via against the native
effective class. Requested differential pairs use their differential width.

Raw requests must list each copper layer at most once per pad. Native board
and netclass dimensions use the same 200 mm upper bound as parsed project
dimensions. The router checks them before calculating clearances or via sizes.
A positive meander amplitude is a ceiling. Search samples excursions up to
the board diagonal and checks the resulting integer coordinates.

Copper geometry uses
KiCad's standard layer names, such as `F.Cu` and `In1.Cu`. When the board
renames a copper layer, the optional `board_design_settings.layer_aliases` map
in the request associates its standard name with its display name. KiCad
custom rules can select a layer by either name. Applying edits preserves the
display names on the saved board.

The adapter represents supported pad shapes, drills, tracks, vias, copper
artwork, filled zones and keepouts. Hatched zones and board or footprint
Margin geometry, for example, cause an explicit refusal. The
[capability matrix](rust-parity.md) records the tested forms and board outcomes.

Paste-only stencil apertures without a drill or a copper net survive export
and application as footprint features. They contribute no electrical terminal.
Supported custom pads include connected unions of a rectangular or circular
anchor and supported filled primitives. Rust retains the pieces as one pad
when testing contact and clearance. The importer identifies a custom pad with
disconnected copper outlines when KiCad rejects it.

When routing all nets with the default routing options, native refill can
split a zone that joined two pads in the original board. If KiCad reports open
connections after refill, it supplies the resulting zone and teardrop copper
to Rust. Rust can join the islands once using the original operation's
remaining work and expansion budgets. Its previous tracks and vias are fixed
during this search. It reapplies the combined routing edits to the original
board and repeats connectivity, DRC and pour checks before publication.
The operation result records both native checks and the Rust repair.

The version1 input uses integer nanometres, explicit net and item IDs, bounds,
rules, fixed copper, an expansion budget and optional resource `limits`. Rust
owns the limits and their defaults; native export passes `--limits FILE` through
without selecting routing policy. Results are separately versioned at **3** and
require `request_sha256` plus unsigned 64-bit `expanded_states` and `work_used`
counters. Native apply rejects version1 and version2 results; regenerate them.
In a native operation, Rust checks the wall-clock deadline and cancellation
signal during charged work, including search after zone refill. The next
check occurs after 8,192 additional work units or a charge that crosses that
threshold. Parsing, native calls and transaction cleanup have separate
execution costs.

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
The byte limits apply to serialized files. The Rust supervisor separately
enforces its operation deadline, terminates native child processes and records
phase times. The matched runs in [the parity matrix](rust-parity.md) report
measured process RSS. Native KiCad and JSON allocation sizes depend on the
input and the host runtime.

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

## Ownership and evidence history

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

At `d7e488f`, Carbon's nested-checkpoint objection concerned a future extension:
the crate-private API could not nest transactions and its journal did not support
nested commits. Later scoped nested transactions supersede that early restriction;
see the current requirements catalog and crate README. One-nanometre tracks
remain conservatively disconnected by the contact predicate; this can cause
extra routing or budget exhaustion and is not a manufacturability claim.
Both dispositions awaited independent review at that historical checkpoint.

The combined gate after these corrections and the retained diagonal regression
passes 26 Rust and 24 native tests. Four native Margin controls prove that board
and footprint Margin drawings cause edge-clearance errors and are refused;
identical Dwgs.User drawings remain accepted. At that point, first-slice acceptance
remained open pending independent exact-revision review. Both continuing reviewers accepted
the correction scope at `3a763b7` but identified native validation inside the
operation as the next foundation gate. The subsequent candidate implemented
that gate. Later accepted milestones in the requirements catalog supersede this
early pending-review status. No production backend changed.

Run the combined focused gate with `mise run test:rust-engine`. It uses the
existing host process boundary, locked offline Cargo dependencies and the native
test fixture. It does not rebuild or invoke the installed legacy routing engine.
