# Saved copper custody and disposable removal

2026-10-21, agent-510 with a Sol builder and independent Sol reviewer.
Implementation base: `bfb9477fadf55cfd95fa65cc64c7de2ae3e241d5`.
This is a K93/K94 prerequisite; the repair worker and native promotion remain open.

## Behavior and authority

`cli.run_attempt` can emit `validate/copper-ownership.json` after the owned
builtin candidate has been saved and natively graded. `copper_ownership.emit`
binds native UUID/integer geometry inventory, input/output boards, rules,
backend identity, attempt, grade and evidence records. The additive result
reference reports `bound` or `unavailable`; missing custody does not change
native validity. Freerouting is outside this producer.

Every input primitive stays fixed in this first version, including earlier
generated copper. All input UUIDs and native records must survive exactly before
any new copper becomes eligible for removal. A changed or missing input item
makes every generated unit ineligible: a writer could have reminted that item
onto another net. Existing `--protect human` classification is unchanged.

`copper_ownership.prepare_disposable(run_dir, selected_nets, destination,
timeout_s)` verifies the saved result and prepares a new scratch directory. It
requires an eligible native grade, preservation evidence and complete native
terminal coverage. It selects all current-run generated primitives of each
named net, retains same-net input copper, and refuses declared pair splits,
selected arcs, unsupported via modes, locked generated copper, and stale or
malformed evidence. No inherited removal authority or inferred pair naming is used.

`kicad/remove_owned.py` removes exact native UUIDs on the scratch board, saves
and reparses it, and checks every survivor. Native facts and canonical board
structure cover non-track state, including pad UUIDs and zone definitions;
surviving copper attributes must also remain unchanged. A deadline bounds native
subprocesses. Failure or cancellation leaves the incumbent untouched. Scratch
publication uses exclusive directory creation, owned-directory cleanup on error,
and a receipt written last as the completion marker.

The prepared output is explicitly ungraded. Its inherited fill is stale after
copper removal. It must be routed, refilled, have connectivity regenerated and
pass native grading before future promotion. There is no automatic caller that
reroutes, adopts or promotes this scratch board, and `replay_authority` is false.

## Issues caught during verification

Reference partitions describe a native-refilled baseline, whose board hash can
differ from the unfilled input. The producer retains the existing immutable
baseline generation and receipt inside the run. The consumer reuses
`baselinecache.terminal_facts` to validate the source/rules-to-graded-generation
chain and compares the complete reference partition. Every bundle file is hashed
in custody evidence; preparation does not depend on the external baseline cache.

Native `BOARD.Save` expanded the minimal fixture's project defaults. Preparation
now restores the exact bound project/rule bytes on scratch and rereads facts
under those rules before publication. It refuses changed facts or rules. It does
not normalize or modify the incumbent project.

Other review corrections included complete grading evidence instead of a lone
`PASS`, rejection of `..` destinations, whole-input lineage checking, JSON limits,
and failure cleanup. The related wrong-twin writer defect was fixed separately
in `ec91e07745d81ab226be538361a8c44b82adce21`; see
[writer validation](writer-removal-identity-validation.md).

## Evidence and limits

The final custody suite passed 30 native/refusal cases. It exercises exact twins,
multi-branch/via units, input reminting, pair splits, unsupported/locked members,
native mutation/save failure, timeout, pad UUID corruption, stale
grade/rules/evidence and reference-stage binding. Authorization-path fixtures
use native boards/facts with explicitly synthetic grading records; they are not
independent native DRC acceptance measurements. The distinct-refill-stage unit
case verifies the generation contract and portability with constructed hashes;
it is not an end-to-end zone-board qualification.

Two additional root tests passed in 8.53 seconds. They interrupt after successful
native mutation and during publication after a scratch file has moved but before
the receipt. Both verify the entire incumbent run remains byte-identical, the
scratch output/staging are removed, and an unrelated sibling file survives.

A separate real CLI route/native-grade/consumer fixture passed in 18.70 seconds
using its required 0.4 mm default clearance. Its earlier incorrectly configured
run failed grading and was retained. After the final baseline adapter, the
producer and native consumer were exercised against a copy of that exact retained
PASS run: three generated `/SIG_A` segments were removed, seven input primitives
survived, exact rule bytes were retained, and the incumbent was unchanged.
No additional routing was needed for that check. The prepared board was not
graded or presented as a successful route.

The installed-wheel test verified the native script and schema are included.
Seventeen campaign unit checks passed. Four campaign integration cases initially
failed because the worktree lacked its routing runtime; root reran exactly those
cases with an existing isolated runtime and writable output directory: all four
passed in 153.75 seconds. The shared `campaign.validation_evidence` extraction
preserves the existing eligibility policy. A further 63 schema/evidence checks
passed in 8.78 seconds. Independent review cleared the production change.
No target-board experiment, installed
runtime update, agent-ee re-pin or completion improvement is claimed.

```sh
/usr/bin/python3 -m pytest -q tests/test_copper_ownership.py -k 'not real_owned_route'
/usr/bin/python3 -m pytest -q tests/test_copper_ownership.py::test_real_owned_route_emits_consumable_final_custody
/usr/bin/python3 -m pytest -q tests/test_installed_wheel.py::test_copper_ownership_native_consumer_and_schema_ship
/usr/bin/python3 -m pytest -q tests/test_campaign.py
```

Routing integration tests require the existing isolated `KICAD_ROUTE_STATE` and
a writable `KICAD_ROUTE_TEST_OUT`. Exact commands, environment, original failures,
passing logs, boards and manifests are retained under
`/home/halbritt/.local/share/agent-ee/reviews/2026-10-21-krt93-copper-ownership-agent510/`.
Root campaign, contract and cancellation commands/logs are in sibling
`2026-10-21-krt93-ownership-agent510/`.
The builder's final changed-source digest before this report and the two root tests is
`58d5fea014c59261d2f69cb4d63be4865174df8bd39c515356e2a312af4597c1`.

Compatibility is additive. Older runs without this evidence remain valid routing
records but cannot authorize this consumer. Unsupported geometry refuses removal.
Rollback is a revert of this addition; no routing strategy has been enabled.
Next: a supported same-geometry worker contract, whole-cohort rerouting, and
native-qualified atomic promotion with incumbent preservation on every failure.
