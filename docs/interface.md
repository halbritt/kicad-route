# kicad-route machine interface

This page is the contract for programs that call kicad-route, such as the agent-ee
generator's routing stage. It says which records kicad-route writes, what each exit
code promises, which fields a caller may rely on, and how the contract changes.
People reading a result can use [routing.md](routing.md) and the
[skill](../skills/kicad-route/SKILL.md); the meaning of the results is the same.

JSON Schemas (draft 2020-12):

- [`schemas/result-1.json`](../schemas/result-1.json): `route` and `route-validate`
  records without pair evidence, and `route-campaign` records.
- [`schemas/result-2.json`](../schemas/result-2.json): opt-in pair `route` and
  `route-validate` records.
- [`schemas/refusal-1.json`](../schemas/refusal-1.json): the refusal record (exit 4).

KRT's opt-in `run --diff-pairs auto` adds `pairs` to route and re-validation
records, using `kicad-route/route/2` and `kicad-route/route-validate/2` respectively.
Names, native class width/gap settings, native planar lengths and skew,
gap findings, and explicit failure statuses are documented in
[`diff-pairs.md`](diff-pairs.md). Failed pairs keep routing INCOMPLETE even if a
backend's internal single-ended fallback claimed connectivity; exit precedence
remains process 1, validation 3, incomplete 2. Calls without the option omit the
array and retain their existing routing path and `/1` result schema. Opt-in records
use `/2` even when the detected pair list is empty or the backend fails.

`tests/test_interface_contract.py` validates every committed fixture result against
them (`reports/fixture/runs/*/result.json` and `*/against-origin/result.json`) and
checks the exit-code promises below. Validating a record yourself:

```python
import json, jsonschema
record = json.load(open(run_dir + "/result.json"))
major = record["schema"].rsplit("/", 1)[1]
schema = json.load(open(f"schemas/result-{major}.json"))
jsonschema.Draft202012Validator(schema).validate(record)
```

## Calling kicad-route from a program

- **Machine mode.** Pass `--json` to `run`, `validate`, `campaign`, `blocked`, `audit`,
  `floor`, `doctor`, `decoupling` or `barrier`. stdout then carries exactly one JSON
  object on one line: the command's record (the same content as the file it wrote) or
  a refusal record. All progress lines, and the output of every child process, go to
  stderr. Do not parse the human lines; they can change in any release. `--json` on
  another command is a usage refusal.
- **Options anywhere.** `--out DIR` and `--json` are accepted before or after the
  subcommand (`kicad-route --out D run ...` and `kicad-route run ... --out D` are the
  same). When `--out` is given twice, the last one wins.
- **No abbreviations.** Options must be spelled in full (`--budget`, not `--budg`), so
  a new option in a later release cannot change what an existing command line means.
- **A run directory you choose.** `run --run-dir PATH` routes into `PATH`, which must
  not exist (it is created, with its parents). No `latest` link is made, so concurrent
  runs with distinct `--run-dir` values do not interfere. Without it the run directory is
  `OUT/runs/<project>/<label>/<stamp>/` and `OUT/runs/<project>/<label>/latest` points
  at the newest one (replaced atomically, but shared by every run with that label).
- **Campaign steps.** `campaign` runs each step as `kicad-route run --json` (and
  `validate --json`): the step's record goes to `logs/<step>.json`, its progress to
  `logs/<step>.log`, and a step past its timeout is stopped with its process group.
- **Never interactive.** No command prompts or reads stdin, and none checks for a
  terminal. A future change that needs a confirmation adds a flag instead.
- **Network.** Only `update` (backend downloads) and the `doctor` latest-release checks
  use the network; `doctor --offline` skips those checks and opens no socket. Routing
  attempts run offline: Freerouting's update check goes to a dead proxy (routing.md,
  "What the adapters switch off").
- **Sandboxed callers.** `doctor --json --offline` lists in its `sandbox` block the
  read-only mounts, the writable OUT, the environment and the network a sandbox must
  provide; `--require-readonly-state` refuses (`state`) unless STATE is read-only and
  matches its manifest; `result.json` `sandbox` records what the run saw. Details and
  the agent-ee launch: [sandbox.md](sandbox.md).
- **The ledger in a sandbox.** The run ledger (exit 4 on an identical failed attempt)
  is `OUT/ledger.jsonl` unless `run`/`campaign`/`corpus check` get `--ledger PATH` (an
  environment option, like `--require-readonly-state`: never recorded in a corpus entry). A caller that
  gives every stage a fresh OUT chooses one of: pass a persistent, writable
  `--ledger PATH` (recorded as `ledger.path`; repeats stay refused across stages), or
  accept that repeats are not refused inside the sandbox and keep that accounting
  itself. kicad-route supports both; agent-ee records its choice.
- **Paths in records** are relative to the caller's working directory when they lie
  inside it, else absolute (`common.rel`). Resolve them against the directory you ran
  kicad-route from.

## Records and schema ids

Command records carry `"schema": "kicad-route/<kind>/<major>"` and `"kind"`.
`attempt.json` carries its own schema id and retains its existing shape without a
`kind` field.

| `kind` | `schema` | Written to | Printed by `--json` of | JSON Schema |
|---|---|---|---|---|
| `route` | `kicad-route/route/1` | `<run_dir>/result.json` | `run` | result-1.json |
| `route` with pairs | `kicad-route/route/2` | `<run_dir>/result.json` | `run --diff-pairs auto` | result-2.json |
| `route-validate` | `kicad-route/route-validate/1` | `<run_dir>/revalidate*/<stamp>/result.json` | `validate` | result-1.json |
| `route-validate` with pairs | `kicad-route/route-validate/2` | `<run_dir>/revalidate*/<stamp>/result.json` | `validate` | result-2.json |
| attempt without pairs | `kicad-route/attempt/1` | `<run_dir>/attempt.json` | (file only) | key sets below |
| attempt with pairs | `kicad-route/attempt/2` | `<run_dir>/attempt.json` | (file only) | key sets below |
| `route-campaign` | `kicad-route/route-campaign/1` | `OUT/campaigns/<project>/<label>/<stamp>/campaign.json` | `campaign` | result-1.json |
| `refusal` | `kicad-route/refusal/1` | `OUT/refusals/<stamp>-<pid>.json` | any command | refusal-1.json |
| `route-blocked` | `kicad-route/route-blocked/1` | (stdout only; the receipts are `blocked-by.json`) | `blocked RUN_DIR` | none yet |
| blocked-by report | `kicad-route/blocked/2` | `OUT/blocked/<board>/<stamp>/blocked-by.json`, `<validate dir>/blocked-by.json` | `blocked --board B` | `schemas/blocked-by.schema.json` (docs/blocked.md) |
| `audit` | `kicad-route/audit/1` | `OUT/audit/<project>/<old>..<new>/audit.json` | `audit` | none yet |
| `floor` | `kicad-route/floor/1` | `OUT/floor/<project>/floor.json` | `floor` | none yet |
| `doctor` | `kicad-route/doctor/1` | `OUT/doctor/doctor.json` | `doctor` | none yet |
| `decoupling` | `kicad-route/decoupling/1` | `OUT/decoupling/<board>/decoupling.json` | `decoupling --board B` | none yet |
| `decoupling-run` | `kicad-route/decoupling-run/1` | `<run_dir>/decoupling.json` | `decoupling RUN_DIR` | none yet |
| `barrier-preview` | `kicad-route/barrier-preview/1` | `<run_dir>/barrier-preview/barrier-plan.json` | `barrier RUN_DIR` | none yet |

For command kinds without a JSON Schema only `schema`, `kind` and `exit_code` are
stable for now. The attempt key sets are specified below.

## Attempt and result key sets

For ordinary completed attempts, `attempt/1` has exactly these top-level keys:
`schema`, `backend`, `project`, `board`, `schematic`, `input_fingerprint`, `source`,
`reference_dir`, `candidate_dir`, `reference_facts`, `rules_files`, `baseline`,
`scope`, `settings`, `floor`. These are the previously unversioned keys plus
`schema: "kicad-route/attempt/1"`; their meanings are unchanged.

`attempt/2` has those same keys plus `pairs` and `pair_routing`, with
`schema: "kicad-route/attempt/2"`. `pairs` is the native pair plan, optionally
annotated with `initially_connected`; `pair_routing` maps positive net names to
backend outcomes (`status` and optional `report`). The schema id is written with
the initial plan; `pair_routing` is added when the backend returns, before the
result is written. Both versions use the same existing settings fields; `/2`
also requires `settings.diff_pairs: "auto"` and `settings.pairs` (the deterministic
plan without the baseline annotation). `--protect human` may add its existing
provenance fields inside `settings` in either version. `validate` still reads
historical attempts without a schema id.

The emitted `route/1` top-level key set is unchanged: `schema`, `kind`, `backend`,
`measurement`, `input`, `settings`, `scope`, `unsupported`, `floor`, `process`,
`routing`, `validation`, `candidate`, `run_dir`, `runtime_s`, `tool_versions`,
`evidence`, `tool_identity`, `finished_utc`, `exit_code`, `ledger`, `sandbox`.
`route/2` adds only `pairs` at the top level and `diff_pairs`/`pairs` in `settings`.
`route-validate/1` has `schema`, `kind`, `run_dir`, `against`, `dir`, `process`,
`routing`, `validation`, `evidence`, `tool_identity`, `exit_code`;
`route-validate/2` adds only `pairs`. Re-validation chooses `/2` whenever it emits
pair evidence, including evidence accumulated through `--against` chains.
The pair field definitions are in [diff-pairs.md](diff-pairs.md).

## Exit codes

Exit codes 0 to 3 of `run` and `validate` come from one function, `overall_exit`,
applied in this order: a process failure (1) wins over a validation failure (3), which
wins over incomplete routing (2).

| Exit | Meaning | What the record promises |
|---|---|---|
| 0 | complete and valid | `process.status` PASS, `validation.status` PASS, `routing.status` COMPLETE (0 unconnected in scope, no `unrouted_nets`), a `candidate`, and `validation.candidate_board_sha256` equals `validated_board_sha256` (the board in `candidate/` is the file DRC graded; `delivered_board_sha256` is the file before the refill and differs when `refill_modified_board` is true, as it is for Freerouting candidates) |
| 1 | the backend failed | `process.status` FAIL with `process.reasons`, `candidate` null, `routing.status` and `validation.status` NOT_RUN |
| 2 | valid but incomplete | `process.status` PASS, `validation.status` PASS, `routing.status` INCOMPLETE with `unrouted_nets` and `routing.blocked_by` |
| 3 | fails validation | `process.status` PASS, `validation.status` FAIL (whatever the routing) with non-empty `validation.reasons` |
| 4 | refused before any backend ran | no candidate and no run directory; a refusal record names the reason (below) |

Exit 1 from `run` always comes with a `result.json` whose `process.status` is FAIL. That
includes `run --barrier-from` a run with no copper collision to barrier
(docs/barrier.md): the backend never starts, and the result says why. An
input or usage problem is never exit 1 or 2: it is a refusal (exit 4). The one
exception is a kicad-route bug: an uncaught Python exception exits 1 with a traceback
on stderr and no record. Treat exit 1 without a parsable record as a tool failure, never
as a routing verdict.

`validate` follows the same table. Its record carries `process`: the run's own
process result, or FAIL when there is no candidate board to grade (a re-grade of a
run whose candidate was removed exits 1 with `validation.status` NOT_RUN and the
reason "no candidate board").

Other commands:

| Command | 0 | 1 | 3 | 4 |
|---|---|---|---|---|
| `campaign` | the best chain is complete and valid (2 when it is valid but incomplete) | no chain produced a gradable candidate | the best chain fails validation | refused: a usage or input error, or its round-0 run was refused |
| `audit` | no findings | a revision could not be graded | findings (rule changes, hidden findings, floor) | refused |
| `floor` | nothing below the declared floor | | below the declared floor, or a broken declared floor | refused (not a git repository, `--since` not a commit) |
| `blocked` | receipts written, or nothing is blocked | the diagnosis failed (`--board`: DRC or facts did not run, or the board changed while analysed) | | refused (not a run, not a board) |
| `determinism` | the repeats are reproducible | `--record` could not store the verdict | not reproducible | refused |
| `doctor` | every required check passes | a required check fails | | refused |
| `compare` | the runs share one input | the inputs differ | | refused (a run without `result.json`) |
| `decoupling` | the report was written | the analysis ran and failed (the pcbnew script failed, or the board changed while analysed); the record carries `error` | | refused (not a run, a run without its reference or candidate board, no such board, a board KiCad cannot load) |
| `barrier` | at least one barrier was injected into the preview copy | the analysis ran and failed (locating the collisions failed, or every barrier lies on existing copper); the record carries `reasons` | | refused (not a graded run, nothing to barrier, a candidate KiCad cannot load; `state` when the barriers cannot be injected into the preview copy) |

For `decoupling` and `barrier`, exit 1 always comes with its record (the file in the
table above, and the `--json` object); everything that stops them before the analysis
is a refusal.

## Refusals (exit 4)

Every failure before a backend runs takes one path: exit 4 and a refusal record. That
covers argparse usage errors (which would otherwise exit 2, "valid but incomplete"),
input errors, the ledger and the canary pre-flight.

| `reason_class` | Raised when |
|---|---|
| `usage` | the command line is wrong: an unknown option, a bad value, a missing argument, `--backend-env` without `=`, `--library-dir` without `=`, `campaign --rounds` below 0, `corpus add` without a valid `--name`, `blocked` or `decoupling` without (or with both) a run and `--board`, `decoupling --radius` not positive, `--json` on a command without machine mode |
| `input` | the project, run or revision named is not usable: `--dir` is not a directory, not exactly one board, `--rev` outside a git repository or not a commit, the board has no `.kicad_pro` of its own name (KiCad would grade with its defaults; `run` and `audit`), the input board cannot be read, the net selection matches no nets or would not mean what it says (`--nets '!RESET'` on a board with a net named `!RESET`), `validate`/`blocked`/`spokes`/`decoupling`/`barrier`/`compare`/`publish` given something that is not a run, `blocked --board` given something that is not a board, `decoupling` given a run without its reference or candidate board, `decoupling --board` given a missing board or one KiCad cannot load, `barrier` given a run with nothing to barrier (no graded candidate, no copper collision made by new copper, no locatable collision) or whose candidate KiCad cannot load, a `validate --against` chain that does not descend from the origin, `floor` outside a git repository, `run --backend freerouting` on a board whose net names hold `"` in a way the Specctra bridge cannot write one way (two nets KiCad would spell the same, or a plane, wire or class list that reads more than one way; docs/hygiene.md) |
| `ledger` | the run ledger has this exact attempt already failed (`--force` repeats it; docs/hygiene.md) |
| `canary` | `run --canary` and the awkward-net-name canary failed |
| `state` | kicad-route's own state is in the way: `--run-dir` exists, the corpus entry exists; with `--require-readonly-state` (or `KICAD_ROUTE_REQUIRE_READONLY_STATE=1`) the state directory is missing, holds no backend, is not the directory its `manifest.json` describes or has drifted from it, or is writable, and `update` / `corpus add` (which write it) refuse ([sandbox.md](sandbox.md)); `barrier` could not inject its keepouts into the preview copy of the run's reference (`RUN_DIR/barrier-preview/`) |

The record (`schemas/refusal-1.json`):

```json
{"schema": "kicad-route/refusal/1", "kind": "refusal", "exit_code": 4,
 "command": "run", "reason_class": "input", "message": "the net selection matches no nets",
 "argv": ["run", "--backend", "krt", "--nets", "NOPE*"], "cwd": "/work/board",
 "input_fingerprint": "3f5c…", "discarded_run_dir": "build/kicad-route/runs/board/krt/20261017T…Z",
 "refused_utc": "2026-10-17T21:52:53+00:00",
 "record": "build/kicad-route/refusals/20261017T215253.420179Z-638465.json"}
```

- It is written to `OUT/refusals/<stamp>-<pid>.json`, and
  `OUT/refusals/refusal-latest.json` links the newest one. With `--json` it is also the
  one object on stdout. If OUT cannot be written, `record` is null and the record is
  still printed.
- `input_fingerprint` is set once the input has been snapshotted (the ledger and canary
  refusals, a net selection that matches nothing), else null.
- A run directory created before the refusal is removed; `discarded_run_dir` names it.
  A refused campaign removes its campaign directory.
- Without `--json`, a usage refusal prints argparse's usage and error to stderr; the
  other classes print `[kicad-route] refused (<class>): <message>` to stdout.

## Stable and informational fields

Stable fields keep their name and meaning for the whole major version. Removing one or
changing its meaning needs `/2`.

`route` (`result.json`):

- `schema`, `kind`, `exit_code`
- `backend.name`, `version`, `build`, `commit`, `sha256`, `rust_router` (which of the
  last three are present depends on the backend)
- `input.project`, `board`, `fingerprint`, `source_git_head`
- `settings` keys: `backend`, `budget_s`, `protect`, `mode`, `max_iterations`, `passes`,
  `default_clearance_mm`, `clearance_margin_mm`, `pour_area_loss_pct`, `pour_min_spokes`,
  `backend_args`, `backend_env`, `reserve_spokes`, `stall_s`, `net_patterns`,
  `netclasses`, `rule_floor_sha256`
- `scope.mode`
- `unsupported` (a list of strings)
- `process.status`, `process.reasons`
- `routing.status`, `unconnected_in_scope`, `unrouted_nets`
- `validation.status`, `reasons`, `parity_performed`, `delivered_board_sha256`,
  `validated_board_sha256`, `candidate_board_sha256`, `refill_modified_board`,
  `preservation` (check name to status)
- `candidate.board` (`candidate` is null when there is none)
- `ledger.fingerprint`
- With `--diff-pairs auto`: `settings.diff_pairs`, the `settings.pairs` plan,
  and `pairs` entries' `p`, `n`, `netclass`, `width_mm`, `gap_mm`, `routed`,
  `length_p`, `length_n`, `skew`, `gap_violations`, `status`, `backend_status`.
  `backend_report` is an informational upstream diagnostic.

`route-validate`: `schema`, `kind`, `exit_code`, `run_dir`, `against`, `dir`, and the
same `routing`, `validation`, and opt-in `pairs` fields. `route-campaign`: `schema`, `kind`,
`exit_code`, `campaign_dir`, `input.fingerprint`, `best` (`chain`, `run_dir`,
`candidate`, `grade`, `exit_code`; null when no chain was graded). Refusal: every field
of the record above.

Fields whose shape depends on the input: `scope.nets_list` is null above 50 nets,
`candidate` is null on a process failure, `routing.blocked_by` exists only when
INCOMPLETE, and `routing` holds only `status` and `reasons` when NOT_RUN.

Informational (may change within `/1`): `process.backend_report`, `process.commands`
and every other process detail, `routing.stats`, the `routing.blocked_by` summary,
`validation.drc` detail, `runtime_s`, log paths, `tool_versions`, `floor`, and
`RESULT.md`. Planned additions to the stable list: `evidence` and `tool_identity`.

## Stable file names in a run directory

| Path | Contents |
|---|---|
| `result.json` | the `route` record |
| `attempt.json` | input identity, scope, settings (what `validate` re-reads) |
| `RESULT.md` | the same result for people (its wording is informational) |
| `candidate/<project>/<board>` | the routed, refilled board that was graded |
| `validate/preservation.json` | every preservation check with details |
| `validate/new-findings.json` | DRC findings new relative to the input |
| `validate/candidate-drc.json` | the native DRC report of the candidate |
| `validate/blocked-by.json` | blocked-by receipts (only when INCOMPLETE) |
| `revalidate/<stamp>/result.json`, `revalidate-origin/<stamp>/result.json` | `route-validate` records |

## Versioning and deprecation

- Informational additions, a record kind or a reason class keep the major version.
  The opt-in pair shape has its own `/2` ids so callers can bind exact key sets;
  calls without the option keep their `/1` result shape.
- Removing or redefining a stable field bumps the major (`kicad-route/route/2`, a new
  `schemas/result-2.json`). For at least one tagged release the old field is still
  written beside the new one and listed in a top-level `deprecated` array
  (`[{"field": "...", "replaced_by": "...", "since": "<tag>"}]`).
- This page keeps the history below.

### History

- **2, opt-in pairs** (2026-10-18): `route/2` and `route-validate/2` announce pair
  evidence; `schemas/result-2.json` requires the pair array and the route's pair
  settings. `schemas/result-1.json` is unchanged from e497916. Attempts now carry
  `attempt/1` or `attempt/2`: the default attempt gains only `schema`, an intentional
  one-time byte/key-set change from previously unversioned attempts; pair attempts
  additionally carry `pairs` and `pair_routing`. No routing or exit semantics change.

- **1** (2026-10-17): first versioned contract: `schema` ids on every record; refusal
  records with exit 4 for every pre-routing failure, usage errors included; `--json`;
  `--out` after the subcommand; `run --run-dir`; no option abbreviations.

## Planned planes

`planes --board B.kicad_pcb --plan PLAN.json --run-dir NEW_DIR [--json]`
emits `kicad-route/planes/1` (`schemas/planes-1.json`). It has a separate
production contract: exit 0 successful planned zones and preservation checks;
3 produced candidate failing validation; 1 missing native/sandbox evidence, no
published candidate; 4 existing refusal interface. `planes.pour_integrity` holds
refilled before/after facts, the check and delta. Production success does not
certify completed routing or schematic parity. See [planes.md](planes.md).

Publication bundles include `publication.json` (`kicad-route/publication/1`). The
legacy flat receipt layout is retained. `locators` maps original run-relative
receipt paths to bundle paths, and `files` hashes every included file. Each
published result's `evidence.records` has an included path/hash or an explicit
external designation in the manifest. Board, input, rule and backend-output
hashes remain external evidence; these private artifacts are never copied.
Original receipts are copied byte-for-byte. Consumers can call
`kicad_route.publication.verify(directory)` without access to the original run.

Publication accepts a fresh/empty directory or an intact bundle of the same kind
(`run` or `smoke`). Unowned nonempty destinations, changed bundles, symlinks,
project roots and paths overlapping active evidence are refused with exit 4.
Older publications without a manifest must be archived by their owner or given a
new destination. Successful publications replace the complete directory using
Linux `renameat2(RENAME_EXCHANGE)`; failed smoke runs preserve prior publications.

`backend.reproducible` describes the installed build's repeat evidence.
`measurement` qualifies this particular attempt: any command with
`budget_reached`, `stalled`, or `killed` makes it false, including intermediate
pair-routing passes. `process.measurement_qualification` records the build's
repeat flag and the current attempt's qualification reasons. A stopped candidate
still receives its independent process, routing and native-validation grades.
Revalidation retains the original attempt's qualification in its process block;
it does not rerun or upgrade that attempt. Freerouting remains an attempt.

`update.json` includes the required determinism outcome, reasons, record locator
and any `record_error`. Failed repeat qualification or recording makes update
nonzero; a later passing gate cannot erase an earlier failure. The manifest hash
in the final receipt includes the recorded repeat check.
