# 7. Glossary

**attempt** — one `kicad-route run`: a snapshot, a backend run and a grading, in
one run directory. Also the unit the ledger fingerprints.

**attempt key / fingerprint (ledger)** — a hash of the input fingerprint, the
backend name and the normalised settings; the ledger fingerprint adds the backend
build. `compare` shows it as `fp`.

**backend** — the autorouter that places copper: `builtin` or `freerouting`.
The built-in router is compiled from kicad-route's included source into STATE;
Freerouting is acquired separately. Every result records the executed build.

**barrier** — a small temporary keepout placed where an earlier run's new copper
collided, for `run --barrier-from RUN`. Removed before grading.

**baseline** — the untouched input, refilled and graded the same way as a
candidate, cached per input fingerprint. Findings in it are pre-existing and never
fail an attempt.

**blocked-by receipts** — for an incomplete run, the obstacles across the straight
and L-shaped corridors between each open connection's endpoints, ranked by
category (`keepout`, `input_copper`, `new_copper`, `footprint`, `board_edge`).
Heuristic; schema `kicad-route/blocked/2`.

**budget** — the wall-clock limit of a backend run (`--budget`, default 300 s).

**builtin** — the default backend, maintained in `src/kicad_route/router/`.
Reads and writes native `.kicad_pcb` files. Its source derives from KRT; it does not
load a separate KRT checkout. `krt` is a deprecated CLI alias that warns on stderr.
New result backend names are `builtin`; existing manifest, determinism and identity
keys retain `krt` for compatibility.

**campaign** — `kicad-route campaign`: round 0 over the scope, then hard-first
rounds (backlog first on the original input, the rest from that candidate), every
chain graded against the input, the best one chosen.

**canary** — the demo board with awkward net names (`Net-(BT1-+)`, `"q"`,
`/a b/c`, `-VIN(1)`, `GND'x'/"y"`) run through each backend's round trip.
`doctor` runs its cheap checks; `run --canary` refuses when it fails.

**candidate** — the routed, refilled board in `candidate/<project>/`. Exactly the
file KiCad graded (`candidate_board_sha256 == validated_board_sha256` on exit 0).

**chain** — a sequence of runs where each starts from the previous candidate
(`--dir <run>/candidate/<project>`). Graded end to end with
`validate LAST --against FIRST`.

**completion ratchet (`net_completion`)** — the check that every originally joined
set of terminal pads stays joined, including subsets of incomplete nets.

**copper signature** — sha256 over the candidate's tracks, arcs and vias, sorted,
without UUIDs. The way to compare candidates; file hashes differ on every save.

**corpus** — real boards with recorded baseline outcomes under `STATE/corpus/`.
`corpus check` re-routes them; `update` runs it and fails on a regression.

**declared floor** — `<board>.rules-floor.json`: the owner's statement of the
minimum each rule setting may have. See *rule floor*.

**delivered board** — the board as the backend handed it back, after lock restore
and keepout strip, before the refill (`delivered_board_sha256`).

**determinism** — `kicad-route determinism`: the same input routed repeatedly with
the ledger off; reproducible when the copper signature and outcome are identical.
Recorded in the state manifest per built-in router build.

**doctor** — readiness: versions, capabilities, manifest drift, canary, and what a
sandbox must provide (`--json --offline`).

**evidence** — the `evidence` block of a result: hashes of the input, reference
files, rules, floor, backend output, candidate and grading records, plus the
copper signature. Also the committed `reports/fixture/` smoke evidence.

**exit code** — 0 complete and valid, 1 process failure, 2 valid but incomplete,
3 fails validation, 4 refused before routing.

**fingerprint (input)** — a hash over every file in the reference snapshot.
Identifies the input in the ledger, in `compare` and in chains.

**high-water mark** — for each rule setting, the strictest value ever committed
for the board and the commit that set it. Computed from git history by `floor`
and `audit`.

**human / agent copper** — in a `--protect human` chain: copper in the origin
run's input (and anything untraceable) is human and locked; copper some step
delivered as new is agent copper and may be rerouted.

**KRT** — KiCadRoutingTools, the upstream project from which the built-in router
derives. Also the historical backend spelling; see *builtin*.

**ledger** — `OUT/ledger.jsonl`: one line per finished attempt. An exact repeat of
a failed attempt is refused (exit 4) unless `--force`.

**machine mode** — `--json`: one JSON record on stdout, progress on stderr.

**measurement** — a result that is evidence of what a board or setting can
achieve: the built-in router with a passing determinism record for its executed build
and no budget or stall stop in the current attempt. Freerouting
results are attempts, not measurements.

**OUT** — the output directory (`--out`, `$KICAD_ROUTE_OUT`, default
`./build/kicad-route`): runs, campaigns, refusals, ledger, baseline cache, hermetic
KiCad configuration.

**parity** — KiCad's schematic-parity check, run as part of every grading. A
grading that could not run it fails.

**pour integrity** — the preservation check that compares the refilled input's
pours with the candidate's: fill contact, zone reach, spoke counts, islands, area.

**preservation checks** — sixteen comparisons of reference and candidate board
facts that prove the backend changed nothing it must not.

**process group** — each backend runs as the leader of its own session and
process group, so a budget or stall kill takes every child with it.

**protect mode** — `--protect existing | locked | human`: which input copper the
backend may not touch.

**provenance** — how `--protect human` derives human and agent copper from the
chain of run directories; recorded in `provenance.json`.

**reference** — `reference/<project>/`: the untouched snapshot of the input.

**refusal** — exit 4 before any backend ran, with a record under
`OUT/refusals/` (`reason_class`: `usage`, `input`, `ledger`, `canary`, `state`).

**run directory** — `OUT/runs/<project>/<label>/<stamp>/` or `--run-dir PATH`:
reference, work, candidate, facts, validate, attempt.json, result.json, RESULT.md.

**rule floor** — the high-water mark plus the declared floor. `floor` reports
settings below it, `audit` reports what a loosened rule hides, and `run` fails
validation for an input below its declared floor. Nothing in it relaxes a rule.

**scope** — the nets an attempt is asked to route (`--nets`, `--netclass`;
default all). New copper outside it fails `routing_scope`.

**Specctra bridge** — KiCad's DSN export and SES import, used for Freerouting
(`src/kicad_route/kicad/specctra.py`). Out-of-scope nets go to `__skip` classes;
pre-existing copper is locked for the round trip; the placement section is dropped.

**spoke corridor** — the rectangle beyond a thermal-relief spoke that must stay
free of other copper for the spoke to survive. `--reserve-spokes` reserves them;
`kicad-route spokes` attributes starved pads to the tracks that crossed them.

**stall** — `--stall S`: stop a backend whose log shows no progress for S seconds.

**STATE** — the state directory (`$KICAD_ROUTE_STATE`, default the checkout for
source installs or `${XDG_STATE_HOME:-~/.local/state}/kicad-route` for wheels):
`vendor/freerouting`, `.router-generations`, legacy `.venv-krt`, `corpus/`,
`manifest.json`. The owned routing
source is included in the kicad-route package.
Routing reads this state. Writers include `update`, `corpus add`, `state-manifest`
and `determinism --record`.

**state manifest** — `STATE/manifest.json`: what is installed, with hashes.
`doctor` reports drift from it; every result records its sha256.

**tightening** — options that only make routing stricter (`--default-clearance`,
`--clearance-margin`, `--pour-*`). Always allowed. Relaxing never is.

**tool identity** — the `tool_identity` block of a result: kicad-route, kicad-cli,
pcbnew, Python, the backend build and the state manifest hash. No timestamps, so
two runs on the same tools report the same block.

**unsupported** — constraints the backend could not see or approximated. KiCad
grades them regardless.

**validated board** — the board after refill and save, the file DRC read
(`validated_board_sha256`).

**work copy** — `work/input/<project>/`: what the backend sees, with locks and
temporary keepouts applied.
