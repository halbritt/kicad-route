# Rust routing engine

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
mise run host:exec -- --timeout 5m -- cargo test --locked --offline --manifest-path rust-engine/Cargo.toml
```

## Route a KiCad board

From the repository root, build the native executable and give it a source board
and a fresh output path. Keep the original project and rule files beside the
source board. The destination directory must exist.

```sh
cargo build --release --locked --offline --manifest-path rust-engine/Cargo.toml
rust-engine/target/release/kicad-route-engine route-native SOURCE.kicad_pcb OUTPUT.kicad_pcb
```

Rust stages the input, routes the requested nets and publishes after native
KiCad validation. The operation result on stdout records the phase that
failed or the board that was published. Read both the process exit code and
the result. Run `route-native --help` on the executable for options covering
rules, limits, pair routing and length matching.

If the source has a KiCad project or custom rule file, the router publishes
each one beside the PCB under the output basename. For example,
`routed.kicad_pcb` uses `routed.kicad_pro` and `routed.kicad_dru`.
If a file already exists at either path, its contents must match the
corresponding source file. A conflict prevents publication.

The router grades and publishes the project's `fp-lib-table` when present.
Absolute paths and installed-library references such as
`${KICAD10_FOOTPRINT_DIR}` retain their spelling. For `${KIPRJMOD}` or
`$(KIPRJMOD)` references, KiCad looks in the project directory. The router
keeps these references when the destination contains the same libraries.
Otherwise, it writes absolute paths to the source libraries in the new table.
Keep those libraries accessible to reopen or check the saved PCB. To move the
project independently, place equivalent libraries at the destination before
routing. The router preserves an equivalent destination table and refuses
publication when an existing table conflicts with the source project.

Use `--nets 'SIG_[AB]'` to select `SIG_A` and `SIG_B`, or
`--nets '!SIG_[AB]'` to exclude both. A pattern without a slash can match the
last component of a hierarchical net name. A pattern containing a slash
matches the full name. To select the literal bus net `DATA[0]`, use
`--nets 'DATA[[]0]'`. Use `--nets '\!RESET'` for a literal leading
exclamation mark. Native apply checks that every proposed track and via is
on a selected net. KiCad refills the whole board when pours or teardrops
require it. If that refill creates a teardrop on an excluded net, the router
names the net and leaves the output unpublished.

After linking a board, saved request or operation report, the controller opens
and syncs its containing directory. A failure after the board link preserves
the board and its recorded digest and returns exit 8 with
`ambiguous_termination`. If the operation report's own directory sync fails,
the linked report contains the state before that sync. The final stdout result
and process exit include the error.

On Linux, each standalone operation keeps a private staging directory and
launches the native phase in a dedicated process group. If the router dies
during a native phase, a surviving helper terminates that group and releases
its staging directory. An interruption before publication leaves the destination
absent. A caller can detect an abrupt exit from the process status even when
the router could not finish writing its operation result.

The [capability matrix](../docs/rust-parity.md) describes current multilayer
routing, net classes, supported custom rules, fills, keepouts, negotiation
and pair tuning. The measurements there give the board, rules and revision
for each result.

On reversible multilayer joins, the router uses fixed tracks and solid
rectangular keepouts to estimate how far a trace must travel around an
obstacle. A layer-specific obstruction can be crossed by changing layers.
A route that must finish on the face where it started needs a second via
to return if it takes this detour. A via may itself finish a connection
when the target copper and board rules permit it.
When a rectangular keepout spans the board from edge to edge on selected
layers, the search counts the layer changes needed to cross successive
keepouts and reach the terminal. A route can pass an all-layer keepout only
around an open end. Tracks added while routing are checked for clearance at
each step and remain available for negotiation.

For an uncapped surface pad in a rectangular pin field, an exhausted search
can also try an offset grid that passes through a legal via site between
neighboring lands. A land's via clearance must actually restrict the passage.
The ordinary track, via and net-class rules apply to the escape.

When routing resolves differential pairs, the result lists their net IDs and
names in `diff_pairs`. A `coupled` disposition identifies both legs routed as
a pair unit. An `existing` disposition identifies connectivity already present
in the input. Each row includes the net class, design width and gap and, when
requested, the solved impedance plan. These are routing parameters. Native
measurements on the saved board separately establish the actual width, gap
and length of the copper. The native operation report carries the same rows in
`routing.diff_pairs`.

Automatic pair discovery uses case-sensitive names on the same schematic
sheet. It recognizes paired `_P` and `_N`, `+` and `-`, or bare `P` and `N`
suffixes. A lone selected `RESET_N` can stop auto discovery, and names such
as `12V+` and `12V-` can select the class's pair width. For numbered or
lowercase suffixes, or mixed power and signal names, pass each intended
pair explicitly, for example `--diff-pairs 'D_P1,D_N1'`. Other selected nets
then route as ordinary signals. Use the names from the PCB, including any
sheet prefix.

Use `--diff-pair-intra-match` to tune the lengths of a pair's two legs.
A coupled `--length-match-group` can also use `--length-match-tolerance`
and `--meander-amplitude`. Single-ended groups have their own
`--group-*` controls. A native skew rule on a tunable pair supplies its own
tolerance and accepts `--meander-amplitude`. AC chains use
`--ac-meander-amplitude`. The router rejects a pair tolerance or amplitude
when the request has no matching operation that uses it.

### Coupled centerline search

For a fixed pair of terminal fanouts, a search label stores its grid point,
layer, heading and whether the preceding move was straight, a turn or a via.
The turn after a grid via costs 300,000 extra units. A turn elsewhere costs
25,000. A via immediately following a turn on a grid finer than 800,000 nm
also costs 300,000 extra units. Every label retains its own parent chain.
Two prefixes at the same pose can yield different mitered tracks, via jogs
and separation from earlier copper when the router builds the complete pair.

The search favors the first or cheaper arrival at each pose. Other arrivals
remain in a separate queue. Every eighth expansion can visit one of those
alternatives, and they consume the same work, expansion and snap allowances.
Within each queue the lower estimate is the accumulated cost plus the cost
of an obstacle-free octilinear walk to the end fanout's snap box. Each axis
needs as many whole grid steps as it takes to enter the six-step radius.
The estimate uses the cheapest rounded axial and diagonal step costs on the
pair's common layers. A via changes no coordinate and costs at least zero.
The bound reaches zero inside the box, including on a different layer.
The snap generator tests the allowed end layers and endpoint via sites,
and tries a finite set of octilinear tails. A tail can change layers and
adds no search cost. The full copper checks still apply to that tail.
Every grid transition between layers must leave a track run on each side.

The router tries at most 64 ranked fanout combinations on direct paths and
16 per grid spacing in the centerline search. Each centerline attempt can
expand at most 35,000 labels or the requested expansion limit, whichever is
smaller, and try at most 128 snap sites (including repeated visits with
different prefixes). Shared work and negotiation budgets can end it sooner.
The search checks each proposed via against earlier vias in the same pair
using the effective board rules, including hole spacing. It returns after
both legs pass geometry and connectivity checks.
A candidate that fails the connectivity check rolls back its copper and IDs.
The next candidate uses the same frontier and remaining work budget. Both
routing entries use a nested transaction for this check, subject to the
request's transaction depth and journal limits. Failed validations retain
their graph-building and traversal work charges. The coupled search tests
compare the snap estimate against independent finite-grid Dijkstra results.
They also send two prefixes with the same final pose through the copper
builder. The shorter prefix crosses the shared terminal approach. The
longer prefix connects both nets.
These bounds and the enumerated fanouts, sampled grid spacings and tail
shapes limit reachability. Exhausting a local snap or expansion allowance
advances to the next fanout or grid spacing. Exhausting the public search
storage or work allowance returns `admission_exceeded` or `work_exhausted`.
Storage includes the immutable labels, both queues, the expanded-pose map
and the temporary reconstructed chain. Physical length, via count and total
pair cost carry no optimality guarantee.

### Signal layer policy

Pass `--layer-policy-json` to `route-native`, or supply the same object in
`routing_options.layer_policy` for a saved request. For example:

```json
{"signal_layers":["F.Cu","B.Cu"],"layer_costs":[1.5,1],"per_net_signal_layers":{"RF":["F.Cu"]}}
```

The router creates tracks only on the declared signal layers. An entry for a
named net further limits its tracks and every copper layer traversed by its
vias. With only a global declaration, a through via may connect the permitted
outer faces across the inner layers. Allowed layers use the board's physical
stack names. The original board's copper and design rules still apply.

The native `measure-layer-policy` inventory reports via diameters in
nanometres. Its `diameter` field gives the diameter on the via's top layer.
When the diameter varies across its span, `diameters_by_layer` lists the
diameter on each enabled copper layer the via reaches.

Optional track cost multipliers range from 1 through 1000 in signal-layer
order. They weight planar steps in both search directions and coupled-pair
path selection. Fractional multipliers have a resolution of one millionth.
A lower weight can favor a longer physical path on a preferred layer.

### Optional single-ended bus routing

With `routing_options.bus`, the engine looks for adjacent terminal banks among
the single-ended nets selected for routing. It uses the longer of each net's X-
and Y-extreme pad spans, checks nearby endpoints at both banks, and requires
the lanes to keep their transverse order and share a longitudinal run. Extra
terminals remain part of their original net. The scheduler routes members from
the middle out, retaining the scheduled positions of unrelated nets and pairs.

Each member keeps its transverse offset from the group's average direction.
When two search states have the same estimated total cost and accumulated
routing cost, the queue prefers the edge with the closer midpoint. The rank
increases with distance up to `attraction_radius_nm`. Its maximum is
`attraction_bonus` divided by 1000, rounded up. A zero bonus enables ordering
alone. The base grid, via and congestion costs still determine shortest paths.
An obstacle or a branch can take the route outside the preferred lane.
The Rust detector uses geometry, so nearby unrelated signals with matching
banks can qualify and wide fan-outs can be missed.

The engine represents a copper or solder-mask arc by its start point, a point on
the sweep and its end point. For clearance it widens the chord strokes by a
bound on their deviation from the circle. For connectivity it narrows them by
the same bound. Subdivision retains all three supplied points and uses at most
64 chords for a clockwise or counterclockwise sweep. A generated trace finishes
at an arc when its centerline reaches the arc stroke or an original arc end
lands on the trace. This also permits a connection in the interior of the sweep.

A new trace ending on a filled copper graphic or zone fill carries its
centerline onto the copper. Pads, graphic strokes and trace junctions require
an overlap of at least the smaller feature radius. A via joining a surface must
reach the surface with its land and have copper in common outside the drilled
opening. The search rejects a via whose drill would remove an entire pad on
its layer span. It can leave a small terminal on a trace before changing layers.
A clipped trace from a via can extend within its last grid step to reach both
the annulus and its terminal. A compound pad can supply contact across its
connected primitives. The component search checks layer spans, polygon
cutouts and the actual curved edges. It can extend a clipped pad approach
before accepting it. If the proposed copper cannot join both islands, the
search continues against the landing geometry using its remaining work and
expansion budgets.
Existing connections between input items retain their native contact rules.

## Original single-layer JSON routing subset

The following geometry, search and validation description documents the
original front-copper implementation and its acceptance tests. The native
board path also supports the multilayer features described above.

The version-1 input JSON contract is enforced with typed deserialization, including
unknown-field and duplicate-field rejection. The original subset combined a
rectangular outline, F.Cu pads (circles, orthogonal rectangles, roundrects or
ovals) and straight tracks. All incumbent tracks, locked or unlocked, are immutable. Nonzero nets may have any number of pads within the admitted item and work
limits. Every nonzero net is partitioned into connected components
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
Later native releases added via, zone, rotated-pad, project-rule and pair
support. Native import still refuses objects it cannot represent.

A single-layer undrilled pad may use `{"kind":"union","shapes":[...]}` to
combine 2–16 existing pad primitives into one terminal. The members share
the pad coordinate frame. Use `box_exact` for an offset circle or rectangle.
At least one member must contain the pad anchor in its interior. All members
must connect through at least 1 nm of overlap. Each primitive counts toward the input item limit.
The router checks contact and clearance against the individual primitives.
An overlapping zone that requires a thermal connection to this pad is unsupported.

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
heading in the dominance key. All multi-component and two-pad nets use multi-source A* from every
legal pad center and track endpoint in the first component, targeting all other
components. If all ordinary starts in the source component are blocked, the
router checks grid points inside the actual source pads on their original grid
phases. A track or via keepout covering the pad center can leave a usable end
of the land exposed. Each candidate consumes work and the usual track, via and
contact rules apply. A final pad can also branch perpendicularly from a straight track.
When both ends of a source track already connect to other copper in its
component, a terminal can branch from an interior point sharing its x or y
coordinate, including an exact integer point on a diagonal trace. After all spatial grid searches fail between two isolated surface pads on the
same layer, the router can try a zero-via 45-degree segment or a 45-degree
segment joined to an orthogonal one. It can also follow clear 45-degree rays
from both pads and join them at a bend or across a horizontal or vertical
segment. It checks the full copper width and both
pad landings, and uses the same work and expansion budgets. Generated
blockers remain eligible for the existing rip-up process. The grid heuristic
is the minimum, over target items, of the sum of the two rounded-up axis gaps
in grid steps to a bounding box containing the endpoints that can make contact.
For pads, tracks and vias the expansion uses the route's floored half-width
less the required 1 nm of copper overlap, clamped at zero. Graphics and zone
fills use the outward-rounded route radius.
For a slanted track or oval, the engine also bounds the distance to the strip
containing its swept copper. The strip uses the trace's actual orientation.
This excludes the empty corners of its bounding box from the distance estimate.
These outward-rounded boxes contain every endpoint-cap contact; each cardinal
edge, including a shortened final edge, moves at most one grid step in one axis.
The estimate therefore underestimates remaining unit-step cost, is consistent
across edges, and is zero at every accepted goal. Reversible multilayer search
also bounds the cost around fixed obstacles. A single elongated surface lead
can force a signal to pass either end before it reaches its destination.
The router considers a cut perpendicular to the long axis of such a lead.
On a shared centerline, a bank of
foreign surface pads can leave too little room for a track under the actual
layer width and pair clearance. Strictly overlapping clearance intervals
constrain the planar detour on that layer. Tangent intervals still admit a
crossing, and each layer change remains available in the relaxation. Each join
computes target bounds once. Its work budget covers ring and compound-pad
geometry, each target evaluation and each grid point examined
when building reverse seeds. Each seed retains its own grid phase, so off-grid
incumbent endpoints are supported. For two isolated pads without a same-net
hole-clearance constraint, equal estimated total costs favor the state with
more of the route already traversed.
In bidirectional search, the reverse frontier estimates distance to source
anchors on its own grid phase. A full grid step preserves both coordinate
residues, and a via preserves the position. For phases with more than 16
anchors, each layer records the extrema of x, y, x+y and x−y. For example,
a point inside the bounding box of a diagonal trace can still lie many grid
steps from the trace. The largest projection gap and the sum of the two
Cartesian gaps each bound that distance from below.
On fixed copper, an empty reverse frontier prompts a check for full-grid
track endpoints on those phases. When neither a clipped track, a full
track nor a via can land, the source search can finish immediately and
leave its remaining budget for a different launch phase.
For a join between two isolated pads, their anchor coordinates may generate
different grid phases. If the first search exhausts its frontier and the other
pad offers another phase, the router searches from that pad with the remaining
work and expansion budgets.
An elongated rectangular or rounded surface pad can launch from its medial
axis on the pad-center grid. The router chooses the site closest to the
remaining same-layer copper. If the ordinary starts are blocked, it still
checks the full pad interior.
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
the shorter route. A shortened final edge costs one grid step. During the initial
multilayer forward search, a settled state can finish a straight connection when
the remaining grid-step cost exactly equals its A* lower bound. The engine uses
the ordinary terminal landing in the final grid cell and checks the full
conductor for clearance. In a board-wide search with zero congestion cost
on the proposed segments and vias, and with bus guidance disabled, the router can also finish on a surface pad using a 45-degree
segment and an optional axial leg. Each diagonal step costs one step per axis.
At a launch, bend or layer change the router checks whether the entire legal
path to a pad-interior landing meets the minimum forward priority. Its last
cell may end between grid points. At a source on the opposite board face
from the destination, the two legs can meet at a through-via when both
layers have the same track cost. The engine checks both conductors, the
entire via span and the pad landing, and includes the via cost in the bound.
For a same-layer pad, the source and destination can also extend parallel clear
runs to opposite ends of a perpendicular segment. The router tries the middle
and the two ends of the overlap. Legal 45-degree cuts shorten the corners
while preserving the weighted distance. An aligned connection can also leave
its original layer on a through-via, cross an obstacle on another layer of
equal track cost, and return on a second through-via. The search checks both
drills, each track and the clipped landing. For offset terminals, the middle
layer can use a 45-degree elbow between the two axial escapes. For uncapped surface pads
that require track contact, the obstacle-distance bound uses the track cap's
reach around the destination. When all launch sites and landings lie on one
face, the router queries the current copper index for an earlier net’s track
that makes a same-face detour more expensive than two through-vias. It uses
this bound at a launch site only when it finds a legal path of that cost.
Each component join sees the copper left by the most recent negotiation.

When a rectangular via keepout covers a surface join, a shortest planar
route may need several successive turns. The search follows clear axial runs
toward a clipped pad contact and validates every segment after chamfering
the bends. When a mechanical keepout or copper obstructs that route inside a
rectangular through-via exclusion, the mesh can also try bends beyond the
obstacle corners. Those corners are candidates. The router sweeps the full
track width and clearance along every segment and charges each expanded
position to the existing mesh allowance.

When a terminal can reach a common plane through a new via and the other
already has copper there, the mesh compares the cheapest registered pair
on each plane. From both ends it checks an elbow after the first clear
mesh step and after the last clear point along each axial or 45-degree
direction. The resulting route pays the actual terminal, trace and via
costs, including congestion along its full path. The regular mesh search
can still replace it within the same expansion budget.

When several remaining islands contain many separate traces and pads, the
forward search tries the ordinary mesh first. If that search finds no path,
it checks a straight conductor between the nearest launch on each allowed
layer and each remaining pad. The two ends may need one or two through-vias.
Every attachment must meet the current clearance and congestion rules.
The probes share a portion of the remaining trial expansions.

A board-wide multilayer search can also follow a same-layer detour by
extending horizontal and vertical lines from both contacts. It checks the
track's full swept width up to each line end, then tries perpendicular lines
at the ends and at interior points. When the two searches meet, it slides
backtracking segments toward the obstacles and checks 45-degree cuts across
the bends. The final route must fit the original expansion and work budgets.
The grid search continues when these lines do not meet.

When a routed tree has a plated connection and its remaining terminals are
pads on one face, a pad can need several turns before a through-via fits.
For a pad whose axial drill samples are blocked, the router can search nearby
surface tracks for a reachable hole. On each permitted support layer it probes
clear horizontal and vertical runs from the existing tree and the discovered
holes. The complete branch includes the surface tracks and the through-via,
with congestion charged on both layers and across the via span.

If two isolated surface pads cannot leave the ordinary grid, the router can
use the gap between four adjacent pad centers. A diagonal departure reaches
the gap, then a straight track follows the space between pad rows to a legal
drill outside the package. Each departure and the connecting copper use the
same rules and operation budget.

A net with many isolated terminals can use the forward-only queue. At a
source on existing copper, this queue also checks whether a legal straight
or octilinear connection attains its smallest remaining cost. The candidate
must reach an actual target on the same layer. If this queue stalls on a
board-wide join, it can also search the multilayer lattice described below
from the existing copper to any reachable surface terminal. It keeps every
disconnected target in the ordinary queue. A complete lattice connection
can be used once the join has consumed its share of the board budget.
When the source tree has more launch states than the mesh can visit, a
disconnected surface pad can escape toward the tree from its own end. The
router tries this before the mesh when another target island already contains
a track or via. It checks the surface approach, through-via and buried track
under the same per-join allowance.

For a net with four or more disconnected pad groups, the board-wide router
can join unobstructed neighboring pads on a common layer before starting a
maze search. It builds a geometric spanning forest. Where existing traces
link several pads, any pad in that component can supply the next branch.
The multilayer search handles dangling trace ends and partially routed groups
without a shared routing layer. A new branch can meet the
interior of a trace already in its tree. The router checks each segment
against the board, keeps plated connections at their barrel centers, and
recomputes the actual copper components before routing the remaining gaps.
When several separately wired groups already have plated contacts, the router
can connect their existing barrels on a shared layer. It gives isolated
surface pads the first opportunity to escape. It keeps ordinary search first
when the number of source and target copper-item pairs per layer fits the
remaining share of the join. Larger trees can check direct and bent routes
between the nearest barrels of each pair of groups. Each trial uses the
remaining board search allowance and native clearance checks. These branches
add tracks without drilling new holes.

When an ordinary board-wide search starts probing from both ends of a join,
it can retain a checked surface route as its best known path. It samples
short pad departures in each axial direction. If the spaced samples all miss
and the join still needs a route, it checks intervening grid points in order
of distance. Each extra probe spends an expansion from a shared allowance.
If the axial rays have no reachable drill, the same allowance can check the
four 45-degree rays. A solid no-via rectangle can exclude a run of drill
centers up to the first edge the ray exits.
Every resulting via and the entire surface track to it must be legal.
When previously routed copper blocks the ordinary search, a legal first
drill may still lead into an obstructed inner-layer passage. The router can
slide that drill farther along its clear surface track and try the passage
again. It explores both terminals in turn and reserves search effort for
the remaining starting positions.
A pair of contacts on
different faces can share one through-via. A blocked face can use a pair
of legal through-vias and an intermediate layer. A trunk on an inner layer
can reach the center of a plated pad directly if its padstack includes that
layer. A surface source can use one checked through-via to enter the trunk.
The ordinary frontier can
improve on that path. The initial expansion budget divided among the
active nets' possible pad joins determines how long the search spends
improving an available connection. For a bidirectional search that has found
a path using the minimum possible number of through-vias, refinement can
also stop after one expansion per unit of unscaled route cost. Starting and
target layers and complete cross-board keepouts give a lower bound on the
number of vias. This leaves the other nets more of the shared budget.
When a negotiated trial can trigger rip-up, a complete connection also takes
precedence once the trial reaches its expansion limit or spends its share of
the board's budget. At the board's final expansion, either frontier may use
a complete path it has already found. All joins must still finish within the
original budget. A trial without a connection still reports blockers.
Once the search reaches its allowance, reverse
terminal checks still look for cheaper connections to reached forward states
and the current step's possible landings. Other reverse roots need no queue. Every prospective connection and track
sweep consumes the normal expansion or work allowance. The board transaction
still requires full connectivity.

If simple departures cannot connect the terminals, the router can search
on a lattice with spacing derived from via diameter and clearance. The spacing
is a whole number of ordinary grid steps. Short segments connect the existing
copper to nearby lattice points. Every segment and layer change must clear
native geometry, including hole spacing between new vias on the same path.
A target with a plated barrel can accept the lattice trace on each permitted
copper layer in its padstack. The connection reuses the existing hole.
When the shortest pair of lattice attachments faces an obstacle, a planar
trunk may have to move away from the destination before it can approach it.
If every first step on a shortest path is blocked at either end, the mesh
compares another attachment on that side with the cost of the unavoidable
out-and-back distance. It keeps the original attachment for detours.
If none of the reachable nearby intersections permits a drill, a surface
contact can attach to the lattice on another layer through a legal drill
outside the lattice. The search checks both terminal stubs and the distance
between source and destination drills before accepting the path. When
direct departures cannot reach a drill, a local search follows legal surface
segments around obstacles up to two lattice pitches from the contact. It
uses the nearest reachable drilling window and charges each explored point
to the same expansion allowance as the remaining lattice search. A rectangular
through-via exclusion covering that whole local window rules out every drill
in it. Where a contact can already reach the routing lattice, its optional
drill search shares the remaining allowance with the other contacts and
the search between them.
When a coarse frontier runs out of reachable points, the router can halve
the number of grid steps between lattice lines, rounding up to a whole step.
All resolutions draw from the same expansion allowance. A forward search with many
target copper items gives the first lattice attempt a per-net share. If the lattice
uses its share without a route, it can try again with twice that allowance after the
ordinary search has settled that many additional states. Each attempt charges the
same trial and work budgets. The ordinary search can still use passages between
lattice points. During a multilayer launch inside a
rectangular through-via keepout, the ordinary search can also take longer axial
steps toward a pad. It sweeps the whole segment, charges its full grid distance
and keeps one-cell steps available for turns between those longer endpoints.

For an oblique imported track separating the terminals, the planar bound
uses the shorter detour past either end. When the existing same-net tree
surrounds the target, reverse search can bound the distance to each section
of its launch sites separately on the same grid phase and layer.

The original single-layer search used unit step costs, a fixed net-ID order
and no rip-up. Current multilayer negotiation can change the routing order
and rip up generated copper. When several single-ended nets still require
routing, a surface pad with only one or two clear axial departures and no
legal via at its center gets priority. An existing pad exit can supply another
start, as can a same-net zone or graphic on that face. The router retains the
combined zone and artwork bounds for each net and layer, including refillable
zone outlines. Pads outside those bounds still compete for their own exits.
These probes use the board's track and via rules. Pair priority and the
relative order of equally constrained nets are preserved. A later net can fail even
when a different earlier route would permit a solution; this is reported as
unroutable, not proof of global routing impossibility. Success/failure is
deterministic. Each completed join must strictly reduce the net's conservative
component count, and all joins share the operation/batch expansion budget.

For separated regions of a refillable zone, the search starts on the component
with the smallest fill envelope and can attach anywhere on a larger region.
When both components contain fill from the same zone on the same layer, the
search first seeds that layer and targets the zone copper. If those endpoints
have no legal path, the remaining budget can route between the full copper
components, including plated pads on other layers. A track or via center
must reach the interior of a refillable target. Orthogonal scanlines locate
narrow regions between grid points while excluding holes and polygon edges.
Failure after earlier joins rolls all of that operation back. The expansion
budget is 1..=1,000,000 for the whole batch; settled
states, including the found goal, count once per state/direction. Stale queue
entries do not count. Frontier exhaustion and budget exhaustion are distinct.

The component search covers a finite set of seed points. The grid can miss
a legal corridor or another usable point inside a source track. Along each
searched edge, the convex-interval calculation covers integer terminal
endpoints. Multi-terminal routing first tries to join the first component to any
other component. When the search exhausts against fixed geometry and the net
contains only pads, tracks and vias, each remaining component can become the
source in turn. Its pad or trace may offer a different grid phase or reach a
third island. Generated-copper obstructions return to negotiation. All attempts
share the work and expansion limits, and each join must reduce the number of
disconnected components.
For three separate surface pads on the same layer in a multi-net batch, the
router also checks a common junction at the Cartesian or diagonal coordinate
median. Each arm must be straight, horizontal, vertical or 45 degrees. The
three arms must clear the design rules and use less copper than the shortest
two pairwise octilinear spans. The complete proposal is inserted together and
the router checks the resulting connected components.

A track whose two ends are already connected can launch a branch from an
interior point aligned with a target pad on another layer. The starting point
must lie exactly on the track centerline. The search still has to find a legal
via and an uninterrupted copper path to the pad.

Already-connected pad groups and disconnected incumbent tracks participate
equally; a net succeeds only when all required copper is connected. This can
produce branches but does not optimize a Steiner tree or promise invariance
under input permutation. Identical inputs remain deterministic. Work and expansions are shared
across all joins (and nets for route_all); failure rolls back the whole operation.
For a congested multilayer join, the terminal search tries a direct segment
between the two escape points when its horizontal, vertical and 45-degree
elbows cannot improve the current route. The whole segment and both escape
connections must clear the applicable board rules. When several terminal layers
reach the same pair of escape points, a fully obstructed set of middle segments
is checked once per layer on the current copper. A geometrically clear segment
remains available to a different terminal connection, including one with a
different congestion cost.
Consecutive proposal segments with the same heading coalesce at any angle.
The router subtracts collinear overlap against
existing or earlier same-net tracks of equal or greater width before clearance
validation and insertion. When a generated branch ends on the interior of another
generated track’s centerline on the same net and layer, and its other end cap
also overlaps that track, the router divides the host at the tap. Both pieces
retain the host’s geometry and width. The same rule applies when the branch
was inserted first.

Thin copper that the
conservative contact model cannot join fails honestly rather than reporting
complete. Native connectivity can merge contacts that this model separates or
apply other integer geometry rules; no universal native equivalence is claimed.
The mandatory native publication check remains necessary. Components are
recomputed after each join using borrowed nodes; graph contact scans can be
quadratic and are charged to total work, separately from search expansions. This increment does not
provide a wall-time or real-board quality guarantee.

Every proposed edge checks its entire swept copper against foreign-net pads
and incumbent/generated tracks. A conservative broad-phase spatial index prunes
disjoint subtrees using outward `Pad::copper_bounds` and `Track::copper_bounds`
`i64` envelopes (`CopperAabb`) against a wide `i128` swept query box
(`SweptAabb`) with inclusive `<=` overlap checks so exact tangency candidates
are never dropped. Exact integer-fraction comparisons in `i128` cover
segment/segment, segment/circle, segment/rectangle, and segment/roundrect
(`copper_core`) distance, including corners and endpoint caps, and remain the
sole clearance authority. Equality at the requested clearance is allowed.
Half-width plus edge clearance applies at both endpoints, sufficient for the
convex rectangular outline. The coordinate/size limits bound arithmetic;
critical squares/products, position steps and counters use checked arithmetic.

`Engine` owns an immutable leaf-bucketed (`STATIC_LEAF_CAP = 32`) flat
median-balanced static spatial index (`pad_spatial`) storing contiguous
`(CopperAabb, net, ordinal)` entries ordered by `(min[0], min[1], ordinal)` and
compact `PadCore` payloads, enabling same-net filtering and uniform same-net
leaf fast-charging before exact geometry checks. The copper store owns immutable
input tracks, an immutable leaf-bucketed static spatial index (`input_spatial`)
with `TrackGeom` payloads, generated tracks in dense slot storage, a
generated-net index, an iterative history-independent deterministic treap
spatial index (`generated_spatial`, `TreapNode<u64, u32> <= 112 B`) storing
`slot` plus `net`, `subtree_uniform_net`, and `subtree_len` (enabling
`TREAP_UNIFORM_SKIP_CAP = 32` uniform same-net subtree bulk-charging), and an undo journal.
`Engine::transaction` exposes a borrowed transaction
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
and source-copper search seeds, without claiming global infeasibility.
`route_all` retains its existing batch-wide expansion budget. The enclosing
transaction shares one total-work limit across all nested calls; expansion
limits remain per operation.

Inner commits retain their journal entries for an outer rollback; only the
outermost commit releases them. Rollback restores generated object fields and
IDs, the by-net index, `generated_spatial` (via `add_raw`/`remove_raw` without
consuming forward work), and the ID allocator; immutable input copper and static
indexes stay intact. Because `generated_spatial` is a deterministic-priority
treap keyed solely by live `(copper.min[0], seq)` with deterministic
`splitmix64` priority, tree topology and subsequent query/reroute `work_used`
are invariant across rolled-back attempts and unwinds.
Each frame holds only its journal offset and allocator position, and undo work
is proportional to the changed generated objects (and treap height), without
whole-board copies.
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
quality or speedup. Obstacle broad-phase queries use conservative AABB spatial
indexes (`StaticSpatialIndex` for pads/input tracks and deterministic-priority
`SpatialIndex` treap for generated tracks) verified against linear-scan and
per-object superset oracles in `tests/spatial.rs`, though dense neighborhoods
with overlapping envelopes can still visit up to `O(N)` candidates, and
input-copper connectivity checks (`components::partition`) and collinear overlap
subtraction remain linear/quadratic in a net's track count.

The component-search test oracle enumerates every integer terminal endpoint
on every reachable grid edge in 4,096 small axis-aligned worlds. Independent
box-gap geometry checks clearance/contact, and position-only breadth-first
search checks reachability and unit-step cost. Cases vary grid phase, width,
clearance, circle/rectangle/track targets and all subsets of six obstacles.
A further 256 worlds compare multiple targets and multiple off-grid seeds
against the independent cost oracle. Envelope checks cover zero cost at
contacting caps and consistency over full and shortened edges with odd widths.
The goal-cap contract uses the production contact predicate for circle/rectangle
pads, axis tracks and arbitrary-angle tracks in both endpoint orders, including
negative coordinates. The axis-aligned cost oracles retain independent geometry.
Direct estimate tests protect one work unit per target item and every exact
cutoff against the caller's shared outer budget, without pinning whole-route
work totals. Empty target sets in `component_estimate` and `join_components`
refuse cleanly with `Status::Unroutable` instead of panicking.
The exact authored two-net native request that previously exhausted 100,000
expansions is retained byte-for-byte as a Rust regression, including its original
limits and request SHA-256. Its complete result must connect all components,
obey independent clearance checks, and repeat identically.
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
| `max_search_entries` | 1,000,000 | 4,000,000 | Combined search queues and retained states and parents, including coupled centerline histories. Also bounds seed-set size. |
| `max_journal_entries` | 1,000,000 | 1,000,000 | All retained undo entries, including inner commits |
| `max_depth` | 64 | 256 | Active transaction frames, including routing method scopes |

`Engine::new` admits input cardinality before indexing/validation. Net names
are at most 1,024 UTF-8 bytes (the refusal reason states this limit); IDs have
their existing fixed sizes. Validation uses
bounded linear/logarithmic collections. Pad count per net is no longer an
admission restriction; total input items remain bounded before validation. Both the JSON library entry and CLI accept
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
| Component estimate evaluation | 1 per target item before computing its envelope step bound, including targets visited after an estimate of zero |
| Edge outline check, spatial broad-phase, candidate lookup, foreign geometry | 1 per edge check; 1 per visited or pruned static/treap node; 1 per entry in overlapping static leaves (before net/AABB filter, bulk-charged on uniform same-net leaves with per-entry exhaustion); `subtree_len - 1` bulk-charged on overlapping uniform same-net treap subtrees up to `TREAP_UNIFORM_SKIP_CAP = 32` (with per-unit exhaustion fallback); 1 per overlapping foreign pad or input track (covering payload access and exact geometry); and 1 per overlapping foreign generated track slot lookup (covering exact geometry; same-net treap nodes skip slot lookup) |
| Search seeds, successor attempts, admitted queue pushes and all pops (including stale) | 1 per action; push includes bounded map updates |
| Path reconstruction, reversal, coalescing and proposal construction | 1 per visited/reserved point per stage |
| Collinear overlap subtraction | 1 per scanned track, 1 per overlap before sort, 1 per merge, 1 per reversed/emitted span |
| Short branch junctions | Generated spatial visits, one check per candidate, scratch admission for each tap, sorting and deduplication, and journaled index updates for each split |
| Copper insert/remove | Insert: 1 + ID candidates + `generated_spatial.mutation_work()`. Remove: 1 per removed track + `1 + generated_spatial.mutation_work()` per removal |

Admission checks bound combined queue/maps before insertion, total copper before
insertion, undo history before mutation, and frame depth before push. Collection
capacity growth and spatial/heap comparisons are included in their
bounded abstract action, not charged per allocator byte or comparison. `Engine::new`
builds `pad_spatial`, `input_spatial` and the fixed surface-pad rows after
`max_items` admission. Each supported pad contributes at most two row entries.
Each join charges the applicable rule and row visits, interval sorting and
merging. The search maps and queue can hold stale entries only within the
admitted sum. Seed and path
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
remain intact. The contact test helper checks rectangle/rectangle overlap as well;
none of the budget tests extends that helper's domain.


Multi-terminal controls cover 3, 4, 6 and 9 pads, branched joins, connected
incumbent groups plus isolated tracks, multiple nets, nested reroute/rollback,
late work/expansion exhaustion and next-ID restoration. An independent
axis-aligned contact graph checks every terminal and incumbent component,
generated clearance and item identity. A separate position-only flood fill
classifies 620 small worlds with 3/4/6/8 terminals and optional full-span walls:
175 connected and 445 disconnected. This proves reachability/completion only
in that chamber domain, not general path optimality, arbitrary geometry,
continuous completeness or native parity. The earlier 256-world and 4,096-world
oracles remain. Representative native boards and native publication checks are
still required; these Rust controls alone are not fabrication acceptance.
