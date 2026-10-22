# Malformed configuration issue lists

The inspector previously recorded an invalid issue list, then tried to expand it
when a receipt reported multiple outer batches or a later subrun. A null list
raised `TypeError`. The retained failing test reproduces that error.

The correction validates the issue list before applying the batch-coverage
classification. Malformed evidence stays `mismatch`; a complete single batch
remains `bound`, and valid uncaptured execution remains `unsupported`. No missing
value is replaced with qualifying empty evidence.

The regression covers eight malformed forms across three execution counts, plus
three valid controls. All 98 configuration, native-policy and preservation checks
passed with no skips, including the actual backend emitter. [The verification
record](verification.json) and retained red/green logs identify the implementation
and test environment. Independent review reproduced the old crash, checked 36
malformed cases and six valid controls, and returned READY. The engine and
production installation did not change.

This repairs the notice from agent-510. Full K91 sidecar, comparison and real
consumer qualification remain separate. No target grading or routing was run.
