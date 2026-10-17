# kicad-route machine interface (contract version 1)

This page is the contract for programs that call kicad-route, such as the agent-ee
generator's routing stage. It says which records kicad-route writes, what each exit
code promises, which fields a caller may rely on, and how the contract changes.
People reading a result can use [routing.md](routing.md) and the
[skill](../skills/kicad-route/SKILL.md); the meaning of the results is the same.

JSON Schemas (draft 2020-12):

- [`schemas/result-1.json`](../schemas/result-1.json): the `route`, `route-validate` and
  `route-campaign` records.
- [`schemas/refusal-1.json`](../schemas/refusal-1.json): the refusal record (exit 4).

`tests/test_interface_contract.py` validates every committed fixture result against
them (`reports/fixture/runs/*/result.json` and `*/against-origin/result.json`) and
checks the exit-code promises below. Validating a record yourself:

```python
import json, jsonschema
schema = json.load(open("schemas/result-1.json"))
jsonschema.Draft202012Validator(schema).validate(json.load(open(run_dir + "/result.json")))
```

## Calling kicad-route from a program

- **Machine mode.** Pass `--json` to `run`, `validate`, `campaign`, `blocked`, `audit`,
  `floor` or `doctor`. stdout then carries exactly one JSON object on one line: the
  command's record (the same content as the file it wrote) or a refusal record. All
  progress lines, and the output of every child process, go to stderr. Do not parse the
  human lines; they can change in any release. `--json` on another command is a usage
  refusal.
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
  is `OUT/ledger.jsonl` unless `run`/`campaign` get `--ledger PATH`. A caller that
  gives every stage a fresh OUT chooses one of: pass a persistent, writable
  `--ledger PATH` (recorded as `ledger.path`; repeats stay refused across stages), or
  accept that repeats are not refused inside the sandbox and keep that accounting
  itself. kicad-route supports both; agent-ee records its choice.
- **Paths in records** are relative to the caller's working directory when they lie
  inside it, else absolute (`common.rel`). Resolve them against the directory you ran
  kicad-route from.

## Records and schema ids

Every top-level record carries `"schema": "kicad-route/<kind>/<major>"` and `"kind"`.

| `kind` | `schema` | Written to | Printed by `--json` of | JSON Schema |
|---|---|---|---|---|
| `route` | `kicad-route/route/1` | `<run_dir>/result.json` | `run` | result-1.json |
| `route-validate` | `kicad-route/route-validate/1` | `<run_dir>/revalidate*/<stamp>/result.json` | `validate` | result-1.json |
| `route-campaign` | `kicad-route/route-campaign/1` | `OUT/campaigns/<project>/<label>/<stamp>/campaign.json` | `campaign` | result-1.json |
| `refusal` | `kicad-route/refusal/1` | `OUT/refusals/<stamp>-<pid>.json` | any command | refusal-1.json |
| `route-blocked` | `kicad-route/route-blocked/1` | (stdout only; the receipts are `blocked-by.json`) | `blocked RUN_DIR` | none yet |
| blocked-by report | `kicad-route/blocked/2` | `OUT/blocked/<board>/<stamp>/blocked-by.json`, `<validate dir>/blocked-by.json` | `blocked --board B` | `schemas/blocked-by.schema.json` (docs/blocked.md) |
| `audit` | `kicad-route/audit/1` | `OUT/audit/<project>/<old>..<new>/audit.json` | `audit` | none yet |
| `floor` | `kicad-route/floor/1` | `OUT/floor/<project>/floor.json` | `floor` | none yet |
| `doctor` | `kicad-route/doctor/1` | `OUT/doctor/doctor.json` | `doctor` | none yet |

For the kinds without a JSON Schema only `schema`, `kind` and `exit_code` are stable
for now.

## Exit codes

Exit codes 0 to 3 of `run` and `validate` come from one function, `overall_exit`,
applied in this order: a process failure (1) wins over a validation failure (3), which
wins over incomplete routing (2).

| Exit | Meaning | What the record promises |
|---|---|---|
| 0 | complete and valid | `process.status` PASS, `validation.status` PASS, `routing.status` COMPLETE (0 unconnected in scope, no `unrouted_nets`), a `candidate`, and `validation.delivered_board_sha256` equals `validated_board_sha256` |
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

## Refusals (exit 4)

Every failure before a backend runs takes one path: exit 4 and a refusal record. That
covers argparse usage errors (which would otherwise exit 2, "valid but incomplete"),
input errors, the ledger and the canary pre-flight.

| `reason_class` | Raised when |
|---|---|
| `usage` | the command line is wrong: an unknown option, a bad value, a missing argument, `--backend-env` without `=`, `--library-dir` without `=`, `campaign --rounds` below 0, `corpus add` without a valid `--name`, `blocked` or `decoupling` without (or with both) a run and `--board`, `decoupling --radius` not positive, `--json` on a command without machine mode |
| `input` | the project, run or revision named is not usable: `--dir` is not a directory, not exactly one board, `--rev` outside a git repository or not a commit, the board has no `.kicad_pro` of its own name (KiCad would grade with its defaults; `run` and `audit`), the input board cannot be read, the net selection matches no nets or would not mean what it says (`--nets '!RESET'` on a board with a net named `!RESET`), `validate`/`blocked`/`spokes`/`barrier`/`compare`/`publish` given something that is not a run, `blocked --board` given something that is not a board, a `validate --against` chain that does not descend from the origin, `floor` outside a git repository |
| `ledger` | the run ledger has this exact attempt already failed (`--force` repeats it; docs/hygiene.md) |
| `canary` | `run --canary` and the awkward-net-name canary failed |
| `state` | kicad-route's own state is in the way: `--run-dir` exists, the corpus entry exists; with `--require-readonly-state` (or `KICAD_ROUTE_REQUIRE_READONLY_STATE=1`) the state directory is missing, holds no backend, is not the directory its `manifest.json` describes or has drifted from it, or is writable, and `update` / `corpus add` (which write it) refuse ([sandbox.md](sandbox.md)) |

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
  `validated_board_sha256`, `preservation` (check name to status)
- `candidate.board` (`candidate` is null when there is none)
- `ledger.fingerprint`

`route-validate`: `schema`, `kind`, `exit_code`, `run_dir`, `against`, `dir`, and the
same `routing` and `validation` fields. `route-campaign`: `schema`, `kind`,
`exit_code`, `campaign_dir`, `input.fingerprint`, `best` (`chain`, `run_dir`,
`candidate`, `grade`, `exit_code`; null when no chain was graded). Refusal: every field
of the record above.

Fields whose shape depends on the input: `scope.nets_list` is null above 50 nets,
`candidate` is null on a process failure, `routing.blocked_by` exists only when
INCOMPLETE, and `routing` holds only `status` and `reasons` when NOT_RUN.

Informational (may change within `/1`): `process.backend_report`, `process.commands`
and every other process detail, `routing.stats`, the `routing.blocked_by` summary,
`validation.drc` detail, `runtime_s`, log paths, `tool_versions`, `floor`, and
`RESULT.md`. Planned additions to the stable list (tracked in Plane KICADROUTE):
`evidence` and `tool_identity`.

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

- Adding a field, a record kind or a reason class keeps the major version.
- Removing or redefining a stable field bumps the major (`kicad-route/route/2`, a new
  `schemas/result-2.json`). For at least one tagged release the old field is still
  written beside the new one and listed in a top-level `deprecated` array
  (`[{"field": "...", "replaced_by": "...", "since": "<tag>"}]`).
- This page keeps the history below.

### History

- **1** (2026-10-17): first versioned contract: `schema` ids on every record; refusal
  records with exit 4 for every pre-routing failure, usage errors included; `--json`;
  `--out` after the subcommand; `run --run-dir`; no option abbreviations.
