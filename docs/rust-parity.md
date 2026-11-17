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
| Native contact, geometry and clearance | Circle/orthogonal rectangle pads, outline strokes, tangency, diagonal contact and clipped/first-contact targets. Use actual executable/native readback and adverse controls. | Native regressions and finite-world Rust oracles provide subset evidence. Arbitrary pads, custom/local rules, board cuts and artwork remain unsupported; no universal rounding proof. |
| Effective project rules and multilayer connectivity | Real project rules/net classes/local overrides, stackup, via spans and drill constraints. Route under unchanged effective rules and verify native outcomes. | Project/custom-rule import, vias and non-front routing are refused. Do not substitute relaxed constraints for missing capability. |
| Multi-terminal and board-wide routing | Congested competing nets with multiple terminals. Verify per-net component partitions, deterministic scheduling, resource exhaustion and no illegal fallback. | Bounded deterministic routing and incumbent components exist. General trees, multilayer scheduling and congestion strategy remain open. |
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
