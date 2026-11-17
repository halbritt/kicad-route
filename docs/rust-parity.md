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
| Ordinary nets around protected copper | Two-terminal board with locked obstacles, then multiple independent signal nets. Preserve exact existing IDs/nets/locks and source bytes, close intended terminals, introduce no native DRC violations. | `test_rust_route_preserves_copper_and_passes_native_connectivity_drc` covers bounded cases. F.Cu, rectangular two-layer boards and uniform rules only. Full parity remains open. |
| Native contact, geometry and clearance | Circle/orthogonal rectangle pads, roundrect/oval/rotated pads, PTH/NPTH drills, outline strokes, tangency, diagonal contact and clipped/first-contact targets. Use actual executable/native readback and adverse controls. | Native regressions and finite-world Rust oracles provide subset evidence. Arbitrary pads, custom/local rules, board cuts and artwork remain unsupported; no universal rounding proof. |
| Effective project rules and multilayer connectivity | Real project rules/net classes/local overrides, stackup, via spans, pad/hole drill constraints, same-net/cross-net hole clearances and solder-mask-web rules. Route under unchanged effective rules and verify native outcomes. | Project/custom-rule import, vias and non-front routing are refused. Do not substitute relaxed constraints for missing capability. |
| Search-state soundness and history-dependent rules | Paths reaching the same position with different prior via histories and different feasible continuations. Compare against a fully augmented independent oracle; retain a counterexample to position-only dominance and verify native legality. | Current single-layer subset has no via-history state. Finite-world oracles cover their named rules only; before adding history-dependent constraints, prove the state/dominance argument and add the adverse regression. RE-02 remains open. |
| Multi-terminal and board-wide routing | Congested competing nets with multiple terminals. Verify per-net component partitions, deterministic scheduling, resource exhaustion and no illegal fallback. | Both independent reviewers accept bounded M7 at7575313: 3/4/6 terminals, preconnected groups, shared budgets and ignored-terminal rejection. R-M7-01 completes the unchanged original request within its original budget and passes native validation; the initial failure is retained. Follow-up heuristic-work, diagonal-goal and multipin/full-copper test corrections await focused independent acceptance. General trees, multilayer scheduling and congestion strategy remain open. |
| Differential pairs, length and delay | Authored pairs/matching groups and native timing/layer rules. Verify coupled geometry, separation, length/delay and preservation with positive and negative controls. | Not implemented in the standalone engine. Backend options are behavioral references only. |
| Rip-up, restore and generated ownership | Blocked multi-net case with protected copper. Inject failures through edits/publication; restore exact IDs/indexes/allocator/geometry and verify native outcomes. | Scoped transactions, generated IDs and nested rollback/panic controls exist. General victim selection, whole-board recovery and migration provenance remain open. |
| Zones, planes, thermals and keepouts | Native zones/rule areas/plane plan with refill. Preserve component partitions, pad contact, thermal spokes and pour integrity. | Zones and unsupported rule areas are refused. No plane/pour parity claim. |
| Execution, publication and routing quality | Real process success/failure, stale results, resource limits, source mutation and destination races. Require current execution success, expected-request identity and native validation. Attribute performance on frozen representative inputs and tools. | Structured failures, request identity and native apply controls exist. Process orchestration, full cancellation/resource guarantees and performance/parity acceptance remain open. Matching incomplete KRT output is not success. |

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
