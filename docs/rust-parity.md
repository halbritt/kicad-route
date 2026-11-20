# Greenfield Rust capability acceptance

Owner source: Heath's explicit direction for agent-ee/kicad-route, Cairn
`d159855b-5bc5-4632-b43d-5ab2d70ea942/v1`, 2026-11-16. This is a migration contract,
not a declaration that parity has been achieved.

Build a greenfield Rust engine. Use KRT to discover useful behavior and regression
inputs; do not wrap, fork or continuously sync its implementation. Rust owns
state, geometry, legality, search, orchestration, transactions and route edits.
The narrow Python exception is native KiCad import/apply/validation, with no
routing decisions. Preserve the current KRT backend separately. Upstream sync
convenience is not a design constraint. Record source provenance and honor
attribution and licenses wherever code is reused.

The reference inventory starts from the current backend's documented behavior in
the repository README and `docs/routing.md`, `docs/campaign.md`, `docs/provenance.md`
and `docs/pour.md`. These documents aid discovery; they do not prove either
implementation satisfies a capability. Extend this matrix as useful behavior or
missing native rules are identified.

| Capability | Representative input and acceptance | Current Rust evidence / gap |
| --- | --- | --- |
| Ordinary nets around protected copper | Two-terminal board with locked obstacles, then multiple independent signal nets. Preserve exact existing IDs/nets/locks and source bytes, close intended terminals, introduce no native DRC violations. | In T4-07, `route_demo` retains the locked signal copper and closes six opens. The saved board passes the native DRC, net-class width and pour checks. The same test connects all nine nets in `bus_routing`, including three pads on D3. T5-02 also completes a partially routed net under `--protect human` while preserving its locked track, and refuses the board when a locked wall and a keepout block both layers. |
| Native contact, geometry and clearance | Circle/orthogonal rectangle/oval/roundrect pads, rotated pads, PTH/NPTH drills, outline strokes, tangency, diagonal contact and clipped/first-contact targets. Use actual executable/native readback and adverse controls. | The native shape suite routes rotated and odd-sized pads, trapezoids and a footprint polygon with a hole. The bridge also represents pad drills and selected copper/mask artwork. It refuses unsupported shapes such as chamfered pads. Connected unions of supported round and rectangular primitives are represented as one pad, including the four jumper pads in the Tigard fab-rules input. The native shape suite and its explicit refusals define the supported geometry. |
| Effective project rules and multilayer connectivity | Real project rules/net classes/local overrides, stackup, via spans, pad/hole drill constraints, same-net/cross-net hole clearances and solder-mask-web rules. Route under unchanged effective rules and verify native outcomes. | `test_route_native_accepts_valid_sidecars_and_multilayer_vias_end_to_end` exercises project minimums, a net class and a net-specific custom width rule on four layers. Tier 4 also covers layer-specific via spans. Support depends on the rule and padstack forms the importer can represent. |
| Search-state soundness and history-dependent rules | Paths reaching the same position with different prior via histories and different feasible continuations. Compare against a fully augmented independent oracle; retain a counterexample to position-only dominance and verify native legality. | The multilayer search records prior via placements when they constrain a continuation. T1-F6-05 exercises a four-layer witness with a hole-to-hole clearance constraint. The independent last-via-only Dijkstra counterexample in `search_tests.rs` loses the feasible three-via path. The full-history search retains it. Independent small-world oracles check costs on the stated search domains. |
| Multi-terminal and board-wide routing | Congested competing nets with multiple terminals. Verify per-net component partitions, deterministic scheduling, resource exhaustion and no illegal fallback. | The Rust engine joins existing copper components and negotiates competing nets. T4-04 routes a four-layer fixture with 24 three-pad nets under a shared work budget. The M7 corridor regressions exercise collinear joins, isolated pads and preservation of pre-existing copper. On the placed external four-layer Tigard project, all 85 nets connect at the default limits and native KiCad confirms zero saved opens. |
| Differential pairs, length and delay | Authored pairs/matching groups and native timing/layer rules. Verify coupled geometry, separation, length/delay and preservation with positive and negative controls. | Coupled pairs, intra/group length/delay matching, impedance control, and `--ac-couple-chain-json` AC-coupled chain matching are implemented in `rust-engine` (`diff_pair.rs`, `orchestration.rs`). On the eight-lane `bus_routing` fixture at `e2cf9ce`, `--bus` ordered D0 through D7 by physical position and excluded AUX. The saved board closed all ten opens with 16 tracks, zero vias and zero native DRC violations. Equal-cost corridor preference used 3,148 expansions at the ordinary 100,000 limit. |
| Rip-up, restore and generated ownership | Blocked multi-net case with protected copper. Inject failures through edits/publication; restore exact IDs/indexes/allocator/geometry and verify native outcomes. | `rust-engine/tests/negotiate.rs` exercises whole-net victim repair, protection of source copper and rollback after a failed repair. The four-layer T4-04 fixture checks the published board and deterministic copper on repeated runs. |
| Zones, planes, thermals and keepouts | Native zones/rule areas/plane plan with refill. Preserve component partitions, pad contact, thermal spokes and pour integrity. | T4-03 exercises filled power and ground zones, thermal pads, keepouts and via spans on four layers. The native fixture `route_demo` exercises refill and measures surviving pour area, islands and spokes. On the external two-layer `complex_hierarchy` board, Rust reconnects ground islands after native refill and passes the original pour check. The bridge refuses unsupported zone geometry, including curved polygon outlines. |
| Execution, publication and routing quality | Real process success/failure, stale results, resource limits, source mutation and destination races. Require current execution success, expected-request identity and native validation. Attribute performance on frozen representative inputs and tools. | Rust stages and snapshots the input, checks the applied board and publishes with a no-overwrite link. Tests cover destination races, timeout, cancellation, stale requests and SIGKILL between durable board publication and operation reporting. The fixed-input table below measures wall time, process CPU, peak process RSS, work and copper. |

### Transactions, spatial indexing and work accounting

The fixed input index groups up to 32 entries in each leaf. A deterministic
ordered tree indexes generated copper. The tests check strict key ordering,
subtree lengths and net membership, compare query and reroute work after rollback,
and exercise exhaustion during a bulk same-net charge. In the original M2.1
mutation run, the tests detected all 80 behavior-changing mutations. Four
mathematically equivalent variants were recorded separately.

In repeated paired raw Rust runs on the original sparse and dense workloads,
the median sparse routing time fell from 3,033.53 to 25.43 ms and the dense
time from 2,435.85 to 24.95 ms (`1ca08e72` to `ca2abec`). Both revisions
used 8,017 expansions on the sparse input and 6,656 on the dense input.
In the earlier 22-cell data set the measured candidate/baseline route-time
ratios on the dense static, tree, corridor and mixed workloads ranged from
0.1268 to 1.0555. On the 50,000-item case the GNU-time peak RSS ratio was
1.1415 and the sampled /proc RSS ratio was 1.0261. The native CLI runs below
include the additional costs of KiCad, source staging and board publication.

At `72b6c1b`, the native five-tier suite passed all 148 cases. In T4-04,
the saved four-layer board connected all 24 three-pad nets with zero opens
and zero DRC violations. Repeated runs produced identical copper. The case
used 851,586 of its specified 1,000,000 expansions and 167,434,192 of its
250,000,000 work units. The external-board runs below use the production
default of 100,000 expansions.

On the protected CAN control, native KiCad reported zero opens and zero DRC
violations before routing. The two preserved tracks measured 10.120 mm and
10.000 mm. The Rust router refused the requested 0.100 mm skew limit and
left the output absent. T5-02 separately connected an open net while
preserving the original locked track's identity, endpoints and layer.

On 2026-11-19, release builds of `4859553` and `36f7b11` produced identical
copper signatures for each of the three T4-07 fixtures under KiCad 10.0.8.
The inputs had six opens on `route_demo`, ten on `bus_routing` and four on
`ac_matching`. All three outputs had zero opens and zero native DRC findings.
The composed AC-chain skew was 0.000 mm. The four-layer AC fixture requests
pair routing on F.Cu. These measurements cover the checked-in fixture boards.

The table gives one release CLI run per revision on the same three fixture
inputs, with their original rule files. CPU is user plus system time. Peak
RSS is the largest single process reported by GNU time. These shared-host
observations do not establish a speedup.

| Fixture | Revision | Wall (s) | CPU (s) | Peak RSS (KiB) | Work units | Search expansions |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| route_demo | 4859553 | 2.03 | 1.46 | 279,796 | 1,664,579 | 10,498 |
| route_demo | 36f7b11 | 1.78 | 1.23 | 279,492 | 1,664,579 | 10,498 |
| bus_routing | 4859553 | 1.43 | 1.15 | 226,432 | 4,906,812 | 22,577 |
| bus_routing | 36f7b11 | 1.42 | 1.14 | 226,368 | 4,906,812 | 22,577 |
| ac_matching | 4859553 | 1.31 | 1.02 | 226,104 | 25,067 | 0 |
| ac_matching | 36f7b11 | 1.26 | 0.96 | 226,004 | 26,227 | 0 |

The four-layer T4-04 comparison used the same saved PCB (SHA-256
`b438e9cdcb499d0a54d0da5c03881f2a5892c72882ad2543c1b4601de9d77f23`),
project rules, 1,000,000-expansion limit and 250,000,000-unit work budget.
Two runs per revision produced identical board files, zero opens, zero DRC
errors, 64 vias and 2,741.775 mm of track. Each run used 132,878,493 work
units and 718,493 expansions. Native CLI wall time was 5.12 and 5.13 seconds
on `4859553`, and 5.75 and 5.98 seconds on `36f7b11`. Largest-process RSS
was 236,904 and 236,136 KiB on the baseline and 238,324 and 239,052 KiB on
the candidate. The shared-host runs used Rust 1.99.1 and KiCad 10.0.8.
They establish a reproducible board outcome. More repetitions under
controlled load would be needed to attribute the timing difference.

### Matched native runs at `04e9386`

KiCad 10.0.8 regraded each saved board with its input project, custom rules
and zone refill. Each release build (Rust 1.99.1, locked dependencies) ran
three times per input, alternating revisions. The three published boards
were byte-identical for every completed case at each revision. Wall time,
process CPU time and GNU time's maximum process RSS are medians.

| Input | Revision | Opens | Native errors | Vias | Track length (mm) | Expansions | Work units | Wall (s) | CPU (s) | RSS (KiB) |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| route_demo | 9ac137a | 0 | 0 | 3 | 163.553 | 3,325 | 668,516 | 6.52 | 2.22 | 278,608 |
| route_demo | 04e9386 | 0 | 0 | 3 | 159.984 | 1,757 | 332,464 | 9.42 | 2.33 | 278,736 |
| T4-04 | 9ac137a | 0 | 0 | 64 | 2,741.775 | 718,513 | 135,756,020 | 12.35 | 8.17 | 236,024 |
| T4-04 | 04e9386 | 0 | 0 | 58 | 2,611.802 | 181,861 | 47,961,982 | 7.87 | 4.22 | 232,436 |
| complex_hierarchy | 9ac137a | 0 | 0 | 3 | 1,125.903 | 78,412 | 40,830,733 | 6.18 | 5.99 | 289,708 |
| complex_hierarchy | 04e9386 | 0 | 0 | 0 | 1,083.288 | 8,993 | 6,425,161 | 3.28 | 3.15 | 291,228 |
| tigard_fab | 9ac137a | — | — | — | — | 100,000 | 44,890,302 | 1.45 | 1.43 | 167,600 |
| tigard_fab | 04e9386 | 0 | 6 | 109 | 1,887.108 | 89,824 | 82,680,830 | 3.30 | 3.20 | 240,452 |

T4-04 closed all 48 opens across 24 three-pad nets under its original
1,000,000-expansion and 250,000,000-work-unit limits. Both saved boards had
zero native DRC findings. The newer revision used 58 vias and
2,611.802 mm of track. The two-layer complex_hierarchy
project closed all 87 opens with 0 vias, zero errors and its original
68 library warnings. Its saved tracks totalled 1,083.288 mm.
The demo closed six opens with 3 vias and zero DRC findings.
Tigard's four-layer input has 92 footprints, 290 opens,
6 native errors and 94 warnings.
At `9ac137a`, routing ended with `budget_exhausted` after 100,000 expansions and left the destination absent.
At `04e9386`, KiCad regraded the published board with 0 opens, 6 errors, 94 warnings, 109 vias and 1,887.108 mm of track.

The four-layer perpendicular connector test also runs under the 100,000-expansion
production limit. With obstructions at both twelve-pad banks, `f64719d`
exhausted the limit. At `395e0d6` all three runs saved a board with zero opens,
zero DRC findings, 13 vias and 745.389 mm of track in 98,214 expansions. On
the variant with open connector exits, `f64719d` already closed all twelve
opens in 70,482 expansions with 16 vias and 674.101 mm of track.

### Native phase costs

One additional release run at `04e9386` measured each public operation
with the original KiCad project. Values give elapsed seconds per phase. The
last column is the highest sampled sum of resident memory across the parent
and its concurrent descendants, in MiB. Shared pages count once per process.
The matched table above uses maximum single-process RSS.

| Input | Native export (s) | Rust route (s) | Apply and native validation (s) | Sampled process tree (MiB) |
| --- | ---: | ---: | ---: | ---: |
| route_demo | 1.540 | 0.032 | 5.210 | 396.0 |
| t4-04 | 1.149 | 4.839 | 6.470 | 479.1 |
| complex_hierarchy | 2.578 | 1.211 | 27.076 | 531.3 |
| tigard_fab | 3.246 | 7.093 | 3.280 | 419.0 |

The Rust phase runs in the parent process. Native apply and DRC run together.
The operation totals also include source staging, publication and cleanup.

### External corpus boards

Under KiCad 10.0.8, two release runs of `route-native` at `c9e7d08` on the
placed, unrouted two-layer `complex_hierarchy` input (PCB SHA-256
`3cfcfff4a55022ba399f0bddcfac4d9dc3877207ca897a6486e026ecbb421ef7`)
closed all 87 opens across 68 footprints. The first native refill exposed
two ground connections that the original pour had supplied. Rust added
three tracks and two vias to reconnect the islands. Independent KiCad DRC
with the input project and zone refill measured zero errors and the same
68 warnings before and after routing. Both runs published the same board
bytes, with 419 tracks, 1,126.734 mm of track and three vias. Each consumed
88,458 of 100,000 expansions and 46,030,166 of 100,000,000 work units.

At `c5beac7` under the same project and limits, KiCad again found zero opens,
zero errors and the original 68 warnings. The board contained 417 tracks,
1,127.210 mm of track and three vias. This run used 88,046 expansions and
46,609,487 work units.

The external four-layer `tigard_fab` case uses the corpus's revision-2 fab
rules. Its 92 footprints have 290 opens under those rules. After importing
its stencil apertures and compound jumper pads, Rust reached its default
100,000-expansion limit at both `95b3968` and `c5beac7`. Neither run published a board. The archived
reference router output on the same PCB and project has zero opens and
retains the input's six errors and 94 warnings.

At `04e9386` the greenfield Rust executable completed the original 85-net
request in 89,824 expansions and 82,680,830 work units. The PCB SHA-256 was
`561486ffc417455993b7232f14b3319006c80de5b70511ae630077c6497fbb5a` and the
project SHA-256 was
`2636eb9b21e6d7e3939fa5ef8e4d2fb827c0d82d95a3ac2a58d9d14a095da5b6`.
KiCad 10.0.8 reopened the published board with that original project. DRC
with `--refill-zones --all-track-errors` and the KiCad connectivity graph
both reported zero opens. The 901 tracks total 1,887.108 mm and the board
has 109 vias. Every track, via diameter on each applicable copper layer
and drill met its effective net class. All 92 footprints retained their
placement, side, orientation and lock state. All 451 pads retained their
identity, net, position, dimensions, drill and layer set.

The same underlying items account for all six inherited errors (two solder
mask bridges, two courtyard overlaps, one short and one clearance error)
and all 94 warnings (92 library footprint issues and two silkscreen-to-edge
clearances). These violations remain on the saved board. Its SHA-256 is
`d81bd48bcabb230a97a40c30f1aeca12125400c958af89bcaafadae63a945e0c`.

On the separate four-layer
One-Air-Max input, KiCad reports invalid padstacks on U6 pads 3, 4, 7 and 9.
The Rust native workflow refuses this input during import.

For each accepted row record the candidate revision, input/source hashes, fixed
dependency/tool versions, explicit rules, native before/after results,
preservation assertions and independent review. Retain the adverse test that
detects the original failure. Where KRT is wrong, its result is a counterexample,
never the expected output. Tiny-world oracles and representative native boards
serve different purposes; retain both.

The matrix describes the admitted features, refusal cases and board outcomes
covered by its tests. Revision-specific independent review uses those same
acceptance conditions and separately exercises the final implementation.

Current provenance position (2026-11-16): author agent-1484 reports no known
unattributed KRT implementation reuse in its authored Rust changes; Fable's
whole-crate milestone reads found no apparent KRT-derived source. These are
bounded observations, not a complete origin comparison with KRT's kernel or a
fixture/dependency audit. Exact sources: author acknowledgment
`fa12571d-c27e-4b53-b980-8fc04f6c7288/v1` and Fable correction review
`548f3176-7052-4ac4-a9e2-af64968d2ddc/v1`.

Direct dependencies are fixed in Cargo.toml (`serde 1.0.229`,
`serde_json 1.0.154`, `sha2 0.11.0` and Linux `libc 0.2.191`). Cargo.lock records
transitive versions, sources and checksums. Its SHA-256 at `8e7af2f` is
`dc3bb9a7506e055c206f9876907b21a718d7f20cc44b94d6dbd778323ba010ca`.
Keep root LICENSE and the separately retained KRT router/LICENSE; preserve
applicable dependency and future reused-source notices. Record source, version,
license and modifications when code or test material is reused.

Feature coverage is bounded by the inventory above. An additional KRT capability
needs a representative input, a specified outcome and native verification under
the project rules.

### Short generated track junctions

KiCad reported a dangling track on a two-sided bus when a short diagonal
branch ended on the centerline of a longer track. Its other end also lay
within the trunk's copper. In five three-pad reproductions, KiCad found one
dangling-track warning before the fix and zero findings after it. These cover
axial and diagonal trunks, both insertion orders and different track widths.
Splitting the trunk at the contact preserved its length and the total length
of track at each width. The saved two-sided bus also passed native DRC with
zero opens under its original rules.

On 73 held-out engine requests, the junction change at `04e9386` preserved
the copper edit lists, expansion counts and routing outcomes seen at
`b096fc1`. Both revisions completed 58 requests, exhausted a configured
budget on 12 and returned `unroutable` on three. The spatial lookups added
38,126 charged units across all 73 requests (0.011%).
