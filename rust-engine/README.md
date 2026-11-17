# Rust routing engine: first supported slice

This crate owns route decisions, geometry checks, search, copper state,
generated IDs, reroute rollback and output edits. It does not invoke the Python
router. The native adapter is outside this crate and owns KiCad extraction and
application of these edits; KiCad remains the external DRC/connectivity oracle.
This is an implemented subset, not a complete engine or fabrication claim.

```
cargo run --locked --manifest-path rust-engine/Cargo.toml -- INPUT.json OUTPUT.json
mise run host:exec --timeout 5m -- cargo test --locked --offline --manifest-path rust-engine/Cargo.toml
```

The version-1 JSON contract is enforced with typed deserialization, including
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
quadratic and are not charged as search expansions. This increment does not
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
    })
}
```

`route`, `reroute` and `route_all` each use their own nested scope, including
when called through a transaction view. A handled operation error leaves prior
outer changes intact. `route` preserves already-connected copper; `reroute`
removes only generated copper for that net before searching. Both require a
known nonzero net and a budget of 1..=1,000,000 expansions for that operation.
`route_all` retains its existing batch-wide expansion budget. The enclosing
transaction does not impose an additional combined search budget.

Inner commits retain their journal entries for an outer rollback; only the
outermost commit releases them. Rollback restores generated object fields and
IDs, the by-net index and the ID allocator; immutable input copper stays intact.
Each frame holds only its journal offset and allocator position, and undo work
is proportional to the changed generated objects, without whole-board copies.
Journal memory grows with changes retained by active outer scopes. Repeated
reroutes can therefore consume memory until the outer scope ends. The API has
no configured nesting-depth limit; callers must bound nesting and work.

Callers receive no checkpoint tokens, owning guards or mutable `Engine` access
inside a scope. The private guard remains bound to its original engine; borrowed
views cannot escape, be swapped across independent scopes, or access ancestors
out of order. Compiler-checked examples cover these rejected uses. Safe callers
cannot forget an owning guard to leave a scope open. Store frame/mutation methods
remain crate-private and are used only by these engine operations. Concurrent
mutation of an engine is excluded by its exclusive Rust borrow.

Proposals are validated before insertion. A failed batch rolls back all earlier
generated routes and publishes no edits. JSON version 1 and CLI behavior are
unchanged: the CLI routes one fresh batch and does not expose transaction or
reroute commands. This API adds transactional ownership, not a routing strategy.

Generated IDs use a deterministic custom UUIDv8 layout: a prefix from source_id
plus a counter, skipping all reserved pad/track IDs. These are board-local stable
item identities, not content hashes or globally unique cryptographic IDs.
Removing and successfully rerouting an item creates a new identity; failed
rerouting restores the original one. Collinear path steps coalesce without
changing their swept geometry.

Output is atomically replaced. `complete` exits 0; `invalid`/`unsupported` exit 2,
`unroutable` exits 3 and `budget_exhausted` exits 4. Every structured failure has
empty edit lists and a reason. I/O publication failure exits nonzero and must
not be interpreted as fresh output. No geometry/search acceptance policy is
delegated to Python, and no default backend or shared runtime is changed.

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
