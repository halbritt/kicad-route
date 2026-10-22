# Explicit candidate grading for isolated repair

2026-10-21. Base: `8484f9176235e7be56e52a7d295186260465f988`.
Agent-510 verification, Sol implementation and independent Sol review.

An isolated repair must be checked against the original board, including the
connections deliberately removed to make the trial. Treating the stripped
scratch board as its baseline could accept the loss of those connections.

`cli.validate_candidate(run_dir, attempt, cand_dir, scope, out_dir,
pour_limits=None, chain=None)` now exposes the existing native grading body to
an internal caller that has verified its reference context. Ordinary `validate`
still resolves real ancestry, cumulative scope and pair outcomes before calling
it. The native grading body is byte-for-byte unchanged from the base revision.
There is no new CLI option or promotion authority.

The caller owns a disposable candidate: grading can restore its rule files and
refill/save its board. The reference context stays read-only. The new entry
point does not verify the caller's claimed ancestry, establish copper removal
permissions, compare an incumbent partition, or publish an accepted repair.
Those remain requirements of the future repair coordinator.

## Verification

The two new native cases in
`tests/test_grading_scope.py::test_explicit_candidate_context_grades_real_saved_board`
use a candidate outside the original run, with no fabricated `attempt.json` or
candidate ancestry. They exercise real saved-board native grading and verify:

- An unchanged candidate passes; removing protected input copper fails.
- The original project stays byte-identical.
- Native schematic parity runs, reference and candidate terminal coverage is
  complete, and final board hashes match the grading/evidence records.

Builder results: two native cases passed in 25.17 seconds; 75 existing grading
and evidence cases passed in 3.37 seconds. The latter selection excluded the new
native cases and one existing native-refill integration case. Independent source
review approved the extraction; root independently compared the grading tail
against the base and verified the reviewed two-file digest:
`605286b7063be16182effa049a0aa8d804391fcb42e3564e19cccb56f36e4de9`.
No router/kernel behavior changes, runtime installation or target-board campaign
are part of this change.

Focused verification command, using a fresh writable output directory:

```sh
mkdir -p /tmp/kicad-route-explicit-grade-check
KICAD_ROUTE_TEST_OUT=/tmp/kicad-route-explicit-grade-check \
  PYTHONPATH=src:tests /usr/bin/python3 -m pytest -q \
  tests/test_grading_scope.py tests/test_evidence_integrity.py \
  -k 'not native_refill_and_human_ledger_reconstruction'
```

The output directory above is test scratch, not a source worktree. Root landing
uses a retained output directory instead. Raw commands, logs and the builder
receipt are retained at
`~/.local/share/agent-ee/reviews/2026-10-21-krt93-cohort-worker-agent510/`.

## Repair evidence and remaining work

A separate bounded synthetic corridor fixture exercised four actual owned A*
searches. Routing B first blocks A; retrying A with B retained fails. Removing
only generated B copper permits A, then B finds a detour around A's left
terminal. The saved final board has zero native unconnected items and zero DRC
errors, with six unchanged fixture-library warnings. Fixed copper, terminals,
rules and allowed layers were preserved. No supplied solution path was used.
The fixture demonstrates a useful displacement opportunity, not a complete
transaction, target-board improvement or campaign qualification.

Fixture receipt and reproduction are retained under
`~/.local/share/agent-ee/reviews/2026-10-21-krt93-worker-agent510/fixture/`.
Receipt SHA-256:
`10d3499cfebe4480fd08e0be28f47d591b5bb7cdfaf57063d9ee8d42b180b62d`.

Next implementation is an opt-in isolated whole-net repair worker using
`net_rescue._attempt_net_at_geometry`, exact custody and the captured supported
physical policy. It must resolve native net codes by name, preserve requested
width/clearance/via/layer settings, share one whole-trial deadline, and reject
unsupported configurations. Native grading must use the original reference;
acceptance must additionally preserve incumbent terminal equivalences, complete
the root and every removed net, preserve all nonselected copper, and strictly
reduce native component deficit. Failure, timeout, cancellation or publication
failure must leave the incumbent unchanged. Pair and zone support must be
implemented explicitly or refused, never silently omitted.

K93/K94 remain unfinished. Agent-449 owns dispatch and budget under agent-508;
agent-510 verifies. The extraction can be reverted independently until a repair
caller is integrated. Default routing behavior and agent-ee's router pin are
unchanged.
