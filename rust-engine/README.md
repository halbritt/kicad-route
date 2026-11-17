# Rust routing engine: first supported slice

This is a greenfield implementation. KRT is a behavioral reference and a source
of test cases, not an implementation to wrap, fork or continuously sync. Follow
the owner's [capability acceptance matrix](../docs/rust-parity.md); matching legacy
output does not prove correctness, and known bugs must not become target behavior.
The existing backend remains separate during migration. Upstream sync convenience
does not constrain this design; any source reuse retains required attribution and
license obligations.

This crate owns route decisions, geometry checks, search, copper state,
generated IDs, reroute rollback and output edits. It does not invoke the Python
router. The native adapter is outside this crate and owns KiCad extraction and
application of these edits; KiCad remains the external DRC/connectivity oracle.
This is an implemented subset, not a complete engine or fabrication claim.

```
cargo run --locked --manifest-path rust-engine/Cargo.toml -- INPUT.json OUTPUT.json
mise run host:exec --timeout 5m -- cargo test --locked --offline --manifest-path rust-engine/Cargo.toml
```

The version-1 input JSON contract is enforced with typed deserialization, including
unknown-field and duplicate-field rejection. Bounds are an axis-aligned
rectangle; only F.Cu, circular/axis-aligned rectangular pads and straight tracks
are supported. All incumbent tracks, locked or unlocked, are immutable. Connected net IDs
have at most two pads. Every nonzero net is partitioned into connected components
of all its pads and incumbent/generated tracks, including zero-pad or singleton
nets. A net is complete only when it has at most one copper component. Two pads
already joined do not excuse a detached incumbent track. Net ID 0 means
unconnected copper: any number of its pads/tracks are foreign obstacles, never
autorouted or joined as one net. Contact requires at least 1 nm of overlap through
pads/tracks and track chains; tangency and sub-nanometre overlap do not establish
connectivity.
Connectivity floors each track's half-width to integer nm, subtracts 1 nm
from the summed contact radii, then compares distances inclusively using exact
integer fractions. Rectangle pairs require at least 1 nm overlap on both axes;
rounded corners use Euclidean distance. A negative effective radius never
connects, including for a 1 nm wide track against a rectangle. Arbitrary-angle
incumbent tracks remain supported; this rule can add routing for contacts
KiCad already accepts. Clearance uses full widths and still permits equality
at the required gap.

Native KiCad 10.0.8 probes found 21 false completions in 400 near-tangent
diagonal track pairs with the old strict-positive-overlap rule. The 1 nm rule
removed those sampled false completions, with 138 conservative non-reuse
results where native connectivity already existed. Another 800 sampled
track/circle, track/rectangle-corner, circle/circle and circle/rectangle-corner
contacts had no false completion after this change. These are bounded contact
experiments, not proof of KiCad equivalence at all orientations, sizes or
sub-nanometre boundaries. Native validation remains required after routing.
Vias, zones, non-orthogonally rotated pads, custom rules, differential/length constraints and
other unrepresented native objects must be rejected by the adapter, never
silently omitted from the request.

Coordinates must lie within +/-100,000,000 integer nanometres (up to a 200 mm
span). Pad centers and track endpoints must lie inside bounds; positive pad
sizes and track widths are at most 100,000,000 nm. This is structural validation,
not a certification of existing copper clearance. Width/grid are positive and
clearances nonnegative, each at most 100,000,000 nm. Pad centers align to grid
relative to `bounds.min`. Nets have unique integer IDs and unique names; positive IDs require a
nonempty name, while net ID 0 requires the empty name. Item IDs are globally unique canonical lowercase non-nil UUIDs.
`source_id` is exactly 64 lowercase SHA256 hex digits, echoed unchanged; the
adapter checks it against the original board bytes. CLI input is at most 16 MiB.

Search uses four orthogonal directions, uniform one-step cost and an incoming
heading in the dominance key. Two isolated pads retain center-to-center A* with
a Manhattan heuristic. General component joins use multi-source Dijkstra
(zero heuristic) from every legal pad center and incumbent/generated track
endpoint in the first component, targeting all other components. Each seed
retains its own grid phase, so off-grid incumbent endpoints are supported.
Attachment targets the entire copper geometry, including arbitrary-angle
interiors, not only target endpoints. The final edge stops at the first integer endpoint in the target contact
interval. Exact swept-prefix contact is monotone for these convex targets;
bisection finds the first integer prefix and an endpoint-cap check rejects
intervals lying wholly between integer endpoints. No floating-point rounding
or extra contact margin is used. If several targets intersect an edge, the
nearest legal contact is selected independently of target order. A full grid
step past the target is not published as a dangling spur.
A shortened goal edge is checked for clearance before the full grid step is
rejected: an obstacle or board boundary beyond the attachment cannot invalidate
the shorter route. It is queued at the same one-step cost and must be settled
within the expansion budget before success. There is no bend cost,
layer change, arbitrary-angle search, rip-up negotiation or global multi-net
optimization. Nets route in increasing net-ID order. A later net can fail even
when a different earlier route would permit a solution; this is reported as
unroutable, not proof of global routing impossibility. Success/failure is
deterministic. Each completed join must strictly reduce the net's conservative
component count, and all joins share the operation/batch expansion budget.
Failure after earlier joins rolls all of that operation back. The expansion
budget is 1..=1,000,000 for the whole batch; settled
states, including the found goal, count once per state/direction. Stale queue
entries do not count. Frontier exhaustion and budget exhaustion are distinct.

The component search is not complete for continuous routing. A component whose
pad centers and track endpoints are all illegal seeds can fail even when a
mid-span exit exists. The grid may miss a legal corridor or an attachment away from its searched
edges. Along each searched edge, integer terminal endpoints are covered by the
convex-interval calculation; this does not add mid-span source seeds. Thin copper that the
conservative contact model cannot join fails honestly rather than reporting
complete. Native connectivity can merge contacts that this model separates or
apply other integer geometry rules; no universal native equivalence is claimed.
The mandatory native publication check remains necessary. Components are
recomputed after each join using borrowed nodes; graph contact scans can be
quadratic and are charged to total work, separately from search expansions. This increment does not
provide a wall-time or real-board quality guarantee.

Every proposed edge checks its entire swept copper against foreign-net pads
and incumbent/generated tracks. Exact integer-fraction comparisons in i128
cover segment/segment, segment/circle and segment/rectangle distance, including
corners and endpoint caps. Equality at the requested clearance is allowed.
Half-width plus edge clearance applies at both endpoints, sufficient for the
convex rectangular outline. The coordinate/size limits bound arithmetic;
critical squares/products, position steps and counters use checked arithmetic.

The copper store owns immutable input tracks, generated tracks, a generated-net
index and an undo journal. `Engine::transaction` exposes a borrowed transaction
view inside a closure. Returning `Ok` commits that scope; returning `Err` rolls
it back. A panic that unwinds through the closure rolls back and propagates its
original payload. A caller may catch an inner panic outside the inner scope and
continue the outer scope. A panic caught *inside* a scope does not exit it; its
eventual `Ok` or `Err` controls the outcome. This is in-memory unwind behavior,
not recovery from abort, process termination, allocation failure or a crash.

```rust
use kicad_route_engine::{Engine, model::Failure};

fn grouped(engine: &mut Engine) -> Result<(), Failure> {
    engine.transaction(|outer| {
        outer.route(1, 1000)?;
        outer.transaction(|inner| {
            inner.reroute(2, 1000)?;
            Ok::<_, Failure>(())
        })?;
        outer.route(3, 1000)?;
        Ok(())
    }).map_err(Failure::from)
}
```

`route`, `reroute` and `route_all` each use their own nested scope, including
when called through a transaction view. A handled operation error leaves prior
outer changes intact. `route` preserves already-connected copper; `reroute`
removes only generated copper for that net before searching. Both require a
known nonzero net and a budget of 1..=1,000,000 expansions for that operation.
Unroutable errors from all three methods name the fixed current copper, grid
and pad-center/track-endpoint seed domain, without claiming global infeasibility.
`route_all` retains its existing batch-wide expansion budget. The enclosing
transaction shares one total-work limit across all nested calls; expansion
limits remain per operation.

Inner commits retain their journal entries for an outer rollback; only the
outermost commit releases them. Rollback restores generated object fields and
IDs, the by-net index and the ID allocator; immutable input copper stays intact.
Each frame holds only its journal offset and allocator position, and undo work
is proportional to the changed generated objects, without whole-board copies.
Journal memory is admitted before mutation against a fixed entry limit,
including changes retained by every active outer scope. Scope entry checks a
nesting-depth limit before invoking the closure. Rollback and journal cleanup
remain available after forward work is exhausted.

Callers receive no checkpoint tokens, owning guards or mutable `Engine` access
inside a scope. The private guard remains bound to its original engine; borrowed
views cannot escape, be swapped across independent scopes, or access ancestors
out of order. Compiler-checked examples cover these rejected uses. Safe callers
cannot forget an owning guard to leave a scope open. Store frame/mutation methods
remain crate-private and are used only by these engine operations. Concurrent
mutation of an engine is excluded by its exclusive Rust borrow.

Each proposed segment is validated before insertion. A failed batch rolls back all earlier
generated routes and publishes no edits. Input JSON remains version 1 with
defaulted limits; output JSON is version 3. The CLI routes one fresh batch
and does not expose transaction or reroute commands. This API adds transactional ownership, not a routing strategy.

Generated IDs use a deterministic custom UUIDv8 layout: a prefix from source_id
plus a counter, skipping all reserved pad/track IDs. These are board-local stable
item identities, not content hashes or globally unique cryptographic IDs.
Removing and successfully rerouting an item creates a new identity; failed
rerouting restores the original one. Collinear path steps coalesce without
changing their swept geometry.

Output is atomically replaced. `complete` exits 0; `invalid`/`unsupported` exit 2,
`unroutable` exits 3, expansion `budget_exhausted` exits 4, total
`work_exhausted` exits 5, and `admission_exceeded`, `journal_exhausted` or
`nesting_exhausted` exit 6. An unwinding executor panic produces
`internal_error` and exits 7. Every structured failure has
empty edit lists and a reason. All results carry version 3, independently of
input version 1, and require unsigned `expanded_states` and `work_used` counters.
Consumers must refuse older or unsupported result versions, including old
version-1 results with missing counters and version-2 results without request identity. For `internal_error` only, both counters
are 0 sentinels meaning unavailable, not a measurement of work done; its reason
says so. A valid source ID is recovered when possible, otherwise it is empty.

The CLI catches unwinding panics around the routing executor only, after
library transaction scopes have restored state; direct library callers still
receive the original panic payload. The typed failure atomically replaces any
older complete output when publication succeeds. Abort, SIGKILL, allocation
failure and process death are not handled by this boundary. Output publication
failure exits 8 and stderr states that no fresh result was established and the
destination may still contain an older result. Callers must check the process
exit before using output; existence of an old file never establishes success.
Argument-count and same-input/output path refusals also exit 8 without writing
output. Thus 2 means a fresh invalid/unsupported result was published, while 8
means no fresh result was established. A read failure can publish an edit-free
invalid result (exit 2); if that publication also fails, exit 8 takes precedence.
No geometry/search acceptance policy is delegated to Python, and no default
backend or shared runtime is changed.

### Request identity

Every version-3 result contains `request_sha256`. Complete results require a
64-character lowercase SHA-256 digest. For the CLI and `run_json`, this is the
hash of the exact input bytes, without normalization or a domain prefix. It
covers rules, limits, expansion budget, copper and all other fields. Whitespace,
key order and explicit versus omitted defaults also change the identity.
Unchanged bytes yield the same identity; this is request consistency, not an
invocation nonce, authentication or proof of provenance.

A fully read request of at most 16 MiB gets its digest even when malformed or
when its executor panics. An oversized request, failed or partial read, or
`Output::failure` constructed without a request has JSON null in this required
field. Oversized input is never identified by the CLI's bounded prefix, and
`run_json` follows the same rule even when supplied an entire oversized slice.
Null identity is allowed only for failure output; it is never permission to
apply edits. Consumers must retain the expected request document and compare
its exact-byte digest with the result, in addition to checking the board's
`source_id`, result version, status and counters. A same-board old result cannot
satisfy a changed request. An old complete file may remain after exit 8; its
identity still belongs to its original request.

Typed `run(Input)` has no original wire bytes. Its identity is the hash of the
full compact `serde_json` serialization of `Input`: all defaults explicit,
struct fields in declaration order, vector order retained. `Input` and nested
input types implement Serialize, and `input_request_sha256(&Input)` exposes the
same digest without routing. Serialization streams into the digest without
allocating another whole document. The typed API keeps its structural/item
limits and does not impose the wire's 16 MiB cap or hash a prefix. For serialized
inputs within that cap, `run(input)` and `run_json(serde_json::to_vec(&input))`
agree. Parsing arbitrary original JSON to Input loses formatting and omitted
fields; do not expect that original byte hash to equal the typed hash. Use
`run_json` and retain the original bytes when byte identity is needed.

Digesting/serialization is outside charged routing work, like JSON parsing;
it does not refund or alter routing counters. SHA-256 uses RustCrypto
[`sha2` 0.11.0](https://docs.rs/sha2/0.11.0/sha2/), pinned with transitive versions
and registry checksums in Cargo.lock. No custom digest algorithm is used.

Tests compare direction-state A* against an independent position-only Dijkstra
oracle over 256 small obstacle worlds (the oracle uses no production geometry
or search). Other checks cover exact swept clearance, odd half-widths and
boundaries, competing nets, total budgets, last-net failure, failed reroute
restoration, deterministic IDs, immutable incumbents, malformed/unsupported
input and the executable output protocol. These tests do not measure real-board
quality or speedup. Obstacle scans are currently linear; input-copper
connectivity checks can be quadratic in a net's track count.

The component-search test oracle enumerates every integer terminal endpoint
on every reachable grid edge in 4,096 small axis-aligned worlds. Independent
box-gap geometry checks clearance/contact, and position-only breadth-first
search checks reachability and unit-step cost. Cases vary grid phase, width,
clearance, circle/rectangle/track targets and all subsets of six obstacles.
This bounded enumeration does not prove arbitrary-angle geometry, continuous
completeness, component ordering or multi-net optimality. Separate diagonal
contact controls and mandatory native checks cover different concerns.


## Work and storage limits

An optional version-1 input `limits` object supplies the following unsigned
integers. An omitted object or field uses its default; unknown fields fail.
Zero is allowed to refuse any work, storage or nesting of that kind. These are
resource defaults, not board-specific routing settings.

| Field | Default | Maximum accepted | What it bounds |
| --- | ---: | ---: | --- |
| `max_work` | 100,000,000 | 1,000,000,000 | Charged forward units per outer operation/transaction |
| `max_items` | 100,000 | 1,000,000 | Input nets + pads + tracks + live generated tracks |
| `max_search_entries` | 1,000,000 | 4,000,000 | Combined heap + best-map + parent-map entries; also seed-set size |
| `max_journal_entries` | 1,000,000 | 1,000,000 | All retained undo entries, including inner commits |
| `max_depth` | 64 | 256 | Active transaction frames, including routing method scopes |

`Engine::new` admits input cardinality before indexing/validation. Net names
are at most 1,024 UTF-8 bytes (the refusal reason states this limit); IDs have
their existing fixed sizes. Validation uses
bounded linear/logarithmic collections, including pad counts by net rather
than a net-by-pad quadratic scan. Both the JSON library entry and CLI accept
at most 16 MiB. Native decoding/export remains the adapter's separate work.
The defaults cap storage without using a board's coordinates, nets or outcome.
They retain all existing witness coverage; they are not a wall-time promise.

`model::Limits` configures an engine at construction and is immutable thereafter.
`Engine::work_used()` and `Transaction::work_used()` expose charged work in the
active or last outer scope. Output and `Failure` expose `work_used` separately
from `expanded_states`. A failed charge does no associated work and consumes
no units; a bulk reservation can therefore refuse with unused units remaining.
Scope entry, child errors and child rollback never replenish the active
counter. A later independent outer operation starts a fresh counter. A caller
may handle a child failure and commit earlier valid work; the child is rolled
back and its work stays spent. Increasing a child's expansion allowance cannot
increase the parent's total-work allowance.

`transaction<T,E>` now returns `Result<T, TransactionError<E>>`: `Operation(E)`
preserves a closure's error, and `Limit(Failure)` reports refused scope entry
without invoking the closure. `From<TransactionError<Failure>> for Failure`
flattens the wrapper for callers using the engine error type. `route`, `reroute`
and `route_all` still return `Failure`. `legal_edge` now returns
`Result<bool, Failure>` rather than collapsing work exhaustion into blocked
geometry. Public standalone search/edge queries get a finite work scope; inside
an active transaction they use its counter. No unlimited legacy routing path
is retained. Borrowed transaction and unwind guarantees are unchanged.

### Accounting inventory

One unit is a deterministic bounded engine action, not one CPU instruction.
Charges occur before the corresponding work or allocation. A bulk charge of N
reserves N iterations before an array build, sort or reverse.

| Work | Charge |
| --- | --- |
| Scope admission | 1 per entry attempt |
| Net enumeration, sorted list, dispatch, selected-net lookup | 1 per visited/reserved net per stage |
| Component item enumeration, seen initialization, root visits, pending pops, pair visits | 1 per item/iteration; pair visits count even when already seen |
| Component pair contact, source/goal cap contact, target prefix/cap contact | 1 per exact geometry predicate, including every bisection probe |
| Source anchors and target enumeration | 1 per source item/anchor/target before processing |
| Edge outline check, pad/track scans, foreign geometry | 1 per edge check, item visit, and applicable geometry predicate |
| Search seeds, successor attempts, admitted queue pushes and all pops (including stale) | 1 per action; push includes bounded map updates |
| Path reconstruction, reversal, coalescing and proposal construction | 1 per visited/reserved point per stage |
| Copper insert/remove | 1 per mutation; 1 per ID candidate; removal additionally reserves 1 per copied ID |

Admission checks bound combined queue/maps before insertion, total copper before
insertion, undo history before mutation, and frame depth before push. Collection
capacity growth and balanced-tree/heap comparisons are included in their
bounded abstract action, not charged per allocator byte or comparison. Search
maps/queue can hold stale entries only within the admitted sum. Seed and path
scratch storage is additionally bounded by admitted item/search cardinalities;
these limits are entry counts, not an exact process RSS cap.

Deliberate exclusions are input reading/JSON parsing and `Engine::new`
validation/index construction (bounded separately as above), scalar bookkeeping
and fixed-size internals of exact predicates, allocator/runtime implementation
costs, dropping temporary collections, rollback and commit cleanup (bounded by
admitted search/items/journal/depth), output cloning/serialization/I/O (bounded
by admitted copper), independent public `geometry` helpers, caller-directed
copper inspection/diagnostics, and arbitrary caller code inside closures.
Inspection and caller loops do not constitute a routing operation; nothing
promises to interrupt arbitrary Rust code. Allocation failure, abort, kill,
and process-wide wall-clock deadlines remain outside this in-memory contract.
No work charge is attempted during rollback, so exhausting forward work cannot
prevent restoring the journal, IDs and index or replace an unwinding panic.

Budget tests include exact independently counted singleton boundaries, exhausted
work before quadratic scans, every work cutoff of a component join with state
and next-ID checks, repeated nested reroutes, shared counters across nets,
pre-mutation journal/depth admission, panic payload preservation and CLI/library
counter equality. The two existing search oracles and all their world inputs
remain intact. The contact test helper still excludes rectangle/rectangle
pairs; none of the budget tests extends that helper's domain.
