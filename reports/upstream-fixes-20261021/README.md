Six selected upstream KRT fix families are merged and locally deployed in
`builtin`. The integrated source commit is `f42b8796de2006bae147e65340da99462b5fd9be`;
the owned engine SHA-256 is
`89a2efbf53057a0694b3cef365ec3acdc01b61f5f57d86bfbe8a62dc8f11db6d`.
`--backend krt` remains a deprecated alias for this same implementation.

| Plane item | Repair |
| --- | --- |
| KICADROUTE-83 | Preserve resolved class, edge and same-net pad floors across subpasses; classify slanted outlines correctly; read and swap legacy bare pad/net tokens. |
| KICADROUTE-84 | Reject forbidden layer destinations, carry the no-stub-swap option, and apply existing via-fit/refusal rules at rescue sites. |
| KICADROUTE-85 | Include pair-member offset in native rule-area and user-polygon keepout maps. |
| KICADROUTE-86 | Rebuild retry maps after copper changes; remove temporary tap state; reverse stub changes on failure or cancellation. |
| KICADROUTE-87 | Carry rescued copper into the final write list while preserving restoration, supersession and later-rip custody. |
| KICADROUTE-88 | Release board-text caches at parse exit, including nested and concurrent parses; share repeated scans within a parse. |

[UPSTREAM.json](../../src/kicad_route/router/UPSTREAM.json) records the selected
commits. Extraction version 0.23.1, Rust and dependencies are unchanged; use the
source/build identity to distinguish this deployment. Review head `0b3b6a2d`
is selection provenance, rather than a wholesale upstream replacement.

All 816 collected tests and six subtests passed with zero failures, errors or
skips. Two strict native repeats passed on Archon. Independent verification
reconstructed the exact test population, runtime identity, grading records,
board hashes and repeat comparison. Source commit `f42b879` differs from benchmark
commit `4fb4fa1` only in two qualification test files; all shipping bytes match.

The original full run (797 passes, 12 failures, six setup errors) is retained.
Environment corrections supplied Docker `/proc` access, KiCad footprints and
wheel build tools. KICADROUTE-89 fixes two assertions that still expected the old
canonical name and replaces a hardware-dependent hard-kill assumption with
explicit unresponsive-worker and cooperative-checkpoint cases. Those cases use
the real watchdog and native grader; all 814 other test nodes are preserved.
A subsequent 815-pass/one-skip run remains non-accepting evidence. The final
invocation enabled `KICAD_ROUTE_WHEEL_RUNTIME` and executed that wheel test.

Archon completed one declared four-board benchmark: five measured attempts and
one separate warmup per side, 48 attempts total. Every attempt completed with
native validation and preservation passing, no new violations and no censoring.
The expected `/CAN+`–`/CAN-` pair was present in all 12 differential-pair attempts;
the other 36 attempts had no configured pairs. Copper geometry matched the
reference on all four boards. Algorithm-change mode permits corrected copper
to differ while enforcing the other gates.

| Board | Wall median ratio | GNU RSS median ratio | Sampled process-tree RSS median ratio |
| --- | ---: | ---: | ---: |
| Differential pair | 0.972399 | 0.999421 | 0.997664 |
| Four-layer plane | 1.021978 | 1.001103 | 1.004183 |
| Multipoint tap | 1.003390 | 0.998020 | 0.994117 |
| Route demo | 1.000000 | 1.001444 | 1.002628 |

Each limit remains 1.10. GNU Time reports the largest individual-process RSS;
sampled concurrent RSS is a lower bound on the process-tree peak. Three explicit
process-exit observation races are retained. These measurements describe the
four declared workloads on Archon.

The fresh local generation `84b30dc286f44df6b465ae8d40aa1363` passed offline
doctor with canaries and two recorded native repeats. Independent installed-main
verification confirmed source, module, exact package versions, current generation,
strict worker arguments, native grading and manifest drift `[]`. Regenerated
`krt-*` diagnostic skills point to this generation. The first local preparation
failed on an empty offline dependency cache before activation; its failure is
retained. The normal retry used the same exact four package versions.

Focused proof has explicit limits. Synthetic via-fit boards check hole spacing
and retain unrelated findings. User-layer keepouts use a geometry oracle;
paired routes and additional ordinary single-ended routes exercise both area
kinds, with native rule-area checks. Kernel/parity/writer checks passed with
unchanged tested Rust/parser/writer inputs before the final retry-rollback
follow-up. UTF-8 legacy token swaps match native readback. The static import
inventory is conservative and does not claim whole-engine runtime coverage.

The parser diagnostic retains all 60 attempts: a 512 KiB ignored property left
635,064 bytes after baseline parsing and 68,932 bytes with parse-local caches.
Unscoped extractor CPU medians increased about 2.5 times; local wall measurements
were load-confounded. No universal speedup is claimed.

`focused-evidence.tar.gz`, `archon-evidence.tar.gz`, `followup-evidence.tar.gz`
and `deployment-evidence.tar.gz` retain the focused, benchmark and release proof,
with external indexes. Installer download-cache omissions are enumerated in
`deployment-cache-omissions.json`; native/source/grading artifacts are preserved.
The lossless four-profile test CAS retains 7,961 blobs and 53,356 original-locator
bindings, representing 2.49 GiB before deduplication. It includes failed,
corrective, one-skip and passing studies. Reconstruct its verified archive with:

```sh
cat test-evidence.tar.gz.part* > test-evidence.tar.gz
tar -xzf test-evidence.tar.gz
```

`test-evidence-archive.json` records part and stream hashes. Resolve raw absolute
locators through the matching profile in `test-evidence-cas/test-evidence-index.json`;
identical `/test-evidence` aliases from separate runs intentionally stay separate.
[verification.json](verification.json) is the technical receipt;
[cleanup.json](cleanup.json) records scoped resource removal. All four owned
worktrees/branches, five Archon containers, three task image tags and the exclusive
remote directory are removed. Unrelated resources and rollback C6 are preserved.
