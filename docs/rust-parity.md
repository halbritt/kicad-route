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
| Ordinary nets around protected copper | Two-terminal board with locked obstacles, then multiple independent signal nets. Preserve exact existing IDs/nets/locks and source bytes, close intended terminals, introduce no native DRC violations. | In T4-07, `route_demo` retains the locked signal copper and closes six opens. The saved board passes the native DRC, net-class width and pour checks. The same test connects all nine nets in `bus_routing`, including three pads on D3. |
| Native contact, geometry and clearance | Circle/orthogonal rectangle/oval/roundrect pads, rotated pads, PTH/NPTH drills, outline strokes, tangency, diagonal contact and clipped/first-contact targets. Use actual executable/native readback and adverse controls. | The native shape suite routes rotated and odd-sized pads, trapezoids and a footprint polygon with a hole. The bridge also represents pad drills and selected copper/mask artwork. It refuses unsupported shapes such as chamfered pads. Connected unions of supported round and rectangular primitives are represented as one pad, including the four jumper pads in the Tigard fab-rules input. General KiCad shape and rounding equivalence remains open. |
| Effective project rules and multilayer connectivity | Real project rules/net classes/local overrides, stackup, via spans, pad/hole drill constraints, same-net/cross-net hole clearances and solder-mask-web rules. Route under unchanged effective rules and verify native outcomes. | `test_route_native_accepts_valid_sidecars_and_multilayer_vias_end_to_end` exercises project minimums, a net class and a net-specific custom width rule on four layers. Tier 4 also covers layer-specific via spans. Support depends on the rule and padstack forms the importer can represent. |
| Search-state soundness and history-dependent rules | Paths reaching the same position with different prior via histories and different feasible continuations. Compare against a fully augmented independent oracle; retain a counterexample to position-only dominance and verify native legality. | The multilayer search records prior via placements when they constrain a continuation. T1-F6-05 exercises a four-layer witness with a hole-to-hole clearance constraint. Position-only dominance and general search completeness remain subjects of RE-02 verification. |
| Multi-terminal and board-wide routing | Congested competing nets with multiple terminals. Verify per-net component partitions, deterministic scheduling, resource exhaustion and no illegal fallback. | The Rust engine joins existing copper components and negotiates competing nets. T4-04 routes a four-layer fixture with 24 three-pad nets under a shared work budget. The M7 corridor regressions exercise collinear joins, isolated pads and preservation of pre-existing copper. Solvable external multilayer board coverage remains open. |
| Differential pairs, length and delay | Authored pairs/matching groups and native timing/layer rules. Verify coupled geometry, separation, length/delay and preservation with positive and negative controls. | Coupled pairs, intra/group length/delay matching, impedance control, and `--ac-couple-chain-json` AC-coupled chain matching are implemented in `rust-engine` (`diff_pair.rs`, `orchestration.rs`). Optional `--bus*` geometric bus-corridor ordering preferences remain unimplemented and excluded from `USAGE_MSG` (M5.2+ open item; `fixtures/bus_routing` routes via native single-ended negotiation). |
| Rip-up, restore and generated ownership | Blocked multi-net case with protected copper. Inject failures through edits/publication; restore exact IDs/indexes/allocator/geometry and verify native outcomes. | `rust-engine/tests/negotiate.rs` exercises whole-net victim repair, protection of source copper and rollback after a failed repair. The four-layer T4-04 fixture checks the published board and deterministic copper on repeated runs. |
| Zones, planes, thermals and keepouts | Native zones/rule areas/plane plan with refill. Preserve component partitions, pad contact, thermal spokes and pour integrity. | T4-03 exercises filled power and ground zones, thermal pads, keepouts and via spans on four layers. The native fixture `route_demo` exercises refill and measures surviving pour area, islands and spokes. On the external two-layer `complex_hierarchy` board, Rust reconnects ground islands after native refill and passes the original pour check. The bridge refuses unsupported zone geometry, including curved polygon outlines. |
| Execution, publication and routing quality | Real process success/failure, stale results, resource limits, source mutation and destination races. Require current execution success, expected-request identity and native validation. Attribute performance on frozen representative inputs and tools. | Structured failures, request identity and native apply controls exist. M2.1 integrates leaf-bucketed (`STATIC_LEAF_CAP = 32`) `StaticSpatialIndex` (`pad_spatial`/`input_spatial`) + iterative deterministic Treap broad-phase (`generated_spatial: SpatialIndex<u64, u32>`, `TreapNode <= 112 B` with `net`/`subtree_uniform_net`/`subtree_len` and `TREAP_UNIFORM_SKIP_CAP = 32`, `tests/spatial.rs` RoundRect/oval + `0` post-rollback work diffs across nets `0..=4`, `80/80` non-equivalent mutants killed (`84` total incl. `4` mathematically equivalent) including `M5_uncharged_pruned_query_visit_treap`, `NEW_uniform_net_charge_bulk_no_fallback`, `ITER3_treap_uniform_net_charge_bulk_no_fallback`, `CopperAabb::overlaps` 4-direction tangency, `point_near_segment` zero-gap boundary, and all `consistent()` / `TREAP_UNIFORM_SKIP_CAP` boundary checks), passing the unified 22-cell `RE-08` matrix vs `1ca08e72` (`0.0006x–0.0381x` route_all speedup on S1/S2/dense_cluster/dense_xy_overlap/scale_50k/N1, `0.0071x` D2_churn, `0.1268x–1.0555x` across all dense static/treap/corridor/mixed cells including `0.1752x`/`0.1268x` isolated `generated_treap_overlap_samenet` and `0.6056x` `mixed_95_5`, `diag_overlap_2000` default `100M` completion at `75.33M`, `1.1415x` GNU / `1.0261x` `/proc` RSS on 50k items); M6 post-landing audit pending. Rust `route-native` stages the operation, validates the result and publishes the saved board. The orchestration tests exercise cancellation, timeouts and destination collisions. Full resource guarantees and representative-board performance acceptance remain open. Matching incomplete KRT output is not success. |

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

The archived four-layer `tigard_fab` case uses the corpus's revision-2 fab
rules. Its 92 footprints have 290 opens under those rules. After importing
its stencil apertures and compound jumper pads, Rust reached its default
100,000-expansion limit at `95b3968`. It published no board. The archived
reference router output on the same PCB and project has zero opens and
retains the input's six errors and 94 warnings. On the separate four-layer
One-Air-Max input, KiCad reports invalid padstacks on U6 pads 3, 4, 7 and 9.
The Rust native workflow refuses this input during import.

For each accepted row record the candidate revision, input/source hashes, fixed
dependency/tool versions, explicit rules, native before/after results,
preservation assertions and independent review. Retain the adverse test that
detects the original failure. Where KRT is wrong, its result is a counterexample,
never the expected output. Tiny-world oracles and representative native boards
serve different purposes; retain both.

No row here is marked complete parity. Existing suites demonstrate only their
named subset. Expand boards within existing admission: this matrix does not
reopen spent attempts, held projects, broad benchmarks or deployment.

Current provenance position (2026-11-16): author agent-1484 reports no known
unattributed KRT implementation reuse in its authored Rust changes; Fable's
whole-crate milestone reads found no apparent KRT-derived source. These are
bounded observations, not a complete origin comparison with KRT's kernel or a
fixture/dependency audit. Exact sources: author acknowledgment
`fa12571d-c27e-4b53-b980-8fc04f6c7288/v1` and Fable correction review
`548f3176-7052-4ac4-a9e2-af64968d2ddc/v1`.

Direct dependencies are fixed in Cargo.toml (`serde 1.0.229`,
`serde_json 1.0.154`, `sha2 0.11.0`); Cargo.lock records transitive versions,
sources and checksums. Lock SHA-256 at the reviewed candidate is
`4b5a525c5100fa630537508b9f1bd679872d3fc4b55f72d70436ab1970b4861f`.
Keep root LICENSE and the separately retained KRT router/LICENSE; preserve
applicable dependency and future reused-source notices. Record source, version,
license and modifications when code or test material is reused.

The matrix is an initial inventory, not a verified exhaustive list of KRT's useful
capabilities. Current evidence is bounded fixture evidence. Identify representative
inputs within existing authorization before accepting a row; preserve named holds
and spent attempts. Neither this document nor an acknowledgment admits a new held
board campaign.
