# K91 experiment receipt review: HOLD

Candidate `6e41a13d3543426c2fbf1e5e9c99c066038c432c` composes builder
`4f06368fbd68bab44cbfde839647aa5b3f7ef7c5` onto landed `57c512d` and its malformed
configuration correction. Its experiment source SHA-256 is
`98fbe298cbb22aa286570c71b7fa18d29805e90dda33bc3b70a88d43642dd806`.
The candidate is not integrated. The existing `krt91d-continuation` owns correction;
no duplicate builder was started by this review.

The retained independent report and reproductions demonstrate six blockers:

- EI2: a stored configuration `bound` status can conceal an incomplete or wrongly
  bound raw request/manifest after resealing.
- EI3: a self-referential origin path becomes an eligible depth-zero origin.
- EI4: nonempty list-shaped retained attempt/facts records can crash qualification.
- EI5: physical inner layers with negative routing cost are incorrectly treated as
  routable layers, refusing a conforming four-layer current-policy configuration.
- EI6: a valid candidate terminal partition can replace the input partition after
  resealing; the reference lacks source-to-refilled baseline custody verification.
- EI7: malformed or inconsistent request/command budgets can remain eligible.

Root's experiment module gate passed **57 tests, zero skips**, in 159.42 seconds,
including eight actual public CLI runs and strict comparison. Independent review
verified all 610 supplied artifact hashes, the real current/historical positives,
stale-runtime ineligibility, all eight comparison arms and six refusal controls.
These positive checks do not override the demonstrated blockers. The initial root
pytest invocation failed during setup because the selected temporary directory's
parent was absent; the corrected invocation produced the retained passing log.

The builder's broad suite remains uncleared: baseline 56 failures/901 passes/two
skips versus candidate 51 failures/967 passes/two skips. Reported failure-name
subset equality is not full clearance. The native via field-set mismatch was
independently reproduced and repaired separately in `3089950`; other failures need
their own matched-runtime disposition. No broad environmental attribution is
certified here.

The existing AGENTEE-442 lanes must replay final real receipts with the exact
five-field native policy contract. No final schema acceptance, pin/install change,
target grading/routing, performance improvement or K91 release follows from this
review. Existing correctness gates still precede the single bounded diagnostic.
