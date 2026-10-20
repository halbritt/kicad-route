# kicad-route improvement analysis

Source: `f0c09ac3b8d39a6674a734d3caaa9d7e3ae9633d`, clean main, 2026-10-20.

## Result and authority

Fourteen work items are selected for filing: one urgent, seven high and six
medium priority. Each has a causal trace or a bounded protection gap, proposed
scope and falsifiable acceptance criteria in `issues-draft.json`. Plane is the
durable work queue; this report retains the audit basis and exclusions.

Filing completed and read back: **KICADROUTE-68 through KICADROUTE-81**.
All fourteen titles, complete HTML bodies, priorities, external identities,
Backlog states and empty assignee lists matched the submitted payloads.
`verification.json` records that readback. No pre-existing issue was changed.

| Work item | Priority | Topic |
| --- | --- | --- |
| [KICADROUTE-68](https://proximal.tail0ecc2e.ts.net:10000/proximal/projects/79dc75bd-db64-41d2-a919-fe92ec510642/issues/a9bf3120-f349-452c-9193-39305e515199) | Urgent | Smoke publication deletion |
| [KICADROUTE-69](https://proximal.tail0ecc2e.ts.net:10000/proximal/projects/79dc75bd-db64-41d2-a919-fe92ec510642/issues/11f7bb9e-49ba-4fdb-b447-9a5f3b2efe6f) | High | Corpus missing-evidence gate |
| [KICADROUTE-70](https://proximal.tail0ecc2e.ts.net:10000/proximal/projects/79dc75bd-db64-41d2-a919-fe92ec510642/issues/6af92d81-fbb1-4474-930c-6d52fa57ed88) | High | Final candidate hash |
| [KICADROUTE-71](https://proximal.tail0ecc2e.ts.net:10000/proximal/projects/79dc75bd-db64-41d2-a919-fe92ec510642/issues/d5ebc22e-3f08-415e-aa8f-e4251ec1f69e) | High | Baseline cache identity/publication |
| [KICADROUTE-72](https://proximal.tail0ecc2e.ts.net:10000/proximal/projects/79dc75bd-db64-41d2-a919-fe92ec510642/issues/cab6cb20-8a7c-43f5-b255-5beeb26c3e8b) | High | Quoted-text writer loop |
| [KICADROUTE-73](https://proximal.tail0ecc2e.ts.net:10000/proximal/projects/79dc75bd-db64-41d2-a919-fe92ec510642/issues/77a20d7e-24b3-4ad1-9ea4-5e2cbbb0ca36) | High | Preserve copper artwork |
| [KICADROUTE-74](https://proximal.tail0ecc2e.ts.net:10000/proximal/projects/79dc75bd-db64-41d2-a919-fe92ec510642/issues/f40f22a2-fe73-4125-8019-3ca79e256cbe) | Medium | Ledger execution identity |
| [KICADROUTE-75](https://proximal.tail0ecc2e.ts.net:10000/proximal/projects/79dc75bd-db64-41d2-a919-fe92ec510642/issues/26202ec1-6369-4c14-8618-1a09c52903f7) | High | Staged environment installation |
| [KICADROUTE-76](https://proximal.tail0ecc2e.ts.net:10000/proximal/projects/79dc75bd-db64-41d2-a919-fe92ec510642/issues/66a312ed-dc3b-4106-8d63-6728069f82ea) | Medium | Update determinism exit status |
| [KICADROUTE-77](https://proximal.tail0ecc2e.ts.net:10000/proximal/projects/79dc75bd-db64-41d2-a919-fe92ec510642/issues/6298e3e6-fbb7-44b5-9325-db688a1d9fa4) | Medium | Wall-clock measurement qualification |
| [KICADROUTE-78](https://proximal.tail0ecc2e.ts.net:10000/proximal/projects/79dc75bd-db64-41d2-a919-fe92ec510642/issues/dfb64fe3-baec-444f-8dc9-aee18163f40d) | Medium | Verifiable publication bundles |
| [KICADROUTE-79](https://proximal.tail0ecc2e.ts.net:10000/proximal/projects/79dc75bd-db64-41d2-a919-fe92ec510642/issues/27f2126e-76d3-43c7-8811-87470e22307d) | Medium | Installed-wheel runtime layout |
| [KICADROUTE-80](https://proximal.tail0ecc2e.ts.net:10000/proximal/projects/79dc75bd-db64-41d2-a919-fe92ec510642/issues/fe8ddeb3-5513-449f-9c1a-b147cfaccbd3) | High | Owned kernel regression gate |
| [KICADROUTE-81](https://proximal.tail0ecc2e.ts.net:10000/proximal/projects/79dc75bd-db64-41d2-a919-fe92ec510642/issues/310fa9df-db9b-4a16-9cd7-208b3025cdfa) | Medium | Benchmark quality/resource contract |

The user authorized analysis and detailed Plane filing. No routing fixes, source
edits, dependency updates, test execution, benchmark runs or destructive probes
were authorized or performed in this analysis. Existing migration receipts are
inherited evidence, not new qualification of current main.

Failure-mode verdict: **HIGH_RISK_FAILURES**, medium confidence. The strongest
failure paths are unrestricted recursive deletion by smoke publication, a corpus
gate that can accept missing validation evidence, and stale/misbound grading
evidence. The failure ledger has one BLOCKER, ten SERIOUS and three MINOR items.
These are audit severity classes, not a claim that all items are production
incidents. Plane priorities also account for exposure and available workarounds.

Test-protection verdict: **FALSE_CONFIDENCE**, medium confidence for the selected
boundaries. The relied-on regression/evidence gates omit important negative
cases. This is not a verdict that the existing native suite is useless: it
provides substantial protection for route isolation, preservation, process
cleanup and the two motivating fork defects.

## Survey and depth ledger

| Area | Depth | Evidence read | Why selected / residual risk |
| --- | --- | --- | --- |
| Grading and evidence, corpus, baseline cache, ledger and repeat qualification | Deep dive | `cli.py` grading/run/cache/update paths; `evidence.py`, `corpus.py`, `ledger.py`, `determinism.py`, `statemanifest.py`, `provenance.py`; `test_hygiene.py`, `test_evidence_manifest.py`, relevant interface/pour assertions | These paths determine whether a candidate is accepted and whether prior evidence applies. Static traces, plus one inherited changed-refill receipt. Native DRC internals were not requalified. |
| Installation, distribution and publication | Deep dive | update/build/smoke/publish paths; `common.py`, `pyproject.toml`, installer; `test_owned_build.py`, `test_publish_update_docs.py`, `test_owned_router.py` | State mutation, data-loss exposure and standalone installability. Static only; no install or failure injection. |
| Owned engine and regression/performance protection | Deep dive | Writer transforms/callers, Rust obstacle-map state and pair-search sections, module inventory; two retained regression entrypoints; benchmark script/test and migration acceptance/report | Ownership now includes implementation invariants, not just subprocess adaptation. Python/Rust search algorithms were sampled, not exhaustively verified; no speedup or optimality claims. |
| Planes and differential pairs | Survey | `plane_plan.py`, `planes.py` producer/worker, `diff_pairs.py` detection/grade/route sections, current KICADROUTE-66 body and design note | Current plan validation and independent native evidence are strengths. Full geometry/EMC review and plane-plan/2 implementation are outside this audit. |
| Snapshot, preservation and process lifecycle | Survey | snapshot/export sections, copy/preservation call paths, `procgroup.py`, budgeted runner, existing test names/assertions | Source isolation, no-rule-relaxation and process-group cleanup remain preservation boundaries. Symlink/library and operating-system edge cases were not exhaustively retested. |
| Campaign, corpus replay, sandbox | Survey with adjacent traces | campaign invocation/cache preparation, corpus entry replay and comparisons, sandbox requirement inventory, relevant tests | Followed cross-boundary consumers needed for findings. No new sandbox escape or concurrency claim beyond the traced cache/publication windows. |
| Placement, rendering, diagnostic geometry, optional stage3d assets | Inventory / mostly unread | source-closure inventory and file topology | Imported source is intentionally conservative. No deletion/refactor recommendation based only on size. These branches remain unqualified by this review. |

The code graph command could not provide usable coverage: its coverage query
reported `daemon-backed CLI execution failed`. The audit therefore used direct
source reads and `rg`; it makes no graph-completeness or graph-based absence
claim. A repository file survey found no tracked `.github` CI configuration.
That observation is not evidence that no external CI exists.

## Ranked findings

| Slug | Severity | Failure or improvement | Detection and recovery today |
| --- | --- | --- | --- |
| safe-smoke-publication | BLOCKER | A user-selected publication destination is recursively removed without proving ownership or checking overlap. | No guard in the traced path. Recovery would depend on backups, not this command. |
| corpus-fail-closed | SERIOUS | Missing counters and a failed validation can produce no regression entries. | Raw result contains the failure, but corpus/update can report PASS. Manually inspect raw results. |
| final-candidate-evidence-hash | SERIOUS | Evidence identifies pre-refill bytes while the candidate path contains post-refill bytes. | Comparing both validation hashes reveals it; use final candidate hash pending repair. |
| baseline-cache-identity-publication | SERIOUS | Cache omits selected-board/grader identity and exposes separately written records. | Recompute in a fresh OUT as a workaround; there is no generation validation on hits. |
| quote-safe-board-writer | SERIOUS | Raw parenthesis counting can fail to advance on valid quoted text. | Wrapper budget eventually stops the backend; no candidate may be delivered. No parser-local recovery. |
| preserve-copper-artwork | SERIOUS | Writer moves protected input copper artwork to silk. | Native preservation rejects the candidate. No supported preservation mode fixes the engine behavior. |
| staged-router-install | SERIOUS | Live dependency upgrade and extension replacement precede complete startup validation. | Update reports failure; reinstall is the available recovery. Prior full environment is not retained by this path. |
| owned-kernel-regression-gate | SERIOUS | Important mutable obstacle/search invariants lack a focused owned fast test gate. | Native full-board checks and two fork regression scripts remain useful, but are not equivalent kernel protection. |
| update-determinism-exit | SERIOUS | Update prints failed determinism but can return zero. | Inspect separate repeat record/manifest; update receipt is incomplete. |
| wall-clock-measurement-qualification | SERIOUS | Build-level repeat proof labels a wall-clock-stopped attempt a measurement. | Stop flags remain in raw commands. Consumers must currently override the top-level flag. |
| published-evidence-bundle | SERIOUS | Flattened paths break hash lookup; reused destinations retain stale files and can follow destination symlinks. | Original run is still usable. Publication lacks a self-contained verification contract. |
| ledger-build-identity | MINOR | Legacy identity keys ignore owned content while incorporating unrelated Git commits. | `--force` permits reruns; fingerprint meaning remains misleading. |
| wheel-runtime-layout | MINOR | Installed console script retains checkout-only resource/subprocess paths. | Symlink checkout installation works; wheel consumers need the missing checkout. |
| benchmark-contract-coverage | MINOR | Warmup quality, routing/pair state and aggregate memory are outside the current automated comparison gate. | Historical migration manually inspected all attempts; future runs require that manual work again. |

All defect rows are `static-traced`; the final-candidate row also has `inherited`
artifact corroboration. Enhancements are explicitly described as such rather than
as observed runtime incidents. Every item remains unimplemented and unverified as
a repair.

## Evidence integrity example

The retained four-layer-plane migration candidate has
`refill_modified_board=true`. Its top-level evidence candidate digest is
`a10f30735f774b422ecc74208fe38ade8572ac8ad46719354c4142eb38f9c1c6`;
the recorded graded/final digest is
`9522498140172ddb6df7f52fc9607e4e15d87cbe26a6c3ed042f681258cb82f9`.
The artifact is
`build/owned-engine-migration/comparisons/four-layer-plane/candidate-2/run/result.json`.
Current `evidence.block` still selects the pre-refill field. This does not overturn
that board's native validation: it identifies the wrong artifact in one public
evidence field.

## Failure windows, retries and protection

Installation has useful local protection: the new Rust module is imported and
version-checked before an atomic file replacement. The missing boundary is the
whole numerical environment and its activation/manifest. Proposed staging must
not silently change the documented policy that corpus regressions are reported
without automatic rollback.

Baseline caching writes report, metadata and pour facts independently. The
campaign warms its own cache, which reduces its normal race exposure, but does
not serialize separate CLI processes sharing OUT. Cache identity and publication
need one generation contract. This review did not induce an actual torn write.

The run ledger prevents selected repeated failures but is not an execution lock.
No exactly-once guarantee is claimed. The proposed identity fix leaves the repeat
policy intact. Publication should become recoverable without treating arbitrary
existing destination contents as owned.

The process runner starts a new session/process group, asks for a stop, escalates
to termination, and checks leftovers. Those paths are worth retaining. The quote
parser finding concerns a loop inside the backend; it is not evidence that the
wrapper's process-group cleanup is broken.

## Standing test protection

The inspected corpus test asserts that an increased open-connection count fails
the update gate. It does not assert that failed DRC with missing counters fails.
The evidence repeat fixture equates pre- and post-refill hashes; a fixture that
actually changes during refill is needed to distinguish them. Publication tests
assert receipt inventory and skip source symlinks, but do not verify published
path/hash closure or destination symlink safety.

The build test verifies that an invalid new extension leaves the old module
bytes intact. It says nothing about dependency upgrades that already happened or
startup checks after replacement. The owned engine has two retained regression
files; the wrapper's extensive native suite should not be presented as exhaustive
obstacle-map and search-state protection.

Smallest tests are specified in each work item: status-transition tables for
corpus, changed-refill hash assertions, disposable destination sentinels, bounded
quoted-text cases, cache generation races, update orchestration fakes and
obstacle-refcount/search invariants. Numeric line coverage is not the objective.
Tests that launch real processes must use real safe process groups and intercept
process-control side effects in fake-process cases.

No new pass/fail/skip count is reported because no suite was run. The 490-case
migration accounting belongs to earlier source/runtime evidence and must not be
described as a current whole-suite pass. Useful later commands include the
repository's `make smoke`, targeted system-Python pytest invocations and the
comparison harness; they were not run here. A fast kernel gate and installed-wheel
acceptance command do not yet exist as documented project gates.

## Alternatives, excluded candidates and residual risk

The selected action is filing scoped work, not implementing it. Leaving code
unchanged remains the lower-risk action in this turn. Broad decomposition of
`cli.py` or the imported router was rejected: size alone does not establish a
useful boundary. No GPU rewrite, solver replacement, automatic rule relaxation,
paid compute or private-board upload is proposed. Optimization is deferred until
representative profiles identify a cost and an independent correctness gate
protects it.

Missing README-linked documents were an initial candidate, rejected after direct
file reads confirmed they exist. KICADROUTE-66's plane-evidence fixes are present;
its stitching-via design remains an existing scoped decision, not a new duplicate.
Cancelled external-fork pin/registry items 65 and 67 stay cancelled. Follow-ups
to 7, 27, 31 and 41 identify new uncovered edges and preserve the prior fixes.

Unmeasured risks remain in geometry kernels, malformed native reports beyond the
traced paths, optional rendering/placement imports, external Java/KiCad behavior
and large-board performance. Their absence from this issue set does not establish
their correctness. Further dynamic checks require explicit bounded authority.

## Doctrine decision record

This audit used the failure-mode and test-protection skills to separate source
traces from inherited results and prohibit unrequested failure injection. The
design skill discouraged size-only refactors. The prose review removed generic
improvement claims; the issue bodies specify project behavior and assertions.

The selected doctrine supports evidence before intervention, end-to-end integrity
and authority-bounded action. Alternatives are (1) leave code unchanged and file
the evidenced work, selected; (2) implement repairs now, rejected as outside this
goal; (3) prescribe broad architectural changes or speedups, deferred for missing
cost and runtime evidence.

Packet `pkt-ea4b9c0b7cde9e81`, content SHA-256
`ea4b9c0b7cde9e81a206b26999b22f9df54909c0ec8b5972e3403258024fc763`,
corpus `corpus-2026-07-12-a11702cc9217`, doctrine
`doctrine-f6bbb5196a3f8bf9`, retriever `retriever-ec995ecdd083b2c8`.
Five typed records cover user authority, repository contracts, source traces,
test assertions and inherited artifacts.

The remaining packet obligations are nonmaterial to *filing proposals* and are
retained here by group: audit frequency and repair ownership belong to future
implementation; semantic delta, checkpoint verification and per-edit
classification are specified as proposals in individual items, not executed
changes; failure output/last-known-good state require bounded reproductions not
run here; raw history, sample commits, co-change, exclusions and behavioral-data
fitness would be material to a history-based refactoring claim, which is not made;
change-amplification and pressure-reduction obligations likewise do not justify
any selected refactor; no-change cost, future change, intervention uncertainty and
latent-risk assessment are bounded to the traced failure windows and explicit
residual risks; preservation matrices/semantic authority must be completed before
implementing engine behavior changes. Personnel-scoring obligations are
inapplicable and never used. The alternative/no-change obligation is addressed
above. Reopen recommendations if a missing caller guard, current test or changed
source disproves a trace.
