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
| Native contact, geometry and clearance | Circle/orthogonal rectangle/oval/roundrect pads, rotated pads, PTH/NPTH drills, outline strokes, tangency, diagonal contact and clipped/first-contact targets. Use actual executable/native readback and adverse controls. | The native shape suite routes rotated and odd-sized pads, trapezoids and a footprint polygon with a hole. The bridge also represents pad drills and selected copper/mask artwork. It refuses unsupported shapes such as chamfered pads. General KiCad shape and rounding equivalence remains open. |
| Effective project rules and multilayer connectivity | Real project rules/net classes/local overrides, stackup, via spans, pad/hole drill constraints, same-net/cross-net hole clearances and solder-mask-web rules. Route under unchanged effective rules and verify native outcomes. | `test_route_native_accepts_valid_sidecars_and_multilayer_vias_end_to_end` exercises project minimums, a net class and a net-specific custom width rule on four layers. Tier 4 also covers layer-specific via spans. Support depends on the rule and padstack forms the importer can represent. |
| Search-state soundness and history-dependent rules | Paths reaching the same position with different prior via histories and different feasible continuations. Compare against a fully augmented independent oracle; retain a counterexample to position-only dominance and verify native legality. | The multilayer search records prior via placements when they constrain a continuation. T1-F6-05 exercises a four-layer witness with a hole-to-hole clearance constraint. Position-only dominance and general search completeness remain subjects of RE-02 verification. |
| Multi-terminal and board-wide routing | Congested competing nets with multiple terminals. Verify per-net component partitions, deterministic scheduling, resource exhaustion and no illegal fallback. | The Rust engine joins existing copper components and negotiates competing nets. T4-04 routes a four-layer fixture with 24 three-pad nets under a shared work budget. The M7 corridor regressions exercise collinear joins, isolated pads and preservation of pre-existing copper. Solvable external multilayer board coverage remains open. |
| Differential pairs, length and delay | Authored pairs/matching groups and native timing/layer rules. Verify coupled geometry, separation, length/delay and preservation with positive and negative controls. | Coupled pairs, intra/group length/delay matching, impedance control, and `--ac-couple-chain-json` AC-coupled chain matching are implemented in `rust-engine` (`diff_pair.rs`, `orchestration.rs`). Optional `--bus*` geometric bus-corridor ordering preferences remain unimplemented and excluded from `USAGE_MSG` (M5.2+ open item; `fixtures/bus_routing` routes via native single-ended negotiation). |
| Rip-up, restore and generated ownership | Blocked multi-net case with protected copper. Inject failures through edits/publication; restore exact IDs/indexes/allocator/geometry and verify native outcomes. | `rust-engine/tests/negotiate.rs` exercises whole-net victim repair, protection of source copper and rollback after a failed repair. The four-layer T4-04 fixture checks the published board and deterministic copper on repeated runs. |
| Zones, planes, thermals and keepouts | Native zones/rule areas/plane plan with refill. Preserve component partitions, pad contact, thermal spokes and pour integrity. | T4-03 exercises filled power and ground zones, thermal pads, keepouts and via spans on four layers. The native fixture `route_demo` exercises refill and measures surviving pour area, islands and spokes. The bridge refuses unsupported zone geometry, including curved polygon outlines. |
| Execution, publication and routing quality | Real process success/failure, stale results, resource limits, source mutation and destination races. Require current execution success, expected-request identity and native validation. Attribute performance on frozen representative inputs and tools. | Structured failures, request identity and native apply controls exist. M2.1 integrates leaf-bucketed (`STATIC_LEAF_CAP = 32`) `StaticSpatialIndex` (`pad_spatial`/`input_spatial`) + iterative deterministic Treap broad-phase (`generated_spatial: SpatialIndex<u64, u32>`, `TreapNode <= 112 B` with `net`/`subtree_uniform_net`/`subtree_len` and `TREAP_UNIFORM_SKIP_CAP = 32`, `tests/spatial.rs` RoundRect/oval + `0` post-rollback work diffs across nets `0..=4`, `80/80` non-equivalent mutants killed (`84` total incl. `4` mathematically equivalent) including `M5_uncharged_pruned_query_visit_treap`, `NEW_uniform_net_charge_bulk_no_fallback`, `ITER3_treap_uniform_net_charge_bulk_no_fallback`, `CopperAabb::overlaps` 4-direction tangency, `point_near_segment` zero-gap boundary, and all `consistent()` / `TREAP_UNIFORM_SKIP_CAP` boundary checks), passing the unified 22-cell `RE-08` matrix vs `1ca08e72` (`0.0006x–0.0381x` route_all speedup on S1/S2/dense_cluster/dense_xy_overlap/scale_50k/N1, `0.0071x` D2_churn, `0.1268x–1.0555x` across all dense static/treap/corridor/mixed cells including `0.1752x`/`0.1268x` isolated `generated_treap_overlap_samenet` and `0.6056x` `mixed_95_5`, `diag_overlap_2000` default `100M` completion at `75.33M`, `1.1415x` GNU / `1.0261x` `/proc` RSS on 50k items); M6 post-landing audit pending. Rust `route-native` stages the operation, validates the result and publishes the saved board. The orchestration tests exercise cancellation, timeouts and destination collisions. Full resource guarantees and representative-board performance acceptance remain open. Matching incomplete KRT output is not success. |

On 2026-11-19, release builds of `4859553` and `36f7b11` produced identical
copper signatures for each of the three T4-07 fixtures under KiCad 10.0.8.
The inputs had six opens on `route_demo`, ten on `bus_routing` and four on
`ac_matching`. All three outputs had zero opens and zero native DRC findings.
The composed AC-chain skew was 0.000 mm. The four-layer AC fixture requests
pair routing on F.Cu. These measurements cover the checked-in fixture boards.

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
