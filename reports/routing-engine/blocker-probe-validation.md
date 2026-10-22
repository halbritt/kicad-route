# Relaxed blocker probe validation

2026-10-21, KICADROUTE-92. Base revision: `967891c9408047eb5106f4fc3303b8fecb5b19c1`.

The optional `mincut` ordering now queries span-backed tracks and via exclusions,
keeps fixed pads and unscheduled copper, and uses recorded route membership for
each proposed victim. The original cached multiset is removed only from a cloned
map; a reconstructed survivor contribution replaces it. The returned path is an
ordering hint. It is never emitted copper or a proof of physical feasibility.

The default remains `count`. Recipe metadata is recorded only in `mincut` mode.
An empty native probe-price map contributes zero cost to ordinary searches.
The extra native lookup has not received a whole-route performance comparison.

## Independent checks

The reviewed source-file digest was
`c488e3affc98ebb598794445295232424d542b335770151adb6748401d46d8d4`
(sorted changed/untracked paths, path NUL bytes NUL, excluding this receipt).
Independent Sol review found no blocking correctness defect. Root verification:

- `make kernel KERNEL_PYTHON=<Makefile-selected routing interpreter>` rebuilt
  this checkout's extension: six kernel tests, all retained add/remove parity
  modes, three writer tests, and 17 blocker-resource tests passed (15.80 s,
  including a 14.76 s build).
- Eight retained `test_obstacle_lifecycle.py` tests passed (3.22 s), loading that
  extension with the test directory on `PYTHONPATH`. Native KiCad 10.0.7 DRC
  confirmed that the fresh-map control preserved baseline violations and closed
  its two connections; the deliberately stale-map negative control introduced
  clearance and width findings. These are retained lifecycle controls, not a
  new routing gain from the blocker probe.
- The exact 0.1 mm fixture has zero explicit track cells, 27 track spans and
  185 expanded track cells. It now runs a native search and identifies its
  blocker. Other fixtures exercise fixed pads, static walls, overlapping owners,
  an off-grid diagonal shared branch, preserved unscheduled same-net copper,
  via-only contention on a non-routing copper layer, stale caches, search
  exhaustion and injected native failure. Original geometry/cache payloads,
  sampled map membership and cache-removal fingerprints remain unchanged.

An initial lifecycle invocation omitted the test directory from the bootstrap's
module path and failed one import. The corrected invocation passed all eight;
the first invocation is not counted as a passing test.

## Bounded timing experiment

Ten synthetic probes, five per representation, alternated order using identical
settings. Explicit cells were expanded with an independent literal loop. Every
probe found the same owner in 419 search iterations and preserved the source.

| Representation | Median | Samples, milliseconds |
| --- | ---: | --- |
| Spans | 4.69 ms | 16.732, 3.356, 4.685, 3.517, 10.734 |
| Explicit cells | 2.71 ms | 2.704, 2.630, 2.711, 2.728, 8.097 |

The ratio was 1.73, so the plan's provisional 1.10 overhead target was **not
met**. The small sample and substantial timing variation do not establish a
stable regression estimate. Process peak RSS was 42,744 KiB across both arms;
it cannot establish a per-arm memory ratio. No further probes or target-board
campaign were run in that experiment to search for a more favorable result.

## Follow-up: reuse bounded decoded resources

Base revision: `a71397d7d90637098703e3c8b6fca528fa9bc2e9`. A separate, bounded
profile of the same ten probes found six decodes per probe: the original cache
three times, its rebuilt validation contribution twice, and its survivor once.
For the span arm this expanded 2,570 rows. The first profiled call was dominated
by imports; its timing is not comparable to a warm explicit-cell call. Across
the other eight instrumented calls, the span/explicit medians were 1.662/1.378
ms. These measurements identify redundant work, not a stable speedup target.

The helper now returns bounded decoded arrays from validation and passes them
directly to multiset comparison and ownership extraction. This reduces the
fixture to three logical decodes and 1,028 expanded rows. Reuse lasts only for
the current probe; there is no global or board-attached memo. Small-via and all
rung channels still undergo exact row-multiplicity comparison. Ownership sets
alone deduplicate; authoritative map updates still use the original and
survivor cache payloads. Limits, refusal reasons, search costs and native code
are unchanged.

A regression failed before the change with original/rebuilt/survivor decode
multiplicities of 3/2/1. It now checks at most one decode per unchanged
contribution, preserved source state, and rejection of a cache missing a
duplicate even though its unique footprint is unchanged. The selected-Python
kernel gate rebuilt this checkout's extension and passed six kernel tests,
all parity modes, three writer tests and all 18 resource tests. The changed
source/test digest before this receipt was
`2e6b99a0ee47b3acd376d64d7d8343fe3692d79557b28053b031d14e086f2bde`.

Root repeated the retained, uninstrumented ten-probe comparison **once**:

| Representation | Median | Samples, milliseconds |
| --- | ---: | --- |
| Spans | 1.290 ms | 6.426, 2.106, 1.290, 1.275, 1.269 |
| Explicit cells | 1.396 ms | 1.863, 1.780, 1.396, 1.255, 1.219 |

All ten probes retained owner 2, 419 search iterations and unchanged source
state. The observed ratio, 0.924, is below the provisional 1.10 threshold for
this run only. The earlier profile and this run have different instrumentation,
and neither is a paired before/after performance qualification. Shared-process
peak RSS was 44,872 KiB; per-arm memory remains unqualified. No target-board
campaign, native board-output gain, expanded supported cases or default change
is claimed. K92 remains in progress.

Retained commands, profile, regression log and comparison results are under
`/home/halbritt/.local/share/agent-ee/reviews/2026-10-21-krt92-followup-agent510/`.

## Remaining qualification

This is a correctness slice, not release or performance qualification of K92.
K91's current-policy identity gate remains open. Keep the optional mode disabled
by default pending matched board experiments and the remaining timing/memory
gate. Per-net via geometries/alternative rungs, existing same-net vias/PTH
terminals, swept wider-track margins, paired repair units and live callbacks
explicitly return unknown and preserve the existing ordering. Native rung-price
tests do not establish integration support for those cases.

Resource expansion is capped at 250,000 contributions per cache and 250,000
retained owner resources per proposal. Search uses the smaller of existing
`max_iterations` and `max_probe_iterations`. These are work limits, not physical
clearance or demand thresholds. Protection refusals may append to the existing
diagnostic `PROTECTED_SKIPPED` ledger under `relaxed probe`; authoritative board,
route, cache and occupancy state remains untouched.

Rollback is selecting the existing `count` mode. No electrical constraints,
route-ownership mutation policy, history tuning or final grading rules changed.
