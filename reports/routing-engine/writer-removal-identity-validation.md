# Copper removal identity validation

2026-10-21, agent-510 with a Sol builder and independent Sol reviewer.
Base: `bfb9477fadf55cfd95fa65cc64c7de2ae3e241d5`.

## Observed defect and repair

The production `kicad_writer.remove_segments_from_content` helper could delete
a locked input segment when asked to remove a different, unlocked segment with
the same endpoints, layer and net. Its key omitted UUID, width and lock state.
The retained reproduction demonstrated both equal-width and different-width
twins. The via helper similarly omitted UUID, diameter, drill and layer span.

The shared removal loop now requires UUID and exact supported geometry when
the target carries a UUID. A missing or changed identity stays unmatched.
Locked blocks and graphic targets remain untouched. UUID-less generated objects
retain counted geometry matching, including the existing six-decimal coordinate
emission convention: some oracle callers do not receive the UUID minted by the
writer. Width, net, copper layer, and via size/drill/span participate in matching.
Unsupported via shapes remain unmatched.

The output writer retries some targets after net/layer transforms. The helpers
now return the actual unmatched objects, preserving identity and multiplicity
across both passes. Parsing uses the existing quote-aware expression spans and
depth-one field reader; quoted net text cannot impersonate a lock token.

This fixes an existing default-path deletion defect. It does not grant permission
to remove copper, establish writer lineage, or implement a repair transaction.
The separate disposable-removal design still requires native UUID custody and
bound evidence. No routing defaults, electrical rules or native grading change.

## Validation

Fourteen focused production-helper tests passed. They cover both twin orders,
locks and quoted lock text, precise UUID-bearing positions, generated-coordinate
rounding, geometry/net/span mismatches, counted duplicates, residual identity,
escaped net names, legacy timestamps, mask-exposed segments, unsupported vias
and malformed-expression refusal. The new suite runs in the owned kernel gate.

The full kernel gate passed, including native occupancy parity, existing writer
tests and blocker-resource checks. Independent review traced cleanup, plane
repair, route output and oracle callers, inspected the passing logs, and found
the bounded change ready. No target-board routing campaign was run; these tests
establish deletion correctness and compatibility, not improved completion.

```sh
/usr/bin/python3 src/kicad_route/router/tests/test_writer_removal_identity.py
make kernel KERNEL_PYTHON=/home/halbritt/git/kicad-route/.venv-krt/bin/python
git diff --check
```

Retained local evidence:
`/home/halbritt/.local/share/agent-ee/reviews/2026-10-21-krt93-ownership-agent510/`:
`wrong_twin_removal.py`, `wrong-twin/receipt.json`,
`writer-fix/{receipt.json,focused-red.log,focused-green.log,kernel-green.log,independent-review.md}`.
The independent reviewed-source digest (sorted changed path, NUL, bytes, NUL)
is `24f6b5fbc90ccca05ec2e6e5cc3cb02e09e4b43bf784ae963d797c3abd5418c6`;
it excludes this subsequently added report. The receipt records individual hashes.

Rollback is a revert of the repair commit and its kernel-suite registration;
that restores the demonstrated wrong-twin deletion behavior. Existing UUID-less
compatibility is intentionally retained until callers provide stronger lineage.
