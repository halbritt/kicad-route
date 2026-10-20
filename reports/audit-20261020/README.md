# KICADROUTE-68–81 qualification

All **14 repairs and 66 original acceptance clauses** are verified in [final-acceptance.json](final-acceptance.json), with a readable [acceptance report](final-acceptance.md). The landed and pushed source is `b4bd511d782e87efcefd9d7be2494fff7461a5e4`; its tree matches qualified candidate `f0f73da86835ec583f2a2c9128e6893fa59c03b7`. [Integration provenance](provenance/land-integration.json) preserves the source history, restored ancestors and landing checks.

The checkout-default runtime is generation `f23025e1b26d406d83e2167aa0c0da9f`. Normal installation passed all **29 gates**, followed by two actual native repeats with process PASS, routing COMPLETE, validation PASS, equal geometry and the executed build recorded. Manifest drift is empty. Doctor is overall PASS across 24 checks: 19 PASS, four optional NOT_RUN and one UNSUPPORTED. [Installation proof](runtime/landed-main-20261020T224951.985950Z-2835650/receipt.json), [doctor](runtime/landed-main-20261020T224951.985950Z-2835650/doctor/doctor/doctor.json) and the [later observation](provenance/landed-runtime-observation.json) retain actual identities, raw runs and logs. Python is 3.12.3; exact NumPy/Pillow/SciPy/Shapely versions and the Rust lock are recorded. Fresh builds have their own binary hashes; cross-host binary equality is not required.

[Test coverage](tests/coverage-union.json) accounts for **741 current node IDs** through passing observations across five retained JUnit invocations. The original broad run ended **686 passed, 2 failed**; corrective reports passed **193/193, 25/25, 24/24 and 113/113**. These observations form a union, not one green full-suite invocation at the final commit. Original failures and corrective receipts remain retained.

The final performance authority is user-selected **Archon**, session **8896**: one persistent same-image container, all available CPUs 0–15, normal priority and no task-imposed quota. All four predeclared boards passed five measured repetitions per side and separate warmups: **48 native attempts**, with completion, validation, preservation, copper equality and uncensored timing. Candidate/reference median ratios are below the unchanged **1.10** limits:

| Board | Full-CLI wall | GNU-time RSS | Process-tree RSS |
| --- | --- | --- | --- |
| [diff-pair](performance/archon/diff-pair-archon/comparison.json) | 0.777778 | 0.998983 | 1.007962 |
| [four-layer](performance/archon/four-layer-archon/comparison.json) | 0.865248 | 0.998234 | 1.001951 |
| [multipoint](performance/archon/multipoint-archon/comparison.json) | 0.846154 | 0.998949 | 1.010543 |
| [route-demo](performance/archon/route-demo-archon/comparison.json) | 0.884368 | 1.000180 | 1.004623 |

[Independent verification](provenance/archon-final-verification.json), the [terminal matrix](performance/archon/archon-metadata/matrix.json), raw results/timings and 48 resource/membership records bind those observations to actual tools, source, runtimes and host/container controls. Both scientific environments used matching versions and 1,619 prepared bytecode files. This qualifies the declared Archon profile; results and medians are not combined across hosts.

The complete local one-CPU study **94696 also passed 48 attempts** and is retained as [supporting history](performance/history/cpu-rate/cpu-rate-metadata/matrix.json), with its calibration, protocol and resources. Earlier route-demo, multipoint and differential-pair failures remain under [history](performance/history/) and [failures](performance/failures/). The quiet-host study **18111** expired after its fixed admission budget with **zero native launches**; its inconclusive metadata and journal remain retained. No earlier failure is relabeled as passing.

[Publication bundles](publication/), [kernel and mutant logs](kernel/), [implementation acceptance](acceptance/) and [doctrine evidence](provenance/doctrine/) supply the remaining clause-specific proof. Doctrine provenance retains all 27 typed records, the final decision receipt, obligations, validated citations and the packet-consumption trace.

`AUDIT-MANIFEST.json` maps original local and remote absolute locators to retained paths and SHA256 values. The [Archon index](performance/archon/archon-evidence-index.json) preserves its 2,540 original-source entries unchanged. Separate reviewed transport omitted only task HOME/download trees: 5,330 files and 406,121,715 bytes, with an explicit [omission receipt](performance/archon/transport-omissions.json). Required native work, grading records, boards, baseline cache, ledger, build logs and resource proof remain. Whole environments are omitted; two hash-bound extensions are retained for audit, never relocated for execution. Raw cross-project resource coordination is excluded.

Verify using only Python and this retained directory:

```sh
python3 provenance/stage-audit-evidence.py --verify .
```

[Closure comments](tracker/closure-comments-final.json) are prepared from the verified evidence. Tracker updates follow the report commit and push; their read-back receipt is retained separately when performed. Verification tools themselves made no tracker changes.
