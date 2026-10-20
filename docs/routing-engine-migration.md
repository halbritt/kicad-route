# Routing engine ownership

The owner authorized moving the supported KRT routing implementation into
`kicad-route` on 2026-10-20. This supersedes the external-backend acquisition
policy for KRT. Freerouting remains an external backend.

The migration baseline is the owner's fork at
`0494a05e30063b849c9cf1b9ac02098073d82216`, tree
`288b2baca668892c812c82f0e7db4c2b2b0a50c1`, derived from upstream v0.23.1
(`295f9ee197517e8323726853361eb137cc08ba9e`). It includes the tap-point grid
fix (KICADROUTE-64) and forbidden-layer weld fix (KICADROUTE-63). The comparison
wrapper is kicad-route `50af32245b4c3e3440de5757bff6222b351fb52b`.

## Source and build

The maintained implementation lives in `src/kicad_route/router/`, including its
Rust crate, locked crate dependencies, numerical-package requirements, MIT
license and `UPSTREAM.json`. The historical backend spelling `--backend krt`
is retained. It selects this owned implementation; no runtime source selector,
upstream checkout or prebuilt-router download is used.

`scripts/routing_closure.py` inventories static imports, including package
initializers and literal dynamic imports, from the two routing entry points and
the diagnostic tools used by the existing agent skills. Its frozen output is
`reports/routing-engine/source-closure.json`. Optional branches count, so this is
a conservative import inventory rather than proof of the runtime closure.
Nonliteral imports and sibling-script references are recorded for review.
Shared diagnostic and placement helpers account for code outside `py_router`.
They are initially preserved to keep source extraction separate from cleanup.

`kicad-route update` prepares a complete generation under
`STATE/.router-generations/<unique-id>/`: a frozen copy of the owned routing source,
a fresh `venv`, and Cargo output in `build`. It installs dependencies and prepares
site-packages bytecode inside the unselected generation, so read-only routing
can read cached numerical imports without writing runtime caches. The checked
compilation ignores an external `PYTHONPYCACHEPREFIX`; a failure preserves the
previous selection. It compiles the native extension with
`cargo build --release --locked`, verifies extension import, startup checks and
route help, then collects the source/module/package manifest. Atomically
replacing `STATE/manifest.json` activates the generation recorded in
`krt.generation`. Overlapping updates hold `STATE/.router-update.lock` for the
whole update. Routing pins the source and environment paths when its CLI starts;
already running workers keep their old generation across activation.

Preparation failures leave the selected environment and manifest unchanged.
`preparation.json` and logs identify failed or interrupted preparations; retrying
always starts a fresh generation. A cold failure leaves no active manifest.
Validated prior generations remain available; to recover, stop updates and preserve
the current manifest. Replace its `krt` object with the one from a prior generation's
`manifest.json`, clear the `krt` determinism record, and atomically write the merged
manifest to `STATE/manifest.json` (`statemanifest.write` uses temporary-file rename).
Retain current facts for the other backend: older Freerouting jars may have been
removed. Later determinism records can be regenerated. Update
never removes a generation automatically, and corpus regressions continue to be
reported without rollback. Legacy checkout environments at `STATE/.venv-krt`
remain readable until a successful generation activation.

Routing does not build or modify that environment or the source tree. The source
tree can be mounted read-only. Cargo and the Python package manager need their
ordinary registry access when dependencies are uncached.

Wheels include the public fixture and JSON Schemas as package resources. Writable
state defaults to `${XDG_STATE_HOME:-~/.local/state}/kicad-route`, outside
site-packages. Campaign children invoke `python -m kicad_route` from the same
package. `smoke` explicitly requires a development checkout with its test suite;
run installed-package acceptance with
`KICAD_ROUTE_WHEEL_RUNTIME=/path/to/native/venv python3 -m pytest tests/test_installed_wheel.py`.
The existing `scripts/install.sh` checkout symlink installation remains supported.

The backend identity records a digest of the owned source tree (`source_sha256`),
the kicad-route repository commit when available, and the upstream
provenance. The state manifest records that source digest, the compiled extension
hash and Python packages. A historical external-backend manifest needs regeneration
with `kicad-route state-manifest` after the owned extension is installed.
The repository commit is informational; source drift follows the engine digest,
so unrelated commits and absent Git metadata do not invalidate the installed engine.

## Acceptance

The owner selected a maximum 10% runtime regression and 10% peak-memory regression
per representative board. Use at least three measured repetitions per implementation
under the same toolchain, numerical dependencies, board files and routing settings.
Record warmup separately. Compare median runtime and median maximum resident set
size, keeping raw samples. GNU time's maximum RSS measures the largest individual
process, including children; it is not aggregate process-tree memory.

Correctness and completion are strict gates: no new native KiCad violations,
preservation failures or lost connections versus the frozen patched baseline.
Complete fixtures must remain complete. Both single-ended and differential-pair
routing, scoped routing, protected copper, pours, blocked attempts, cancellation,
machine-readable results and read-only/no-network execution remain required.
Compare copper geometry independently of UUIDs and native file serialization.

The known-fix tests are retained under `src/kicad_route/router/tests/`; they include
native connectivity/DRC and repeatability checks. The main integration suite remains
the authority for the wrapper's result and preservation contracts. A passing source
inventory or one passing board does not complete migration acceptance.

## Qualification results

The four-board comparison ran one excluded warmup and three measured attempts per
implementation. All 32 attempts completed, passed native validation and preservation,
and produced identical copper geometry for their board. Ratios below compare the
owned implementation with the frozen patched fork; 1.10 is the maximum allowed.

| Board | Median full-CLI runtime ratio | Median maximum-RSS ratio |
| --- | ---: | ---: |
| Bundled route demo | 1.094 | 0.993 |
| Differential pair | 0.774 | 1.011 |
| Four-layer power plane | 1.082 | 0.998 |
| Multipoint tap | 1.046 | 1.007 |

`reports/routing-engine/benchmark-matrix.json` retains the individual measurements,
input and output hashes, and native-result locators. The host was shared and busy;
these measurements establish the selected gates on these boards, not a general
speedup or a guarantee for other designs. Host metadata and sandbox-mount corrections
made after the comparison have separate regression coverage; the qualified engine
source digest remained unchanged.

The benchmark inputs are retained under `reports/routing-engine/inputs/`; the
route demo is `fixtures/route_demo/`. To repeat a comparison, pass the frozen
wrapper and patched-fork state as `--reference-cli` and `--reference-state` to
`scripts/compare_router.py`, and this checkout's CLI and installed state as
`--candidate-cli` and `--candidate-state`. Set `--project` to the input directory
and `--output` to a new evidence directory. All four cases used `--arg=--budget
--arg=180`. Additional arguments were:

- Route demo: `--arg=--default-clearance --arg=0.4`.
- Differential pair: `--arg=--diff-pairs --arg=auto`.
- Four-layer plane: `--arg=--backend-arg=--layers --arg=--backend-arg=F.Cu
  --arg=--backend-arg=B.Cu`.
- Multipoint tap: no additional arguments.

Both known-fix suites pass against the frozen fork and the extracted source
(seven tests, including 32 subtests). The normal integration suite invokes the two
retained regression files too. A real empty-state installation created no
`vendor/krt`, compiled the included Rust source, passed startup checks and left the
source unchanged; its receipt is `reports/routing-engine/clean-install.json`.
A separate native route uses a state directory containing only the routing
environment and no external checkout.

Regression coverage spans all 490 current test cases. The long full invocation
collected 488 before two new regressions were added: 485 passed and three sandbox
cases failed because its cached mount list omitted the external Freerouting
symlink target. A fresh run of the complete sandbox module passed all 17 cases
after the mount-declaration fix. A fresh metadata run passed 14 cases (excluding
the two-route repeat test, already passed in the full invocation). The union
covers every current case; this is not a claim that the original full invocation
was green. The original failures and corrected reruns are retained together.

Installation on `main` passed after integration. The installed binary matches the
qualified artifact, the refreshed state manifest verifies, and offline doctor
passes all 24 checks. A real route using the independently compiled empty state
completes, passes native validation and preservation, and matches the frozen
baseline's copper without a KRT checkout.
`reports/routing-engine/main-installation.json` records this checkpoint against
code commit `1e96ce32ce17b1a1ec350d50001f4dec2f635c95`; the subsequent receipt commit
changes only these reports and notes. Local raw evidence lives under
`build/owned-engine-migration/`.
These checks qualify routing software and fixtures, not fabrication or physical
board acceptance.

## Subsequent comparison harness (engine-comparison/2)

KICADROUTE-81 adds a separate all-attempt correctness gate: warmups and measured
runs must supply native validation, preservation, input/copper identity, routing
outcome, open connections, pair completion and uncensored command evidence.
A COMPLETE reference must remain COMPLETE. At least three measured repetitions
and one separate warmup per implementation are required. Raw result locators,
CLI digest, tool identities and input file hashes are retained per sample.

The default `--mode migration-equivalence` also requires identical copper geometry
in every attempt. `--mode algorithm-change` allows changed copper only under the
same independent native-validation, preservation, pair and completion gates; it
does not relax rules or permit lost connections. Performance uses measured
samples only. Each board gates median wall time, legacy GNU-time individual-process
maximum RSS, and sampled concurrent process-tree summed RSS at a ratio of 1.10.
The latter sums resident pages of concurrent descendants, including backend
sessions, with a 50 ms polling wait (actual maximum sample intervals are recorded). It is a sampled lower bound on peak and can double-count
shared pages; it is not unique physical memory or an exact kernel high-water mark.
The raw legacy metric and this aggregate metric remain distinct in each report.
The completed historical migration receipt above keeps its original meaning.

The retained sanitized `reports/routing-engine/inputs/multipoint-tap` workload
covers multipoint routing and its tap rescue path; its files are source-pinned in
Git and each comparison records their snapshot hashes. Include it alongside the
route demo, differential-pair and four-layer-plane workloads when qualifying
engine changes. This bounded corpus does not establish dense-board performance.
