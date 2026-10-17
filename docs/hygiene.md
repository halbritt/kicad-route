# Run hygiene: stall detection, run ledger, canary, corpus gate

Four guards around routing attempts. None of them touches the grading: every
candidate is still graded by KiCad against the input's unchanged rules
(docs/routing.md). They only decide when to stop a backend, when not to start
one, and how to tell whether a backend update made real boards worse.

| Guard | Command / flag | Code |
|---|---|---|
| Stall detection | `run --stall SECONDS` | `src/kicad_route/stall.py` |
| Run ledger | always on; `run --force`; `compare` shows `fp` | `src/kicad_route/ledger.py` |
| Canary pre-flight | `kicad-route canary`, `run --canary`, part of `doctor` | `src/kicad_route/canary.py`, `kicad/rename_nets.py` |
| Real-board gate | `kicad-route corpus add/check/list`; run by `update` | `src/kicad_route/corpus.py` |

Exit status 4 means **refused before routing**: the ledger shows this exact
attempt already failed, or the `--canary` pre-flight failed. No run directory
is left behind.

## Stall detection

`run_budgeted` used to watch only the wall clock. Now a `stall.Monitor` tails
the backend log while it runs and records a heartbeat whenever the backend's
progress state moves forward. The heartbeats go to `work/heartbeat.jsonl`, one
JSON object per line: `t_s`, `utc`, `phase`, `metric`, `line`.

What counts as progress:

- **KRT** (py_router console output):
  - phase banners (`=====` / title / `=====`), `Routing N single-ended net(s)...`
  - per-net `[i/N (k failed)] Routing NET`, `SUCCESS:` / `FAILED:` counts
  - `Per-net fine-parameter rescue`, `Rescuing NET (..., N disconnected parts)`
    and its `improved (a -> b parts)` result
  - plane repair (`[NET] on LAYER`), `Plane finalize`, KiCad-oracle rounds,
    `Final reconciliation`, `JSON_SUMMARY`, `JSON_REGRADE`,
    `JSON_IMPROVEMENT_GATE`, `Writing output to`, `CHECKPOINT STOP`
  - Search chatter (`Probe: ...`, `No route found after N iterations`) is
    **not** progress. A search that never ends is the stall this catches.
- **Freerouting**:
  - `<stage> stage started|completed`, the fanout passes, `Loading board file`,
    `Saving output file`
  - an `Auto-routing pass #N ... score S (U unrouted ...)` line counts only
    when it beats the best result seen in the current stage (fewer unrouted,
    then higher score). Passes that repeat or worsen the score are not
    progress.

Observation is always on, so `result.json` records `process.last_phase` and
`process.heartbeats` (count, log path, last five records) even for a budget
kill. For KRT, `PYTHONUNBUFFERED=1` is set so the log is written in time. It
changes only output buffering.

With `--stall S` (default off; without it the budget path is exactly as
before), when there has been no progress for S seconds:

1. The stop file is touched. This is KRT's `KICAD_STOP_FILE`; Freerouting has
   none.
2. After a grace period of `min(budget grace, max(15, S))` seconds, the
   backend is terminated, then killed.
3. `process.reasons` says `stalled in <phase> at <metric> for N s` instead of
   a bare budget kill. `process.stalled` is true. The process record of the
   command (`process.commands[0]`) holds `stall` (phase, metric and the
   seconds without progress at the moment of the stall), `last_phase` and
   `heartbeats`.

If KRT honours the stop file, it writes the board as it stood. That candidate
is graded like any other, and the reason says it stalled and stopped early.

Choose S above the longest *silent* leg of the board. KRT's KiCad refills and
oracle rechecks print nothing while they run. On the 50 x 30 mm fixture that
leg takes about 13 s; it grows with the board and its pours. `--stall` is a way
to give up on a hopeless search sooner than `--budget`. It is not a substitute
for the budget.

## Run ledger

Every finished attempt appends one line to `OUT/ledger.jsonl`. Each line holds:

- `fingerprint`, `attempt_key`
- `run_dir`, `backend`, `backend_version`, `input_fingerprint`, `project`
- `outcome`: `pass`, `process_fail`, `validation_fail` or `incomplete`
- `exit_code`, `unconnected_in_scope`, `forced`, `upstream_changed_since`
- `finished_utc`

The two keys:

- **attempt key**: a hash of the input fingerprint, the backend name and the
  normalized settings.
  - The settings cover the scope patterns and classes, budget, protection,
    the tightening controls, backend args and env, and `--stall`.
  - `None` values are dropped, so a new optional setting left unset keeps old
    keys valid.
  - Net patterns and classes are order-free. Backend args are not.
- **fingerprint**: a hash of the attempt key and the backend version identity
  (version, commit, Rust router, jar sha256). `result.json` carries it in
  `ledger.fingerprint` / `ledger.short`.

`run` computes both before the baseline DRC.

- **Refused (exit 4):** an earlier attempt with the same fingerprint finished
  `process_fail`, `validation_fail` or `incomplete`. The message names that
  run directory. Deterministic backends fail the same way twice, so change
  the input, scope or settings.
- **`--force`:** runs it anyway. `result.json` and the ledger record
  `forced: true` and `forced_over: <run dir>`.
- **Allowed, with a note:** the same attempt key under a different backend
  version: `upstream changed since <run dir> (krt 0.23.1 -> 0.24.0)`.
- An earlier identical attempt that passed is not refused.

The fingerprint can be derived from `result.json` alone
(`ledger.from_result`). `compare` uses this to print it as the first column
(`fp`, 12 hex characters), also for runs older than the ledger.

`KICAD_ROUTE_LEDGER=off` keeps recording every attempt but never refuses one (a note
in the gate names the run it repeats). The test suite sets it, because test modules
share one output directory and deliberately repeat each other's attempts; the ledger
tests switch it back on. The declared rule floor (`<board>.rules-floor.json`) is part
of the fingerprint (`settings.rule_floor_sha256`): it is not a KiCad project file, but
it changes the verdict.

## Canary pre-flight

The canary works on the bundled fixture.

- **Renamed nets.** `kicad/rename_nets.py` uses KiCad's own loader to give
  the fixture's nets awkward names:
  - `/SIG_A` becomes `Net-(BT1-+)`
  - `/SIG_A_OUT` becomes `"q"`
  - `/SIG_B` becomes `/a b/c`
  - `/SIG_B_OUT` becomes `+3V3/ü µ`
  - `/VIN` becomes `-VIN(1)`
  - `GND` becomes `GND 'x' (y)`
- **Net classes kept.** `.kicad_pro` netclass patterns are rewritten so the
  two Power nets stay in Power.
- **Scope.** `Net-(BT1-+)` and `"q"` are scoped out, so the Specctra class
  split runs on quoted, parenthesised names.

| Check | What | Cost |
|---|---|---|
| `split` | `specctra.split_classes` on a synthetic KiCad-style network (Default and Power classes, three scoped-out names) | ms, no KiCad |
| `bridge` | KiCad's DSN export of the renamed board must parse. Then the class split, in process and through `specctra.py --skip-nets`: every net stays in exactly one class, scoped-out nets are in the `__skip` copies with identical rules, the ignored-class list matches | ~2 s |
| `krt`, `freerouting` | a short run of the adapter itself (`krt_route` / `freerouting_route`). Then a facts compare of output against input: nets, net classes and pad-to-net assignments are equal; new copper is only on in-scope nets; every in-scope net that needed a track got one | ~20 s each |

- `doctor` runs `split` and `bridge`, so the parenthesis class of bug shows up
  in seconds.
- `kicad-route canary [--backend krt|freerouting|all]` runs everything.
- `run --canary` runs it for the run's backend first, and refuses (exit 4) if
  it fails.

Results go to `OUT/canary/<stamp>/canary.json` (`OUT/doctor/canary/` for
doctor).

The split check uses its own quote-aware parser rather than `find_open`. With
`find_open` reverted to a plain `text.find("(")`, it fails
(`tests/test_hygiene.py`).

Bugs the canary found while it was written (fixed on this branch):

- **KRT, leading `-`.** A scoped net whose name starts with `-` (`-12V`,
  `-VIN(1)`) reached KRT's argparse as an option, and KRT exited 2.
- **KRT, glob characters and leaf matching.** `*`, `?` and `[` in names were
  globs. An unqualified scope name such as `GND` also selected every
  `/sheet/GND` through KRT's leaf rule.
  - Fix: `netnames.krt_patterns` passes exact, argparse-safe patterns: `[-]`,
    `[*]`, `[[]`, `\!`, and exclusions for leaf collisions. Ordinary names are
    passed unchanged.
- **Freerouting, literal `"` in names.** KiCad's DSN exporter writes a
  scoped-out net whose name contains `"` raw in class lists (`"q"`) but as
  `''q''` in its `(net ...)` definition. `split_classes` therefore left it in
  the routable class, and Freerouting routed it outside the scope. The
  `routing_scope` check failed the candidate.
  - Fix: `split_classes` matches both spellings and writes a moved member in
    the definition's spelling (`specctra.dsn_net_token`).
  - Still an upstream KiCad defect: a name such as `x"y` makes KiCad write an
    unbalanced class list. The bridge does not repair that.

## Real-board update gate (corpus)

```sh
kicad-route corpus add --dir ~/git/board --rev HEAD --name board-can \
    --backend krt --nets '/CAN*' --default-clearance 0.2 --budget 600
kicad-route corpus check [--name board-can]
kicad-route corpus list
```

`add` takes a snapshot of the project with `snapshot_input`: sheets, rules,
library tables and local libraries. The snapshot goes into
`STATE/corpus/<name>/reference/`. A directory mapped with `--library-dir NAME=PATH`
stays a link to PATH there. The attempt in `entry.json` records the mappings with
the other run options, and every `check` passes them again, so the check's snapshot
links the same library (an entry added before `library_dir` was recorded has no
mappings: re-add it). `add` then runs the attempt once and records its baseline in
`entry.json`:

- exit code, process / routing / validation status
- unconnected items in scope, new DRC violations, new parity issues
- failing preservation checks, backend version, fingerprint, run directory

The corpus location is `STATE/corpus/`, git-ignored. Override it with `--root`
or `$KICAD_ROUTE_CORPUS`. To re-add an entry, remove its directory first.

`check` routes every entry again with the installed backends. The runs go to
`OUT/runs/<name>/corpus-<name>/`. These are deliberate repeats, so they pass
the ledger as forced. A **regression** is any of:

- process FAIL
- more unconnected items in scope
- more new DRC violations
- more new parity issues
- a preservation check that now fails

`check` also lists improvements. Each check goes to
`STATE/corpus/<name>/checks/<stamp>.json`, and the summary to
`OUT/corpus/check-<stamp>.json`. Exit status is 1 on any regression.

`update` runs `corpus check` after smoke when the corpus is not empty:

- The outcome goes into `update.json` under `corpus` (status, regressions,
  report path).
- On any regression, `update` exits non-zero.
- It does not roll back: the previous backend is gone by then, and whether to
  pin, report upstream or adapt is a person's decision.
