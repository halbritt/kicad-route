# kicad-route campaign: hard nets first

`kicad-route campaign` automates a retry pattern that agents used to run by hand.
It routes the nets that a full attempt left open *first*, on the original input.
Then it routes the rest of the scope from that candidate and grades the end of the
chain against the original input. It changes only the order and the scope of the
work. Rules are never relaxed: every step is an ordinary `kicad-route run` with the
campaign's own options, and the grading is KiCad's DRC against the input's
unchanged project files.

Why it helps: with `--protect existing`, a later attempt cannot move copper that an
earlier attempt placed. On a real 448-net 4-layer board, a historical KRT attempt left 16
signal nets open. Finishing those 16 on that candidate routed 0 of 16. Routing the 16
first on the original input routed 11, and routing everything else from that
candidate gave the best result overall (20 unconnected instead of 26, and fewer new
findings).

```sh
kicad-route campaign --backend builtin --budget 900 --rounds 2
kicad-route campaign --backend freerouting --clearance-margin 0.01 --total-budget 7200
kicad-route campaign --backend builtin --nets '/CAN*' '/USB*' --race 3 --default-clearance 0.4
```

## Options

- **Per-run options, passed to every step unchanged:** every routing option of `run`:
  `--budget` (wall clock for each run; cut to the time left under `--total-budget`),
  `--max-iterations`, `--passes`, `--protect`, `--default-clearance`,
  `--clearance-margin`, `--stall`, `--force`, `--canary`, `--pour-area-loss`,
  `--pour-min-spokes`, `--backend-arg`, `--backend-env`, `--floor-history`,
  `--reserve-spokes`. `run`, `campaign` and `corpus add` share one definition of these
  options (`cli.add_route_args`); the campaign rebuilds each step's command line from it
  (`cli.route_argv`), so a new `run` option reaches every step. (`run --barrier-from`
  is not a campaign option: a barrier is planned from one earlier run of one input.)
- **Input options:** `--dir`, `--board`, `--schematic`, `--name` and `--rev` name the
  original input; a step on a candidate gets that candidate's `--dir`, `--board` and
  `--schematic` instead. `--library-dir` goes to **every** step. A candidate keeps a
  mapped directory as a link to the library, and the step's snapshot links it again,
  so the library is never copied into a chained input or its fingerprint.
- **Scope:** `--nets` / `--netclass` select the campaign scope, as for `run`.
- `--rounds N` (default 2): the number of hard-first rounds after round 0. With
  `--rounds 0`, the campaign runs round 0 only (useful with `--race`).
- `--strategy hardfirst` (default and only strategy).
- `--race K` (0–3, default 0): round 0 also tries K two-pass orderings in parallel
  (see below).
- `--total-budget SECONDS`: the wall clock for the whole campaign (see
  [Time limits](#time-limits)). No new run starts after it, a run that starts with
  less than `--budget` left gets the time left as its `--budget`, and a run that is
  still going a grace period after the deadline is stopped. A chain whose second pass
  could not start is recorded as `stopped` and is ranked last.
- `--label NAME` (default `campaign-<backend>`): the campaign's name. Its runs are
  labelled `<label>-r0`, `<label>-r1-backlog`, `<label>-r1-rest`, `<label>-r0-pads-first`
  and so on.

## How it works

**Round 0** is one ordinary attempt over the campaign scope on the input. If it is
complete, the campaign stops. Otherwise its unrouted nets (KiCad's view after zone
refill, `routing.unrouted_nets`) start the **backlog**.

**Round k** is a two-step chain:

1. `run --nets <backlog>` on the **original input**, not on a candidate. Each net
   name is escaped into an exact fnmatch pattern.
2. `run --dir <step 1 candidate> --nets <campaign patterns> !<backlog>…`: the rest of
   the scope, from step 1's candidate. The exclusions use `run`'s own scope rules
   (exclusions apply last), so `--netclass` scopes work too. This step is skipped when
   the backlog is the whole scope.
3. `validate <step 2> --against <step 1>`: this walks the `--dir` chain back to
   step 1 and grades the chain end against the original input, its baseline and its
   protection, with the union of the steps' scopes (the whole campaign scope).

After each round, the backlog becomes the union of itself and every net that is still
unrouted. The campaign stops when one of these is true:

- a round is complete;
- the backlog did not grow (the next round would repeat this one with the same
  scopes);
- `--rounds` hard-first rounds are done;
- `--total-budget` is used up;
- round 0 produced no gradable candidate (for example, a backend budget overrun);
- the input changed during the campaign.

## Time limits

Every step and every `validate --against` runs in its own process group with a
timeout. At the timeout the campaign stops the group (SIGTERM, then SIGKILL), so the
backend and any `kicad-cli` it started stop too. The step is recorded with
`timed_out: true` and its `timeout_s`, its chain becomes an `error`, and the step is
listed under `timeouts` in `campaign.json` (and in `CAMPAIGN.md`). Interrupting the
campaign (Ctrl-C or SIGTERM) stops the running steps the same way.

| | `--budget` of a run | Run timeout | `validate --against` timeout |
|---|---|---|---|
| no `--total-budget` | `--budget` | budget + stop grace + 1800 s | 1800 s |
| `--total-budget` | min(`--budget`, time left), at least 1 s | time left + stop grace + 600 s | time left (at least 0) + 600 s |

The stop grace is what a run itself allows its backend after the budget (the clean
stop request, then terminate and kill): max(30, budget/5) + 15 s. The 600 s
allowance (`GRADE_ALLOWANCE_S`) covers the run's own grading after the backend
stops: snapshot, refill, DRC, parity and preservation. Without `--total-budget`,
the timeout only catches a hung step; 1800 s (`HANG_ALLOWANCE_S`) is the run's own
limit for one KiCad DRC.

So a run never ends later than the deadline plus its grace. A chain whose last run
finished near the deadline is still graded within the 600 s allowance. A campaign
can therefore overrun `--total-budget` by at most one run's grace plus one grading.
A short budget is a real cut: a run that gets 20 s routes less than one that gets
`--budget`.

## Checks between steps

Each step checks its run's `attempt.json`. The scope that ran must equal the scope
that was intended. The input fingerprint of every chain's first step must equal
round 0's. A mismatch stops that chain, so a working tree that changes during a long
campaign cannot mix inputs. Use `--rev` when other people edit the tree.

## Choosing the best chain

Every finished chain is graded against the original input: a one-step chain by its
own `result.json` (its input *is* the original), a longer chain by
`validate --against`. Chains are ranked in this order:

1. a graded candidate before none;
2. complete and valid (exit 0) before the rest;
3. no preservation failure before any (a board that moved a footprint or lost
   protected copper cannot be adopted, whatever its counts);
4. fewer unconnected items in scope;
5. fewer new DRC violations plus new parity issues;
6. shorter runtime (the sum of the steps' wall time and the grading).

The campaign prints the best chain's last run directory and candidate. It exits with
that chain's graded exit code: 0 complete and valid, 2 valid but incomplete, 3 fails
validation. If no chain produced a gradable candidate, it exits 1. A usage or input
error, or a refused round-0 run (for example the ledger), is a refusal: exit 4 with a
record under `OUT/refusals/`, and no campaign directory is left
([interface.md](interface.md)).

Every step runs in machine mode (`kicad-route run --json`, `validate --json`): the
campaign reads the step's result record from its stdout and keeps the step's progress
(stderr) in `logs/<step>.log`. `kicad-route campaign --json` prints the campaign record
itself.

## Race (round 0 orderings)

With `--race K`, the campaign snapshots the input once, extracts its board facts and
warms the baseline DRC cache, so that parallel runs share one baseline. Then it starts
round 0 together with K two-pass lanes, each in its own processes. A lane routes a
"first" subset on the original input and then the rest from that candidate, and is
graded like a hard-first chain. The orderings, in the order they are taken:

| Ordering | First pass |
|---|---|
| `pads` | the quarter of the in-scope nets with the most pads (nets with fewer than 2 pads ignored) |
| `span` | the quarter with the largest pad bounding box (half perimeter) |
| `netclass` | every net whose effective class is not `Default` |

An ordering that selects nothing or the whole scope is skipped. The backlog after
round 0 is the union of the unrouted nets of every round-0 chain.

The lanes share the machine. Budgets are wall clock, so each parallel run gets less
search than it would get alone, and the result can depend on load. Compare lanes
within one campaign, not with runs made on an idle machine. When the per-run budget
is what limits routing, keep `--race 0`.

## Output

`OUT/campaigns/<project>/<label>/<stamp>/` (with a `latest` link):

| Path | Contents |
|---|---|
| `campaign.json` | settings (with the timeout allowances), every round (backlog routed first, chains, unrouted after, next backlog), `backlog_evolution`, every chain with its steps (argv, `budget_s`, `timeout_s`, `timed_out`, log, run directory, source, fingerprint, scope, statuses) and its grade, `timeouts` (every subprocess stopped at its timeout), the `leaderboard`, `best` (chain, run directory, candidate, `graded_against`, the grading result file, exit code), `stop_reason`, `exit_code` |
| `CAMPAIGN.md` | the same, for people |
| `logs/<run label>.log` | each step's `kicad-route` output |
| `input/` | with `--race`: the input snapshot and facts used for the orderings |

`campaign.json` is rewritten after every round, so an interrupted campaign still
shows how far it got. Each step is an ordinary run under `OUT/runs/<project>/`, so
`validate`, `compare` and `publish` work on it as usual. A chain's grade is the
`revalidate-origin/<stamp>/result.json` of its last run.

## Limitations and future work

- The backends route a scope in their own order. A campaign can only change which
  nets are in which pass. It cannot order nets inside a pass.
- A backlog net that its first pass cannot route is not tried again in the rest
  pass. That pass would most likely fail too, because the copper is locked, and it
  would use budget that the remaining nets need.
- A failed rest pass (a process failure) leaves its chain ungraded. The campaign
  does not grade the backlog candidate alone, because its scope is only the backlog.
- The race orderings are fixed heuristics. Other orderings (for example by
  differential pair or by layer pressure) and a race in later rounds are future work.
